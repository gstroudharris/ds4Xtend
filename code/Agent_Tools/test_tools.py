#!/usr/bin/env python3
"""Edge-case + safety tests for the ds4 agent tool registry.

Run:  python3 test_tools.py            (verbose: add -v)
  or: python3 -m unittest test_tools

Stdlib `unittest` only — no pytest, matching the stdlib-only sidecar. The point of these tests is the
FAILURE modes (where a tool either misbehaves silently or must refuse), because that's what makes the
model get tool use right the first time AND what keeps the sandbox a sandbox:

  - the workspace lock holds against `..` traversal AND symlink escapes
  - per-tool guards fire: unique-match edit, empty-dir-only delete, root-delete refusal
  - UTF-8 / size limits are enforced; read_file paging is correct
  - the registry auto-discovers tools and captures the risk + validate() extension, and a broken
    tool folder is skipped rather than crashing the server

Add a case here whenever you add a tool or a guard — this is the "iterative evaluation" backstop.
"""
import json, os, shutil, tempfile, types, unittest
import agent_tools as A


def make_ctx(max_bytes=A.MAX_BYTES, max_entries=A.MAX_ENTRIES):
    """A ctx like the server's CTX, but tests can shrink the caps to exercise the limits cheaply."""
    return types.SimpleNamespace(safe_path=A.safe_path, workspace=A.workspace,
                                 MAX_BYTES=max_bytes, MAX_ENTRIES=max_entries,
                                 run_process=A.run_process, read_commands=A.read_commands)


def run(name, args, ctx):
    """Invoke a registered tool's run() exactly as the dispatcher would."""
    return A.REGISTRY[name]["run"](args, ctx)


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def spit(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class ToolBase(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="ds4test_")
        A.set_workspace(self.ws)
        self.ctx = make_ctx()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def put(self, rel, data=""):
        p = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(p) or self.ws, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p


class TestSandbox(ToolBase):
    """The lock: every path is realpath-confined to the workspace."""

    def test_parent_traversal_blocked(self):
        with self.assertRaises(PermissionError):
            A.safe_path("../escape")

    def test_absolute_path_is_neutralized_to_workspace(self):
        # a leading '/' is stripped, so "/etc/passwd" resolves INSIDE the workspace, not at root
        self.assertEqual(A.safe_path("/etc/passwd"), os.path.join(self.ws, "etc/passwd"))

    def test_symlink_escape_blocked(self):
        os.symlink("/etc", os.path.join(self.ws, "etclink"))
        with self.assertRaises(PermissionError):
            A.safe_path("etclink/hostname")            # realpath resolves the symlink out of the workspace

    def test_read_through_traversal_blocked(self):
        with self.assertRaises(PermissionError):
            run("read_file", {"path": "../../../../etc/passwd"}, self.ctx)

    def test_no_workspace_refuses(self):
        A._ws["root"] = None
        try:
            with self.assertRaises(PermissionError):
                A.safe_path("anything")
        finally:
            A.set_workspace(self.ws)


class TestReadFile(ToolBase):
    def test_read_whole(self):
        self.put("a.txt", "hello\nworld\n")
        self.assertEqual(run("read_file", {"path": "a.txt"}, self.ctx)["content"], "hello\nworld\n")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            run("read_file", {"path": "nope.txt"}, self.ctx)

    def test_non_utf8_rejected(self):
        self.put("bin.dat", b"\xff\xfe\x00\x01\x80")
        with self.assertRaises(ValueError):
            run("read_file", {"path": "bin.dat"}, self.ctx)

    def test_too_large_rejected(self):
        self.put("big.txt", "x" * 50)
        with self.assertRaises(ValueError):
            run("read_file", {"path": "big.txt"}, make_ctx(max_bytes=10))

    def test_offset_limit_paging(self):
        self.put("lines.txt", "L0\nL1\nL2\nL3\nL4\n")
        r = run("read_file", {"path": "lines.txt", "offset": 1, "limit": 2}, self.ctx)
        self.assertEqual(r["content"], "L1\nL2")
        self.assertEqual(r["offset"], 1)
        self.assertEqual(r["lines_returned"], 2)
        self.assertEqual(r["total_lines"], 5)
        self.assertTrue(r["truncated"])

    def test_offset_string_coerced(self):
        self.put("lines.txt", "L0\nL1\nL2\n")
        r = run("read_file", {"path": "lines.txt", "offset": "1", "limit": "1"}, self.ctx)
        self.assertEqual(r["content"], "L1")        # defensive coercion: model may send numeric strings


class TestWriteFile(ToolBase):
    def test_write_returns_bytes(self):
        r = run("write_file", {"path": "out.txt", "content": "abc"}, self.ctx)
        self.assertEqual(r["bytes"], 3)
        self.assertEqual(slurp(os.path.join(self.ws, "out.txt")), "abc")

    def test_creates_parent_dirs(self):
        run("write_file", {"path": "deep/nested/f.txt", "content": "x"}, self.ctx)
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "deep/nested/f.txt")))

    def test_directory_collision(self):
        os.mkdir(os.path.join(self.ws, "adir"))
        with self.assertRaises(ValueError):
            run("write_file", {"path": "adir", "content": "x"}, self.ctx)

    def test_too_large_rejected(self):
        with self.assertRaises(ValueError):
            run("write_file", {"path": "big.txt", "content": "x" * 50}, make_ctx(max_bytes=10))


class TestEditFile(ToolBase):
    def test_not_a_file(self):
        with self.assertRaises(FileNotFoundError):
            run("edit_file", {"path": "nope.txt", "find": "a", "replace": "b"}, self.ctx)

    def test_empty_find(self):
        self.put("a.txt", "abc")
        with self.assertRaises(ValueError):
            run("edit_file", {"path": "a.txt", "find": "", "replace": "x"}, self.ctx)

    def test_find_not_present(self):
        self.put("a.txt", "abc")
        with self.assertRaises(ValueError) as cm:
            run("edit_file", {"path": "a.txt", "find": "zzz", "replace": "x"}, self.ctx)
        self.assertIn("not found", str(cm.exception))

    def test_non_unique_without_replace_all(self):
        self.put("a.txt", "x x x")
        with self.assertRaises(ValueError) as cm:
            run("edit_file", {"path": "a.txt", "find": "x", "replace": "y"}, self.ctx)
        msg = str(cm.exception)
        self.assertIn("matches", msg)
        self.assertIn("replace_all", msg)             # the message must tell the model how to recover

    def test_unique_replace(self):
        self.put("a.txt", "alpha beta gamma")
        r = run("edit_file", {"path": "a.txt", "find": "beta", "replace": "BETA"}, self.ctx)
        self.assertEqual(r["replacements"], 1)
        self.assertEqual(slurp(os.path.join(self.ws, "a.txt")), "alpha BETA gamma")

    def test_replace_all(self):
        self.put("a.txt", "x x x")
        r = run("edit_file", {"path": "a.txt", "find": "x", "replace": "y", "replace_all": True}, self.ctx)
        self.assertEqual(r["replacements"], 3)
        self.assertEqual(slurp(os.path.join(self.ws, "a.txt")), "y y y")

    def test_empty_replace_deletes(self):
        self.put("a.txt", "keepDROPkeep")
        run("edit_file", {"path": "a.txt", "find": "DROP", "replace": ""}, self.ctx)
        self.assertEqual(slurp(os.path.join(self.ws, "a.txt")), "keepkeep")


class TestListDir(ToolBase):
    def test_lists_files_and_dirs(self):
        self.put("f.txt", "hi")
        os.mkdir(os.path.join(self.ws, "sub"))
        entries = {e["name"]: e for e in run("list_dir", {"path": ""}, self.ctx)["entries"]}
        self.assertEqual(entries["f.txt"]["type"], "file")
        self.assertEqual(entries["f.txt"]["size"], 2)
        self.assertEqual(entries["sub"]["type"], "dir")
        self.assertIsNone(entries["sub"]["size"])

    def test_not_a_directory(self):
        self.put("f.txt", "hi")
        with self.assertRaises(FileNotFoundError):
            run("list_dir", {"path": "f.txt"}, self.ctx)


class TestSearch(ToolBase):
    def test_finds_matches(self):
        self.put("a.txt", "needle here\nnothing\nneedle again\n")
        r = run("search", {"query": "needle"}, self.ctx)
        self.assertEqual(len(r["matches"]), 2)
        self.assertEqual(r["matches"][0]["line"], 1)

    def test_literal_not_regex(self):
        self.put("a.txt", "a.c\nabc\n")
        r = run("search", {"query": "a.c"}, self.ctx)     # "a.c" is literal: must NOT match "abc"
        self.assertEqual(len(r["matches"]), 1)
        self.assertEqual(r["matches"][0]["text"], "a.c")

    def test_empty_query(self):
        with self.assertRaises(ValueError):
            run("search", {"query": "   "}, self.ctx)


class TestMkdir(ToolBase):
    def test_creates(self):
        run("mkdir", {"path": "newdir"}, self.ctx)
        self.assertTrue(os.path.isdir(os.path.join(self.ws, "newdir")))

    def test_file_collision(self):
        self.put("clash", "x")
        with self.assertRaises(ValueError):
            run("mkdir", {"path": "clash"}, self.ctx)


class TestDelete(ToolBase):
    def test_delete_file(self):
        self.put("f.txt", "x")
        self.assertEqual(run("delete", {"path": "f.txt"}, self.ctx)["deleted"], "file")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "f.txt")))

    def test_delete_empty_dir(self):
        os.mkdir(os.path.join(self.ws, "empty"))
        self.assertEqual(run("delete", {"path": "empty"}, self.ctx)["deleted"], "directory")

    def test_non_empty_dir_refused(self):
        os.mkdir(os.path.join(self.ws, "full"))
        self.put("full/inner.txt", "x")
        with self.assertRaises(ValueError):
            run("delete", {"path": "full"}, self.ctx)     # no recursive delete

    def test_workspace_root_refused(self):
        with self.assertRaises(PermissionError):
            run("delete", {"path": ""}, self.ctx)

    def test_missing_target(self):
        with self.assertRaises(FileNotFoundError):
            run("delete", {"path": "ghost"}, self.ctx)


class TestRegistry(unittest.TestCase):
    """Auto-discovery + the risk/validate extension + broken-folder resilience."""

    FILE_TOOLS = ["delete", "edit_file", "list_dir", "mkdir", "read_file", "search", "write_file"]
    EXEC_TOOLS = ["execute", "run_command", "list_processes", "process_output", "stop_process"]
    WEB_TOOLS = ["web_scrape", "web_search"]                     # network tools: mutating:false, validate(); scrape risk:medium

    def test_registry_has_file_tools_plus_execution(self):
        self.assertEqual(sorted(A.REGISTRY), sorted(self.FILE_TOOLS + self.EXEC_TOOLS + self.WEB_TOOLS))
        for name in self.FILE_TOOLS:
            r = A.REGISTRY[name]
            self.assertTrue(callable(r["run"]))
            self.assertEqual(r["risk"], "low")            # the file tools are all default-risk
            self.assertIsNone(r["validate"])              # none declare a validate() hook
        self.assertEqual(A.REGISTRY["execute"]["risk"], "high")     # the execution tools carry the new metadata
        self.assertTrue(callable(A.REGISTRY["execute"]["validate"]))
        self.assertTrue(callable(A.REGISTRY["run_command"]["validate"]))
        # web tools reach the internet (not the workspace): non-mutating, with a validate() guard; scrape is medium-risk
        for name in self.WEB_TOOLS:
            self.assertFalse(A.REGISTRY[name]["mutating"])
            self.assertTrue(callable(A.REGISTRY[name]["validate"]))
        self.assertEqual(A.REGISTRY["web_search"]["risk"], "low")
        self.assertEqual(A.REGISTRY["web_scrape"]["risk"], "medium")

    def test_payload_shape(self):
        p = A.tools_payload()                             # default registry, no .ds4 manifest in CWD
        self.assertEqual(len(p["tools"]), len(self.FILE_TOOLS) + len(self.EXEC_TOOLS) + len(self.WEB_TOOLS))
        self.assertEqual(sorted(p["mutating"]), ["delete", "edit_file", "execute", "mkdir", "run_command", "write_file"])
        self.assertEqual(p["risk"], {"execute": "high", "web_scrape": "medium"})   # above-default risk: execute + scrape

    def _fixture_dir(self):
        base = tempfile.mkdtemp(prefix="ds4reg_")
        self.addCleanup(shutil.rmtree, base, True)
        # a high-risk tool that declares a validate() hook
        d = os.path.join(base, "danger"); os.mkdir(d)
        with open(os.path.join(d, "spec.json"), "w") as f:
            json.dump({"function": {"name": "danger", "description": "demo",
                                    "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}}},
                       "mutating": True, "risk": "high"}, f)
        with open(os.path.join(d, "tool.py"), "w") as f:
            f.write("def validate(args):\n"
                    "    if not args.get('ok'):\n"
                    "        raise ValueError('ok required')\n\n"
                    "def run(args, ctx):\n"
                    "    return {'ran': True}\n")
        # a folder with invalid JSON -> must be skipped
        b = os.path.join(base, "broken"); os.mkdir(b)
        spit(os.path.join(b, "spec.json"), "{ this is not json")
        spit(os.path.join(b, "tool.py"), "def run(args, ctx):\n    return {}\n")
        # a folder whose tool.py lacks run() -> must be skipped
        n = os.path.join(base, "noimpl"); os.mkdir(n)
        with open(os.path.join(n, "spec.json"), "w") as f:
            json.dump({"function": {"name": "noimpl", "description": "x", "parameters": {"type": "object"}}}, f)
        spit(os.path.join(n, "tool.py"), "X = 1\n")
        return base

    def test_discovers_risk_and_validate(self):
        reg = A.load_registry(self._fixture_dir())
        self.assertIn("danger", reg)
        self.assertEqual(reg["danger"]["risk"], "high")
        self.assertTrue(reg["danger"]["mutating"])
        self.assertTrue(callable(reg["danger"]["validate"]))

    def test_validate_rejects_and_accepts(self):
        reg = A.load_registry(self._fixture_dir())
        with self.assertRaises(ValueError):
            reg["danger"]["validate"]({})                 # missing ok -> raises (dispatcher maps to 400)
        self.assertIsNone(reg["danger"]["validate"]({"ok": True}))

    def test_broken_folders_skipped(self):
        reg = A.load_registry(self._fixture_dir())
        self.assertNotIn("broken", reg)                   # invalid JSON
        self.assertNotIn("noimpl", reg)                   # no run()

    def test_payload_exposes_high_risk(self):
        p = A.tools_payload(A.load_registry(self._fixture_dir()))
        self.assertEqual(p["risk"].get("danger"), "high")
        self.assertIn("danger", p["mutating"])


class TestExecute(ToolBase):
    """The execute tool: argv/shell runs, exit codes, cwd confinement, timeout, output cap, validate guards."""

    def test_argv_success(self):
        r = run("execute", {"argv": ["python3", "-c", "print('argv ok')"]}, self.ctx)
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["stdout"], "argv ok\n")
        self.assertFalse(r["timed_out"])

    def test_shell_pipes_and_chains(self):
        r = run("execute", {"shell": True, "command": "echo a && echo b"}, self.ctx)
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["stdout"], "a\nb\n")

    def test_nonzero_exit_is_a_result_not_error(self):
        r = run("execute", {"argv": ["python3", "-c", "import sys; sys.exit(3)"]}, self.ctx)
        self.assertEqual(r["exit_code"], 3)            # surfaced as a normal result, not an exception

    def test_stderr_captured(self):
        r = run("execute", {"argv": ["python3", "-c", "import sys; sys.stderr.write('boom')"]}, self.ctx)
        self.assertIn("boom", r["stderr"])

    def test_runs_in_the_workspace(self):
        self.put("marker.txt", "x")
        r = run("execute", {"shell": True, "command": "ls"}, self.ctx)
        self.assertIn("marker.txt", r["stdout"])       # cwd defaulted to the workspace, NOT the repo/process dir

    def test_cwd_escape_blocked(self):
        with self.assertRaises(PermissionError):
            run("execute", {"argv": ["ls"], "cwd": "../.."}, self.ctx)   # safe_path confines cwd

    def test_timeout_kills_process(self):
        r = A.run_process({"argv": ["sleep", "5"]}, timeout=1)            # short timeout via run_process directly
        self.assertTrue(r["timed_out"])
        self.assertIsNone(r["exit_code"])

    def test_output_capped(self):
        orig = A.EXEC_MAX_OUTPUT
        A.EXEC_MAX_OUTPUT = 16
        try:
            r = A.run_process({"argv": ["python3", "-c", "print('z' * 1000)"]})
            self.assertTrue(r["truncated"])
            self.assertIn("output truncated", r["stdout"])
        finally:
            A.EXEC_MAX_OUTPUT = orig

    def test_validate_requires_a_command_form(self):
        v = A.REGISTRY["execute"]["validate"]
        with self.assertRaises(ValueError): v({})                         # neither argv nor shell
        with self.assertRaises(ValueError): v({"argv": "notalist"})       # argv must be an array
        with self.assertRaises(ValueError): v({"argv": []})               # ... a non-empty one
        with self.assertRaises(ValueError): v({"shell": True})            # shell needs a command string
        self.assertIsNone(v({"argv": ["echo", "hi"]}))
        self.assertIsNone(v({"shell": True, "command": "echo hi"}))


class TestRunCommand(ToolBase):
    """run_command: resolves pre-vetted .ds4/commands.json entries; refuses unknown names / missing manifest."""

    def _manifest(self, obj):
        os.makedirs(os.path.join(self.ws, ".ds4"), exist_ok=True)
        spit(os.path.join(self.ws, ".ds4", "commands.json"), json.dumps(obj))

    def test_runs_argv_command(self):
        self._manifest({"commands": {"hello": {"argv": ["python3", "-c", "print(1)"]}}})
        r = run("run_command", {"name": "hello"}, self.ctx)
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["stdout"], "1\n")
        self.assertEqual(r["name"], "hello")

    def test_runs_shell_command(self):
        self._manifest({"commands": {"sh": {"shell": "echo hi"}}})
        r = run("run_command", {"name": "sh"}, self.ctx)
        self.assertEqual(r["stdout"], "hi\n")

    def test_unknown_name_lists_available(self):
        self._manifest({"commands": {"test": {"argv": ["true"]}, "lint": {"argv": ["true"]}}})
        with self.assertRaises(ValueError) as cm:
            run("run_command", {"name": "nope"}, self.ctx)
        msg = str(cm.exception)
        self.assertIn("unknown command", msg)
        self.assertIn("lint", msg); self.assertIn("test", msg)            # actionable: shows what IS available

    def test_no_manifest(self):
        with self.assertRaises(ValueError) as cm:
            run("run_command", {"name": "test"}, self.ctx)
        self.assertIn(".ds4/commands.json", str(cm.exception))

    def test_read_commands_ignores_bad_json(self):
        os.makedirs(os.path.join(self.ws, ".ds4"))
        spit(os.path.join(self.ws, ".ds4", "commands.json"), "{ not json")
        self.assertEqual(A.read_commands(), {})                           # tolerant: invalid manifest -> no commands

    def test_payload_surfaces_commands(self):
        self._manifest({"commands": {"test": {"argv": ["pytest"], "description": "run tests"}}})
        p = A.tools_payload()
        names = [c["name"] for c in p["commands"]]
        self.assertIn("test", names)
        rc = next(t for t in p["tools"] if t["function"]["name"] == "run_command")
        self.assertIn("Available: test", rc["function"]["description"])   # description augmented live


class TestAuditFixes(ToolBase):
    """Regression tests for the best-practices audit fixes (truncation signals, foot-guns, flags)."""

    def test_read_full_remainder_not_truncated(self):
        self.put("f.txt", "L0\nL1\nL2\nL3\nL4\n")
        r = run("read_file", {"path": "f.txt", "offset": 3}, self.ctx)   # lines 3-4 = the whole tail
        self.assertEqual(r["content"], "L3\nL4")
        self.assertFalse(r["truncated"])                                 # nothing beyond `end` was omitted

    def test_read_actually_truncated(self):
        self.put("f.txt", "L0\nL1\nL2\nL3\nL4\n")
        r = run("read_file", {"path": "f.txt", "offset": 1, "limit": 2}, self.ctx)
        self.assertTrue(r["truncated"])                                  # lines 3-4 omitted

    def test_read_full_has_uniform_shape(self):
        self.put("f.txt", "a\nb\nc\n")
        r = run("read_file", {"path": "f.txt"}, self.ctx)
        self.assertEqual(r["total_lines"], 3)
        self.assertFalse(r["truncated"])

    def test_list_dir_truncation_flag(self):
        for i in range(5):
            self.put("f%d.txt" % i, "x")
        full = run("list_dir", {"path": ""}, self.ctx)
        self.assertFalse(full["truncated"]); self.assertEqual(full["total"], 5)
        clipped = run("list_dir", {"path": ""}, make_ctx(max_entries=2))
        self.assertTrue(clipped["truncated"])
        self.assertEqual(len(clipped["entries"]), 2)
        self.assertEqual(clipped["total"], 5)

    def test_search_reports_scanned_not_truncated(self):
        self.put("a.txt", "needle\n")
        r = run("search", {"query": "needle"}, self.ctx)
        self.assertFalse(r["truncated"])
        self.assertEqual(r["scanned"], 1)                                # every inspected file counts toward the budget

    def test_write_requires_content(self):
        with self.assertRaises(ValueError):                             # missing content -> loud, never a silent empty write
            run("write_file", {"path": "x.txt"}, self.ctx)

    def test_write_empty_string_is_ok(self):
        r = run("write_file", {"path": "e.txt", "content": ""}, self.ctx)
        self.assertEqual(r["bytes"], 0)
        self.assertEqual(slurp(os.path.join(self.ws, "e.txt")), "")

    def test_write_requires_path(self):
        with self.assertRaises(ValueError):
            run("write_file", {"path": "", "content": "x"}, self.ctx)

    def test_write_created_flag(self):
        self.assertTrue(run("write_file", {"path": "n.txt", "content": "a"}, self.ctx)["created"])
        self.assertFalse(run("write_file", {"path": "n.txt", "content": "b"}, self.ctx)["created"])  # overwrite

    def test_mkdir_created_flag(self):
        self.assertTrue(run("mkdir", {"path": "d"}, self.ctx)["created"])
        self.assertFalse(run("mkdir", {"path": "d"}, self.ctx)["created"])   # already existed -> no-op

    def test_mkdir_requires_path(self):
        with self.assertRaises(ValueError):
            run("mkdir", {"path": ""}, self.ctx)

    def test_execute_rejects_both_forms(self):
        with self.assertRaises(ValueError):                             # argv AND shell both set -> ambiguous, refused
            A.REGISTRY["execute"]["validate"]({"argv": ["echo", "hi"], "shell": True, "command": "echo hi"})


# ---------------------------------------------------------------------------------------------------------------
# Web tools (web_search / web_scrape). DEP-FREE: the network/extractor libs (ddgs, primp, trafilatura) are
# monkeypatched or faked, so this runs under plain `python3` with no venv. As elsewhere, the point is the FAILURE
# modes: SSRF refusal, that BM25 actually narrows, byte caps + paging, the untrusted-content label, and search
# dedupe/normalize.
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _web_common as _wc


class TestWebSSRF(unittest.TestCase):
    def test_validate_url_blocks(self):
        for u in ["http://127.0.0.1/", "http://10.0.0.1/x", "http://169.254.169.254/latest/meta-data/",
                  "http://localhost:8080/", "https://192.168.1.1/", "http://host.internal/",
                  "file:///etc/passwd", "ftp://h/x", ""]:
            with self.assertRaises(ValueError, msg="should block %r" % u):
                _wc.validate_url(u)

    def test_scrape_run_refuses_metadata_endpoint(self):
        res = run("web_scrape", {"url": "http://169.254.169.254/latest/meta-data/"}, make_ctx())
        self.assertIn("non-public", res.get("error", ""))


class TestWebBM25(unittest.TestCase):
    def setUp(self):
        pad = " filler clause to push this paragraph past the chunk-merge threshold so chunks stay distinct." * 3
        self.text = "\n\n".join([
            "Cooking pasta: boil water, add salt, stir occasionally." + pad,
            "The capital of France is Paris on the river Seine, home of the Eiffel Tower." + pad,
            "Quarterly tax filing deadlines, invoices, and accounting spreadsheets." + pad,
        ])

    def test_keeps_relevant_drops_irrelevant(self):
        out = _wc.bm25_filter(self.text, "capital of France Paris Seine", max_chars=400)
        self.assertIn("Paris", out)
        self.assertNotIn("tax", out)
        self.assertNotIn("pasta", out)

    def test_empty_query_returns_head(self):
        self.assertTrue(_wc.bm25_filter(self.text, "", 30).startswith("Cooking"))


class TestWebScrapeWindowing(unittest.TestCase):
    """web_scrape.run with fetch/extract/cache monkeypatched: cap, offset paging, truncated, untrusted label, and
    metadata passthrough — no network, no trafilatura."""
    def setUp(self):
        self._save = {k: getattr(_wc, k) for k in ("fetch", "extract_markdown", "cache_get", "cache_set", "bm25_filter")}
        full = "alpha bravo charlie delta echo foxtrot. " * 200          # ~8000 chars
        _wc.fetch = lambda url, **k: {"final_url": url, "status": 200, "content_type": "text/html",
                                      "html": "<html>x</html>", "bytes": 9, "truncated": False}
        _wc.extract_markdown = lambda html, url=None: (full, "Demo Title")
        _wc.cache_get = lambda *a, **k: None
        _wc.cache_set = lambda *a, **k: None

    def tearDown(self):
        for k, v in self._save.items():
            setattr(_wc, k, v)

    def test_cap_label_metadata(self):
        r = run("web_scrape", {"url": "https://example.com/a", "max_chars": 600}, make_ctx())
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["title"], "Demo Title")
        self.assertEqual(r["final_url"], "https://example.com/a")
        self.assertTrue(r["content"].startswith("[untrusted web content"))
        self.assertEqual(r["chars"], 600)                 # 600 >= the tool's 500-char floor, so not clamped
        self.assertTrue(r["truncated"])

    def test_offset_paging(self):
        r = run("web_scrape", {"url": "https://example.com/b", "max_chars": 600, "offset": 600}, make_ctx())
        self.assertEqual(r["chars"], 600)
        self.assertTrue(r["truncated"])

    def test_query_invokes_bm25(self):
        _wc.bm25_filter = lambda text, q, mc, **k: "NARROWED:" + q
        r = run("web_scrape", {"url": "https://example.com/c", "query": "needle"}, make_ctx())
        self.assertIn("NARROWED:needle", r["content"])

    def test_no_query_truncated_note(self):                  # nudge the model to pass a query when it scraped blind
        r = run("web_scrape", {"url": "https://example.com/d", "max_chars": 600}, make_ctx())   # no query; content >> 600
        self.assertTrue(r["truncated"])
        self.assertIn("query=", r.get("note", ""))


class TestWebSearchDedup(unittest.TestCase):
    """Inject a fake `ddgs` module so dedupe-by-URL + normalize + cap are tested without the real library/network."""
    def setUp(self):
        fake = types.ModuleType("ddgs")

        class _DDGS:
            def text(self, query, **k):
                return [
                    {"title": "A",     "href": "https://x.com/1", "body": "first"},
                    {"title": "A dup", "href": "https://x.com/1", "body": "dup"},      # duplicate url -> dropped
                    {"title": "B",     "url":  "https://x.com/2", "body": "second"},   # 'url' key variant accepted
                    {"title": "C",     "href": "",                "body": "no url"},   # empty url -> dropped
                ]
        fake.DDGS = _DDGS
        self._saved_mod = sys.modules.get("ddgs")
        sys.modules["ddgs"] = fake
        self._save_cache = (_wc.cache_get, _wc.cache_set)
        _wc.cache_get = lambda *a, **k: None
        _wc.cache_set = lambda *a, **k: None

    def tearDown(self):
        if self._saved_mod is not None:
            sys.modules["ddgs"] = self._saved_mod
        else:
            sys.modules.pop("ddgs", None)
        _wc.cache_get, _wc.cache_set = self._save_cache

    def test_dedupe_and_normalize(self):
        res = run("web_search", {"query": "anything", "max_results": 5}, make_ctx())
        self.assertEqual(res["count"], 2)
        self.assertEqual([r["url"] for r in res["results"]], ["https://x.com/1", "https://x.com/2"])
        self.assertTrue(all(set(r) == {"title", "url", "snippet"} for r in res["results"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
