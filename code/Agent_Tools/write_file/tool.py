"""write_file — create or overwrite a file with its full contents (within the workspace)."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    content = args.get("content", "")
    if content is None:
        content = ""
    enc = content.encode("utf-8")
    if len(enc) > ctx.MAX_BYTES:
        raise ValueError("content too large (>%d bytes)" % ctx.MAX_BYTES)
    p = ctx.safe_path(rel)
    if os.path.isdir(p):
        raise ValueError("path is a directory: %s" % rel)
    parent = os.path.dirname(p)
    ctx.safe_path(os.path.relpath(parent, ctx.workspace()))    # re-validate parent stays inside
    os.makedirs(parent, exist_ok=True)
    with open(p, "wb") as f:
        f.write(enc)
    return {"path": rel, "bytes": len(enc)}
