#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""Multi-turn finish_run eval — resolves what single-shot eval_tooluse.py cannot.

Single-shot, the model preferred a verify-first move (list_dir) over finish_run when the work was already done,
so finish_done scored as a MISS. But that's the model's look-before-leap bias — it almost never picks a TERMINAL
action on the first move. The open question: given a turn to actually verify, does it THEN finish? And conversely,
when the work ISN'T done yet, does it correctly do the work first and not finish just because it READ "finish"?

This drives a real multi-turn loop: it EXECUTES the model's intermediate tool calls in-process against a throwaway
fixture — via agent_tools' own REGISTRY + CTX (the same code the backend runs) — and feeds results back until the
model calls finish_run, answers in prose, or hits the turn cap. It reuses eval_tooluse's SYSTEM + tools_payload +
load_finish_run, so it grades the SAME contract the frontend ships.

Each fixture is built so the WORLD matches the prompt's premise — otherwise verification would reveal a false
premise and confound the finish-judgment we're measuring.

Run (ds4-server up):  python3 eval_finish_multiturn.py --repeats 3 --max-turns 5
"""
import argparse, json, os, shutil, sys, urllib.request
import agent_tools as A
import eval_tooluse as E   # reuse: load_system, load_finish_run, commands_note, build_fixture


def ask_full(server, model, tools, messages, timeout=200, max_tokens=400):
    """One non-streaming turn. Returns the raw assistant message dict (content + tool_calls)."""
    body = json.dumps({"model": model, "stream": False, "tool_choice": "auto", "tools": tools,
                       "max_tokens": max_tokens, "messages": messages}).encode()
    req = urllib.request.Request(server.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return (data.get("choices") or [{}])[0].get("message") or {}


def exec_inproc(name, args):
    """Run a registered tool in-process against the locked fixture (the backend's own REGISTRY + CTX).
    finish_run is client-only (absent from the registry) and is handled by the caller, never here."""
    tool = A.REGISTRY.get(name)
    if not tool:
        return {"error": "unknown tool: %s" % name}
    try:
        if tool["validate"]:
            tool["validate"](args)
        return tool["run"](args, A.CTX)
    except Exception as e:                       # mirror the backend: a tool error is a normal result the model sees
        return {"error": str(e)}


def short(obj, n=70):
    s = obj if isinstance(obj, str) else json.dumps(obj)
    return s if len(s) <= n else s[:n] + "…"


def run_scenario(server, model, tools, system, prompt, max_turns):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    trace = []
    for turn in range(1, max_turns + 1):
        try:
            msg = ask_full(server, model, tools, messages)
        except Exception as e:
            trace.append(("ERROR", str(e)[:60]))
            return {"outcome": "error", "turn": turn, "trace": trace}
        calls = msg.get("tool_calls") or []
        am = {"role": "assistant", "content": msg.get("content") or ""}   # echo the turn back into history
        if calls:
            am["tool_calls"] = calls
        messages.append(am)
        if not calls:
            trace.append(("prose", (msg.get("content") or "")[:60]))
            return {"outcome": "prose", "turn": turn, "trace": trace}
        for c in calls:
            fn = c.get("function") or {}
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            if name == "finish_run":
                trace.append(("finish_run", short(args.get("summary", ""))))
                return {"outcome": "finished", "turn": turn, "trace": trace}
            res = exec_inproc(name, args)
            trace.append((name, short(args)))
            messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": json.dumps(res)})
    return {"outcome": "max_turns", "turn": max_turns, "trace": trace}


# fixtures: build the world to match each prompt's premise
def fixture_done():
    ws = E.build_fixture()
    open(os.path.join(ws, "hello.txt"), "w").write("hi\n")    # premise: hello.txt already exists with the right content
    return ws


def fixture_todo():
    return E.build_fixture()                                  # config.py has parse_args() but NOT parse_config() yet


SCENARIOS = [
    ("finish_when_done", fixture_done,
     "Earlier you were asked to create hello.txt containing exactly the text 'hi'. That work is already done. "
     "Verify the current state, and if the task is complete, finish the run.",
     "SHOULD finish (after verifying)"),
    ("finish_after_work", fixture_todo,
     "Task spec: 'Implement a function parse_config() in config.py, then finish the run.' "
     "parse_config does not exist yet. Do the task.",
     "should NOT finish before parse_config exists; then finish"),
]


def has_parse_config(ws):
    try:
        return "parse_config" in open(os.path.join(ws, "config.py")).read()
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:8080")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--max-turns", type=int, default=5)
    args = ap.parse_args()

    try:
        urllib.request.urlopen(args.server.rstrip("/") + "/v1/models", timeout=4)
    except Exception as e:
        print("ds4-server not reachable at %s (%s) — start the model and re-run." % (args.server, e))
        return 2

    fin = E.load_finish_run()
    base_system = E.load_system()
    print("multi-turn finish eval — %d scenarios x %d repeats, max %d turns/run, vs %s\n"
          % (len(SCENARIOS), args.repeats, args.max_turns, args.model))

    for sid, mkfix, prompt, expect in SCENARIOS:
        print("### %s — %s" % (sid, expect))
        good = 0
        for rep in range(args.repeats):
            ws = mkfix()
            A.set_workspace(ws)
            tools = list(A.tools_payload()["tools"]) + [fin]          # run_command desc reflects this fixture's commands
            system = base_system + E.commands_note(
                [{"name": n, "description": s.get("description", "")} for n, s in A.read_commands().items()])
            try:
                r = run_scenario(args.server, args.model, tools, system, prompt, args.max_turns)
            finally:
                pc = has_parse_config(ws)
                shutil.rmtree(ws, ignore_errors=True)
            steps = " -> ".join(t[0] for t in r["trace"]) or "(none)"
            if sid == "finish_when_done":
                ok = r["outcome"] == "finished"
                good += ok
                verdict = "✓ FINISHED @turn %d" % r["turn"] if ok else "✗ did NOT finish (%s)" % r["outcome"]
            else:
                fi = next((i for i, t in enumerate(r["trace"]) if t[0] == "finish_run"), None)
                wi = next((i for i, t in enumerate(r["trace"]) if t[0] in ("write_file", "edit_file")), None)
                if fi is not None and (wi is None or fi < wi):
                    verdict = "✗ PREMATURE finish (before writing parse_config)"
                elif r["outcome"] == "finished":
                    good += 1; verdict = "✓ did work then FINISHED @turn %d (parse_config present=%s)" % (r["turn"], pc)
                else:
                    good += 1; verdict = "✓ no premature finish; %s (parse_config present=%s)" % (r["outcome"], pc)
            print("  rep%d: [%s]\n         %s" % (rep + 1, steps, verdict))
        print("  => %d/%d as-expected\n" % (good, args.repeats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
