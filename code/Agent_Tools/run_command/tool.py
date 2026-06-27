"""run_command — run a PRE-VETTED named command from the project's .ds4/commands.json.

A human authored the manifest, and the model only chooses a name (it cannot pass arguments), so a declared
command stays exactly as vetted. That's why spec.json leaves this at default risk (mutating, not high): it is
approved in Ask mode but may run unattended in Auto — enabling an edit -> run tests -> fix loop."""


def validate(args):
    if not isinstance(args.get("name"), str) or not args["name"].strip():
        raise ValueError("'name' must be a non-empty string")


def run(args, ctx):
    name = args["name"].strip()
    cmds = ctx.read_commands()
    if not cmds:
        raise ValueError("no .ds4/commands.json in this project — no named commands are declared")
    spec = cmds.get(name)
    if not spec:
        raise ValueError("unknown command %r — available: %s" % (name, ", ".join(sorted(cmds))))
    # Translate a manifest entry ({argv:[...]} or {shell:"..."}) into a run_process spec.
    if isinstance(spec.get("shell"), str):
        runspec = {"shell": True, "command": spec["shell"], "cwd": spec.get("cwd")}
    else:
        runspec = {"argv": spec.get("argv"), "cwd": spec.get("cwd")}
    res = ctx.run_process(runspec)
    res["name"] = name
    return res
