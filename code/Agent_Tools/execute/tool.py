"""execute — run an arbitrary command in the workspace (HIGH-RISK; gated by human approval in Ask mode).

Hybrid input: an `argv` array runs with NO shell (no injection surface); `shell:true` + `command` runs via
bash -c for pipes/&&/redirects. cwd is confined to the workspace; the command itself can still reach the wider
machine — which is why spec.json marks this risk:"high". In the UI that means a ⚠ approval in Ask mode; in
Auto mode the agent runs commands autonomously (the Ask/Auto switch is the approval gate)."""


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


def run(args, ctx):
    return ctx.run_process({"argv": args.get("argv"), "shell": bool(args.get("shell")),
                            "command": args.get("command"), "cwd": args.get("cwd")})
