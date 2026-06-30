# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""search — find a literal substring across workspace files (returns file:line snippets)."""
import os


def run(args, ctx):
    query = args.get("query", "")
    rel = args.get("path", "")
    root = ctx.workspace()
    if not root:
        raise PermissionError("no workspace selected")
    if not (query or "").strip():
        raise ValueError("empty query — provide a non-empty 'query' substring to search for")
    base = ctx.safe_path(rel)
    hits, scanned = [], 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if len(hits) >= 200 or scanned >= 4000:
                break
            scanned += 1                       # count every file inspected so the budget actually bounds traversal
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > ctx.MAX_BYTES:
                    continue
                rp = os.path.realpath(fp)
                if rp != root and not rp.startswith(root + os.sep):
                    continue
                with open(fp, "r", encoding="utf-8") as f:
                    for ln, line in enumerate(f, 1):
                        if query in line:
                            hits.append({"file": os.path.relpath(fp, root),
                                         "line": ln, "text": line.rstrip()[:300]})
                            if len(hits) >= 200:
                                break
            except (OSError, UnicodeDecodeError):
                continue
        if len(hits) >= 200 or scanned >= 4000:
            break                              # stop walking once a budget is spent (don't traverse the rest of the tree)
    # truncated reflects EITHER cap: the 200-hit cap or the 4000-file scan budget
    return {"query": query, "matches": hits, "scanned": scanned,
            "truncated": len(hits) >= 200 or scanned >= 4000}
