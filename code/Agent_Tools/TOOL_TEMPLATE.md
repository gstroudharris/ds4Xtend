# Agent tools — how they're built + best practices

> **This file is developer documentation. It is NOT a tool and is never loaded into the model.**
> The agent only sees the tool catalog (the `spec.json` *function* blocks, sent as `tools` each turn)
> and only ever touches the user's locked **workspace** — never this repo. Nothing here can affect or
> "confuse" a tool call.

## A tool is a folder (drop-in registry)

Each tool is one self-contained folder beside this file, holding exactly two files:

```
Agent_Tools/
  read_file/      spec.json  tool.py
  write_file/     spec.json  tool.py
  edit_file/      spec.json  tool.py
  ...
```

- **`spec.json`** — the model-facing contract:
  ```json
  { "function": { "name": "...", "description": "...", "parameters": { ... } },
    "mutating": false,
    "risk": "low" }
  ```
  `function` is the OpenAI tool definition the model sees. `mutating: true` marks a tool that changes
  the workspace, so it's gated behind an approval/diff in **Ask** mode. `risk` is optional (default
  `"low"`); `"high"` shows a `⚠` label and asks for approval in **Ask mode** (see *Risky tools* below).
- **`tool.py`** — the implementation, exposing:
  ```python
  def run(args, ctx):       # REQUIRED
      ...
      return { ... }        # a small JSON-able dict

  def validate(args):       # OPTIONAL — runs before run(); raise ValueError on bad/unsafe args
      ...
  ```

**Auto-discovery wires everything.** At startup [`agent_tools.py`](agent_tools.py) globs these folders,
imports each `tool.py`, and serves the combined catalog at **`GET /tools`**. The frontend
([`tools.js`](tools.js) → `window.DS4_AGENT.load()`) fetches it once and derives the tool list, the
`name → /tools/<name>` endpoints, and the mutating set. Dispatch is generic on both sides.

> **To add a tool: drop a folder with `spec.json` + `tool.py`, then restart the backend. No edits to
> `agent_tools.py`, `tools.js`, or `app.js`.**

(The one exception is `tree`: a built-in UI-only endpoint that stays inside `agent_tools.py`. It is
deliberately *not* a tool folder, so the model can't call it — only the file-tree panel does.)

## The `ctx` handed to every `run(args, ctx)`

`args` is the tool-call arguments (a dict). `ctx` carries the sandbox + shared limits:

| `ctx.` | what it is |
|---|---|
| `safe_path(rel)` | **The sandbox lock.** Resolves a workspace-relative path with `realpath()` and raises `PermissionError` if it escapes the workspace (defeats `..` and symlink escapes). **Resolve EVERY path through this.** |
| `workspace()` | absolute path of the locked workspace root |
| `MAX_BYTES` | per-file read/write cap (2 MB) |
| `MAX_ENTRIES` | listing cap |
| `run_process(spec)` | Run a command (cwd confined to the workspace, hard timeout, output cap, scrubbed env). `spec` = `{"argv":[...]}` or `{"shell":True,"command":"..."}` (+ optional `"cwd"`). Returns `{exit_code, stdout, stderr, timed_out, duration_sec, truncated}`. Used by `execute`/`run_command`. |
| `read_commands()` | The workspace's `.ds4/commands.json` as `{name: {argv\|shell, description, cwd}}` (or `{}`). Human-authored, pre-vetted named commands. |

## Adding a tool — template

**`rename/spec.json`**
```json
{
  "function": {
    "name": "rename",
    "description": "<one line: exactly what it does>",
    "parameters": {
      "type": "object",
      "properties": {
        "path": { "type": "string" },
        "to":   { "type": "string", "description": "<format / constraint>" }
      },
      "required": ["path", "to"]
    }
  },
  "mutating": true
}
```

**`rename/tool.py`**
```python
"""rename — move a file within the workspace."""
import os

def run(args, ctx):
    rel = args.get("path", "")
    to  = args.get("to", "")
    p = ctx.safe_path(rel)                                 # sandbox: EVERY path through ctx.safe_path
    if not os.path.exists(p):
        raise FileNotFoundError("nothing at: %s" % rel)    # actionable error -> model self-corrects
    os.rename(p, ctx.safe_path(to))
    return {"path": rel, "renamed_to": to}                 # small structured result the model can act on
```

That's it — restart the backend and `rename` is live. Raised exceptions are returned to the model as the
tool result: `PermissionError → 403/denied`, `FileNotFoundError`/`ValueError → 400/bad_request`,
`OSError → 500/io` (any other exception → 500, never crashes the loop).

For a mutating tool you *may* also add a nicer diff/preview in `approvalFor()` (in `app.js`) — but it's
optional: with `"mutating": true`, the generic Ask-mode approval already gates it.

## Command execution — `execute` + `run_command` (shipped)

Command tools exist now, built on the four guards below. The key boundary to keep in mind:
**`ctx.safe_path` confines file paths, but it does NOT confine a subprocess** — a child process can reach
anything the user can. **The Ask/Auto switch is the approval gate**: in Ask mode the schema, `validate()`,
and HITL approval are the containment; in **Auto** mode the agent runs commands autonomously (no approvals),
so Auto is a deliberate "I trust this agent to run unattended" choice — confinement there falls to the
`validate()`/`_wrap()` layers (and a future bwrap sandbox), not a human.

- **`execute`** — `risk:"high"`. In **Ask** mode it requires human approval (a `⚠ High-risk` confirmation);
  in **Auto** mode it runs autonomously like any other tool — the high-risk flag drives the warning *label*,
  not an unconditional gate. Hybrid input: an `argv` array (no shell) or `shell:true` + `command` (run via
  `["bash","-c",cmd]`, so `shell=False` at the Python level — no interpolation injection).
- **`run_command`** — runs a **named** command from `.ds4/commands.json`. The model only picks a name
  (no arg passthrough), so a human-vetted command stays vetted. It is `mutating` but **not** high-risk →
  approved in Ask mode, but may run unattended in Auto (enables an edit → run-tests → fix loop).

Both go through `ctx.run_process()`, the single chokepoint that confines `cwd` to the workspace, enforces
a hard timeout (kills the whole process group), caps output, and scrubs the env. `_wrap(argv)` inside it
is the **bwrap-ready hook**: today it returns argv unchanged; to add OS-level isolation later, wrap it
there once and every command inherits it.

If you add another risky tool, follow the same four guards:
1. **Model the input so unsafe states are unrepresentable** — argv array / enum, not a freeform string passed to a real shell.
2. **`"risk": "high"`** in `spec.json` → a `⚠ High-risk` approval in **Ask** mode (in Auto the agent runs autonomously; the Ask/Auto switch is the gate).
3. **`validate(args)`** in `tool.py` → enforce the hard constraints in code (raise `ValueError` → clean 400). Defense in depth: schema **and** code, because approval is a UI control, not the only gate.
4. **Bound execution** via `ctx.run_process` (timeout + output cap + confined cwd). True OS isolation (namespaces/seccomp/container) is beyond this stdlib sidecar — slot it into `_wrap()` when needed.

### Project commands — `.ds4/commands.json`

A workspace declares pre-vetted commands a human trusts the agent to run:
```json
{ "commands": {
  "test":  { "argv": ["pytest", "-q"], "description": "Run the test suite" },
  "build": { "argv": ["make"], "description": "Build" },
  "lint":  { "shell": "ruff check . && echo ok", "description": "Lint" } } }
```
Each entry is `{argv:[...]}` **or** `{shell:"..."}`, plus optional `description` and `cwd`. The names are
surfaced to the model (injected into `run_command`'s description + the system note) and to the UI.

### Background processes — `execute background:true` + the JobManager (`_jobs.py`)

For a long-lived process (server/watcher), `execute background:true` (a required `goal`, optional
`ready_when`/`scope`/`max_lifetime_sec`) returns a `job_id` + `pid` immediately. The agent polls with
`process_output`, lists with `list_processes`, and stops with `stop_process`. The frontend tags each call
with an `X-DS4-Run-Id` header so jobs are owned by the agent run.

**Cleanup never depends on the agent remembering** — [`_jobs.py`](_jobs.py) reaps a process the moment ANY
layer fires (whichever first): process-group `killpg` · wall-clock deadline · **lease** (a run-scoped job
dies if the agent stops polling) · run-end cleanup (`POST /jobs/cleanup`) · backend `atexit`/SIGTERM ·
start-up sweep of a SIGKILLed backend (PID-reuse-safe via `/proc` start-time) · kernel `PR_SET_PDEATHSIG` +
`RLIMIT_CPU`. A process that `setsid`s a daemon out of the group is still caught by an **env-marker `/proc`
sweep** (`DS4_JOB`/`DS4_OWNER`, inherited by descendants). The one residual: a child that scrubs its own env
(`env -i`) AND `setsid`s away — only a cgroup or PID-namespace closes that, which is the job of the
`_wrap()` hook (bubblewrap) if you ever need it. `test_jobs.py` exercises every layer, including the escapee.

## Bounded + tested

- **Every tool call has a hard timeout** (`toolTimeoutMs`; `executeTimeoutMs` for commands) and is abortable
  mid-flight by **Stop** — a wedged or slow tool can't hang the agent loop. Keep `run()` itself bounded too
  (scan caps, `ctx.MAX_BYTES`, the exec timeout/output cap), so the backend never relies on the client to stop it.
- **[`test_tools.py`](test_tools.py)** is the iterative-evaluation backstop (stdlib `unittest`,
  `python3 test_tools.py`). When you add a tool or a guard, add its success **and** failure cases there —
  especially anything that must *refuse* (escape attempts, non-unique edits, cwd escapes, risky-arg rejection).

## Best practices (so the model gets it right the first time, every time)

- **One job per tool, action-first description.** Orthogonal tools beat many overlapping ones — fewer choices, fewer wrong picks.
- **Behavior-changing detail goes in *param* descriptions**, not prose — and only what the model can get wrong (formats, units, "relative to the workspace root", "must be unique"). Don't document the obvious.
- **Make foot-guns impossible or loud.** e.g. `edit_file` requires a unique match unless `replace_all`, so it can't silently edit the wrong/extra place — which matters most in **Auto mode**, where there is no approval to catch it.
- **Actionable errors = self-correction.** Raise `ValueError("'find' matches 3 places — make it unique or set replace_all")`, not `"error"`. The message returns as the tool result, so the model fixes itself on the next turn. Say *what* was wrong and *what to do*.
- **Return small, structured results** (`{path, bytes, replacements, truncated, total_lines}`). That's how the model knows what happened and plans the next call. (Large read outputs get trimmed by the context cap, so the metadata matters more than the bulk.)
- **Brevity vs clarity — the real tension.** Every `spec.json` `function` block + the `SYSTEM` prompt re-send every turn and count against the (small) context budget. Keep each description one line; spend words only where they prevent a mistake.
- **Defense in depth.** State the constraint in the description *and* enforce it in `tool.py` with a clear error.
- **The system prompt is the *procedure*, not the catalog** (paths relative to root; read/search before edit; full contents on write). It lives in `tools.js → SYSTEM`; keep it short — it prefills every turn.
- **Always go through `ctx.safe_path`** in `tool.py` — it realpath-confines every path to the workspace — and respect the `ctx.MAX_BYTES` / `ctx.MAX_ENTRIES` caps.
