"""process_output — read a background process's output + status by job_id (renews its lease)."""


def validate(args):
    if not isinstance(args.get("job_id"), str) or not args["job_id"].strip():
        raise ValueError("'job_id' is required (from execute background:true / list_processes)")


def run(args, ctx):
    return ctx.jobs.output(args["job_id"], tail=args.get("tail"))
