#!/usr/bin/env python3
"""ds4 agent tools — sandboxed filesystem executor for the dashboard's Agent mode.

The browser can't touch the filesystem, so the web agent loop calls THIS localhost-only
service to run its file tools. THE LOCK: every path is confined to a chosen workspace
folder — paths are resolved with realpath() and rejected unless they stay inside
realpath(workspace), which defeats '..' traversal AND symlink escapes. Read / list / write
only; there is no shell execution by design. Stdlib only; binds 127.0.0.1.

Tools are a REGISTRY: each tool is a sibling folder holding `spec.json` (the model-facing
function definition + a "mutating" flag) and `tool.py` (exposing `run(args, ctx)`). They are
auto-discovered at startup, served to the frontend at GET /tools, and dispatched generically
at POST /tools/<name>. To add a tool, drop a folder and restart — see TOOL_TEMPLATE.md.
(`tree` is a built-in UI-only endpoint, intentionally NOT a registry tool so the model can't
call it.)

Endpoints
  GET  /healthz                 -> {ok, workspace}
  GET  /workspace               -> {root}
  POST /workspace   {path}      -> lock to a folder (must be an existing dir)
  GET  /browse?path=ABS         -> list sub-dirs of ABS (folder picker; read-only, dirs only)
  GET  /tools                   -> {tools:[...defs], mutating:[...]} (the auto-discovered registry)
  POST /tools/<name> {...args}  -> run a registered tool (sandboxed)
  POST /tools/tree   {path}     -> file tree for the UI (built-in; not a model tool)
"""
import argparse, importlib.util, json, os, threading, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_lock = threading.Lock()
_ws = {"root": None}                 # locked workspace root (absolute realpath) or None
MAX_BYTES = 2 * 1024 * 1024          # per-file read/write cap
MAX_ENTRIES = 5000                   # list_dir cap


# ---------------- workspace + the sandbox boundary ----------------
def set_workspace(path):
    if not path or not str(path).strip():
        raise ValueError("empty path")
    real = os.path.realpath(os.path.expanduser(str(path)))
    if not os.path.isdir(real):
        raise ValueError("not a directory: %s" % path)
    with _lock:
        _ws["root"] = real
    return real


def workspace():
    with _lock:
        return _ws["root"]


def safe_path(rel):
    """Resolve a workspace-relative path. Raise PermissionError if it escapes the lock."""
    root = workspace()
    if not root:
        raise PermissionError("no workspace selected")
    rel = (rel or "").strip().lstrip("/")          # neutralize absolute paths
    real = os.path.realpath(os.path.join(root, rel))   # resolves .. and symlinks
    if real != root and not real.startswith(root + os.sep):
        raise PermissionError("path escapes workspace: %r" % rel)
    return real


# Handed to every tool's run(args, ctx). Tools MUST resolve paths through ctx.safe_path —
# that call IS the sandbox lock. ctx.workspace()/MAX_BYTES/MAX_ENTRIES are the shared limits.
CTX = types.SimpleNamespace(safe_path=safe_path, workspace=workspace,
                            MAX_BYTES=MAX_BYTES, MAX_ENTRIES=MAX_ENTRIES)


# ---------------- tool registry (auto-discovered: each tool = a folder with spec.json + tool.py) ----------------
# spec.json: {"function": {...}, "mutating": bool, "risk": "low"|"medium"|"high"}  (risk optional, default "low")
# tool.py:   run(args, ctx) [required]  +  validate(args) [optional — raise ValueError on bad/unsafe args]
def load_registry(base=None):
    base = base or os.path.dirname(os.path.abspath(__file__))
    reg = {}
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        spec_f, impl_f = os.path.join(d, "spec.json"), os.path.join(d, "tool.py")
        if not (os.path.isdir(d) and os.path.isfile(spec_f) and os.path.isfile(impl_f)):
            continue
        try:                                    # a broken tool folder is skipped, never fatal
            with open(spec_f, encoding="utf-8") as f:
                spec = json.load(f)
            ms = importlib.util.spec_from_file_location("ds4tool_" + name, impl_f)
            mod = importlib.util.module_from_spec(ms)
            ms.loader.exec_module(mod)
            if not callable(getattr(mod, "run", None)):
                raise ValueError("tool.py has no run(args, ctx)")
            validate = getattr(mod, "validate", None)
            fn = spec.get("function") or {}
            tname = fn.get("name") or name
            reg[tname] = {"def": fn, "mutating": bool(spec.get("mutating")),
                          "risk": (spec.get("risk") or "low"),
                          "run": mod.run,
                          "validate": validate if callable(validate) else None}
        except Exception as e:
            print("agent-tools: skipped tool %r: %s" % (name, e), flush=True)
    return reg


REGISTRY = load_registry()


def tools_payload(reg=None):
    """The model-facing contract the frontend fetches once: OpenAI tool defs + which names mutate + per-tool risk.
    `risk` only lists tools above the default ("low"); the frontend forces approval on high-risk tools even in Auto."""
    reg = REGISTRY if reg is None else reg
    return {"tools": [{"type": "function", "function": r["def"]} for r in reg.values()],
            "mutating": [name for name, r in reg.items() if r["mutating"]],
            "risk": {name: r["risk"] for name, r in reg.items() if r.get("risk") and r["risk"] != "low"}}


# ---------------- built-in UI-only endpoints (NOT registry tools) ----------------
def t_tree(rel=""):
    root = workspace()
    base = safe_path(rel)
    entries, n = [], 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted([d for d in dirnames if not d.startswith(".")])
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            entries.append({"path": rel_dir, "name": os.path.basename(dirpath), "type": "dir",
                            "depth": rel_dir.count(os.sep) + 1})
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            n += 1
            if n > 2000:
                return {"entries": entries, "truncated": True}
            frel = os.path.relpath(os.path.join(dirpath, fn), root)
            entries.append({"path": frel, "name": fn, "type": "file", "depth": frel.count(os.sep) + 1})
    return {"entries": entries, "truncated": False}


def browse(path):
    """List immediate sub-directories of an absolute path — for the folder picker only."""
    base = os.path.realpath(os.path.expanduser(path or os.path.expanduser("~")))
    if not os.path.isdir(base):
        base = os.path.expanduser("~")
    dirs = []
    try:
        for name in sorted(os.listdir(base)):
            if name.startswith("."):
                continue
            fp = os.path.join(base, name)
            if os.path.isdir(fp) and not os.path.islink(fp):
                dirs.append(name)
    except OSError:
        pass
    parent = os.path.dirname(base)
    return {"path": base, "parent": (parent if parent != base else None), "dirs": dirs[:MAX_ENTRIES]}


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
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

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._json({"ok": True, "workspace": workspace()})
        if u.path == "/workspace":
            return self._json({"root": workspace()})
        if u.path == "/tools":
            return self._json(tools_payload())
        if u.path == "/browse":
            q = parse_qs(u.query)
            return self._json(browse((q.get("path") or [""])[0]))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        b = self._body()
        try:
            if path == "/workspace":
                return self._json({"root": set_workspace(b.get("path"))})
            if path == "/tools/tree":                       # built-in UI endpoint (not a model tool)
                return self._json(t_tree(b.get("path", "")))
            if path.startswith("/tools/"):                  # generic registry dispatch
                name = path[len("/tools/"):]
                tool = REGISTRY.get(name)
                if not tool:
                    return self._json({"error": "unknown tool: %s" % name}, 404)
                if tool["validate"]:                         # optional hard-constraint check (raises ValueError -> 400)
                    tool["validate"](b)
                return self._json(tool["run"](b, CTX))
            return self._json({"error": "not found"}, 404)
        except PermissionError as e:
            return self._json({"error": str(e), "kind": "denied"}, 403)
        except (FileNotFoundError, ValueError) as e:
            return self._json({"error": str(e), "kind": "bad_request"}, 400)
        except OSError as e:
            return self._json({"error": str(e), "kind": "io"}, 500)
        except Exception as e:                              # a buggy tool returns a clean error, never crashes the loop
            return self._json({"error": str(e), "kind": "error"}, 500)

    def log_message(self, *a):  # quiet
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--workspace", default="", help="optional initial locked folder")
    args = ap.parse_args()
    if args.workspace:
        try:
            set_workspace(args.workspace)
        except ValueError as e:
            print("agent-tools: bad --workspace:", e, flush=True)
    print(f"ds4 agent-tools on http://{args.host}:{args.port}  workspace={workspace()}  tools={list(REGISTRY)}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
