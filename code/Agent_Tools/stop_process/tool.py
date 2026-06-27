"""stop_process — terminate a background process by job_id (group-kill: SIGTERM -> grace -> SIGKILL)."""


def validate(args):
    if not isinstance(args.get("job_id"), str) or not args["job_id"].strip():
        raise ValueError("'job_id' is required (from execute background:true / list_processes)")


def run(args, ctx):
    return ctx.jobs.stop(args["job_id"])
