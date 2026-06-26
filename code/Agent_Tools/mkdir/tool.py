"""mkdir — create a directory (and parents) within the workspace."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    p = ctx.safe_path(rel)
    if os.path.isfile(p):
        raise ValueError("a file already exists at that path: %s" % rel)
    os.makedirs(p, exist_ok=True)
    return {"path": rel, "created": True}
