"""list_dir — list files and folders in a workspace directory."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    p = ctx.safe_path(rel)
    if not os.path.isdir(p):
        raise FileNotFoundError("not a directory: %s" % (rel or "."))
    out = []
    for name in sorted(os.listdir(p))[:ctx.MAX_ENTRIES]:
        fp = os.path.join(p, name)
        try:
            isdir = os.path.isdir(fp)
            out.append({"name": name, "type": "dir" if isdir else "file",
                        "size": (None if isdir else os.path.getsize(fp))})
        except OSError:
            continue
    return {"path": rel or "", "entries": out}
