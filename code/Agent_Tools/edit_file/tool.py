# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""edit_file — unique-match find/replace (set replace_all to change every occurrence)."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    find = args.get("find", "")
    replace = args.get("replace", "")
    replace_all = args.get("replace_all", False)
    if not find:
        raise ValueError("empty 'find' string")
    p = ctx.safe_path(rel)
    if not os.path.isfile(p):
        raise FileNotFoundError("not a file: %s" % rel)
    if os.path.getsize(p) > ctx.MAX_BYTES:
        raise ValueError("file is over the %d-byte edit cap — edit a smaller file or split the change" % ctx.MAX_BYTES)
    try:
        with open(p, "rb") as f:
            text = f.read().decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("file is not UTF-8 text")
    count = text.count(find)
    if count == 0:
        raise ValueError("'find' text not found in file")
    if count > 1 and not replace_all:    # unique-match by default — never silently edit the wrong/extra place
        raise ValueError("'find' matches %d places — add surrounding text to make it unique, or set replace_all=true" % count)
    enc = text.replace(find, replace if replace is not None else "").encode("utf-8")
    if len(enc) > ctx.MAX_BYTES:
        raise ValueError("result would be over the %d-byte cap after replacement" % ctx.MAX_BYTES)
    with open(p, "wb") as f:
        f.write(enc)
    return {"path": rel, "replacements": count, "bytes": len(enc)}
