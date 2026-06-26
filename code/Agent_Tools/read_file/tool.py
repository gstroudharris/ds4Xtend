"""read_file — return a text file's contents (optionally a 0-based line range)."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    p = ctx.safe_path(rel)
    if not os.path.isfile(p):
        raise FileNotFoundError("not a file: %s" % rel)
    if os.path.getsize(p) > ctx.MAX_BYTES:
        raise ValueError("file too large (>%d bytes)" % ctx.MAX_BYTES)
    try:
        text = open(p, "rb").read().decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("file is not UTF-8 text")
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    limit = args.get("limit")
    try:
        limit = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        limit = None
    if offset == 0 and limit is None:
        return {"path": rel, "content": text}
    lines = text.splitlines()
    total = len(lines)
    end = total if limit is None else min(total, offset + max(0, limit))
    return {"path": rel, "content": chr(10).join(lines[offset:end]), "offset": offset,
            "lines_returned": max(0, end - offset), "total_lines": total,
            "truncated": (end < total or offset > 0)}
