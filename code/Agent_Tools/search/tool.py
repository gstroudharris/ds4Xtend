# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""search — find a literal substring across workspace files (returns file:line snippets)."""
import os


def run(args, ctx):
    query = args.get("query", "")
    # ds4's native `search` accepts path/file/filename as aliases and treats `path` as a FILE *or* a directory.
    # Match that: the model (trained on ds4) commonly narrows a search to one file — os.walk() yields nothing on a
    # file, which silently returned scanned:0 and made the tool look broken.
    rel = args.get("path") or args.get("file") or args.get("filename") or ""
    root = ctx.workspace()
    if not root:
        raise PermissionError("no workspace selected")
    if not (query or "").strip():
        raise ValueError("empty query — provide a non-empty 'query' substring to search for")
    base = ctx.safe_path(rel)
    hits, scanned = [], 0

    def scan_file(fp):                                     # returns True once the 200-hit budget is spent
        nonlocal scanned
        scanned += 1                                       # count every file inspected so the budget actually bounds traversal
        try:
            if os.path.getsize(fp) > ctx.MAX_BYTES:
                return False
            rp = os.path.realpath(fp)
            if rp != root and not rp.startswith(root + os.sep):
                return False
            with open(fp, "r", encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    if query in line:
                        hits.append({"file": os.path.relpath(fp, root),
                                     "line": ln, "text": line.rstrip()[:300]})
                        if len(hits) >= 200:
                            return True
        except (OSError, UnicodeDecodeError):
            pass
        return False

    if os.path.isfile(base):                               # a FILE path searches just that file (ds4 parity)
        scan_file(base)
    else:                                                  # a directory (or the whole workspace) walks the tree
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if len(hits) >= 200 or scanned >= 4000:
                    break
                if scan_file(os.path.join(dirpath, fn)):
                    break
            if len(hits) >= 200 or scanned >= 4000:
                break                                      # stop walking once a budget is spent
    # truncated reflects EITHER cap: the 200-hit cap or the 4000-file scan budget
    return {"query": query, "matches": hits, "scanned": scanned,
            "truncated": len(hits) >= 200 or scanned >= 4000}
