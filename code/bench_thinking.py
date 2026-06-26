#!/usr/bin/env python3
"""
bench_thinking.py — does the "Auto" thinking-mode actually save time, or is it net-detrimental?

WHAT THIS DECIDES
  The Thinking switch (Thinking / Off / Auto) has an "Auto" position: a local heuristic turns
  DeepSeek-V4's thinking on/off PER TURN — skip the <think> block on trivial turns, keep it for
  hard ones. Skipping thinking is only worth shipping if the time it SAVES on trivial turns
  outweighs the time it COSTS to switch modes (and if it doesn't under-think real work). This
  script measures those two time terms directly against a live ds4-server so the keep/drop call
  is based on numbers, not vibes.

THE TWO THINGS WE MEASURE
  Test A — per-turn savings (the UPSIDE):
    Send the same prompts both ways (thinking ON vs OFF). `think - skip` is what Auto saves by
    skipping. LOOK FOR a big, consistent saving on the TRIVIAL prompts: their answer is tiny, so
    the whole <think> block is pure overhead — that's the case Auto targets and where the win is.
    On moderate/hard prompts the *answer itself* dominates the time, so skipping saves little
    there anyway — which is fine, because the heuristic keeps thinking ON for those.

  Test B — the switch penalty (the feared DOWNSIDE):
    ds4 reuses its KV cache keyed by the SHA1 of the rendered prompt prefix. Toggling thinking
    mode changes how the prompt renders, which COULD invalidate that prefix and force a cold
    re-prefill of the whole conversation every time Auto flips. We grow one conversation two ways
    — thinking HELD on every turn vs ALTERNATING on/off every turn — and compare per-turn TTFT.
    LOOK FOR a TTFT spike on the alternating run's flipped turns. If alt ~= hold (no spike),
    switching is cheap and Auto is safe. If alt spikes, frequent flipping pays repeated cold
    re-prefills that can erase the Test A savings (the app limits this with escalate-only /
    hysteresis, but the raw cost shows up here).

HOW TO READ IT  (keep the feature if BOTH hold)
  1. Test A trivial rows: skipping is clearly faster (e.g. 2-4x).   -> the upside is real.
  2. Test B: alt TTFT ~= hold TTFT, no spike on the flipped turns.  -> switching is cheap.
  If Test A shows little saving even on trivial turns, or Test B shows large alt spikes, the
  feature isn't paying for itself on this box.

WHY THE NUMBERS ARE NOISY (read before trusting any single run)
  With --ssd-streaming, ds4 fetches ~1.7 GiB of routed-expert weights PER TOKEN, so decode speed
  swings from ~3.7 t/s cold to ~10 t/s warm (a 2.7x range) depending on expert-cache warmth. That
  variance can dwarf the thinking signal on longer prompts (in the first run, one "skip" turn took
  171s purely from a cold cache + a long answer). Mitigations here: an untimed warmup per case
  (--warmup, default on) and median-of-N (--repeats). Even so, treat single numbers as directional.

  The slow box (AMD 780M iGPU) is where this matters most and pulls BOTH ways: prefill is slower
  there (so a switch penalty, if any, is BIGGER) but decode is also slower (so the thinking saving
  is BIGGER too). They push in opposite directions, so RE-RUN THIS ON THE 780M before deciding for
  that box — the CUDA result doesn't transfer.

USAGE
  # 1) get a ds4-server listening on :8080  (./ds4Service, or ds4's run.sh-style launch)
  # 2) python3 code/bench_thinking.py [--repeats 3] [--max-tokens 300] [--no-warmup]
"""
import argparse, json, statistics, time, urllib.request

PROMPTS = [
    ("trivial", "say hi"),
    ("trivial", "rename the variable cfg to config"),
    ("trivial", "list three primary colors"),
    ("moderate", "reverse the words in a sentence but keep each word's letters in order"),
    ("moderate", "why might a retry loop spin forever, and what's the fix?"),
    ("moderate", "explain what a KV cache is, in one short paragraph"),
]
TURNS = ["Summarize what a mutex is.", "Now give an example in C.", "What bug could it cause?",
         "How would you debug that?", "Suggest a safer alternative."]


def chat(args, messages, disabled, max_tokens):
    """One streaming completion. Returns timing + token counts. `disabled` -> thinking:{type:disabled}."""
    body = {"model": args.model, "stream": True, "stream_options": {"include_usage": True},
            "messages": messages, "max_tokens": max_tokens}
    if disabled:
        body["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(args.url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time(); ttft = None; content = ""; reasoning = ""; usage = None
    resp = urllib.request.urlopen(req, timeout=600)
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        d = line[5:].strip()
        if d == "[DONE]":
            break
        try:
            j = json.loads(d)
        except ValueError:
            continue
        if j.get("usage"):
            usage = j["usage"]
        delta = ((j.get("choices") or [{}])[0]).get("delta") or {}
        if delta.get("reasoning_content"):
            if ttft is None:
                ttft = time.time() - t0
            reasoning += delta["reasoning_content"]
        if delta.get("content"):
            if ttft is None:
                ttft = time.time() - t0
            content += delta["content"]
    total = time.time() - t0
    return {"ttft": ttft or total, "total": total, "reason_tok": round(len(reasoning) / 4),
            "answer": content}   # `content` excludes the <think> block, matching what the UI sends back


def timed(args, messages, disabled):
    """Median total/ttft over --repeats, after an optional untimed warmup (stabilizes expert-cache warmth)."""
    if args.warmup:
        chat(args, messages, disabled, args.max_tokens)
    runs = [chat(args, messages, disabled, args.max_tokens) for _ in range(args.repeats)]
    return {"total": statistics.median(r["total"] for r in runs),
            "ttft": statistics.median(r["ttft"] for r in runs),
            "reason_tok": runs[-1]["reason_tok"]}


def main():
    ap = argparse.ArgumentParser(description="Measure whether Auto thinking-mode saves time on this box.")
    ap.add_argument("--url", default="http://127.0.0.1:8080/v1/chat/completions")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--repeats", type=int, default=3, help="timed runs per case; reported value is the median")
    ap.add_argument("--max-tokens", type=int, default=300, help="cap per reply so a cold run can't run away")
    ap.add_argument("--no-warmup", dest="warmup", action="store_false", help="skip the untimed warmup call per case")
    ap.set_defaults(warmup=True)
    args = ap.parse_args()
    print("server=%s  repeats=%d  max_tokens=%d  warmup=%s\n" % (args.url, args.repeats, args.max_tokens, args.warmup), flush=True)

    print("=== TEST A: thinking ON vs OFF, per prompt (median of %d) ===" % args.repeats)
    print("  -> LOOK FOR a big skip<<think saving on the TRIVIAL rows; that's the win Auto captures.")
    print("%-9s %-46s %8s %8s %8s %8s %6s" % ("kind", "prompt", "skip_s", "think_s", "saved_s", "thinkTok", "x"))
    ssum = tsum = 0.0
    for kind, p in PROMPTS:
        s = timed(args, [{"role": "user", "content": p}], True)
        t = timed(args, [{"role": "user", "content": p}], False)
        ssum += s["total"]; tsum += t["total"]
        x = (t["total"] / s["total"]) if s["total"] else 0
        print("%-9s %-46s %8.2f %8.2f %8.2f %8d %5.1fx" % (kind, p[:46], s["total"], t["total"], t["total"] - s["total"], t["reason_tok"], x))
    print("%-9s %-46s %8.2f %8.2f %8.2f   (ignore the total if a row is a streaming-warmth outlier)" % ("TOTAL", "", ssum, tsum, tsum - ssum))

    print("\n=== TEST B: switch penalty (TTFT on a growing conversation) ===")
    print("  hold = thinking always ON;  alt = thinking flips every turn.")
    print("  -> LOOK FOR alt's TTFT spiking above hold on the flipped turns = a cold re-prefill from switching.")
    print("%-5s %10s %10s %10s" % ("turn", "hold_ttft", "alt_ttft", "alt_mode"))
    hold_msgs, alt_msgs, mt = [], [], max(160, args.max_tokens)
    for i, u in enumerate(TURNS):
        hold_msgs.append({"role": "user", "content": u})
        h = chat(args, hold_msgs, False, mt)
        hold_msgs.append({"role": "assistant", "content": h["answer"]})
        alt_msgs.append({"role": "user", "content": u})
        off = (i % 2 == 1)                               # flip every turn -> forces a prefix (mode) change
        a = chat(args, alt_msgs, off, mt)
        alt_msgs.append({"role": "assistant", "content": a["answer"]})
        print("%-5d %10.2f %10.2f %10s" % (i + 1, h["ttft"], a["ttft"], "OFF" if off else "ON"))
    print("\ndone")


if __name__ == "__main__":
    main()
