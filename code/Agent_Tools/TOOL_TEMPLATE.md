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
  `"low"`); `"high"` forces an approval **even in Auto mode** (see *Risky tools* below).
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

## Risky tools (`execute` and friends)

Before adding anything that runs code, hits the network, or is otherwise irreversible/costly, know the
boundary: **`ctx.safe_path` confines file paths, but it does NOT confine a subprocess.** A child process
can touch anything the user can — it escapes the workspace entirely. So for a risky tool, the schema and
`validate()` *are* the containment, not a backstop. Build it like this:

1. **Model the input so unsafe states are unrepresentable.** Don't take a freeform `command: string`
   (`"rm -rf /"` is a valid string). Take a `command` **enum** of allowed binaries + an `args` **array**,
   and never pass it through a shell with string interpolation.
2. **`"risk": "high"`** in `spec.json` → the frontend forces an approval **every time, even in Auto mode**
   (a high-risk call shows a `⚠ High-risk` confirmation; it is never auto-run).
3. **`validate(args)`** in `tool.py` → enforce the hard constraints in code (allowlist, no shell
   metacharacters, path confinement). It runs before `run()`; raise `ValueError` and the model gets a
   clean 400 it can correct from. This is *defense in depth* — state the rule in the schema **and** enforce
   it here, because HITL approval is a UI control and must not be the only gate.
4. **Bound execution inside `run()`**: a hard timeout, an output cap, and a workspace-confined `cwd`
   (true OS-level isolation — namespaces/seccomp/a container — is beyond this stdlib sidecar; if you need
   it, that's a sign the tool belongs in a different layer).

> The current toolset is deliberately *no code execution* — file I/O only. Adding `execute` crosses that
> line on purpose; do it consciously, with the four guards above.

## Bounded + tested

- **Every tool call has a hard timeout** (`toolTimeoutMs` in `config.js`) and is abortable mid-flight by
  **Stop** — a wedged or slow tool can't hang the agent loop. Keep `run()` itself bounded too (scan caps,
  `ctx.MAX_BYTES`), so the backend never relies on the client to stop it.
- **[`test_tools.py`](test_tools.py)** is the iterative-evaluation backstop (stdlib `unittest`,
  `python3 test_tools.py`). When you add a tool or a guard, add its success **and** failure cases there —
  especially anything that must *refuse* (escape attempts, non-unique edits, risky-arg rejection).

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
