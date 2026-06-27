"""mkdir — create a directory (and parents) within the workspace."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    if not rel.strip():
        raise ValueError("'path' is required — give a directory path relative to the workspace root")
    p = ctx.safe_path(rel)
    if os.path.isfile(p):
        raise ValueError("a file already exists at that path: %s — choose a different path or delete the file first" % rel)
    existed = os.path.isdir(p)
    os.makedirs(p, exist_ok=True)
    return {"path": rel, "created": not existed}      # False when the directory already existed (no-op)
