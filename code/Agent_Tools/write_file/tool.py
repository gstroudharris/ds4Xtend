# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""write_file — create or overwrite a file with its full contents (within the workspace)."""
import os


def run(args, ctx):
    rel = args.get("path", "")
    if not rel.strip():
        raise ValueError("'path' is required — give a path relative to the workspace root")
    if "content" not in args or args["content"] is None:    # loud: never silently truncate a file to empty
        raise ValueError("'content' is required — pass the file's COMPLETE new contents (use \"\" for an empty file)")
    content = args["content"]
    if not isinstance(content, str):
        raise ValueError("'content' must be a string (the file's full text)")
    enc = content.encode("utf-8")
    if len(enc) > ctx.MAX_BYTES:
        raise ValueError("content is %d bytes, over the %d-byte cap — split the file or use edit_file for an incremental change" % (len(enc), ctx.MAX_BYTES))
    p = ctx.safe_path(rel)
    if os.path.isdir(p):
        raise ValueError("path is a directory, not a file: %s" % rel)
    existed = os.path.isfile(p)
    parent = os.path.dirname(p)
    ctx.safe_path(os.path.relpath(parent, ctx.workspace()))    # re-validate parent stays inside
    os.makedirs(parent, exist_ok=True)
    with open(p, "wb") as f:
        f.write(enc)
    return {"path": rel, "bytes": len(enc), "created": not existed}
