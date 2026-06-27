#!/usr/bin/env python3
"""DS4 frontend metrics sidecar (Phase 3).

Serves GPU + RAM + disk + model-residency telemetry as JSON so the browser SPA
(which can't run nvidia-smi/rocm-smi) can render the right-rail panel. Stdlib only.

  GET /metrics   -> one JSON sample (see schema below)
  GET /healthz   -> {"ok": true}
  POST /warm     -> warm the model into page cache (only if --enable-warm)

Vendor-agnostic: GPU stats come from `nvidia-smi` (NVIDIA) or `rocm-smi` (AMD),
whichever is present — so a clone on an AMD box renders correctly with no edits.
Binds 127.0.0.1 by default; permissive CORS so the SPA (different origin) can
fetch it. The model-residency ("model warm") figure uses mincore(2) over the
mmap'd GGUF; refreshed on a slow cadence because it scans ~21M page flags.
"""
import argparse, ctypes, ctypes.util, json, mmap, os, re, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = os.sysconf("SC_PAGE_SIZE")
_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_lock = threading.Lock()
_state = {"disk": None, "resident": None, "resident_ts": 0.0, "metrics_hits": 0}
_BIT0 = bytes((v & 1) for v in range(256))  # translate table: byte -> low bit

NV_FIELDS = ("name", "utilization.gpu", "temperature.gpu", "power.draw",
             "power.limit", "clocks.sm", "memory.used", "memory.total")


def _num(s, cast=float):
    """First numeric token of a value, cast — tolerates units like '45 W', '1920Mhz'."""
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(s))
    if not m:
        return None
    try:
        return cast(float(m.group(0)))
    except (ValueError, TypeError):
        return None


def _nvidia_gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + ",".join(NV_FIELDS),
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4).stdout.strip().splitlines()
        if not out:
            return None
        p = [x.strip() for x in out[0].split(",")]
        return {
            "vendor": "nvidia", "name": p[0],
            "util_pct": _num(p[1], int), "temp_c": _num(p[2], int),
            "power_w": _num(p[3]), "power_limit_w": _num(p[4]),
            "sm_clock_mhz": _num(p[5], int),
            "vram_used_mb": _num(p[6], int), "vram_total_mb": _num(p[7], int),
        }
    except Exception:
        return None


def _amd_gpu():
    """AMD GPU via `rocm-smi --json`. Key names vary across versions, so match fuzzily."""
    try:
        out = subprocess.run(
            ["rocm-smi", "--showproductname", "--showuse", "--showtemp", "--showpower",
             "--showmeminfo", "vram", "--showclocks", "--json"],
            capture_output=True, text=True, timeout=5).stdout
        data = json.loads(out)
    except Exception:
        return None
    cards = [v for k, v in (data or {}).items()
             if isinstance(v, dict) and k.lower().startswith("card")]
    if not cards:
        return None
    c = cards[0]

    def pick(*subs):
        for k, v in c.items():
            if all(s in k.lower() for s in subs):
                return v
        return None

    vt = vu = None  # VRAM total vs used — 'total' key must not also say 'used'
    for k, v in c.items():
        kl = k.lower()
        if "vram" in kl and "total" in kl and "used" not in kl:
            vt = _num(v, int)
        elif "vram" in kl and "used" in kl:
            vu = _num(v, int)

    return {
        "vendor": "amd",
        "name": str(pick("card series") or pick("product name") or pick("card model") or "AMD GPU").strip() or "AMD GPU",
        "util_pct": _num(pick("gpu use"), int),
        "temp_c": _num(pick("temperature", "edge") or pick("temperature"), int),
        "power_w": _num(pick("average", "power") or pick("socket", "power") or pick("power")),
        "power_limit_w": _num(pick("max", "power") or pick("cap", "power")),
        "sm_clock_mhz": _num(pick("sclk"), int),
        "vram_used_mb": (vu // (1024 * 1024)) if vu is not None else None,
        "vram_total_mb": (vt // (1024 * 1024)) if vt is not None else None,
    }


def gpu_stats(backend=None):
    """GPU sample, probing the launch backend's vendor first (the other is a fallback).
    On an AMD/ROCm box nvidia-smi may exist but fail every call, so don't lead with it."""
    if str(backend).lower() in ("rocm", "amd", "hip"):
        return _amd_gpu() or _nvidia_gpu()
    if str(backend).lower() in ("cuda", "nvidia"):
        return _nvidia_gpu() or _amd_gpu()
    return _nvidia_gpu() or _amd_gpu()


def ram_stats():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                info[k] = int(rest.strip().split()[0])  # kB
    except OSError:
        return None
    total = info.get("MemTotal", 0) // 1024
    avail = info.get("MemAvailable", 0) // 1024
    cached = (info.get("Cached", 0) + info.get("SReclaimable", 0)) // 1024
    return {"total_mb": total, "available_mb": avail,
            "used_mb": total - avail, "cached_mb": cached}


def disk_read_rate():
    """Aggregate read MB/s across whole disks (NVMe + SATA SSD/HDD), as a delta between calls."""
    total_sectors, now = 0, time.time()
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) > 5 and re.fullmatch(r"nvme\d+n\d+|sd[a-z]+", p[2]):
                    total_sectors += int(p[5])
    except (OSError, ValueError):
        return None
    with _lock:
        prev = _state["disk"]
        _state["disk"] = (now, total_sectors)
    if not prev:
        return 0.0
    dt = now - prev[0]
    if dt <= 0:
        return 0.0
    return max(0.0, (total_sectors - prev[1]) * 512 / dt / 1e6)  # MB/s


def model_residency(path):
    """Resident (page-cache) bytes of the model file via mincore(2). Throttled."""
    now = time.time()
    with _lock:
        cached = _state["resident"]
        if cached is not None and now - _state["resident_ts"] < 4.0:
            return cached
    res = _mincore_bytes(path)
    with _lock:
        _state["resident"], _state["resident_ts"] = res, now
    return res


def _mincore_bytes(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            return None
        # ACCESS_COPY = MAP_PRIVATE + writable, so ctypes can take the address;
        # we never write, so no COW happens and mincore reports cache residency.
        mm = mmap.mmap(fd, size, access=mmap.ACCESS_COPY)
        try:
            npages = (size + PAGE - 1) // PAGE
            vec = ctypes.create_string_buffer(npages)
            base = ctypes.addressof(ctypes.c_char.from_buffer(mm))
            if _libc.mincore(ctypes.c_void_p(base), ctypes.c_size_t(size), vec) != 0:
                return None
            resident_pages = vec.raw[:npages].translate(_BIT0).count(1)
            return {"resident_mb": resident_pages * PAGE // (1024 * 1024),
                    "size_mb": size // (1024 * 1024)}
        finally:
            mm.close()
    except Exception:
        return None
    finally:
        os.close(fd)


class Handler(BaseHTTPRequestHandler):
    model_path = None
    enable_warm = False
    backend = None
    ctx = None
    logs_dir = None
    state_file = None

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected mid-response (tab close / Ctrl+C teardown) — not an error

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 64 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path.startswith("/healthz"):
            return self._json({"ok": True, "metrics_hits": _state["metrics_hits"]})
        if self.path.startswith("/state"):
            return self._json(self._read_state())
        if self.path.startswith("/metrics"):
            with _lock:
                _state["metrics_hits"] += 1
            model = model_residency(self.model_path) if self.model_path else None
            sample = {
                "ts": time.time(),
                "gpu": gpu_stats(self.backend),
                "ram": ram_stats(),
                "disk": {"read_mb_s": disk_read_rate()},
                "backend": self.backend,
                "ctx": self.ctx,
                "model": None,
            }
            if model:
                size = model["size_mb"] or 1
                sample["model"] = {
                    "path": os.path.basename(self.model_path),
                    "size_mb": model["size_mb"],
                    "resident_mb": model["resident_mb"],
                    "warm_pct": round(model["resident_mb"] / size * 100),
                }
            return self._json(sample)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/state"):
            return self._save_state(self._body())
        if self.path.startswith("/log"):
            return self._log(self._body())
        if self.path.startswith("/warm"):
            if not self.enable_warm or not self.model_path:
                return self._json({"error": "warm disabled"}, 403)
            threading.Thread(target=self._warm, daemon=True).start()
            return self._json({"warming": True, "path": os.path.basename(self.model_path)})
        self._json({"error": "not found"}, 404)

    def _log(self, b):
        if not self.logs_dir:
            return self._json({"error": "logging disabled"}, 403)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(str(b.get("name") or "")))
        if not name or name in (".", ".."):
            return self._json({"error": "bad name"}, 400)
        content = b.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
            with open(os.path.join(self.logs_dir, name), "a" if b.get("append") else "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, "name": name, "bytes": len(content)})

    def _read_state(self):
        if not self.state_file:
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self, b):
        if not self.state_file:
            return self._json({"error": "state disabled"}, 403)
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(b, f)
        except (OSError, TypeError) as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, "keys": len(b) if isinstance(b, dict) else 0})

    def _warm(self):
        try:
            with open(self.model_path, "rb", buffering=1024 * 1024) as f:
                while f.read(8 * 1024 * 1024):
                    pass
        except OSError:
            pass

    def log_message(self, *a):  # quiet
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--model",
                    default=os.environ.get("DS4_MODEL", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ds4", "ds4flash.gguf")),
                    help="GGUF for the 'model warm' page-cache gauge (env: DS4_MODEL). "
                         "Defaults to the sibling ds4 checkout (../../ds4); ds4Service passes --model explicitly.")
    ap.add_argument("--backend", default=os.environ.get("DS4_BACKEND", ""),
                    help="inference backend label to report to the UI (cuda/rocm/cpu)")
    ap.add_argument("--ctx", default=os.environ.get("DS4_CTX", ""),
                    help="server context window (--ctx) to report to the UI for the headroom meter. "
                         "Omit when a per-box ds4-server.sh owns ctx; the UI falls back to config.serverCtx.")
    ap.add_argument("--enable-warm", action="store_true",
                    help="expose POST /warm (runs a full read of the model)")
    ap.add_argument("--logs-dir", default=os.environ.get("DS4_LOGS_DIR", ""),
                    help="directory for POST /log conversation logs (default: <repo>/logs)")
    ap.add_argument("--state-file", default=os.environ.get("DS4_STATE_FILE", ""),
                    help="file for GET/POST /state (UI resume across runs; default: <repo>/lastsession.json)")
    args = ap.parse_args()

    Handler.model_path = os.path.realpath(args.model) if os.path.exists(args.model) else None
    Handler.enable_warm = args.enable_warm
    Handler.backend = args.backend or None
    Handler.ctx = _num(args.ctx, int) if args.ctx else None
    Handler.logs_dir = (os.path.realpath(args.logs_dir) if args.logs_dir
                        else os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "logs")))
    Handler.state_file = (os.path.realpath(args.state_file) if args.state_file
                          else os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "lastsession.json")))
    print(f"ds4 metrics sidecar on http://{args.host}:{args.port}  "
          f"model={Handler.model_path}  backend={Handler.backend}  ctx={Handler.ctx}  "
          f"logs={Handler.logs_dir}  warm={'on' if args.enable_warm else 'off'}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
