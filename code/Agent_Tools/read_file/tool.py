# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""read_file — return a text file's contents (optionally a 0-based line range)."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    p = ctx.safe_path(rel)
    if not os.path.isfile(p):
        hint = " (it is a directory — use list_dir)" if os.path.isdir(p) else ""
        raise FileNotFoundError("no file at: %s%s" % (rel, hint))
    if os.path.getsize(p) > ctx.MAX_BYTES:
        raise ValueError("file is over the %d-byte read cap — use search to find the lines you need" % ctx.MAX_BYTES)
    try:
        with open(p, "rb") as f:
            text = f.read().decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("file is not UTF-8 text (binary or another encoding); read_file only returns UTF-8 text")
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    limit = args.get("limit")
    try:
        limit = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        limit = None
    lines = text.splitlines()
    total = len(lines)
    if offset == 0 and limit is None:                       # uniform shape: report total_lines/truncated on the full read too
        return {"path": rel, "content": text, "total_lines": total, "truncated": False}
    end = total if limit is None else min(total, offset + max(0, limit))
    return {"path": rel, "content": chr(10).join(lines[offset:end]), "offset": offset,
            "lines_returned": max(0, end - offset), "total_lines": total,
            "truncated": end < total}                       # only true when content beyond `end` was actually omitted
