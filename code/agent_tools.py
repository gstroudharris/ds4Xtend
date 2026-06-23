#!/usr/bin/env python3
"""ds4 agent tools — sandboxed filesystem executor for the dashboard's Agent mode.

The browser can't touch the filesystem, so the web agent loop calls THIS localhost-only
service to run its file tools. THE LOCK: every path is confined to a chosen workspace
folder — paths are resolved with realpath() and rejected unless they stay inside
realpath(workspace), which defeats '..' traversal AND symlink escapes. Read / list / write
only; there is no shell execution by design. Stdlib only; binds 127.0.0.1.

Endpoints
  GET  /healthz                 -> {ok, workspace}
  GET  /workspace               -> {root}
  POST /workspace   {path}      -> lock to a folder (must be an existing dir)
  GET  /browse?path=ABS         -> list sub-dirs of ABS (folder picker; read-only, dirs only)
  POST /tools/list_dir  {path}  -> entries under workspace/path
  POST /tools/read_file {path}  -> text contents (<=2MB)
  POST /tools/write_file{path,content} -> write within workspace (creates parent dirs)
"""
import argparse, json, os, threading
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


# ---------------- tools (all confined via safe_path) ----------------
def t_list_dir(rel):
    p = safe_path(rel)
    if not os.path.isdir(p):
        raise FileNotFoundError("not a directory: %s" % (rel or "."))
    out = []
    for name in sorted(os.listdir(p))[:MAX_ENTRIES]:
        fp = os.path.join(p, name)
        try:
            isdir = os.path.isdir(fp)
            out.append({"name": name, "type": "dir" if isdir else "file",
                        "size": (None if isdir else os.path.getsize(fp))})
        except OSError:
            continue
    return {"path": rel or "", "entries": out}


def t_read_file(rel):
    p = safe_path(rel)
    if not os.path.isfile(p):
        raise FileNotFoundError("not a file: %s" % rel)
    if os.path.getsize(p) > MAX_BYTES:
        raise ValueError("file too large (>%d bytes)" % MAX_BYTES)
    data = open(p, "rb").read()
    try:
        return {"path": rel, "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        raise ValueError("file is not UTF-8 text")


def t_write_file(rel, content):
    if content is None:
        content = ""
    enc = content.encode("utf-8")
    if len(enc) > MAX_BYTES:
        raise ValueError("content too large (>%d bytes)" % MAX_BYTES)
    p = safe_path(rel)
    if os.path.isdir(p):
        raise ValueError("path is a directory: %s" % rel)
    parent = os.path.dirname(p)
    safe_path(os.path.relpath(parent, workspace()))    # re-validate parent stays inside
    os.makedirs(parent, exist_ok=True)
    with open(p, "wb") as f:
        f.write(enc)
    return {"path": rel, "bytes": len(enc)}


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
            if path == "/tools/list_dir":
                return self._json(t_list_dir(b.get("path", "")))
            if path == "/tools/read_file":
                return self._json(t_read_file(b.get("path", "")))
            if path == "/tools/write_file":
                return self._json(t_write_file(b.get("path", ""), b.get("content", "")))
            return self._json({"error": "not found"}, 404)
        except PermissionError as e:
            return self._json({"error": str(e), "kind": "denied"}, 403)
        except (FileNotFoundError, ValueError) as e:
            return self._json({"error": str(e), "kind": "bad_request"}, 400)
        except OSError as e:
            return self._json({"error": str(e), "kind": "io"}, 500)

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
    print(f"ds4 agent-tools on http://{args.host}:{args.port}  workspace={workspace()}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
