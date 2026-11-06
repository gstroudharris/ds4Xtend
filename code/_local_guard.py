#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""Request checks shared by ds4Xtend's localhost services (agent tools, metrics sidecar).

Binding to 127.0.0.1 keeps other machines out, but not web pages: any site open in the user's
browser can send requests to localhost. These checks refuse requests that didn't come from the
dashboard:

  Host            must name loopback and this service's port, which stops DNS rebinding (a hostile
                  domain re-pointed at 127.0.0.1 looks same-origin to the browser, but keeps its Host).
  Origin          when present, must exactly match the dashboard's address. Browsers set it on every
                  cross-origin fetch and every POST, and page scripts can't forge it. Command-line
                  clients send none, which is allowed; the agent service still requires its token.
  Sec-Fetch-Site  a cross-site GET from a tag like <img> or <script> carries no Origin, so it's
                  caught here instead.
  Token           X-DS4-Token, checked by the agent service only. It also covers local programs
                  that aren't browsers, unless they can read the token file.

Stdlib only.
"""
import hmac

DEFAULT_WEB_PORT = 8090
TOKEN_HEADER = "X-DS4-Token"
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "[::1]")


def default_origins(port=DEFAULT_WEB_PORT):
    """The dashboard's addresses when served by serve.py on its usual port."""
    return ["http://localhost:%d" % port, "http://127.0.0.1:%d" % port]


class Guard:
    def __init__(self, port, origins=None, token=None, host=None):
        self.origins = frozenset(origins or default_origins())
        self.token = token
        names = set(LOOPBACK_NAMES)
        if host and host not in ("0.0.0.0", "::"):       # --host naming a real interface
            names.add(host.lower())
        self.hosts = frozenset("%s:%d" % (name, port) for name in names)

    def cors_origin(self, headers):
        """The Origin to echo in Access-Control-Allow-Origin, or None. Never a wildcard."""
        origin = headers.get("Origin")
        return origin if origin in self.origins else None

    def deny_reason(self, headers):
        """Why this request can't be from the dashboard or a local client, or None if it's acceptable."""
        if (headers.get("Host") or "").lower() not in self.hosts:
            return "Host not allowed"
        origin = headers.get("Origin")
        if origin is not None:
            return None if origin in self.origins else "Origin not allowed"
        if headers.get("Sec-Fetch-Site") == "cross-site":
            return "cross-site request not allowed"
        return None

    def has_token(self, headers):
        """True only when a token is configured and the request carries exactly that token."""
        if not self.token:
            return False
        return hmac.compare_digest((headers.get(TOKEN_HEADER) or "").encode(), self.token.encode())
