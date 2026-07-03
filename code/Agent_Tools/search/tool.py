# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""search — find a literal substring across workspace files (file:line snippets + surrounding context).

Best-practice defaults (see docs research): skip build/dependency dirs and .gitignore'd names, skip binary files,
smart-case matching, a few lines of context per hit, and a bounded result set with a "narrow it" hint when truncated.
"""
import os
import fnmatch

# Dirs that never hold source worth searching — skipping them keeps the scan budget (and the model's tokens) on real
# code, mirroring ripgrep's ignore defaults. Dot-dirs (.git, .venv, …) are pruned separately.
SKIP_DIRS = {"node_modules", "__pycache__", "dist", "build", "target", "venv", "vendor",
             "site-packages", "bower_components", "coverage", "out", ".gradle"}


def _ignore_patterns(root):
    """Lightweight .gitignore honoring: the SIMPLE basename patterns (no '/', no negation) from the workspace-root
    .gitignore, matched with fnmatch against dir/file basenames. Full gitignore semantics (path anchors, nested
    files, negation) are intentionally NOT reimplemented — SKIP_DIRS covers the common build/dep dirs."""
    pats = []
    gi = os.path.join(root, ".gitignore")
    try:
        if os.path.isfile(gi) and os.path.getsize(gi) < 256 * 1024:
            with open(gi, "r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or line.startswith("!"):
                        continue
                    line = line.rstrip("/")
                    if line and "/" not in line:          # only bare names/globs; skip path-anchored patterns
                        pats.append(line)
    except OSError:
        pass
    return pats


def run(args, ctx):
    query = args.get("query", "")
    # ds4's native search accepts path/file/filename aliases and treats path as a FILE or a directory.
    rel = args.get("path") or args.get("file") or args.get("filename") or ""
    root = ctx.workspace()
    if not root:
        raise PermissionError("no workspace selected")
    if not (query or "").strip():
        raise ValueError("empty query — provide a non-empty 'query' substring to search for")

    # smart-case (ripgrep default): case-insensitive unless the query has an uppercase letter — or the caller pins it.
    cs = args.get("case_sensitive")
    if not isinstance(cs, bool):
        cs = any(c.isupper() for c in query)
    needle = query if cs else query.lower()

    def _clamp(v, default, lo, hi):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default
    ctx_lines = _clamp(args.get("context", 2), 2, 0, 8)          # lines of context each side of a match
    max_results = _clamp(args.get("max_results", 60), 60, 1, 500)

    base = ctx.safe_path(rel)
    ignore = _ignore_patterns(root)
    ignored = (lambda name: any(fnmatch.fnmatch(name, p) for p in ignore)) if ignore else (lambda name: False)

    hits, scanned = [], 0

    def scan_file(fp):                                          # returns True once the result budget is spent
        nonlocal scanned
        scanned += 1
        try:
            if os.path.getsize(fp) > ctx.MAX_BYTES:
                return False
            rp = os.path.realpath(fp)
            if rp != root and not rp.startswith(root + os.sep):
                return False
            with open(fp, "rb") as f:
                raw = f.read()
            if b"\0" in raw[:8192]:                             # ripgrep's binary heuristic: a NUL byte -> skip
                return False
            lines = raw.decode("utf-8").split("\n")             # non-UTF-8 -> UnicodeDecodeError -> skip
        except (OSError, UnicodeDecodeError):
            return False
        relf = os.path.relpath(fp, root)
        for i, line in enumerate(lines):
            if needle in (line if cs else line.lower()):
                m = {"file": relf, "line": i + 1, "text": line.rstrip()[:300]}
                if ctx_lines:                                  # numbered window; the match line is marked with ':'
                    lo, hi = max(0, i - ctx_lines), min(len(lines), i + ctx_lines + 1)
                    m["context"] = "\n".join("%d%s %s" % (j + 1, ":" if j == i else " ", lines[j].rstrip()[:200])
                                             for j in range(lo, hi))
                hits.append(m)
                if len(hits) >= max_results:
                    return True
        return False

    if os.path.isfile(base):                                   # a FILE path searches just that file (ds4 parity)
        scan_file(base)
    else:                                                      # a directory / whole workspace walks the tree
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in SKIP_DIRS and not ignored(d)]
            for fn in filenames:
                if len(hits) >= max_results or scanned >= 4000:
                    break
                if not ignored(fn) and scan_file(os.path.join(dirpath, fn)):
                    break
            if len(hits) >= max_results or scanned >= 4000:
                break                                          # stop walking once a budget is spent

    truncated = len(hits) >= max_results or scanned >= 4000
    out = {"query": query, "case_sensitive": cs, "matches": hits, "scanned": scanned, "truncated": truncated}
    if truncated:
        out["note"] = "results truncated — narrow with a more specific query, a 'path' to one file/subdir, or raise 'max_results'"
    return out
