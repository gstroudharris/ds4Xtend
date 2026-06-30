#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""Tool-use eval — does the MODEL know WHEN to use a tool and HOW (right tool, right args)?

This is fundamentally different from test_tools.py / test_jobs.py. Those test the PLUMBING (when the model
emits a tool call, does it execute correctly). This tests the model's JUDGMENT: given a real task + our real
tool catalog + SYSTEM prompt, does ds4 (a) call a tool when it should, (b) pick the RIGHT one, (c) build VALID
args, and (d) NOT call a tool when it shouldn't. The plumbing tests can never tell you this, because they stub
the model's decision. Only a live eval against the real model can.

What it measures (per scenario, averaged over --repeats):
  - selection    : did the model call an acceptable tool (or correctly call NOTHING for a no-tool task)?
  - args         : were the arguments valid JSON, schema-required fields present, and the expected target hit?
  - over-calling : how often it reached for a tool on a no-tool task (a real failure mode for eager models)
  - under-calling: how often it answered in prose when it should have acted

Run (with ds4-server up):   python3 eval_tooluse.py
                            python3 eval_tooluse.py --server http://localhost:8080 --repeats 5
The harness loads the SAME tools.py SYSTEM + tools_payload() catalog the frontend sends, over a throwaway
fixture workspace — so it grades the real contract, not a copy. ds4-server must be reachable; nothing is
executed (we only inspect the model's chosen tool_calls), so the eval is read-only and safe.
"""
import argparse, json, os, re, shutil, sys, tempfile, urllib.request
import agent_tools as A

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------- load the EXACT contract the model sees ----------
def load_system():
    """Extract the SYSTEM prompt from tools.js (the string-concatenation literal) so it never drifts from prod."""
    js = open(os.path.join(HERE, "tools.js"), encoding="utf-8").read()
    m = re.search(r"SYSTEM:\s*(.*?)\n\s*//|SYSTEM:\s*(.*?),\n\s*\n", js, re.S)
    block = (m.group(1) or m.group(2)) if m else ""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    return "".join(p.encode().decode("unicode_escape") for p in parts)


def _js_concat_string(block):
    """Join a JS  "a" + "b" + ...  string-concatenation literal into one str, honoring escapes AND
    preserving UTF-8 (e.g. an em dash) — unlike load_system's unicode_escape path, which is ASCII-only."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    return "".join(json.loads('"' + p + '"') for p in parts)


def load_finish_run():
    """Build the client-only finish_run def from the REAL description in tools.js, so the eval grades the
    contract that actually ships (mirrors load_system; the backend payload omits this client-only tool)."""
    js = open(os.path.join(HERE, "tools.js"), encoding="utf-8").read()
    m = re.search(r'name:\s*"finish_run".*?description:\s*(.*?)\n\s*parameters:', js, re.S)
    desc = _js_concat_string(m.group(1)) if m else ""
    if not desc:
        raise SystemExit("eval: could not extract finish_run description from tools.js (CONTROL_TOOLS changed?)")
    return {
        "type": "function",
        "function": {
            "name": "finish_run",
            "description": desc,
            "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
        },
    }


def commands_note(commands):
    if not commands:
        return ""
    return ("\n\nThis project declares these commands (run with run_command): "
            + "; ".join(c["name"] + (" — " + c["description"] if c.get("description") else "") for c in commands) + ".")


def build_fixture():
    ws = tempfile.mkdtemp(prefix="ds4eval_")
    os.makedirs(os.path.join(ws, "src"))
    os.makedirs(os.path.join(ws, ".ds4"))
    open(os.path.join(ws, "config.py"), "w").write("DEBUG = False\n\ndef parse_args():\n    return {}\n")
    open(os.path.join(ws, "src", "app.py"), "w").write("def main():\n    pass\n")
    open(os.path.join(ws, "src", "utils.py"), "w").write("# TODO: refactor\n")
    open(os.path.join(ws, "notes.md"), "w").write("Please recieve the file.\n# TODO: write docs\n")
    open(os.path.join(ws, "temp.txt"), "w").write("scratch\n")
    json.dump({"commands": {"test": {"argv": ["pytest", "-q"], "description": "run the test suite"},
                            "lint": {"shell": "ruff check .", "description": "lint the code"}}},
              open(os.path.join(ws, ".ds4", "commands.json"), "w"))
    return ws


# ---------- scenarios: (id, prompt, expect) ----------
# expect = None  -> the model should answer directly, calling NO tool
# expect = {"tools":[ideal...], "prep":[acceptable preparatory...], "args":{param: substring}}
#   `tools`  = the ideal terminal tool(s) for the task.
#   `prep`   = legitimate FIRST moves our SYSTEM prompt invites (read/search/list before edit/write/stop, or
#              list_processes before stop_process). Single-shot, these are CORRECT — so we score them as an
#              acceptable first move (not the ideal, but on the path to it), separate from a genuine wrong pick.
# finish_run is a CLIENT-ONLY tool (absent from the backend tools_payload) — the frontend appends it before
# sending, so the eval must too. Its def is pulled from tools.js at runtime (load_finish_run) to avoid drift.
SCENARIOS = [
    ("read_config",     "What does config.py contain?",                                  {"tools": ["read_file"], "args": {"path": "config"}}),
    ("read_range",      "Show me just the first 15 lines of config.py.",                 {"tools": ["read_file"], "args": {"path": "config"}}),
    ("search_def",      "Where is the function parse_args defined in this project?",     {"tools": ["search"], "args": {"query": "parse_args"}}),
    ("search_todo",     "Which files mention TODO?",                                     {"tools": ["search"], "args": {"query": "TODO"}}),
    ("list_src",        "List the files in the src folder.",                             {"tools": ["list_dir"], "args": {"path": "src"}}),
    ("write_new",       "Create a file hello.txt containing the text hi.",              {"tools": ["write_file"], "prep": ["list_dir", "read_file"]}),
    ("write_nested",    "Create src/utils/new.py with a hello() function.",            {"tools": ["write_file"], "prep": ["list_dir", "read_file"]}),
    ("edit_flag",       "In config.py, change DEBUG = False to DEBUG = True.",          {"tools": ["edit_file"], "prep": ["read_file", "search"], "args": {"path": "config"}}),
    ("edit_typo",       "Fix the typo 'recieve' in notes.md; it should be 'receive'.",  {"tools": ["edit_file"], "prep": ["read_file", "search"], "args": {"path": "notes"}}),
    ("overwrite_full",  "Replace the ENTIRE contents of notes.md with one line: TODO.", {"tools": ["write_file", "edit_file"], "prep": ["read_file"], "args": {"path": "notes"}}),
    ("mkdir_build",     "Create a directory named build.",                              {"tools": ["mkdir"], "args": {"path": "build"}}),
    ("delete_temp",     "Delete the file temp.txt.",                                    {"tools": ["delete"], "prep": ["list_dir", "read_file"], "args": {"path": "temp"}}),
    ("run_tests",       "Run the project's tests.",                                     {"tools": ["run_command", "execute"]}),
    ("run_lint",        "Run the lint command.",                                        {"tools": ["run_command", "execute"]}),
    ("exec_adhoc",      "Run the shell command: echo hello.",                          {"tools": ["execute"]}),
    ("bg_server",       "Start the dev server in the background so I can hit it on port 8000.", {"tools": ["execute"], "prep": ["list_dir", "read_file", "search"]}),
    ("check_proc",      "Is the background server you started still running?",          {"tools": ["list_processes", "process_output"]}),
    ("stop_proc",       "Stop the background server.",                                  {"tools": ["stop_process"], "prep": ["list_processes", "process_output"]}),
    # finish_run JUDGMENT: call it only when the work is genuinely done, not just because an instruction mentions finishing.
    ("finish_done",     "You have already created hello.txt with the required content and the tests you were asked to run all passed. If the task is complete, finish the run.", {"tools": ["finish_run"]}),
    ("finish_premature","The task spec says: 'After you implement parse_config() in config.py and its tests pass, finish the run.' parse_config does not exist yet and no tests have run. Do the right next thing.", {"tools": ["write_file", "edit_file"], "prep": ["read_file", "search", "list_dir"]}),
    # web tools: route "find/look up online" -> web_search, "read/open <url>" -> web_scrape (offered via the live registry)
    ("web_find",        "Find the official trafilatura documentation online.",          {"tools": ["web_search"], "args": {"query": "trafilatura"}}),
    ("web_read_url",    "Open https://example.com/article and summarize what it says.", {"tools": ["web_scrape"], "args": {"url": "example.com"}}),
    ("no_tool_explain", "Explain how a hash map works in two sentences.",              None),
    ("no_tool_thanks",  "Thanks, that's really helpful!",                              None),
    ("no_tool_opinion", "In your opinion, are tabs or spaces better?",                 None),
    ("no_tool_def",     "What does the term 'idempotent' mean?",                       None),
]


# ---------- model query + grading ----------
def ask(server, model, system, tools, prompt, timeout=200):
    # max_tokens caps a no-tool prose answer so it can't time out at streaming speed; 256 is ample for any of
    # our tool calls (their args are small), so it never truncates a real tool call.
    body = json.dumps({"model": model, "stream": False, "tool_choice": "auto", "tools": tools, "max_tokens": 256,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(server.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    calls = msg.get("tool_calls") or []
    if calls:
        fn = calls[0].get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = None                       # invalid JSON args = a real arg failure
        return {"tool": fn.get("name"), "args": args}
    return {"tool": None, "args": None, "content": (msg.get("content") or "")[:120]}


def grade(expect, got):
    """Returns: error / no_tool / tool. For tool tasks, 'ideal' = the terminal tool; 'acceptable' = ideal OR a
    legitimate preparatory first move (read/search/list before edit/write/stop)."""
    if got["tool"] and str(got["tool"]).startswith("ERROR"):
        return {"kind": "error", "ideal": False, "acceptable": False, "args": False}
    if expect is None:                        # no-tool task: correct iff it called NOTHING
        ok = got["tool"] is None
        return {"kind": "no_tool", "ideal": ok, "acceptable": ok, "args": ok}
    ideal = got["tool"] in expect["tools"]
    acceptable = ideal or got["tool"] in (expect.get("prep") or [])
    args_ok = acceptable and got["args"] is not None
    if args_ok:
        for k, sub in (expect.get("args") or {}).items():
            v = got["args"].get(k)
            if v is None or sub.lower() not in str(v).lower():
                args_ok = False
                break
    return {"kind": "tool", "ideal": ideal, "acceptable": acceptable, "args": args_ok}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:8080")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--repeats", type=int, default=3, help="runs per scenario (model is stochastic)")
    args = ap.parse_args()

    ws = build_fixture()
    try:
        A.set_workspace(ws)
        payload = A.tools_payload()
        tools, commands = list(payload["tools"]) + [load_finish_run()], payload["commands"]   # append the client-only finish_run (real def from tools.js)
        system = load_system() + commands_note(commands)
        if not system.strip():
            print("WARNING: could not extract SYSTEM from tools.js", file=sys.stderr)
        try:
            urllib.request.urlopen(args.server.rstrip("/") + "/v1/models", timeout=4)
        except Exception as e:
            print("ds4-server not reachable at %s (%s) — start the model and re-run." % (args.server, e))
            return 2

        print("tool-use eval — %d scenarios x %d repeats vs %s\n" % (len(SCENARIOS), args.repeats, args.model))
        rows, ideal_hits, acc_hits, arg_hits, n_tool, n_notool = [], 0, 0, 0, 0, 0
        overcalls = undercalls = errors = 0
        for sid, prompt, expect in SCENARIOS:
            i_ok = ac_ok = a_ok = 0
            last = {}
            for _ in range(args.repeats):
                try:
                    got = ask(args.server, args.model, system, tools, prompt)
                except Exception as e:
                    got = {"tool": "ERROR:" + str(e)[:40], "args": None}
                g = grade(expect, got); last = got
                if g["kind"] == "error":
                    errors += 1; continue                       # a harness/model error is NOT a tool-choice failure
                i_ok += g["ideal"]; ac_ok += g["acceptable"]; a_ok += g["args"]
                if expect is None and got["tool"] is not None:
                    overcalls += 1
                if expect is not None and got["tool"] is None:
                    undercalls += 1
            n = max(1, args.repeats)
            ideal_hits += i_ok / n; acc_hits += ac_ok / n; arg_hits += a_ok / n
            if expect is None: n_notool += 1
            else: n_tool += 1
            want = "(none)" if expect is None else "/".join(expect["tools"])
            rows.append((sid, want, str(last.get("tool")), ac_ok / n, i_ok / n))

        w = max(len(r[0]) for r in rows)
        print("%-*s  %-22s  %-16s  ideal  ok(+prep)" % (w, "scenario", "ideal tool", "got(last)"))
        for sid, want, got, acc, idl in rows:
            mark = "OK  " if idl == 1 else ("PREP" if acc > 0 else "MISS")
            print("%-*s  %-22s  %-16s  %3d%%   %3d%%   %s" % (w, sid, want, got, round(idl * 100), round(acc * 100), mark))
        print("\n  ideal-tool rate    : %5.1f%%   (picked the terminal tool / correct no-tool)" % (100 * ideal_hits / len(SCENARIOS)))
        print("  acceptable-move    : %5.1f%%   (ideal OR a correct preparatory step — read/list before edit, etc.)" % (100 * acc_hits / len(SCENARIOS)))
        print("  arg validity       : %5.1f%%   (valid JSON + hit the expected target)" % (100 * arg_hits / len(SCENARIOS)))
        print("  over-calling       : %d/%d no-tool runs wrongly reached for a tool" % (overcalls, n_notool * args.repeats))
        print("  under-calling      : %d/%d tool runs answered in prose instead" % (undercalls, n_tool * args.repeats))
        if errors:
            print("  (errors/timeouts   : %d run(s) — excluded from the rates above)" % errors)
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
