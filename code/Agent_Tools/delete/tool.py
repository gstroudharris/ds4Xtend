# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""delete — remove a file, or an EMPTY directory (no recursive delete)."""
import errno
import os


def run(args, ctx):
    rel = args.get("path", "")
    p = ctx.safe_path(rel)
    if p == ctx.workspace():
        raise PermissionError("refusing to delete the workspace root")
    if os.path.isdir(p):
        try:
            os.rmdir(p)            # empty dirs only — no recursive delete
        except OSError as e:
            if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise ValueError("directory not empty (no recursive delete): %s — delete its contents first" % rel)
            raise                  # a different OSError (permission/busy/…) -> generic io error, not mislabeled
        return {"path": rel, "deleted": "directory"}
    if os.path.isfile(p):
        os.remove(p)
        return {"path": rel, "deleted": "file"}
    raise FileNotFoundError("nothing to delete at: %s" % rel)
