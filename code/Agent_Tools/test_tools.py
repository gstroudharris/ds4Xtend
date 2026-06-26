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
                                 MAX_BYTES=max_bytes, MAX_ENTRIES=max_entries)


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

    def test_real_registry_has_seven_file_tools(self):
        self.assertEqual(sorted(A.REGISTRY), ["delete", "edit_file", "list_dir", "mkdir", "read_file", "search", "write_file"])
        for name, r in A.REGISTRY.items():
            self.assertTrue(callable(r["run"]))
            self.assertEqual(r["risk"], "low")            # the file tools are all default-risk
            self.assertIsNone(r["validate"])              # none declare a validate() hook

    def test_payload_shape(self):
        p = A.tools_payload()
        self.assertEqual(len(p["tools"]), 7)
        self.assertEqual(sorted(p["mutating"]), ["delete", "edit_file", "mkdir", "write_file"])
        self.assertEqual(p["risk"], {})                   # nothing above default among the file tools

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
