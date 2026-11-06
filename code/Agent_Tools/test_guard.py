#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""Tests for who may call the localhost services. They start the real HTTP handlers on free ports and
send the requests a hostile web page or a stray local program would send.

Run:  python3 test_guard.py
Stdlib `unittest` only, like test_tools.py. Linux only, because the sidecar and `execute` are.
"""
import http.client, json, os, shutil, sys, tempfile, threading, unittest
from http.server import ThreadingHTTPServer

import agent_tools as A

import metrics_sidecar as M
from _local_guard import Guard, default_origins

TOKEN = "test-token-not-a-secret"
GOOD_ORIGIN = "http://localhost:8090"
EVIL_ORIGIN = "https://evil.example"


class ServerCase(unittest.TestCase):
    handler = None

    def start_server(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, method, path, body=None, headers=None, host=None):
        """Returns (status, parsed JSON or None, response headers)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        sent = {"Host": host or "127.0.0.1:%d" % self.port}
        sent.update(headers or {})
        conn.request(method, path, body=data, headers=sent)
        res = conn.getresponse()
        raw = res.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return res.status, parsed, res.headers


class AgentCase(ServerCase):
    handler = A.Handler

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="ds4guard_")
        self.addCleanup(shutil.rmtree, self.ws, True)
        A.set_workspace(self.ws)
        self.start_server()
        saved, A.GUARD = A.GUARD, Guard(self.port, token=TOKEN)
        self.import_guard = saved
        self.addCleanup(setattr, A, "GUARD", saved)
        self.marker = os.path.join(self.ws, "ran.txt")

    def auth(self, **extra):
        return dict({"X-DS4-Token": TOKEN}, **extra)

    def touch_args(self):
        return {"argv": ["touch", self.marker]}

    def ticket(self, tool, args):
        status, body, _ = self.call("POST", "/approvals", {"tool": tool, "args": args}, self.auth())
        self.assertEqual(status, 200, body)
        return body["ticket"]


class TestHostilePage(AgentCase):
    """What a web page could do before: run commands and move the file lock."""

    def test_fetch_from_other_site_cannot_execute(self):
        status, _, headers = self.call("POST", "/tools/execute", self.touch_args(),
                                       {"Origin": EVIL_ORIGIN, "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))
        self.assertFalse(os.path.exists(self.marker))

    def test_form_style_post_without_preflight_cannot_execute(self):
        status, _, _ = self.call("POST", "/tools/execute", json.dumps(self.touch_args()).encode(),
                                 {"Origin": EVIL_ORIGIN, "Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(self.marker))

    def test_stolen_token_is_useless_from_another_origin(self):
        status, _, _ = self.call("POST", "/tools/read_file", {"path": "."}, self.auth(Origin=EVIL_ORIGIN))
        self.assertEqual(status, 403)

    def test_origin_null_is_refused(self):
        status, _, _ = self.call("GET", "/tools", headers=self.auth(Origin="null"))
        self.assertEqual(status, 403)

    def test_cannot_move_workspace(self):
        status, _, _ = self.call("POST", "/workspace", {"path": "/"}, {"Origin": EVIL_ORIGIN})
        self.assertEqual(status, 403)
        self.assertEqual(A.workspace(), os.path.realpath(self.ws))

    def test_preflight_from_other_site_gets_no_cors(self):
        status, _, headers = self.call("OPTIONS", "/tools/execute", headers={
            "Origin": EVIL_ORIGIN, "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_dns_rebinding_host_is_refused(self):
        status, _, _ = self.call("GET", "/healthz", host="evil.example:%d" % self.port)
        self.assertEqual(status, 403)
        status, _, _ = self.call("POST", "/tools/read_file", {"path": "."}, self.auth(), host="evil.example:%d" % self.port)
        self.assertEqual(status, 403)

    def test_cross_site_tag_request_is_refused(self):
        status, _, _ = self.call("GET", "/healthz", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)


class TestDashboardAccess(AgentCase):
    def test_preflight_from_dashboard_echoes_exact_origin(self):
        status, _, headers = self.call("OPTIONS", "/tools/execute", headers={
            "Origin": GOOD_ORIGIN, "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), GOOD_ORIGIN)
        self.assertIn("Origin", headers.get("Vary", ""))
        allowed = headers.get("Access-Control-Allow-Headers", "")
        for name in ("X-DS4-Token", "X-DS4-Approval", "X-DS4-Run-Id", "Content-Type"):
            self.assertIn(name, allowed)

    def test_no_response_uses_wildcard_cors(self):
        for method, path, body in (("GET", "/healthz", None), ("GET", "/tools", None), ("POST", "/tools/list_dir", {"path": "."})):
            _, _, headers = self.call(method, path, body, self.auth(Origin=GOOD_ORIGIN))
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), GOOD_ORIGIN, path)

    def test_every_endpoint_but_healthz_needs_the_token(self):
        cases = [("GET", "/workspace", None), ("GET", "/browse?path=/", None), ("GET", "/tools", None),
                 ("POST", "/workspace", {"path": "/"}), ("POST", "/tools/tree", {"path": ""}),
                 ("POST", "/tools/read_file", {"path": "x"}), ("POST", "/approvals", {"tool": "execute", "args": {}}),
                 ("POST", "/jobs/cleanup", {"run_id": "1"})]
        for method, path, body in cases:
            for headers in ({}, {"X-DS4-Token": "wrong"}, {"Origin": GOOD_ORIGIN}):
                status, reply, _ = self.call(method, path, body, headers)
                self.assertEqual(status, 401, (method, path, headers))
                self.assertEqual(reply["kind"], "unauthorized")
        self.assertEqual(A.workspace(), os.path.realpath(self.ws))

    def test_healthz_hides_workspace_without_token(self):
        status, body, _ = self.call("GET", "/healthz")
        self.assertEqual((status, body), (200, {"ok": True}))
        status, body, _ = self.call("GET", "/healthz", headers=self.auth())
        self.assertEqual(body["workspace"], os.path.realpath(self.ws))

    def test_read_only_tool_needs_no_ticket(self):
        status, body, _ = self.call("POST", "/tools/list_dir", {"path": "."}, self.auth(Origin=GOOD_ORIGIN))
        self.assertEqual(status, 200, body)

    def test_guard_defaults_to_a_random_token_before_main_runs(self):
        self.assertFalse(Guard(1).has_token({"X-DS4-Token": ""}))      # no token configured: nothing matches
        self.assertTrue(self.import_guard.token)
        self.assertNotEqual(self.import_guard.token, TOKEN)

    def test_custom_origin_replaces_defaults(self):
        guard = Guard(9, origins=["http://localhost:1234"])
        self.assertEqual(guard.cors_origin({"Origin": "http://localhost:1234"}), "http://localhost:1234")
        self.assertIsNone(guard.cors_origin({"Origin": GOOD_ORIGIN}))
        self.assertEqual(default_origins(1), ["http://localhost:1", "http://127.0.0.1:1"])


class TestApprovalTickets(AgentCase):
    def test_gated_tools_refuse_without_ticket(self):
        gated = [n for n, t in A.REGISTRY.items() if A.needs_approval(t)]
        self.assertEqual(sorted(gated), ["delete", "edit_file", "execute", "mkdir", "run_command", "write_file"])
        for name in gated:
            status, body, _ = self.call("POST", "/tools/" + name, {"path": "x"}, self.auth())
            self.assertEqual(status, 403, name)
            self.assertEqual(body["kind"], "approval_required")

    def test_ticket_runs_exactly_once(self):
        args = self.touch_args()
        ticket = self.ticket("execute", args)
        status, body, _ = self.call("POST", "/tools/execute", args, self.auth(**{"X-DS4-Approval": ticket}))
        self.assertEqual(status, 200, body)
        self.assertTrue(os.path.exists(self.marker))
        os.unlink(self.marker)
        status, _, _ = self.call("POST", "/tools/execute", args, self.auth(**{"X-DS4-Approval": ticket}))
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(self.marker))

    def test_ticket_is_tied_to_its_arguments(self):
        ticket = self.ticket("execute", {"argv": ["true"]})
        status, _, _ = self.call("POST", "/tools/execute", self.touch_args(), self.auth(**{"X-DS4-Approval": ticket}))
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(self.marker))

    def test_ticket_is_tied_to_its_tool(self):
        args = {"path": "made.txt", "content": "x"}
        ticket = self.ticket("mkdir", args)
        status, _, _ = self.call("POST", "/tools/write_file", args, self.auth(**{"X-DS4-Approval": ticket}))
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "made.txt")))

    def test_key_order_does_not_matter(self):
        ticket = self.ticket("write_file", {"path": "a.txt", "content": "x"})
        status, body, _ = self.call("POST", "/tools/write_file", {"content": "x", "path": "a.txt"},
                                    self.auth(**{"X-DS4-Approval": ticket}))
        self.assertEqual(status, 200, body)

    def test_expired_ticket_is_refused(self):
        args = self.touch_args()
        ticket = self.ticket("execute", args)
        with A._lock:
            digest, _ = A._approvals[ticket]
            A._approvals[ticket] = (digest, 0.0)
        status, _, _ = self.call("POST", "/tools/execute", args, self.auth(**{"X-DS4-Approval": ticket}))
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(self.marker))

    def test_unknown_tool_gets_no_ticket(self):
        status, _, _ = self.call("POST", "/approvals", {"tool": "nope", "args": {}}, self.auth())
        self.assertEqual(status, 404)

    def test_commands_do_not_inherit_the_token(self):
        os.environ["DS4_AGENT_TOKEN"] = TOKEN
        self.addCleanup(os.environ.pop, "DS4_AGENT_TOKEN", None)
        self.assertNotIn("DS4_AGENT_TOKEN", A._scrubbed_env())


class TestSidecar(ServerCase):
    handler = M.Handler

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ds4side_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "lastsession.json")
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"ds4x:chatMsgs": "private history"}, f)
        for name, value in (("state_file", self.state), ("logs_dir", self.dir)):
            saved = getattr(M.Handler, name)
            self.addCleanup(setattr, M.Handler, name, saved)
            setattr(M.Handler, name, value)
        self.start_server()
        saved, M.Handler.guard = M.Handler.guard, Guard(self.port)
        self.addCleanup(setattr, M.Handler, "guard", saved)

    def stored(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def test_other_site_cannot_read_saved_history(self):
        status, body, headers = self.call("GET", "/state", headers={"Origin": EVIL_ORIGIN})
        self.assertEqual(status, 403)
        self.assertNotIn("private history", json.dumps(body))
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_other_site_cannot_overwrite_state_or_write_logs(self):
        status, _, _ = self.call("POST", "/state", {"ds4x:workspace": "/"}, {"Origin": EVIL_ORIGIN, "Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        self.assertEqual(self.stored(), {"ds4x:chatMsgs": "private history"})
        status, _, _ = self.call("POST", "/log", {"name": "x.md", "content": "x"}, {"Origin": EVIL_ORIGIN})
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x.md")))

    def test_dns_rebinding_cannot_read_state(self):
        status, _, _ = self.call("GET", "/state", host="evil.example:%d" % self.port)
        self.assertEqual(status, 403)

    def test_dashboard_can_read_and_beacon(self):
        status, body, headers = self.call("GET", "/state", headers={"Origin": GOOD_ORIGIN})
        self.assertEqual(status, 200)
        self.assertEqual(body["ds4x:chatMsgs"], "private history")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), GOOD_ORIGIN)
        status, _, _ = self.call("POST", "/state", json.dumps({"ds4x:mode": "chat"}).encode(),
                                 {"Origin": GOOD_ORIGIN, "Content-Type": "text/plain"})
        self.assertEqual(status, 200)
        self.assertEqual(self.stored(), {"ds4x:mode": "chat"})

    def test_launcher_health_check_still_works(self):
        status, body, _ = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIn("metrics_hits", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
