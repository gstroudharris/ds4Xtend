"""list_processes — show the background processes the agent has running (via execute background:true)."""


def run(args, ctx):
    return {"processes": ctx.jobs.list() if ctx.jobs else []}
