"""list_dir — list files and folders in a workspace directory."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    p = ctx.safe_path(rel)
    if not os.path.isdir(p):
        hint = " (it is a file — use read_file)" if os.path.isfile(p) else ""
        raise FileNotFoundError("not a directory: %s%s" % (rel or ".", hint))
    names = sorted(os.listdir(p))
    out = []
    for name in names[:ctx.MAX_ENTRIES]:
        fp = os.path.join(p, name)
        try:
            isdir = os.path.isdir(fp)
            out.append({"name": name, "type": "dir" if isdir else "file",
                        "size": (None if isdir else os.path.getsize(fp))})
        except OSError:
            continue
    return {"path": rel, "entries": out, "total": len(names),
            "truncated": len(names) > ctx.MAX_ENTRIES}      # so the model can tell a complete listing from a clipped one
