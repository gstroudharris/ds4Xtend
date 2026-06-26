"""delete — remove a file, or an EMPTY directory (no recursive delete)."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    p = ctx.safe_path(rel)
    if p == ctx.workspace():
        raise PermissionError("refusing to delete the workspace root")
    if os.path.isdir(p):
        try:
            os.rmdir(p)            # empty dirs only — no recursive delete
        except OSError:
            raise ValueError("directory not empty (refusing recursive delete): %s" % rel)
        return {"path": rel, "deleted": "directory"}
    if os.path.isfile(p):
        os.remove(p)
        return {"path": rel, "deleted": "file"}
    raise FileNotFoundError("nothing to delete at: %s" % rel)
