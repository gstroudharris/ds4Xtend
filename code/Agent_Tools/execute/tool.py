"""execute — run an arbitrary command in the workspace (HIGH-RISK; gated by human approval in Ask mode).

Hybrid input: an `argv` array runs with NO shell (no injection surface); `shell:true` + `command` runs via
bash -c for pipes/&&/redirects. cwd is confined to the workspace; the command itself can still reach the wider
machine — which is why spec.json marks this risk:"high". In the UI that means a ⚠ approval in Ask mode; in
Auto mode the agent runs commands autonomously (the Ask/Auto switch is the approval gate)."""
import os


def validate(args):
    # Enforce the schema in CODE (defense in depth): exactly one valid command form must be present.
    if args.get("shell") and args.get("argv"):
        raise ValueError("provide EITHER argv OR shell+command, not both")
    if args.get("shell"):
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("shell=true requires a non-empty 'command' string")
    else:
        argv = args.get("argv")
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
            raise ValueError("provide 'argv' as a non-empty array of strings, or set shell=true with 'command'")
    if args.get("background") and not (args.get("goal") or "").strip():
        raise ValueError("background:true requires a 'goal' (state why the process is being started)")


def run(args, ctx):
    spec = {"argv": args.get("argv"), "shell": bool(args.get("shell")),
            "command": args.get("command"), "cwd": args.get("cwd")}
    if args.get("background"):                       # long-lived process: return a handle, don't block
        argv, shown, cwd = ctx.resolve_command(spec)
        return ctx.jobs.spawn(argv, cwd, shown, goal=args.get("goal"),
                              run_id=ctx.current_run(), scope=(args.get("scope") or "run"),
                              max_lifetime=args.get("max_lifetime_sec"),
                              ready_when=args.get("ready_when"))
    res = ctx.run_process(spec)                      # foreground: run to completion
    _hint_if_abs_path(args, ctx, res)                # loud footgun: a leading-'/' path used like a file-tool path
    return res


def _hint_if_abs_path(args, ctx, res):
    """If the command couldn't find a file and referenced a leading-'/' path that exists RELATIVE to the workspace
    (the cwd) but not on the real filesystem, the model used a file-tool-style absolute path in a shell. Add an
    actionable 'hint' so it self-corrects (per TOOL_TEMPLATE) instead of flailing. Additive — never changes the run."""
    if res.get("timed_out") or res.get("exit_code") in (0, None):
        return
    if res.get("exit_code") != 127 and "No such file or directory" not in (res.get("stderr") or ""):
        return
    ws = ctx.workspace() or ""
    tokens = [a for a in (args.get("argv") or []) if isinstance(a, str)]
    if args.get("shell") and isinstance(args.get("command"), str):
        tokens += args["command"].split()
    for t in tokens:
        if t.startswith("/") and len(t) > 1:
            rel = t.lstrip("/")
            if rel and not os.path.exists(t) and os.path.exists(os.path.join(ws, rel)):
                res["hint"] = ("commands run from the workspace root, so use a path relative to it: %r is not on the "
                               "real filesystem, but %r exists in the workspace — re-run with the relative path "
                               "(drop the leading '/')." % (t, rel))
                return
