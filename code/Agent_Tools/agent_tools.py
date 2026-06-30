#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""ds4 agent tools — sandboxed filesystem executor for the dashboard's Agent mode.

The browser can't touch the filesystem, so the web agent loop calls THIS localhost-only
service to run its file tools. THE LOCK: every path is confined to a chosen workspace
folder — paths are resolved with realpath() and rejected unless they stay inside
realpath(workspace), which defeats '..' traversal AND symlink escapes. File I/O is fully
sandboxed this way. Command execution (the `execute` / `run_command` tools) runs a real
subprocess with cwd confined to the workspace, a hard timeout, an output cap and a scrubbed
env — but a subprocess can still reach the wider machine, so `execute` is marked high-risk: the UI
gates it behind human approval in ASK mode, while AUTO mode runs the agent autonomously (no approvals).
Stdlib only; binds 127.0.0.1.

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
  GET  /tools                   -> {tools:[...defs], mutating:[...], risk:{...}, commands:[...]} (the registry)
  POST /tools/<name> {...args}  -> run a registered tool (sandboxed)
  POST /tools/tree   {path}     -> file tree for the UI (built-in; not a model tool)
"""
import argparse, atexit, importlib.util, json, os, signal, subprocess, tempfile, threading, time, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import _jobs

_lock = threading.Lock()
_ws = {"root": None}                 # locked workspace root (absolute realpath) or None
MAX_BYTES = 2 * 1024 * 1024          # per-file read/write cap
MAX_ENTRIES = 5000                   # list_dir cap

# --- command execution (execute / run_command) caps. The frontend waits a bit longer than EXEC_TIMEOUT_SEC
#     (config.executeTimeoutMs) so the backend kills the process and returns a clean result first. ---
EXEC_TIMEOUT_SEC = 120               # hard wall-clock per command; on expiry the whole process group is killed
EXEC_MAX_OUTPUT = 64 * 1024          # cap on captured stdout AND stderr each (bytes) before truncation
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "SHELL", "TMPDIR")  # scrubbed env allowlist


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


# ---------------- command execution (shared by the execute / run_command tools) ----------------
def _scrubbed_env():
    """A minimal environment for executed commands — only the allowlisted keys, never the full os.environ."""
    env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def _wrap(argv):
    """bwrap-ready chokepoint: today returns argv unchanged. To sandbox EVERY command the same way later,
    prepend a bubblewrap invocation here (e.g. bwrap --bind <ws> <ws> --unshare-net ...). Keep this the
    ONLY place the final argv is assembled so the sandbox cannot be bypassed by an individual tool."""
    return argv


def resolve_command(spec):
    """Validate + resolve a command spec into (argv, shown, cwd). Shared by foreground run_process and the
    background JobManager so both apply the SAME rules. cwd is confined to the workspace via safe_path."""
    if not workspace():
        raise PermissionError("no workspace selected")
    if spec.get("shell"):
        cmd = spec.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("shell command must be a non-empty string")
        argv, shown = ["bash", "-c", cmd], cmd
    else:
        argv = spec.get("argv")
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
            raise ValueError("argv must be a non-empty array of strings")
        shown = " ".join(argv)
    cwd = safe_path(spec.get("cwd") or "")             # confine the working directory to the workspace
    if not os.path.isdir(cwd):
        raise ValueError("cwd is not a directory: %r" % (spec.get("cwd") or "."))
    return argv, shown, cwd


def run_process(spec, timeout=None):
    """Run a FOREGROUND command to completion and return a captured result dict (blocks; for long-lived
       processes use the JobManager / execute background:true). Never raises on a non-zero exit (that's a
       normal result); raises ValueError on a malformed spec and PermissionError if cwd escapes the workspace.
       On timeout the whole process group is killed and timed_out=True is returned."""
    argv, shown, cwd = resolve_command(spec)
    t0, timed_out = time.time(), False
    proc = subprocess.Popen(_wrap(argv), cwd=cwd, env=_scrubbed_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=(timeout or EXEC_TIMEOUT_SEC))
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # kill children too, not just the direct child
        except (ProcessLookupError, PermissionError):
            pass
        out, err = proc.communicate()
        rc = None

    def dec(b):
        s = (b or b"").decode("utf-8", "replace")
        return (s[:EXEC_MAX_OUTPUT] + "\n…(output truncated)") if len(s) > EXEC_MAX_OUTPUT else s

    return {"command": shown, "exit_code": rc, "timed_out": timed_out,
            "stdout": dec(out), "stderr": dec(err),
            "duration_sec": round(time.time() - t0, 2),
            "truncated": len(out or b"") > EXEC_MAX_OUTPUT or len(err or b"") > EXEC_MAX_OUTPUT}


def read_commands():
    """Read <workspace>/.ds4/commands.json -> {name: {argv|shell, description, cwd}}. {} if absent/invalid.
    These named commands are human-authored (pre-vetted), so run_command may run them with lighter friction."""
    ws = workspace()
    if not ws:
        return {}
    try:
        p = safe_path(".ds4/commands.json")
    except PermissionError:
        return {}
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    cmds = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(cmds, dict):
        return {}
    out = {}
    for name, spec in cmds.items():       # keep only well-formed entries (argv array OR shell string)
        if isinstance(spec, dict) and (isinstance(spec.get("argv"), list) or isinstance(spec.get("shell"), str)):
            out[str(name)] = spec
    return out


# Per-request thread-local: the dispatch stashes the owning agent run id (X-DS4-Run-Id header) here so a
# backgrounded process can be tagged for run-scoped cleanup. (ThreadingHTTPServer = one thread per request.)
_req = threading.local()


def current_run():
    return getattr(_req, "run_id", None)


# Handed to every tool's run(args, ctx). Tools MUST resolve paths through ctx.safe_path — that call IS the
# sandbox lock. ctx.run_process runs a foreground command; ctx.jobs is the background JobManager (set in main());
# ctx.resolve_command validates a command spec; ctx.current_run() is the owning agent run for run-scoping.
CTX = types.SimpleNamespace(safe_path=safe_path, workspace=workspace,
                            MAX_BYTES=MAX_BYTES, MAX_ENTRIES=MAX_ENTRIES,
                            run_process=run_process, read_commands=read_commands,
                            resolve_command=resolve_command, current_run=current_run, jobs=None)


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
    """The model-facing contract the frontend fetches: OpenAI tool defs + which names mutate + per-tool risk +
    the project's named commands. `risk` only lists tools above the default ("low"); the frontend shows a ⚠ label
    and asks for approval on high-risk tools in Ask mode (Auto runs autonomously). `commands` is the .ds4/commands.json manifest, surfaced so the model + UI
    know what run_command can run (run_command's description is augmented live with the available names)."""
    reg = REGISTRY if reg is None else reg
    cmds = read_commands()
    cmd_list = [{"name": n, "description": (s.get("description") or "")} for n, s in cmds.items()]
    tools = []
    for name, r in reg.items():
        fn = r["def"]
        if name == "run_command" and cmd_list:        # tell the model which named commands exist right now
            fn = dict(fn, description=(fn.get("description", "") + " Available: " + ", ".join(c["name"] for c in cmd_list) + "."))
        tools.append({"type": "function", "function": fn})
    return {"tools": tools,
            "mutating": [name for name, r in reg.items() if r["mutating"]],
            "risk": {name: r["risk"] for name, r in reg.items() if r.get("risk") and r["risk"] != "low"},
            "commands": cmd_list}


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DS4-Run-Id")

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected mid-response (Stop / navigation) — not an error

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
        _req.run_id = self.headers.get("X-DS4-Run-Id") or None    # tag spawned jobs with the owning agent run
        try:
            if path == "/workspace":
                return self._json({"root": set_workspace(b.get("path"))})
            if path == "/jobs/cleanup":                     # frontend: reap a run's background processes at run end
                return self._json(CTX.jobs.cleanup_run(b.get("run_id")) if CTX.jobs else {"reaped": []})
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
    # Background-process manager: persist file is per-port so two backends don't fight; sweep-on-start reaps
    # survivors of a previously SIGKILLed backend. Cleanup hooks guarantee jobs die when THIS backend exits.
    persist = os.path.join(tempfile.gettempdir(), "ds4_jobs_%d.json" % args.port)
    CTX.jobs = _jobs.JobManager(persist, _scrubbed_env)
    atexit.register(CTX.jobs.shutdown)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: (CTX.jobs.shutdown(), os._exit(0)))
    print(f"ds4 agent-tools on http://{args.host}:{args.port}  workspace={workspace()}  tools={list(REGISTRY)}", flush=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    finally:
        CTX.jobs.shutdown()


if __name__ == "__main__":
    main()
