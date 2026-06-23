#!/usr/bin/env python3
"""DS4 frontend metrics sidecar (Phase 3).

Serves GPU + RAM + disk + model-residency telemetry as JSON so the browser SPA
(which can't run nvidia-smi) can render the right-rail panel. Stdlib only.

  GET /metrics   -> one JSON sample (see schema below)
  GET /healthz   -> {"ok": true}
  POST /warm     -> warm the model into page cache (only if --enable-warm)

Binds 127.0.0.1 by default. Sends permissive CORS so the SPA (different origin)
can fetch it. The model-residency ("model warm") figure uses mincore(2) over the
mmap'd GGUF; it is refreshed on a slow cadence because it scans ~21M page flags.
"""
import argparse, ctypes, ctypes.util, json, mmap, os, re, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = os.sysconf("SC_PAGE_SIZE")
_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_lock = threading.Lock()
_state = {"disk": None, "resident": None, "resident_ts": 0.0, "metrics_hits": 0}
_BIT0 = bytes((v & 1) for v in range(256))  # translate table: byte -> low bit

GPU_FIELDS = ("name", "utilization.gpu", "temperature.gpu", "power.draw",
              "power.limit", "clocks.sm", "memory.used", "memory.total")


def _num(s, cast=float):
    s = s.strip()
    try:
        return cast(s)
    except (ValueError, TypeError):
        return None


def gpu_stats():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + ",".join(GPU_FIELDS),
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4).stdout.strip().splitlines()
        if not out:
            return None
        p = [x.strip() for x in out[0].split(",")]
        return {
            "name": p[0],
            "util_pct": _num(p[1], int),
            "temp_c": _num(p[2], int),
            "power_w": _num(p[3]),
            "power_limit_w": _num(p[4]),
            "sm_clock_mhz": _num(p[5], int),
            "vram_used_mb": _num(p[6], int),
            "vram_total_mb": _num(p[7], int),
        }
    except Exception:
        return None


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
    """Aggregate read MB/s across whole NVMe disks, as a delta between calls."""
    total_sectors, now = 0, time.time()
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) > 5 and re.fullmatch(r"nvme\d+n\d+", p[2]):
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

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path.startswith("/healthz"):
            return self._json({"ok": True, "metrics_hits": _state["metrics_hits"]})
        if self.path.startswith("/metrics"):
            with _lock:
                _state["metrics_hits"] += 1
            model = model_residency(self.model_path) if self.model_path else None
            sample = {
                "ts": time.time(),
                "gpu": gpu_stats(),
                "ram": ram_stats(),
                "disk": {"read_mb_s": disk_read_rate()},
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
        if self.path.startswith("/warm"):
            if not self.enable_warm or not self.model_path:
                return self._json({"error": "warm disabled"}, 403)
            threading.Thread(target=self._warm, daemon=True).start()
            return self._json({"warming": True, "path": os.path.basename(self.model_path)})
        self._json({"error": "not found"}, 404)

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
                    default=os.environ.get("DS4_MODEL", "/home/grant/Dev/ds4/ds4flash.gguf"),
                    help="GGUF for the 'model warm' page-cache gauge (env: DS4_MODEL). "
                         "Lives in the ds4 checkout, not this repo.")
    ap.add_argument("--enable-warm", action="store_true",
                    help="expose POST /warm (runs a full read of the model)")
    args = ap.parse_args()

    Handler.model_path = os.path.realpath(args.model) if os.path.exists(args.model) else None
    Handler.enable_warm = args.enable_warm
    print(f"ds4 metrics sidecar on http://{args.host}:{args.port}  "
          f"model={Handler.model_path}  warm={'on' if args.enable_warm else 'off'}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
