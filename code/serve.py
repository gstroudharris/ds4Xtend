#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""Static file server for the ds4Xtend dashboard. Identical to `python3 -m http.server`
but silently tolerates client disconnects (no Broken pipe tracebacks)."""
import argparse, functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser cancelled an in-flight asset load — not an error

    def log_message(self, *a):  # quiet, matches the other services
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--directory", default=".")
    args = ap.parse_args()
    h = functools.partial(Handler, directory=args.directory)
    ThreadingHTTPServer((args.host, args.port), h).serve_forever()


if __name__ == "__main__":
    main()
