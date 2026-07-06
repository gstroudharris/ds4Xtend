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
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    limit = args.get("limit")
    try:
        limit = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        limit = None
    if os.path.getsize(p) > ctx.MAX_BYTES:
        # Over-cap files are still readable in RANGES: stream line-by-line so a whole-file load never
        # happens. (The old behavior raised unconditionally and pointed at `search` — but search skips
        # over-cap files entirely, so big files were completely unreachable.)
        if limit is None:
            raise ValueError("file is over the %d-byte read cap — pass offset+limit to read it in ranges "
                             "(e.g. offset=0, limit=400), or use search on smaller files" % ctx.MAX_BYTES)
        out, total, budget = [], 0, ctx.MAX_BYTES // 2
        try:
            with open(p, "r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    total = i + 1
                    if offset <= i < offset + limit and budget > 0:
                        line = line.rstrip(chr(10))
                        out.append(line)
                        budget -= len(line) + 1
        except UnicodeDecodeError:
            raise ValueError("file is not UTF-8 text (binary or another encoding); read_file only returns UTF-8 text")
        end = min(total, offset + limit)
        return {"path": rel, "content": chr(10).join(out), "offset": offset,
                "lines_returned": len(out), "total_lines": total,
                "truncated": end < total or budget <= 0}
    try:
        with open(p, "rb") as f:
            text = f.read().decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("file is not UTF-8 text (binary or another encoding); read_file only returns UTF-8 text")
    lines = text.splitlines()
    total = len(lines)
    if offset == 0 and limit is None:                       # uniform shape: report total_lines/truncated on the full read too
        return {"path": rel, "content": text, "total_lines": total, "truncated": False}
    end = total if limit is None else min(total, offset + max(0, limit))
    return {"path": rel, "content": chr(10).join(lines[offset:end]), "offset": offset,
            "lines_returned": max(0, end - offset), "total_lines": total,
            "truncated": end < total}                       # only true when content beyond `end` was actually omitted
