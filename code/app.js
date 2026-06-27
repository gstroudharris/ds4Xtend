/* ============================================================
   DS4 Web Frontend — app logic
   Phase 1: shell/theme · 2: chat streaming · 3: telemetry · 4: agent (sandboxed file tools)
   ============================================================ */
(() => {
  "use strict";
  const C = window.DS4_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const setText = (id, v) => { const e = $(id); if (e) { e.textContent = v; e.classList.remove("is-empty"); } };

  /* ---------------- header / cards / toggle ---------------- */
  if (C.hardware) $("brandSub").textContent = `${C.quant} · ${C.hardware}`;

  const conversationEl = document.querySelector(".conversation");
  const messagesEl = $("messages");
  const emptyState = $("emptyState");
  const agentPanel = $("agentPanel");
  const agentMessagesEl = $("agentMessages");
  let mode = "chat";
  let workspace = null, agentMode = "ask", agentMsgs = [], agentBusy = false, agentAbort = null, pendingApproval = null, agentRunId = 0;

  (C.suggestions || []).forEach((s) => {
    const el = document.createElement("button");
    el.className = "card";
    el.innerHTML = `<div class="card__tag">${esc(s.tag)}</div><div class="card__text">${esc(s.text)}</div>`;
    el.addEventListener("click", () => { input.value = s.text; autosize(input); send(); });
    $("suggestionCards").appendChild(el);
  });

  const toggle = $("modeToggle");
  toggle.querySelectorAll(".segmented__btn").forEach((btn) =>
    btn.addEventListener("click", () => { mode = btn.dataset.mode; try { localStorage.setItem("ds4:mode", mode); } catch (e) {} updateView(); if (mode === "agent") syncWorkspace(); }));
  function updateView() {
    const agent = mode === "agent";
    toggle.classList.toggle("is-agent", agent);
    toggle.querySelectorAll(".segmented__btn").forEach((b) => b.classList.toggle("is-active", b.dataset.mode === mode));
    agentPanel.hidden = !agent;
    $("leftRail").hidden = !agent;            // working-tree rail is agent-only
    $("agentModeToggle").hidden = !agent;     // Ask/Auto is agent-only (now sits next to Thinking)
    $("thinkChip").hidden = agent;            // Chat: Thinking is a plain chip toggle
    $("thinkSwitch").hidden = !agent;         // Agent: Thinking is the on/off/auto switch
    $("sendLoopToggle").hidden = !agent;      // Send/Loop is agent-only too
    messagesEl.hidden = agent;
    emptyState.hidden = agent || messages.length > 0;
    const ae = $("agentEmpty"); if (ae) ae.hidden = agentMsgs.length > 0;
    input.placeholder = agent
      ? (workspace ? "Ask the agent to read or edit files in the folder…" : "Choose a folder for the agent first…")
      : "Message DS4… (Enter to send, Shift+Enter for newline)";
    setSendStop();   // reflect Send/Loop on mode switch (loop is agent-only)
    reflectThink();  // Chat shows the on/off toggle; Agent shows on/off/auto
  }

  /* ---------------- collapsible side rails ---------------- */
  const railResyncs = [];
  function wireRail(railId, btnId, arrows, lsKey) {
    const rail = $(railId), btn = $(btnId);
    if (!rail || !btn) return;
    const mq = window.matchMedia("(max-width: 1100px)");   // stacked top/bottom → vertical arrows; side-by-side → horizontal
    let collapsed = false; try { collapsed = localStorage.getItem(lsKey) === "1"; } catch (e) {}
    const render = () => {
      rail.classList.toggle("is-collapsed", collapsed);
      const v = mq.matches;
      btn.textContent = collapsed ? (v ? arrows.ve : arrows.he) : (v ? arrows.vc : arrows.hc);
      btn.title = collapsed ? "Expand panel" : "Collapse panel";
    };
    render();
    btn.addEventListener("click", () => { collapsed = !collapsed; render(); try { localStorage.setItem(lsKey, collapsed ? "1" : "0"); } catch (e) {} });
    if (mq.addEventListener) mq.addEventListener("change", render); else mq.addListener(render);
    railResyncs.push(() => { try { collapsed = localStorage.getItem(lsKey) === "1"; } catch (e) {} render(); });   // re-apply after loadState seeds localStorage
  }
  wireRail("leftRail", "leftRailToggle", { hc: "«", he: "»", vc: "▲", ve: "▼" }, "ds4:lrail");
  wireRail("rightRail", "rightRailToggle", { hc: "»", he: "«", vc: "▼", ve: "▲" }, "ds4:rrail");

  /* ---------------- composer ---------------- */
  const input = $("input");
  const composerEl = document.querySelector(".composer");
  let thinkMode = (C.thinkDefault === "on" || C.thinkDefault === "off") ? C.thinkDefault : "auto";   // AGENT: "on" | "off" | "auto"
  let chatThink = "on";                                      // CHAT: "on" | "off" only — a plain toggle, no auto heuristic
  let agentThink = { level: "on", reason: "", auto: false }; // auto: per-run agent decision (escalate-only within a run)
  const thinkSwitch = $("thinkSwitch"), thinkChip = $("thinkChip");
  function reflectThink() {                                  // Chat = a Thinking chip (on/off); Agent = the on/off/auto switch
    thinkChip.classList.toggle("is-on", chatThink === "on");
    thinkSwitch.querySelectorAll(".thinksw__btn").forEach((b) => b.classList.toggle("is-active", b.dataset.tm === thinkMode));
  }
  thinkChip.addEventListener("click", () => {                // CHAT thinking: a plain on/off toggle, like Following
    if (looping) return;
    chatThink = chatThink === "on" ? "off" : "on";
    try { localStorage.setItem("ds4:chatThink", chatThink); } catch (e) {}
    reflectThink();
  });
  thinkSwitch.querySelectorAll(".thinksw__btn").forEach((b) => b.addEventListener("click", () => {   // AGENT thinking: on/off/auto
    if (looping) return;                                     // unselectable mid-loop, like the Chat/Agent toggle
    thinkMode = b.dataset.tm;
    try { localStorage.setItem("ds4:thinking", thinkMode); } catch (e) {}
    reflectThink();
  }));
  reflectThink();
  /* ---- auto-thinking heuristic: local, zero round-trip; default-ON, skip only confident-trivial turns ---- */
  function rateDifficulty(text) {
    const t = String(text || "").trim(), low = t.toLowerCase(), words = t ? t.split(/\s+/).length : 0;
    if (/\b(no|nope|wrong|incorrect|try again|redo|that'?s not|you (missed|forgot)|actually|instead|still (broken|failing|wrong))\b/.test(low)) return { level: "on", reason: "follow-up fix" };
    if (/```|(^|[\s("'`])[\w./-]+\.(js|ts|jsx|tsx|py|c|h|cpp|hpp|go|rs|java|rb|php|css|html|json|sh|sql|md|ya?ml|toml)\b/.test(t)) return { level: "on", reason: "code / files" };
    if ((C.thinkOnWords || []).some((w) => low.includes(w))) return { level: "on", reason: "reasoning cue" };
    if (/[=∑√∫×÷]|\b\d+\s*[-+*/^%]\s*\d+\b/.test(t)) return { level: "on", reason: "math" };
    if ((t.match(/\?/g) || []).length >= 2 || /\b(and then|after that|also,|first,|next,|finally,|step \d)\b/.test(low)) return { level: "on", reason: "multi-step" };
    if (words <= (C.thinkSkipMaxWords || 14) && (C.thinkOffWords || []).some((w) => low === w || low.startsWith(w + " ") || low.includes(" " + w))) return { level: "off", reason: "trivial" };
    if (words <= (C.thinkShortWords || 6)) return { level: "off", reason: "very short" };
    return { level: "on", reason: "default" };               // default-ON floor — under-thinking is the costly, unrecoverable error
  }
  function thinkSpread(level) { return level === "off" ? { thinking: { type: "disabled" } } : {}; }
  function logDifficulty(rec) {
    try { const k = "ds4:diffLog", a = JSON.parse(localStorage.getItem(k) || "[]"); a.push(rec); while (a.length > (C.diffLogMax || 200)) a.shift(); localStorage.setItem(k, JSON.stringify(a)); } catch (e) {}
  }
  input.addEventListener("input", () => autosize(input));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (streaming || agentBusy || looping) return; if (loopMode && mode === "agent") startLoop(); else send(); } });
  $("sendBtn").addEventListener("click", () => { if (streaming || agentBusy || looping) return stopLoop(); if (loopMode && mode === "agent") return startLoop(); send(); });
  $("clearBtn").addEventListener("click", () => {
    if (mode === "agent") {                       // Clear only the active mode's conversation
      if (agentBusy) { stopAgent(); agentBusy = false; setSendStop(false); }   // unblock a pending approval + the loop
      agentRunId++;                               // invalidate any in-flight run so it can't write back over the cleared state
      resetLog("agent"); agentMsgs = []; agentMessagesEl.innerHTML = ""; saveAgent(); $("agentEmpty").hidden = false;
    } else {
      if (streaming) stop();
      resetLog("chat"); messages = []; messagesEl.innerHTML = ""; resetTurnMetrics(); saveChat();
    }
    lastStateJson = "";   // bust saveState's change-dedup so the purge always fires
    saveState(false);     // immediately mirror the cleared snapshot to the sidecar /state (don't wait for the 3s timer) so a reload can't resurrect the conversation
    updateView(); input.focus();
  });
  function autosize(t) { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }

  /* ---------------- chat ---------------- */
  let messages = [];          // API history [{role, content}]
  let streaming = false;
  let loopMode = false, looping = false, loopPrompt = "", loopErrored = false;   // Send/Loop toggle
  let abortCtrl = null;

  // --- transient backend-error retry (e.g. ROCm "prefill state reset failed") ---
  // A single backend 5xx/429 must not kill a looping run. Both the chat and agent fetch loops retry the SAME
  // request a few times with abort-aware exponential backoff before surfacing the error. Genuine 4xx (client
  // errors) and user-Stop (AbortError) are NEVER retried. Shared by runStream() and agentStreamTurn().
  const TRANSIENT_TRIES = C.transientRetries != null ? C.transientRetries : 3;
  function isTransientErr(status, txt) {
    return status >= 500 || status === 429 || /state reset|prefill|backend/i.test(txt || "");
  }
  function backoffSleep(tries, signal) {                 // abort-aware: Stop interrupts the wait immediately
    const base = C.transientBackoffMs || 400, cap = C.transientBackoffCapMs || 2000;
    const ms = Math.min(cap, base * Math.pow(2, tries - 1));
    return new Promise((resolve, reject) => {
      if (signal && signal.aborted) return reject(new DOMException("aborted", "AbortError"));
      const t = setTimeout(() => { if (signal) signal.removeEventListener("abort", onAbort); resolve(); }, ms);
      function onAbort() { clearTimeout(t); reject(new DOMException("aborted", "AbortError")); }
      if (signal) signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  let stick = true, rendering = false;
  const stickToggle = $("stickToggle");
  function reflectStick() { stickToggle.classList.toggle("is-on", stick); stickToggle.textContent = stick ? "↓ Following" : "↓ Follow"; }
  function saveStick() { try { localStorage.setItem("ds4:stick", stick ? "1" : "0"); } catch (e) {} }
  function scrollDown(force) { if (rendering) return; if (force || stick) conversationEl.scrollTop = conversationEl.scrollHeight; }
  conversationEl.addEventListener("scroll", () => {
    const atBottom = conversationEl.scrollHeight - conversationEl.scrollTop - conversationEl.clientHeight < 48;
    if (atBottom !== stick) { stick = atBottom; reflectStick(); saveStick(); }   // auto on/off as the user scrolls
  });
  stickToggle.addEventListener("click", () => { stick = !stick; reflectStick(); saveStick(); if (stick) conversationEl.scrollTop = conversationEl.scrollHeight; });
  reflectStick();

  function appendUser(text) {
    const el = document.createElement("div");
    el.className = "msg msg--user"; el.textContent = text;
    messagesEl.appendChild(el); scrollDown(true);
  }

  // typewriter: smoothly reveal streamed text char-by-char (monitor-fps via rAF) with a leading cursor
  function makeTyper(textEl, cursorEl, onFrame) {
    let target = "", shown = 0, raf = 0, dead = false;
    function paint() {
      raf = 0;
      if (dead || shown >= target.length) return;
      const remaining = target.length - shown;
      shown = Math.min(target.length, shown + Math.max(0.5, Math.min(remaining * 0.08, 12)));   // gentle fractional ease — smooth even cadence; a small steady lag is fine
      textEl.textContent = target.slice(0, Math.floor(shown));
      if (onFrame) onFrame();
      schedule();
    }
    function schedule() { if (!raf && !dead) raf = requestAnimationFrame(paint); }
    return {
      feed(full) { target = full || ""; if (shown > target.length) { shown = target.length; textEl.textContent = target; } schedule(); },
      flush() { shown = target.length; textEl.textContent = target; if (onFrame) onFrame(); },
      finish() { dead = true; if (raf) cancelAnimationFrame(raf); textEl.textContent = target; },
    };
  }

  function appendAssistant() {
    const root = document.createElement("div"); root.className = "msg msg--assistant";
    const think = document.createElement("details"); think.className = "think"; think.open = true; think.hidden = true;
    think.innerHTML = '<summary>Thinking</summary><div class="think__body"></div>';
    const thinkBody = think.querySelector(".think__body");
    const thinkText = document.createElement("span"); const thinkCur = document.createElement("span"); thinkCur.className = "cursor"; thinkCur.hidden = true;
    thinkBody.append(thinkText, thinkCur);
    const body = document.createElement("div"); body.className = "md is-streaming";
    const bodyText = document.createElement("span"); const bodyCur = document.createElement("span"); bodyCur.className = "cursor"; bodyCur.hidden = false;   // visible immediately → "working" cursor while awaiting first token
    body.append(bodyText, bodyCur);
    const meta = document.createElement("div"); meta.className = "msg__meta"; meta.hidden = true;
    root.append(think, body, meta);
    messagesEl.appendChild(root); scrollDown(true);
    const tT = makeTyper(thinkText, thinkCur, scrollDown), bT = makeTyper(bodyText, bodyCur, scrollDown);
    return {
      thinking(t) { think.hidden = false; bodyCur.hidden = true; thinkCur.hidden = false; tT.feed(t); },
      stream(t) { thinkCur.hidden = true; tT.flush(); bodyCur.hidden = false; bT.feed(t); },
      finalize(content) { tT.finish(); bT.finish(); thinkCur.remove(); bodyCur.remove(); body.classList.remove("is-streaming"); body.innerHTML = mdRender(content); addCopy(body); think.open = false; },
      meta(s, dec) { meta.hidden = false; meta.textContent = ""; meta.appendChild(document.createTextNode(s)); if (dec && dec.auto) { const k = document.createElement("span"); k.className = "msg__think"; k.textContent = " · auto · " + (dec.level === "off" ? "skipped" : "think") + " (" + dec.reason + ")"; meta.appendChild(k); } },
      notice(text, full) { const n = document.createElement("div"); n.className = "msg__warn" + (full ? " msg__warn--full" : ""); n.textContent = "⚠ " + text; root.appendChild(n); scrollDown(); },
      error(msg) { tT.finish(); bT.finish(); thinkCur.remove(); bodyCur.remove(); root.classList.add("msg--error"); body.classList.remove("is-streaming"); body.textContent = "⚠ " + msg; },
    };
  }

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    if (mode === "agent") { if (agentBusy) return; input.value = ""; autosize(input); return runAgent(text); }
    if (streaming) return;
    input.value = ""; autosize(input);
    await chatOnce(text);
  }
  function stop() { if (abortCtrl) abortCtrl.abort(); }

  async function runStream() {
    streaming = true; setSendStop(true);
    const ui = appendAssistant();
    const t0 = performance.now();
    let tFirst = null, content = "", reasoning = "", usage = null, finishReason = null;
    let outTokEst = 0;
    const estPrompt = estimateTokens(messages);
    beginLiveMetrics();
    const liveTimer = setInterval(() => renderLiveMetrics({ t0, tFirst, outTokEst, estPrompt }), 150);
    abortCtrl = new AbortController();
    const think = { level: chatThink, reason: chatThink, auto: false };   // Chat: manual on/off toggle (no auto heuristic)
    try {
      let res, level = 0, tries = 0, sentMsgs = messages;
      for (;;) {
        sentMsgs = fitForSend(null, messages, { level: level, protectFirst: false });   // trim to fit --ctx
        const mt = maxTokensFor(sumTok(sentMsgs));
        res = await fetch(C.serverUrl + "/v1/chat/completions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: abortCtrl.signal,
          body: JSON.stringify({
            model: C.model, stream: true, stream_options: { include_usage: true },
            messages: sentMsgs, ...thinkSpread(think.level),
            ...(mt ? { max_tokens: mt } : {}),
          }),
        });
        if (res.ok) break;
        const errTxt = await res.text();
        if (res.status === 400 && /context|too long|exceed|maximum/i.test(errTxt) && level < 2) { learnCtxFromError(errTxt); level++; continue; }
        if (isTransientErr(res.status, errTxt) && tries < TRANSIENT_TRIES) {   // transient backend blip (e.g. ROCm reset) — retry the same request, don't fail the turn
          tries++; toast("Backend hiccup — retrying… (" + tries + "/" + TRANSIENT_TRIES + ")");
          await backoffSleep(tries, abortCtrl.signal); continue;
        }
        throw new Error("HTTP " + res.status + " - " + errTxt.slice(0, 180));
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n"); buf = lines.pop();
        for (const line of lines) {
          const s = line.trim();
          if (!s.startsWith("data:")) continue;
          const data = s.slice(5).trim();
          if (data === "[DONE]") continue;
          let j; try { j = JSON.parse(data); } catch { continue; }
          if (j.usage) usage = j.usage;
          const fr = j.choices && j.choices[0] && j.choices[0].finish_reason; if (fr) finishReason = fr;
          const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
          if (d.reasoning_content) { if (tFirst === null) tFirst = performance.now(); reasoning += d.reasoning_content; outTokEst++; ui.thinking(reasoning); }
          if (d.content) { if (tFirst === null) tFirst = performance.now(); content += d.content; outTokEst++; ui.stream(content); }
        }
      }
      // Fallback: some builds embed thinking inline as <think>…</think> in content.
      if (!reasoning) {
        const m = content.match(/^\s*<think>([\s\S]*?)<\/think>\s*([\s\S]*)$/);
        if (m) { reasoning = m[1].trim(); content = m[2]; ui.thinking(reasoning); }
      }
      ui.finalize(content);
      messages.push({ role: "assistant", content });
      const tEnd = performance.now();
      const usedTok = finalizeTurnMetrics({ t0, tFirst, tEnd, usage, outTokEst, estPrompt });
      if (usage && usage.prompt_tokens) calibrateTokenizer(charsOf(sentMsgs), usage.prompt_tokens);   // learn this box's real chars/token
      if (usage) {
        const decSecs = (tEnd - (tFirst == null ? tEnd : tFirst)) / 1000;
        const tps = usage.completion_tokens && decSecs > 0 ? (usage.completion_tokens / decSecs).toFixed(1) : "?";
        ui.meta(`${tps} t/s · ${usage.completion_tokens == null ? "?" : usage.completion_tokens} tokens · ${((tEnd - t0) / 1000).toFixed(1)} s`, think);
      }
      logDifficulty({ ts: Date.now(), mode: "chat", level: think.level, reason: think.reason, auto: think.auto, promptTok: usage && usage.prompt_tokens, completionTok: usage && usage.completion_tokens, reasoningTok: Math.round(reasoning.length / 4), finishReason });
      if (finishReason === "length") noticeTruncation(ui, usedTok);
    } catch (e) {
      if (e.name === "AbortError") { ui.finalize(content || "_(stopped)_"); finalizeTurnMetrics({ t0, tFirst, tEnd: performance.now(), usage, outTokEst, estPrompt }); }
      else { ui.error(String(e.message || e)); loopErrored = true; }
    } finally {
      clearInterval(liveTimer);
      streaming = false; setSendStop(false); abortCtrl = null;
      saveChat(); logFinishTurn("chat");
    }
  }

  function setSendStop() {                 // reflects Send / Loop / Stop + locks the composer while looping
    const b = $("sendBtn");
    const running = streaming || agentBusy || looping;
    const loopActive = loopMode && mode === "agent";   // Send/Loop is agent-only
    b.textContent = running ? "Stop" : (loopActive ? "Loop" : "Send");
    b.classList.toggle("btn--stop", running);
    b.classList.toggle("btn--loop", !running && loopActive);
    b.classList.toggle("btn--primary", !running && !loopActive);
    input.disabled = looping;
    composerEl.classList.toggle("is-locked", looping);
    toggle.style.pointerEvents = looping ? "none" : "";   // can't switch Chat/Agent mid-loop
    toggle.style.opacity = looping ? "0.5" : "";
    thinkSwitch.classList.toggle("is-locked", looping);   // can't change thinking mode mid-loop either
  }

  /* ---------------- Send / Loop toggle + loop driver ---------------- */
  const slToggle = $("sendLoopToggle");
  try { loopMode = localStorage.getItem("ds4:loop") === "1"; } catch (e) {}
  function reflectSL() {
    slToggle.classList.toggle("is-loop", loopMode);
    slToggle.querySelectorAll(".seg2__btn").forEach((x) => x.classList.toggle("is-active", (x.dataset.sl === "loop") === loopMode));
    setSendStop();
  }
  slToggle.querySelectorAll(".seg2__btn").forEach((b) => b.addEventListener("click", () => {
    if (looping) return;                   // can't switch mode mid-loop
    loopMode = b.dataset.sl === "loop";
    try { localStorage.setItem("ds4:loop", loopMode ? "1" : "0"); } catch (e) {}
    reflectSL();
  }));
  function stopLoop() { looping = false; if (abortCtrl) abortCtrl.abort(); stopAgent(); }   // stopAgent also unblocks a pending approval, so Stop works mid-prompt
  async function chatOnce(text) { appendUser(text); messages.push({ role: "user", content: text }); updateView(); await runStream(); }
  async function startLoop() {
    const text = input.value.trim();
    if (!text || streaming || agentBusy || looping) return;
    const loopAgent = mode === "agent";    // lock the loop to the mode it started in
    if (loopAgent && !workspace) { $("agentPick").click(); toast("Lock a folder first, then start the loop."); return; }
    loopPrompt = text;
    looping = true; setSendStop();
    try {
      while (looping) {
        loopErrored = false;
        if (loopAgent) await runAgent(loopPrompt); else await chatOnce(loopPrompt);
        if (!looping) break;               // user pressed Stop
        if (loopErrored) { toast("Loop stopped — the last run errored."); break; }
        await new Promise((r) => setTimeout(r, 350));   // brief breath between iterations
      }
    } finally {
      looping = false; setSendStop();
    }
  }
  reflectSL();

  /* ---------------- per-turn metrics (live while streaming → exact at end) ---------------- */
  const TURN_IDS = ["mTtft", "mTotal", "mPrefill", "mDecode", "mPrompt", "mOutput"];
  function setTile(id, v, live) { const e = $(id); if (!e) return; e.textContent = v; e.classList.remove("is-empty"); e.classList.toggle("is-live", !!live); }

  // rough token estimate from text (~4 chars/token) for live figures before exact usage arrives
  function estimateTokens(msgs) { let c = 0; for (const m of msgs) c += (m.content || "").length; return Math.max(1, Math.round(c / 4)); }

  function beginLiveMetrics() {
    setTile("mTtft", "…", true); setTile("mTotal", "0.0 s", true);
    setTile("mPrefill", "…", true); setTile("mDecode", "…", true);
    setTile("mPrompt", "—", true); setTile("mOutput", "~0", true);
  }
  function renderLiveMetrics({ t0, tFirst, outTokEst, estPrompt }) {
    const now = performance.now();
    setTile("mTotal", ((now - t0) / 1000).toFixed(1) + " s", true);
    setTile("mPrompt", "~" + estPrompt, true);
    setTile("mOutput", "~" + outTokEst, true);
    if (tFirst != null) {
      const ttft = (tFirst - t0) / 1000;
      setTile("mTtft", ttft.toFixed(2) + " s", true);
      if (ttft > 0) setTile("mPrefill", "~" + Math.round(estPrompt / ttft) + " t/s", true);
      const dsec = (now - tFirst) / 1000;
      if (dsec > 0.25) setTile("mDecode", "~" + (outTokEst / dsec).toFixed(1) + " t/s", true);
    }
    updateContextMeter(estPrompt + outTokEst, true);
  }
  function finalizeTurnMetrics({ t0, tFirst, tEnd, usage, outTokEst, estPrompt }) {
    const ttft = tFirst != null ? (tFirst - t0) / 1000 : null;
    const total = (tEnd - t0) / 1000;
    const pt = (usage && usage.prompt_tokens != null) ? usage.prompt_tokens : estPrompt;
    const ot = (usage && usage.completion_tokens != null) ? usage.completion_tokens : outTokEst;
    setTile("mTtft", ttft != null ? ttft.toFixed(2) + " s" : "—", false);
    setTile("mTotal", total.toFixed(2) + " s", false);
    if (pt && ttft) lastPrefillTps = pt / ttft;            // remember this box's prefill speed to size the tool cap
    setTile("mPrefill", (pt && ttft) ? (pt / ttft).toFixed(1) + " t/s" : "—", false);
    setTile("mDecode", (ot && ttft != null && total - ttft > 0) ? (ot / (total - ttft)).toFixed(1) + " t/s" : "—", false);
    setTile("mPrompt", pt != null ? String(pt) : "—", false);
    setTile("mOutput", ot != null ? String(ot) : "—", false);
    const used = (pt || 0) + (ot || 0);
    updateContextMeter(used, false);
    return used;
  }
  function resetTurnMetrics() {
    TURN_IDS.forEach((id) => { const e = $(id); e.textContent = "—"; e.classList.add("is-empty"); e.classList.remove("is-live"); });
    lastCtxUsed = null;
    const t = $("ctxTxt"), f = $("ctxFill"), n = $("ctxNote");
    if (t) { t.textContent = "—"; f.style.width = "0%"; f.className = "bar__fill bar__fill--accent"; n.textContent = "no turns yet"; }
  }

  /* ---------------- context-window headroom ---------------- */
  let serverCtx = null;          // ds4-server --ctx, learned live from the sidecar (m.ctx)
  let serverBackend = null;      // ds4 backend label (cuda/rocm/cpu) from the sidecar, to size the tool-output cap
  let lastPrefillTps = null;     // last measured prefill throughput (t/s), so "auto" can fit this box's real speed
  let lastCtxUsed = null;        // last measured occupancy, so we can re-render if ctx arrives late
  function currentCtx() { return serverCtx || C.serverCtx || null; }
  // Cap (chars) on the tool output the MODEL sees per call — the agent's biggest prefill cost. A number in
  // config pins it; "auto" scales to this box: seed by backend, refine toward a prefill-time budget once we've
  // measured prefill t/s, clamped to [4k, 25% of the context window]. Keeps tool prefills fast on a weak iGPU.
  function toolCapChars() {
    const cfg = C.agentToolOutputChars;
    if (typeof cfg === "number" && cfg > 0) return Math.round(cfg);
    const ctxChars = (currentCtx() || 32768) * 4;
    const hi = Math.min(32000, Math.floor(ctxChars * 0.25));
    const be = String(serverBackend || "").toLowerCase();
    let seed = (be === "cuda" || be === "nvidia") ? 24000 : (be === "rocm" || be === "amd" || be === "cpu") ? 6000 : 8000;
    if (lastPrefillTps && lastPrefillTps > 0) seed = lastPrefillTps * (C.toolPrefillTargetSec || 8) * 4;   // ~4 chars/token
    return Math.max(4000, Math.min(hi, Math.round(seed)));
  }
  // Trim a tool result to the cap with head+tail + the re-read breadcrumb, applied ON INSERT so the model-visible
  // bytes stay stable across turns (preserves ds4's warm KV-prefix reuse; avoids a later stub forcing a cold re-prefill).
  function capToolOutput(res) { const s = JSON.stringify(res) || ""; const cap = toolCapChars(); return s.length <= cap ? s : stubText(s, cap); }
  // Separate, LARGER cap for the write_file/edit_file content the model echoes back (its current work product —
  // usually one recent file). Generous so typical files stay whole (fewer re-reads / less rewrite-from-stub risk),
  // bounded to ≤30% of the window so one write can't dominate. Scales per box via toolCapChars(). Pin with a number.
  function writeCapChars() {
    const cfg = C.agentWriteEchoChars;
    if (typeof cfg === "number" && cfg > 0) return Math.round(cfg);
    const hi = Math.floor((currentCtx() || 32768) * 4 * 0.30);   // one write echo never exceeds ~30% of the window
    return Math.min(hi, Math.max(12000, toolCapChars() * 3));     // ~3× the output cap, ≥12k, capped at hi
  }
  function learnCtxFromError(txt) {                          // discover the server's ctx from a 400 so trimming self-heals on any box
    const m = /context[^0-9]{0,40}([0-9]{3,})/i.exec(txt || "");
    if (m) { const n = parseInt(m[1], 10); if (n > 256 && n !== serverCtx) { serverCtx = n; if (lastCtxUsed != null) updateContextMeter(lastCtxUsed, false); } }
  }
  function fmtTok(n) { n = Math.max(0, Math.round(n || 0)); return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : String(n); }
  function updateContextMeter(used, live) {
    const txt = $("ctxTxt"), fill = $("ctxFill"), note = $("ctxNote");
    if (!txt) return;
    lastCtxUsed = used = Math.max(0, Math.round(used || 0));
    const ctx = currentCtx(), pre = live ? "~" : "";
    if (!ctx) { txt.textContent = pre + fmtTok(used) + " tok"; fill.style.width = "0%"; note.textContent = "ctx unknown — set serverCtx in config.js"; return; }
    const pct = Math.min(100, used / ctx * 100);
    const warn = (C.contextWarnPct || 0.8) * 100, danger = (C.contextDangerPct || 0.92) * 100;
    txt.textContent = pre + fmtTok(used) + " / " + fmtTok(ctx) + " · " + Math.round(pct) + "%";
    fill.style.width = pct + "%";
    fill.className = "bar__fill " + (pct >= danger ? "bar__fill--danger" : pct >= warn ? "bar__fill--warn" : "bar__fill--accent");
    note.textContent = "≈" + fmtTok(Math.max(0, ctx - used)) + " tokens left" + (pct >= danger ? " — start a fresh run soon" : "");
  }
  // Opt-in output cap (C.maxOutputTokens), always clamped to remaining context so we never start a
  // generation that would instantly overflow. Returns null (→ omit max_tokens, today's behavior) when unset.
  function maxTokensFor(estPrompt) {
    if (!C.maxOutputTokens) return null;
    let mt = C.maxOutputTokens;
    const ctx = currentCtx();
    if (ctx) mt = Math.min(mt, ctx - estPrompt - 64);
    return Math.max(16, Math.floor(mt));
  }
  // Inline notice when the server stopped at a hard limit (finish_reason === "length").
  function noticeTruncation(ui, used) {
    const ctx = currentCtx(), full = ctx ? used >= ctx * 0.98 : false;
    if (full) { ui.notice("Reply cut off — context window full. Clear to start a fresh run.", true); toast("Context window full — Clear to start a fresh run."); }
    else { ui.notice("Reply cut off at the output-length cap (max_tokens).", false); toast("Reply hit the output-length cap."); }
  }

  /* ---------------- context fitting + agent message queue (keeps long sessions under --ctx) ---------------- */
  const NL2 = String.fromCharCode(10);
  // Token estimate calibrated to the SERVER's real tokenizer: charsPerTok is learned each turn from
  // usage.prompt_tokens, so the trim budget tracks dense code (~3.3 ch/tok) instead of a fixed 4 → it
  // stops landing "just over" the window. tokFromChars() converts a char count to estimated tokens.
  let charsPerTok = 4;                                     // learned live; clamped to a sane 2.5–4.5 band
  function tokFromChars(n) { return Math.ceil((n || 0) / charsPerTok); }
  function charsOf(msgs) {                                 // chars actually sent (content + tool-call args), for calibration
    let n = 0;
    for (const m of msgs) {
      n += (m.content || "").length;
      if (m.tool_calls) for (const tc of m.tool_calls) { const f = tc.function || {}; n += (f.arguments || "").length + (f.name || "").length; }
    }
    return n;
  }
  function calibrateTokenizer(sentChars, promptTokens) {  // fold real (chars sent ÷ server prompt_tokens) into charsPerTok
    if (!promptTokens || promptTokens < 64 || !(sentChars > 0)) return;   // skip tiny/invalid samples
    const r = sentChars / promptTokens;
    if (r < 1.5 || r > 8) return;                          // skip outliers (measurement noise / template mismatch)
    charsPerTok = Math.min(4.5, Math.max(2.5, charsPerTok * 0.7 + r * 0.3));   // EWMA toward the live ratio
  }
  function tokOf(m) {
    let n = (m.content || "").length;
    if (m.tool_calls) for (const tc of m.tool_calls) { const f = tc.function || {}; n += (f.arguments || "").length + (f.name || "").length; }
    return tokFromChars(n) + 4;
  }
  function sumTok(msgs) { let n = 0; for (const m of msgs) n += tokOf(m); return n; }
  function stubText(s, keepChars) {
    if (!s || s.length <= keepChars) return s;
    const head = Math.ceil(keepChars * 0.6), tail = Math.max(0, keepChars - head);
    const cut = s.length - head - tail;
    return s.slice(0, head) + NL2 + "... [" + cut + " chars trimmed to fit the context window - re-read this file/range with read_file offset+limit to restore detail] ..." + NL2 + (tail ? s.slice(s.length - tail) : "");
  }
  // Stub the bulk fields (content/find/replace) of a write/edit tool-call's ARGS to `cap`, keeping VALID JSON.
  // Re-stringifies ONLY if something was actually trimmed, so untouched args keep their exact bytes (stable KV
  // prefix). Used on insert (generous writeCapChars) and as a fitForSend last-ditch (small cap). Never touches the
  // args used to EXECUTE the tool — only the in-context echo. Short args (reads, small writes) pass through verbatim.
  function stubCallArgs(argsStr, cap) {
    if (!argsStr || argsStr.length <= cap) return argsStr;
    let a; try { a = JSON.parse(argsStr); } catch { return argsStr; }   // unparseable -> don't risk mangling it
    let changed = false;
    for (const k of ["content", "replace", "find"]) {
      if (typeof a[k] === "string" && a[k].length > cap) { a[k] = stubText(a[k], cap); changed = true; }
    }
    return changed ? JSON.stringify(a) : argsStr;
  }
  function wireMsg(m) {
    const o = { role: m.role };
    if (m.content != null) o.content = m.content;
    if (m.tool_calls) o.tool_calls = m.tool_calls;
    if (m.tool_call_id) o.tool_call_id = m.tool_call_id;
    return o;
  }
  // Budget-fitted COPY of msgs (the full history stays in memory/display/log). level escalates on 400-retry.
  function fitForSend(systemContent, msgs, opts) {
    opts = opts || {};
    const level = opts.level || 0, protectFirst = opts.protectFirst !== false;
    const ctx = currentCtx();
    let out = msgs.map(wireMsg);
    if (!ctx) return out;                                  // ctx unknown -> can't fit; rely on the 400-retry
    const reserve = (C.contextReserveTokens || 2048) + level * 1024;
    const safety = (C.contextSafety || 0.9) - level * 0.06;
    const sysTok = tokFromChars((systemContent || "").length) + 4;
    const budget = Math.max(512, Math.floor((ctx - reserve) * safety) - sysTok - (opts.extraTok || 0));
    if (sumTok(out) <= budget) return out;
    const recentKeep = Math.max(2, (C.contextRecentKeep || 6) - level * 2);
    const tailStart = Math.max(0, out.length - recentKeep);
    const stubChars = Math.max(160, (C.contextStubChars || 800) - level * 300);
    // Pass 1 - stub OLD tool results (oldest first, outside the protected recent tail)
    for (let i = 0; i < tailStart && sumTok(out) > budget; i++) {
      if (out[i].role === "tool" && out[i].content && out[i].content.length > stubChars)
        out[i] = { role: "tool", tool_call_id: out[i].tool_call_id, content: stubText(out[i].content, stubChars) };
    }
    if (sumTok(out) <= budget) return out;
    // Pass 2 - drop oldest coherent groups (cut only at user/assistant boundaries to keep tool pairing valid),
    // keeping the optional first user message (task anchor) + a breadcrumb + the largest fitting suffix.
    const firstUser = protectFirst ? out.findIndex((m) => m.role === "user") : -1;
    const anchor = firstUser >= 0 ? [out[firstUser]] : [];
    const breadcrumb = { role: "user", content: "[earlier conversation was trimmed to fit the context window]" };
    for (let c = firstUser + 1; c < out.length; c++) {
      if (out[c].role !== "user" && out[c].role !== "assistant") continue;   // cut only at clean boundaries (tool pairing)
      const kept = anchor.concat([breadcrumb], out.slice(c));
      if (sumTok(kept) <= budget) { out = kept; break; }
    }
    if (sumTok(out) <= budget) return out;
    // Pass 3 - last-ditch: stub recent tool results too, then hard-cap any remaining giant message.
    for (let i = 0; i < out.length && sumTok(out) > budget; i++) {
      if (out[i].role === "tool" && out[i].content) out[i] = { role: "tool", tool_call_id: out[i].tool_call_id, content: stubText(out[i].content, stubChars) };
    }
    for (let i = 0; i < out.length && sumTok(out) > budget; i++) {
      if (out[i].content && out[i].content.length > 400) out[i] = Object.assign({}, out[i], { content: stubText(out[i].content, 400) });
    }
    // last-ditch: stub write/edit tool-call ARGS too — the one thing the passes above can't touch — so a pile-up of
    // recent large writes can never wedge us over --ctx. Only fires when still over budget after everything else.
    for (let i = 0; i < out.length && sumTok(out) > budget; i++) {
      if (!out[i].tool_calls) continue;
      out[i] = Object.assign({}, out[i], { tool_calls: out[i].tool_calls.map((tc) => {
        const f = tc.function || {}, capped = stubCallArgs(f.arguments || "", stubChars);
        return capped === (f.arguments || "") ? tc : Object.assign({}, tc, { function: Object.assign({}, f, { arguments: capped }) });
      }) });
    }
    return out;
  }
  // Out-of-band messages delivered to the agent at the next turn boundary (non-destructive). Foundation for
  // future human-in-the-loop: anything can call injectAgentMessage(); the loop delivers it before the next turn.
  let pendingInject = [], nudgeState = 0;
  function injectAgentMessage(content, role) { pendingInject.push({ role: role || "user", content: content }); }
  function drainPending() {
    if (!pendingInject.length) return;
    for (const m of pendingInject) { agentMsgs.push(m); agUser(m.content); }
    pendingInject = [];
  }
  function maybeNudge() {
    const ctx = currentCtx(); if (!ctx) return;
    const used = sumTok(agentMsgs) + Math.ceil(AGENT_SYSTEM.length / 4);
    const pct = used / ctx, warn = C.contextWarnPct || 0.8, danger = C.contextDangerPct || 0.92;
    if (pct >= danger && nudgeState < 2) {
      nudgeState = 2;
      injectAgentMessage("[automatic context notice] The context window is nearly full (~" + Math.round(pct * 100) + "%). Stop opening new files or running searches. Finish the current step now, then give the user a concise summary of what you did and what remains.");
    } else if (pct >= warn && nudgeState < 1) {
      nudgeState = 1;
      injectAgentMessage("[automatic context notice] You have used ~" + Math.round(pct * 100) + "% of the available context. Start wrapping up: prefer finishing over exploring, avoid large reads, and summarize soon.");
    }
  }

  /* ---------------- minimal markdown (safe %%CB<n>%% code placeholder) ---------------- */
  function mdRender(src) {
    const blocks = [];
    src = src.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_, lang, code) => { blocks.push(code); return `%%CB${blocks.length - 1}%%`; });
    src = esc(src);
    src = src.replace(/`([^`\n]+)`/g, (_, c) => `<code>${c}</code>`);
    src = src.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    src = src.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    src = src.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const lines = src.split("\n");
    let html = "", para = [], list = null;
    const flushP = () => { if (para.length) { html += "<p>" + para.join("<br>") + "</p>"; para = []; } };
    const closeL = () => { if (list) { html += "</" + list + ">"; list = null; } };
    for (const line of lines) {
      const t = line.trim(); let m;
      if (/^%%CB\d+%%$/.test(t)) { flushP(); closeL(); html += t; continue; }
      if ((m = line.match(/^(#{1,3})\s+(.*)$/))) { flushP(); closeL(); const n = m[1].length; html += `<h${n}>${m[2]}</h${n}>`; continue; }
      if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { flushP(); if (list !== "ul") { closeL(); html += "<ul>"; list = "ul"; } html += "<li>" + m[1] + "</li>"; continue; }
      if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) { flushP(); if (list !== "ol") { closeL(); html += "<ol>"; list = "ol"; } html += "<li>" + m[1] + "</li>"; continue; }
      if (t === "") { flushP(); closeL(); continue; }
      closeL(); para.push(line);
    }
    flushP(); closeL();
    return html.replace(/%%CB(\d+)%%/g, (_, i) =>
      `<div class="codewrap"><button class="copy">copy</button><pre><code>${esc(blocks[i].replace(/\n$/, ""))}</code></pre></div>`);
  }
  function addCopy(root) {
    root.querySelectorAll(".codewrap").forEach((w) => {
      const btn = w.querySelector(".copy"), code = w.querySelector("code");
      btn.addEventListener("click", () =>
        navigator.clipboard.writeText(code.textContent).then(() => { btn.textContent = "copied"; setTimeout(() => (btn.textContent = "copy"), 1200); }).catch(() => {}));
    });
  }

  /* ---------------- toast ---------------- */
  let toastTimer;
  function toast(msg) {
    const el = $("toast"); el.textContent = msg; el.classList.add("is-show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("is-show"), 2800);
  }

  /* ================= Agent mode (sandboxed file tools) ================= */
  const AG = window.DS4_AGENT || {};                          // agent tool contract — Agent_Tools/tools.js
  const AGENT_SYSTEM = AG.SYSTEM || "";
  // The tool catalog is fetched live from the backend registry by AG.load() (called in runAgent), so read
  // it through these helpers — capturing AG.TOOLS at boot would freeze an empty list before load() runs.
  const toolDefs  = () => AG.TOOLS || [];                     // function defs sent to ds4 each turn
  const toolEp    = (name) => (AG.ENDPOINTS || {})[name];     // tool name -> backend HTTP path
  const toolsChars = () => JSON.stringify(toolDefs()).length; // tool-schema bytes sent every turn — reserved in the budget so they're not uncounted headroom

  /* ----- folder picker ----- */
  const picker = $("picker"), pickerList = $("pickerList"), pickerPath = $("pickerPath"), agentWs = $("agentWs"), agentEmpty = $("agentEmpty");
  let pickerCwd = "", pickerParent = null;
  $("agentPick").addEventListener("click", () => { const lr = $("leftRail"); if (lr.classList.contains("is-collapsed")) $("leftRailToggle").click(); picker.hidden = false; browseTo(workspace || ""); });
  $("pickerCancel").addEventListener("click", () => { picker.hidden = true; });
  $("pickerUp").addEventListener("click", () => { if (pickerParent != null) browseTo(pickerParent); });
  $("pickerLock").addEventListener("click", () => lockWorkspace(pickerCwd));

  async function browseTo(path) {
    pickerList.innerHTML = '<div class="picker__empty">loading…</div>';
    try {
      const r = await fetch(C.agentUrl + "/browse?path=" + encodeURIComponent(path || ""));
      const d = await r.json();
      pickerCwd = d.path; pickerParent = d.parent; pickerPath.textContent = d.path;
      pickerList.innerHTML = "";
      if (!d.dirs || !d.dirs.length) { pickerList.innerHTML = '<div class="picker__empty">(no sub-folders here — Lock to use this one)</div>'; return; }
      d.dirs.forEach((name) => {
        const row = document.createElement("button");
        row.className = "picker__row";
        row.innerHTML = '<span class="picker__ic">📁</span><span class="picker__nm"></span>';
        row.querySelector(".picker__nm").textContent = name;
        row.addEventListener("click", () => browseTo(d.path.replace(/\/+$/, "") + "/" + name));
        pickerList.appendChild(row);
      });
    } catch {
      pickerList.innerHTML = '<div class="picker__empty">agent-tools backend not reachable (:8082)</div>';
    }
  }

  async function lockWorkspace(path) {
    if (!path) return;
    try {
      const r = await fetch(C.agentUrl + "/workspace", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "failed");
      workspace = d.root; agentWs.textContent = d.root; picker.hidden = true; try { localStorage.setItem("ds4:workspace", d.root); } catch (e) {}
      agentEmpty.textContent = "Locked to " + d.root + " — ask the agent to read or edit files here.";
      updateView(); refreshTree();
      if (typeof AG.load === "function") AG.load(C.agentUrl, true).catch(() => {});   // new project -> refresh tool contract (run_command's .ds4 commands)
    } catch (e) { toast("Couldn't lock folder: " + (e.message || e)); }
  }

  // Restore the locked folder — AGENT MODE ONLY. Never auto-load a workspace while in Chat (no file access there).
  function adoptWs(root) {
    workspace = root; agentWs.textContent = root; picker.hidden = true;
    agentEmpty.textContent = "Locked to " + root + " — ask the agent to read or edit files here.";
    refreshTree(); updateView();
  }
  async function syncWorkspace() {
    if (mode !== "agent" || workspace) return;
    let d = null;
    try { d = await (await fetch(C.agentUrl + "/healthz")).json(); }
    catch (e) { agentWs.textContent = "agent-tools offline (:8082)"; return; }
    if (d && d.workspace) return adoptWs(d.workspace);          // backend already locked (e.g. --workspace)
    let saved = null; try { saved = localStorage.getItem("ds4:workspace"); } catch (e) {}
    if (!saved) return;
    try {
      const r = await fetch(C.agentUrl + "/workspace", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: saved }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "failed");
      adoptWs(j.root);
    } catch (e) {
      try { localStorage.removeItem("ds4:workspace"); } catch (e2) {}   // folder gone — forget it
      toast("Previously-locked folder is unavailable — choose a new one.");
    }
  }

  /* ----- file-tree sidebar ----- */
  const treeList = $("fileTreeList");
  $("fileTreeRefresh").addEventListener("click", refreshTree);
  async function refreshTree() {
    if (!workspace) { treeList.innerHTML = '<div class="filetree__empty">lock a folder to see files</div>'; return; }
    try {
      const r = await fetch(C.agentUrl + "/tools/tree", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: "" }) });
      const d = await r.json();
      treeList.innerHTML = "";
      if (!d.entries || !d.entries.length) { treeList.innerHTML = '<div class="filetree__empty">(empty folder)</div>'; return; }
      d.entries.forEach((e) => {
        const row = document.createElement("div"); row.className = "ftrow ftrow--" + e.type;
        row.style.paddingLeft = (6 + (e.depth - 1) * 14) + "px";
        const ic = document.createElement("span"); ic.className = "ftrow__ic"; ic.textContent = e.type === "dir" ? "📁" : "📄";
        const nm = document.createElement("span"); nm.className = "ftrow__nm"; nm.textContent = e.name;
        row.append(ic, nm);
        if (e.type === "file") { row.title = "click to preview"; row.addEventListener("click", () => previewFile(e.path, row)); }
        treeList.appendChild(row);
      });
      if (d.truncated) { const t = document.createElement("div"); t.className = "filetree__empty"; t.textContent = "…(truncated)"; treeList.appendChild(t); }
    } catch { treeList.innerHTML = '<div class="filetree__empty">agent-tools offline</div>'; }
  }
  async function previewFile(path, row) {
    const nx = row.nextElementSibling;
    if (nx && nx.classList.contains("ftprev")) { nx.remove(); return; }
    const res = await callTool("read_file", { path });
    const pre = document.createElement("pre"); pre.className = "ftprev";
    pre.textContent = res && typeof res.content === "string" ? res.content : (res && res.error ? "error: " + res.error : JSON.stringify(res));
    row.after(pre);
  }

  /* ----- Ask / Auto toggle ----- */
  const amToggle = $("agentModeToggle");
  amToggle.querySelectorAll(".seg2__btn").forEach((b) => b.addEventListener("click", () => {
    agentMode = b.dataset.am;
    try { localStorage.setItem("ds4:agentMode", agentMode); } catch (e) {}
    amToggle.classList.toggle("is-auto", agentMode === "auto");
    amToggle.querySelectorAll(".seg2__btn").forEach((x) => x.classList.toggle("is-active", x.dataset.am === agentMode));
  }));

  function stopAgent() { if (agentAbort) agentAbort.abort(); if (pendingApproval) { const p = pendingApproval; pendingApproval = null; p.resolve(false); } }

  /* ----- agent rendering ----- */
  function agScroll(force) { scrollDown(force); }
  function scrollApprovalIntoView(el) {                       // Follow should reveal the approve buttons, even past inner scroll caps
    if (!stick) return;
    agScroll(true);
    requestAnimationFrame(() => { try { if (el) el.scrollIntoView({ block: "end" }); } catch (e) {} agScroll(true); });
  }
  function agUser(text) { const e = document.createElement("div"); e.className = "msg msg--user"; e.textContent = text; agentMessagesEl.appendChild(e); agScroll(true); }
  function agAssistant() {
    const root = document.createElement("div"); root.className = "msg msg--assistant";
    const think = document.createElement("details"); think.className = "think"; think.open = true; think.hidden = true;
    think.innerHTML = '<summary>Thinking</summary><div class="think__body"></div>';
    const thinkBody = think.querySelector(".think__body");
    const thinkText = document.createElement("span"); const thinkCur = document.createElement("span"); thinkCur.className = "cursor"; thinkCur.hidden = true;
    thinkBody.append(thinkText, thinkCur);
    const body = document.createElement("div"); body.className = "md is-streaming";
    const bodyText = document.createElement("span"); const bodyCur = document.createElement("span"); bodyCur.className = "cursor"; bodyCur.hidden = false;   // visible immediately → "working" cursor while awaiting first token
    body.append(bodyText, bodyCur);
    const meta = document.createElement("div"); meta.className = "msg__meta"; meta.hidden = true;
    root.append(think, body, meta); agentMessagesEl.appendChild(root); agScroll();
    const tT = makeTyper(thinkText, thinkCur, scrollDown), bT = makeTyper(bodyText, bodyCur, scrollDown);
    return {
      meta(s, dec) { meta.hidden = false; meta.textContent = ""; meta.appendChild(document.createTextNode(s)); if (dec && dec.auto) { const k = document.createElement("span"); k.className = "msg__think"; k.textContent = " · auto · " + (dec.level === "off" ? "skipped" : "think") + " (" + dec.reason + ")"; meta.appendChild(k); } },
      thinking(t) { think.hidden = false; bodyCur.hidden = true; thinkCur.hidden = false; tT.feed(t); },
      stream(t) { thinkCur.hidden = true; tT.flush(); bodyCur.hidden = false; bT.feed(t); },
      cursorOff() { thinkCur.hidden = true; bodyCur.hidden = true; tT.flush(); bT.flush(); },   // tool/preview owns the cursor now
      notice(text, full) { const n = document.createElement("div"); n.className = "msg__warn" + (full ? " msg__warn--full" : ""); n.textContent = "⚠ " + text; root.appendChild(n); agScroll(); },
      finalize(t) { tT.finish(); bT.finish(); thinkCur.remove(); bodyCur.remove(); body.classList.remove("is-streaming"); if (t) { body.innerHTML = mdRender(t); addCopy(body); } else { root.remove(); } think.open = false; },
    };
  }
  function agError(msg) { const e = document.createElement("div"); e.className = "msg msg--assistant msg--error"; const b = document.createElement("div"); b.className = "md"; b.textContent = "⚠ " + msg; e.appendChild(b); agentMessagesEl.appendChild(e); agScroll(); }

  function agToolCard(name, args) {
    const card = document.createElement("div"); card.className = "tcall";
    const head = document.createElement("div"); head.className = "tcall__head";
    head.innerHTML = '<span class="tcall__ic">⚙</span><span class="tcall__name"></span><span class="tcall__state">running…</span>';
    head.querySelector(".tcall__name").textContent = name + "(" + (args.path || args.query || "") + ")";
    const pre = document.createElement("pre"); pre.className = "tcall__args"; pre.textContent = JSON.stringify(args);
    const out = document.createElement("div"); out.className = "tcall__out"; out.hidden = true;
    card.append(head, pre, out); agentMessagesEl.appendChild(card); agScroll();
    const stateEl = head.querySelector(".tcall__state");
    return {
      result(res, kind) {
        stateEl.textContent = kind === "ok" ? "done" : kind;
        stateEl.className = "tcall__state tcall__state--" + (kind === "ok" ? "ok" : kind === "error" ? "err" : "skip");
        let txt;
        if (typeof res === "string") txt = res.length > 4000 ? res.slice(0, 4000) + "\n…(truncated)" : res;   // already-stubbed tool text on history re-render (capped result isn't valid JSON)
        else if (res && res.error) txt = "error: " + res.error;
        else if (res && Array.isArray(res.processes)) txt = res.processes.map((p) => p.status + " · " + p.job_id + " — " + p.goal + " (" + p.age_sec + "s, expires in " + p.expires_in_sec + "s)").join("\n") || "(no background processes)";
        else if (res && res.job_id) {                              // background process: execute bg / process_output / stop_process
          const bits = ["⚙ " + res.status + " · " + res.job_id + (res.pid ? " (pid " + res.pid + ")" : "")];
          if (res.goal) bits.push("goal: " + res.goal);
          if (res.ready !== undefined && res.ready !== null) bits.push("ready: " + res.ready);
          if (res.stdout) bits.push(res.stdout);
          if (res.stderr) bits.push("[stderr]\n" + res.stderr);
          txt = bits.join("\n"); if (txt.length > 4000) txt = txt.slice(0, 4000) + "\n…(truncated)";
        }
        else if (res && (typeof res.stdout === "string" || res.timed_out || res.exit_code !== undefined)) {   // execute / run_command
          const head = res.timed_out ? "⏱ timed out" : "exit " + (res.exit_code == null ? "?" : res.exit_code);
          const parts = [head + (res.duration_sec != null ? " · " + res.duration_sec + "s" : "") + (res.command ? " · " + res.command : "")];
          if (res.stdout) parts.push(res.stdout);
          if (res.stderr) parts.push("[stderr]\n" + res.stderr);
          txt = parts.join("\n"); if (txt.length > 4000) txt = txt.slice(0, 4000) + "\n…(truncated)";
        }
        else if (res && Array.isArray(res.entries)) txt = res.entries.map((e) => (e.type === "dir" ? "📁 " : "📄 ") + e.name).join("\n") || "(empty)";
        else if (res && Array.isArray(res.matches)) txt = (res.matches.map((m) => m.file + ":" + m.line + ": " + m.text).join("\n") || "(no matches)") + (res.truncated ? "\n…(truncated)" : "");
        else if (res && typeof res.content === "string") txt = res.content.length > 4000 ? res.content.slice(0, 4000) + "\n…(truncated)" : res.content;
        else if (res && res.bytes != null) txt = "wrote " + res.bytes + " bytes to " + res.path;
        else txt = JSON.stringify(res);
        out.hidden = false; out.textContent = txt; agScroll();
      },
      approval(path, oldContent, newContent, resolve) {
        const exists = oldContent != null && oldContent !== "";
        const box = document.createElement("div"); box.className = "approve";
        box.innerHTML =
          '<div class="approve__head">' + (exists ? "Overwrite" : "Create") + ' <b></b> <span class="approve__meta"></span></div>' +
          '<pre class="approve__body"></pre>' +
          '<div class="approve__btns"><button class="btn btn--ghost approve__no">Decline</button><button class="btn btn--primary approve__yes">Approve write</button></div>';
        box.querySelector("b").textContent = path;
        box.querySelector(".approve__meta").textContent = exists ? "(" + oldContent.length + " → " + newContent.length + " chars)" : "(" + newContent.length + " chars)";
        box.querySelector(".approve__body").textContent = newContent.length > 4000 ? newContent.slice(0, 4000) + "\n…(truncated preview)" : newContent;
        stateEl.textContent = "awaiting approval";
        out.hidden = false; out.appendChild(box); agScroll();
        box.querySelector(".approve__yes").addEventListener("click", () => { box.remove(); resolve(true); });
        box.querySelector(".approve__no").addEventListener("click", () => { box.remove(); resolve(false); });
      },
    };
  }

  async function callTool(name, args, ac) {
    const ep = toolEp(name);
    if (!ep) return { error: "unknown tool: " + name };
    // Abort the fetch on EITHER the run's Stop (ac) OR a timeout, so a wedged backend can't hang the loop and
    // Stop interrupts an in-flight tool call. (ac is optional — non-agent callers still get the timeout.)
    const ctl = new AbortController();
    const onAbort = () => ctl.abort();
    if (ac) { if (ac.signal.aborted) ctl.abort(); else ac.signal.addEventListener("abort", onAbort, { once: true }); }
    const ms = (name === "execute" || name === "run_command") ? (C.executeTimeoutMs || 130000) : (C.toolTimeoutMs || 30000);   // commands may run test suites/builds
    const timer = setTimeout(() => ctl.abort(new DOMException("timeout", "TimeoutError")), ms);
    try {
      const r = await fetch(C.agentUrl + ep, { method: "POST", headers: { "Content-Type": "application/json", "X-DS4-Run-Id": String(agentRunId) }, body: JSON.stringify(args || {}), signal: ctl.signal });   // tag spawned background jobs with the owning run
      return await r.json();
    } catch (e) {
      if (ac && ac.signal.aborted) return { error: "stopped by user" };
      if (e && (e.name === "TimeoutError" || ctl.signal.reason && ctl.signal.reason.name === "TimeoutError")) return { error: "tool '" + name + "' timed out after " + Math.round(ms / 1000) + "s" };
      return { error: "agent-tools unreachable: " + (e.message || e) };
    } finally {
      clearTimeout(timer);
      if (ac) ac.signal.removeEventListener("abort", onAbort);
    }
  }

  const isMutating = (name) => !!(AG.MUTATING || {})[name];   // tools that need approval in Ask mode — set live by AG.load()
  const riskOf     = (name) => (AG.RISK || {})[name] || "low"; // per-tool risk from spec.json (default "low")
  const isHighRisk = (name) => riskOf(name) === "high";        // high-risk -> ⚠ label + approval in Ask mode (Auto runs autonomously)
  function commandsNote() {                                     // tell the model which project commands run_command can run
    const cs = AG.COMMANDS || [];
    if (!cs.length) return "";
    return "\n\nThis project declares these commands (run with run_command): " +
      cs.map((c) => c.name + (c.description ? " — " + c.description : "")).join("; ") + ".";
  }
  function askConfirm(title, body) {
    return new Promise((resolve) => {
      const box = document.createElement("div"); box.className = "approve";
      box.innerHTML = '<div class="approve__head"></div><pre class="approve__body"></pre><div class="approve__btns"><button class="btn btn--ghost approve__no">Decline</button><button class="btn btn--primary approve__yes">Approve</button></div>';
      box.querySelector(".approve__head").textContent = title;
      const bodyEl = box.querySelector(".approve__body");
      if (body) { bodyEl.textContent = body.length > 4000 ? body.slice(0, 4000) + " …(truncated preview)" : body; } else { bodyEl.remove(); }
      agentMessagesEl.appendChild(box); scrollApprovalIntoView(box);
      const done = (v) => { pendingApproval = null; box.remove(); resolve(v); };
      pendingApproval = { resolve: done };               // so Stop/Clear can unblock this
      box.querySelector(".approve__yes").addEventListener("click", () => done(true));
      box.querySelector(".approve__no").addEventListener("click", () => done(false));
    });
  }
  async function approvalFor(name, args, ac) {
    if (name === "write_file") {
      const cur = await callTool("read_file", { path: args.path }, ac);
      const exists = cur && typeof cur.content === "string";
      return { title: (exists ? "Overwrite " : "Create ") + args.path, body: args.content || "" };
    }
    if (name === "edit_file") {
      const cur = await callTool("read_file", { path: args.path }, ac);
      const old = cur && typeof cur.content === "string" ? cur.content : "";
      const hits = args.find ? old.split(args.find).length - 1 : 0;
      const next = args.find ? old.split(args.find).join(args.replace || "") : old;
      return { title: "Edit " + args.path + " (" + hits + " replacement" + (hits === 1 ? "" : "s") + ")", body: next };
    }
    if (name === "mkdir") return { title: "Create folder " + args.path, body: "" };
    if (name === "delete") return { title: "Delete " + args.path, body: "This permanently removes it from the workspace." };
    if (name === "execute") {
      const cmd = args.shell ? (args.command || "") : (Array.isArray(args.argv) ? args.argv.join(" ") : "");
      const where = args.cwd ? " (in " + args.cwd + ")" : "";
      return { title: "Run command" + (args.shell ? " via shell" : "") + where, body: cmd || JSON.stringify(args) };
    }
    if (name === "run_command") return { title: "Run project command: " + (args.name || ""), body: "Pre-vetted command from .ds4/commands.json" };
    return { title: name, body: JSON.stringify(args) };
  }
  async function execToolCall(name, args, card, ac) {
    // Approvals are gated by MODE: in Ask, mutating + high-risk tools need a human OK; in Auto the agent runs
    // autonomously — no approvals, including execute. The high-risk flag still drives the ⚠ label in Ask mode.
    if ((isHighRisk(name) || isMutating(name)) && agentMode === "ask") {
      let ok;
      if (card.approve && !isHighRisk(name)) ok = await card.approve((name === "edit_file" ? "edit to " : "write to ") + args.path);  // inline on the live write-preview
      else { const a = await approvalFor(name, args, ac); const title = isHighRisk(name) ? "⚠ High-risk — " + a.title : a.title; ok = await askConfirm(title, a.body); }
      if (!ok) { card.result({ error: "declined by user" }, "skip"); return { error: "user declined the " + name }; }
    }
    if (ac && ac.signal.aborted) { card.result({ error: "stopped by user" }, "skip"); return { error: "stopped by user" }; }   // Stop pressed during approval
    const res = await callTool(name, args, ac);
    let display = res;
    if (!(res && res.error)) {
      if (name === "mkdir") display = { content: "📁 created " + (res.path || args.path) };
      else if (name === "delete") display = { content: "🗑 deleted " + (res.deleted || "") + " " + (res.path || args.path) };
    }
    const failed = res && (res.error || res.timed_out || (typeof res.exit_code === "number" && res.exit_code !== 0));   // a command that ran but exited non-zero shows as error
    card.result(display, failed ? "error" : "ok");
    if (isMutating(name) && !(res && res.error)) refreshTree();   // a command may have changed files even on a non-zero exit
    return res;
  }

  // tolerant extractor: pull a (possibly still-streaming) JSON string value by key, decoding escapes.
  function partialString(args, key) {
    const BS = 92, tag = '"' + key + '"';
    let i = args.indexOf(tag);
    if (i < 0) return null;
    i = args.indexOf('"', i + tag.length);   // opening quote of the value
    if (i < 0) return null;
    i++;
    let out = "";
    const m = { n: 10, t: 9, r: 13, b: 8, f: 12 };
    while (i < args.length) {
      if (args.charCodeAt(i) === BS) {        // backslash escape (no backslash literal in source)
        const n = args[i + 1];
        if (n === undefined) break;           // incomplete escape at the streaming edge
        if (n === "u") {
          const hex = args.substr(i + 2, 4);
          if (hex.length < 4) break;
          out += String.fromCharCode(parseInt(hex, 16)); i += 6; continue;
        }
        out += (m[n] !== undefined) ? String.fromCharCode(m[n]) : n;
        i += 2; continue;
      }
      if (args[i] === '"') break;             // closing quote → value complete
      out += args[i]; i++;
    }
    return out;
  }

  // inline live preview of a file being written/edited — streams content with a terminal cursor.
  function writePreview(toolName) {
    const verb = toolName === "edit_file" ? "Editing" : "Writing";
    const wrap = document.createElement("div"); wrap.className = "wprev";
    const head = document.createElement("button"); head.className = "wprev__head"; head.type = "button";
    const caret = document.createElement("span"); caret.className = "wprev__caret"; caret.textContent = "▾";
    const titleEl = document.createElement("span"); titleEl.className = "wprev__title";
    const stateEl = document.createElement("span"); stateEl.className = "wprev__state"; stateEl.textContent = "writing…";
    head.append(caret, titleEl, stateEl);
    const pre = document.createElement("pre"); pre.className = "wprev__pre";
    const codeEl = document.createElement("span");
    const cursor = document.createElement("span"); cursor.className = "wprev__cursor";
    pre.append(codeEl, cursor);
    const actions = document.createElement("div"); actions.className = "wprev__actions"; actions.hidden = true;
    wrap.append(head, pre, actions);
    agentMessagesEl.appendChild(wrap); agScroll();
    const wT = makeTyper(codeEl, cursor, () => { pre.scrollTop = pre.scrollHeight; scrollDown(); });
    let path = "", collapsed = false;
    const setTitle = () => { titleEl.textContent = verb + " " + (path || "…"); };
    setTitle();
    head.addEventListener("click", () => {
      collapsed = !collapsed;
      wrap.classList.toggle("is-collapsed", collapsed);
      caret.textContent = collapsed ? "▸" : "▾";
    });
    return {
      setPath(p) { if (p && p !== path) { path = p; setTitle(); } },
      stream(text) { wT.feed(text); },
      approve(label) {
        stateEl.textContent = "awaiting approval";
        actions.hidden = false; actions.textContent = "";
        const q = document.createElement("span"); q.className = "wprev__ask"; q.textContent = "Apply " + (label || "this change") + "?";
        const no = document.createElement("button"); no.className = "btn btn--ghost"; no.type = "button"; no.textContent = "Decline";
        const yes = document.createElement("button"); yes.className = "btn btn--primary"; yes.type = "button"; yes.textContent = "Approve";
        actions.append(q, no, yes);
        scrollApprovalIntoView(actions);                 // Follow: bring the Approve/Decline buttons into view
        return new Promise((resolve) => {
          const done = (v) => { pendingApproval = null; actions.hidden = true; resolve(v); };
          pendingApproval = { resolve: done };           // so Stop/Clear can unblock this
          yes.addEventListener("click", () => done(true));
          no.addEventListener("click", () => done(false));
        });
      },
      result(res, kind) {
        wT.finish(); cursor.remove();
        const skip = kind === "skip", err = !skip && res && res.error;
        stateEl.className = "wprev__state wprev__state--" + (skip ? "skip" : err ? "err" : "ok");
        stateEl.textContent = skip ? "declined" : err ? "error" : (res && res.bytes != null ? res.bytes + " bytes" : "done");
      },
    };
  }

  async function agentStreamTurn(ac) {
    const ui = agAssistant();
    let content = "", reasoning = "", tcs = [], previews = {}, tFirst = null, outTok = 0, usage = null, finishReason = null;
    let sentMsgs = agentMsgs;          // function-scoped: assigned in the stream loop, read again at calibration AFTER the try/finally
    const t0 = performance.now();
    const estPrompt = estimateTokens(agentMsgs);
    beginLiveMetrics();
    const liveTimer = setInterval(() => renderLiveMetrics({ t0, tFirst, outTokEst: outTok, estPrompt }), 150);   // live rail tiles during agent turns
    try {
    let res, level = 0, tries = 0;
    for (;;) {
      const toolsChrs = toolsChars();
      sentMsgs = fitForSend(AGENT_SYSTEM, agentMsgs, { level: level, protectFirst: true, extraTok: tokFromChars(toolsChrs) });   // trim to fit --ctx (reserve the tool-schema tokens)
      const mtA = maxTokensFor(sumTok(sentMsgs) + tokFromChars(AGENT_SYSTEM.length) + tokFromChars(toolsChrs));
      res = await fetch(C.serverUrl + "/v1/chat/completions", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: ac.signal,
        body: JSON.stringify({ model: C.model, stream: true, stream_options: { include_usage: true }, tools: toolDefs(), tool_choice: "auto", messages: [{ role: "system", content: AGENT_SYSTEM + commandsNote() }].concat(sentMsgs), ...thinkSpread(agentThink.level), ...(mtA ? { max_tokens: mtA } : {}) }),
      });
      if (res.ok) break;
      const errTxt = await res.text();
      if (res.status === 400 && /context|too long|exceed|maximum/i.test(errTxt) && level < 2) { learnCtxFromError(errTxt); level++; continue; }   // learn ctx + compact harder + retry
      if (isTransientErr(res.status, errTxt) && tries < TRANSIENT_TRIES) {   // transient backend blip (e.g. ROCm "prefill state reset failed") — retry, don't kill the loop
        tries++; toast("Backend hiccup — retrying… (" + tries + "/" + TRANSIENT_TRIES + ")");
        await backoffSleep(tries, ac.signal); continue;
      }
      throw new Error("HTTP " + res.status + " - " + errTxt.slice(0, 180));
    }
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n"); buf = lines.pop();
      for (const line of lines) {
        const s = line.trim(); if (!s.startsWith("data:")) continue;
        const data = s.slice(5).trim(); if (data === "[DONE]") continue;
        let j; try { j = JSON.parse(data); } catch { continue; }
        if (j.usage) usage = j.usage;
        const fr = j.choices && j.choices[0] && j.choices[0].finish_reason; if (fr) finishReason = fr;
        const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
        if (d.reasoning_content) { if (tFirst === null) tFirst = performance.now(); reasoning += d.reasoning_content; outTok++; ui.thinking(reasoning); }
        if (d.content) { if (tFirst === null) tFirst = performance.now(); content += d.content; outTok++; ui.stream(content); }
        if (d.tool_calls) for (const t of d.tool_calls) {
          ui.cursorOff();                                    // the tool/preview owns the cursor now
          const i = t.index || 0; tcs[i] = tcs[i] || { id: "", name: "", args: "" };
          if (t.id) tcs[i].id = t.id;
          if (t.function) { if (t.function.name) tcs[i].name = t.function.name; if (t.function.arguments) tcs[i].args += t.function.arguments; }
          if (tcs[i].name === "write_file" || tcs[i].name === "edit_file") {   // live preview while args stream
            if (!previews[i]) { previews[i] = writePreview(tcs[i].name); tcs[i].preview = previews[i]; }
            const pp = partialString(tcs[i].args, "path"); if (pp != null) previews[i].setPath(pp);
            const bodyText = partialString(tcs[i].args, tcs[i].name === "write_file" ? "content" : "replace");
            if (bodyText != null) previews[i].stream(bodyText);
          }
        }
      }
    }
    } finally { clearInterval(liveTimer); }
    const tEnd = performance.now();
    ui.finalize(content);
    const usedTok = finalizeTurnMetrics({ t0, tFirst, tEnd, usage, outTokEst: outTok, estPrompt });
    if (usage && usage.prompt_tokens) calibrateTokenizer(AGENT_SYSTEM.length + toolsChars() + charsOf(sentMsgs), usage.prompt_tokens);   // learn real chars/token (incl. system + tools)
    if (finishReason === "length") noticeTruncation(ui, usedTok);
    if (content) {
      const decSecs = (tEnd - (tFirst == null ? tEnd : tFirst)) / 1000;
      const tps = usage && usage.completion_tokens && decSecs > 0 ? (usage.completion_tokens / decSecs).toFixed(1) : "?";
      const tok = usage && usage.completion_tokens != null ? usage.completion_tokens : outTok;
      ui.meta(tps + " t/s · " + tok + " tokens · " + ((tEnd - t0) / 1000).toFixed(1) + " s", agentThink);
    }
    logDifficulty({ ts: Date.now(), agent: true, mode: thinkMode, level: agentThink.level, reason: agentThink.reason, auto: agentThink.auto, promptTok: usage && usage.prompt_tokens, completionTok: usage && usage.completion_tokens, reasoningTok: Math.round(reasoning.length / 4), finishReason });
    if (finishReason === "length" && agentThink.auto) agentThink.level = "on";   // escalate-only AFTER recording this turn
    return { content, toolCalls: tcs.filter(Boolean) };
  }

  async function runAgent(text) {
    if (!workspace) { toast("Choose a folder for the agent first."); $("agentPick").click(); return; }
    if (typeof AG.load === "function") {               // fetch the live tool catalog from the backend registry (cached after first call)
      try { await AG.load(C.agentUrl); }
      catch (e) { toast("Couldn't load agent tools: " + (e.message || e)); return; }
    }
    if (!toolDefs().length) { toast("No agent tools available from the backend."); return; }
    const myRun = ++agentRunId;                        // tag this run so Clear/supersede can invalidate it
    const ac = new AbortController(); agentAbort = ac;  // local handle so Stop/Clear can't NPE the loop
    agentBusy = true; setSendStop(true); agentEmpty.hidden = true;
    agUser(text); agentMsgs.push({ role: "user", content: text });
    nudgeState = 0;                                   // re-evaluate context nudges for this run
    agentThink = thinkMode === "auto" ? Object.assign(rateDifficulty(text), { auto: true }) : { level: thinkMode, reason: thinkMode, auto: false };
    try {
      let guard = 0;
      while (guard++ < 25) {
        if (ac.signal.aborted) break;                 // Stop pressed
        maybeNudge();                                 // enqueue a wrap-up notice if the context is filling
        drainPending();                               // deliver queued out-of-band messages at this turn boundary
        const turn = await agentStreamTurn(ac);
        if (agentRunId !== myRun) return;             // Cleared/superseded mid-turn -> don't write back
        const asg = { role: "assistant", content: turn.content || "" };
        if (turn.toolCalls.length) asg.tool_calls = turn.toolCalls.map((tc) => ({ id: tc.id, type: "function", function: { name: tc.name, arguments: stubCallArgs(tc.args, writeCapChars()) } }));   // cap the ECHOED file content on insert (byte-stable); execution below still uses the full tc.args
        agentMsgs.push(asg);
        if (!turn.toolCalls.length) break;
        for (const tc of turn.toolCalls) {
          let args = {}; try { args = JSON.parse(tc.args || "{}"); } catch { args = {}; }
          const card = tc.preview || agToolCard(tc.name, args);
          let res;
          if (ac.signal.aborted) { res = { error: "stopped by user" }; if (card && card.result) card.result(res, "skip"); }   // pair every tool_call, even after Stop
          else res = await execToolCall(tc.name, args, card, ac);
          if (agentRunId !== myRun) return;
          agentMsgs.push({ role: "tool", tool_call_id: tc.id, content: capToolOutput(res) });   // cap on insert -> small, byte-stable prefill
          if (agentThink.auto && res && res.error && !ac.signal.aborted) agentThink.level = "on";   // escalate-only: a tool failed -> think

        }
        if (ac.signal.aborted) break;
      }
    } catch (e) {
      if (e.name !== "AbortError" && agentRunId === myRun) { agError(String(e.message || e)); loopErrored = true; }
    } finally {
      fetch(C.agentUrl + "/jobs/cleanup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: String(myRun) }) }).catch(() => {});   // reap this run's run-scoped background processes
      if (agentRunId === myRun) { agentBusy = false; setSendStop(false); agentAbort = null; saveAgent(); logFinishTurn("agent"); }
    }
  }

  /* ---------------- server heartbeat ---------------- */
  function setStatus(state, label) {
    const el = $("serverStatus"); el.dataset.state = state;
    el.querySelector(".status__label").textContent = label;
  }
  async function ping() {
    try {
      const c = new AbortController(); const to = setTimeout(() => c.abort(), 3000);
      const r = await fetch(C.serverUrl + "/v1/models", { signal: c.signal }); clearTimeout(to);
      setStatus(r.ok ? "online" : "offline", r.ok ? "ds4-server online" : "ds4-server error");
    } catch { setStatus("offline", "ds4-server offline"); }
  }
  setStatus("connecting", "connecting…"); ping(); setInterval(ping, 5000);

  /* ---------------- telemetry (Phase 3, live sidecar) ---------------- */
  const spark = $("utilSpark"), sctx = spark.getContext("2d");
  const N = 120, hist = new Array(N).fill(0);
  function resizeCanvas() { const dpr = window.devicePixelRatio || 1; spark.width = spark.clientWidth * dpr; spark.height = spark.clientHeight * dpr; sctx.setTransform(dpr, 0, 0, dpr, 0, 0); }
  function drawSpark() {
    const w = spark.clientWidth, h = spark.clientHeight, pad = 4; if (!w || !h) return;
    sctx.clearRect(0, 0, w, h);
    const x = (i) => pad + (i / (N - 1)) * (w - 2 * pad), y = (v) => h - pad - (v / 100) * (h - 2 * pad);
    sctx.beginPath(); sctx.moveTo(x(0), h - pad);
    hist.forEach((v, i) => sctx.lineTo(x(i), y(v)));
    sctx.lineTo(x(N - 1), h - pad); sctx.closePath();
    const g = sctx.createLinearGradient(0, 0, 0, h); g.addColorStop(0, "rgba(124,140,255,0.35)"); g.addColorStop(1, "rgba(124,140,255,0.02)");
    sctx.fillStyle = g; sctx.fill();
    sctx.beginPath(); hist.forEach((v, i) => (i ? sctx.lineTo(x(i), y(v)) : sctx.moveTo(x(i), y(v))));
    sctx.strokeStyle = "#8E9CFF"; sctx.lineWidth = 1.5; sctx.lineJoin = "round"; sctx.stroke();
  }
  window.addEventListener("resize", () => { resizeCanvas(); drawSpark(); });
  if (window.ResizeObserver) new ResizeObserver(() => { resizeCanvas(); drawSpark(); }).observe(spark);
  resizeCanvas(); drawSpark();

  let lastBackendJson = "";
  function renderBackend(m) {
    const dl = $("backendDl"); if (!dl) return;
    const ctx = currentCtx();
    const rows = [["Model", C.model || "—"]];
    if (C.quant) rows.push(["Quant", C.quant]);
    if (m.backend) rows.push(["Backend", String(m.backend).toUpperCase()]);
    rows.push(["Context", ctx ? fmtTok(ctx) + " tokens" : "unknown"]);
    if (m.gpu && m.gpu.name) rows.push(["GPU", m.gpu.name]);
    if (m.model) { rows.push(["Model file", m.model.path]); rows.push(["Warm", m.model.warm_pct + "%"]); }
    rows.push(["Sample", (C.pollHz || 2) + " Hz"]);
    const j = JSON.stringify(rows); if (j === lastBackendJson) return; lastBackendJson = j;
    dl.innerHTML = rows.map((r) => "<dt>" + esc(r[0]) + "</dt><dd>" + esc(String(r[1])) + "</dd>").join("");
  }
  function applyMetrics(m) {
    if (m.gpu) {
      const gp = m.gpu;
      if (gp.name) { $("gpuLabel").textContent = "GPU · " + gp.name; $("brandSub").textContent = (C.quant ? C.quant + " · " : "") + gp.name + (m.backend ? " · " + String(m.backend).toUpperCase() : ""); }
      if (gp.util_pct != null) { setText("gpuUtil", gp.util_pct + " %"); hist.push(gp.util_pct); hist.shift(); $("gpuPeak").textContent = "peak " + Math.max(...hist) + " %"; drawSpark(); }
      if (gp.temp_c != null) setText("gpuTemp", gp.temp_c + " °C");
      if (gp.power_w != null) setText("gpuPower", `${Math.round(gp.power_w)} / ${Math.round(gp.power_limit_w)} W`);
      if (gp.sm_clock_mhz != null) setText("gpuClock", gp.sm_clock_mhz + " MHz");
      if (gp.vram_total_mb) { setText("vramTxt", `${(gp.vram_used_mb / 1024).toFixed(1)} / ${Math.round(gp.vram_total_mb / 1024)} GB`); $("vramFill").style.width = (gp.vram_used_mb / gp.vram_total_mb * 100) + "%"; }
    }
    if (m.ram && m.ram.total_mb) {
      const gb = (mb) => (mb / 1024).toFixed(0);
      setText("ramTxt", `${gb(m.ram.used_mb)} / ${gb(m.ram.total_mb)} GB`);
      $("ramFill").style.width = (m.ram.used_mb / m.ram.total_mb * 100) + "%";
    }
    if (m.model) $("ramNote").innerHTML = `<span class="ok">●</span> model warm: ${(m.model.resident_mb / 1024).toFixed(0)} / ${(m.model.size_mb / 1024).toFixed(0)} GB (${m.model.warm_pct}%)`;
    if (m.disk) { const r = m.disk.read_mb_s || 0; $("diskNote").textContent = `expert stream (disk): ${r < 1 ? "idle (served from RAM)" : r.toFixed(0) + " MB/s"}`; }
    setText("sampleHz", (C.pollHz || 2) + " Hz");
    if (m.ctx) serverCtx = m.ctx;
    if (m.backend) serverBackend = m.backend;
    renderBackend(m);
    if (lastCtxUsed != null && !streaming && !agentBusy) updateContextMeter(lastCtxUsed, false);   // re-render if ctx arrived after a turn
  }
  async function poll() {
    try {
      const c = new AbortController(); const to = setTimeout(() => c.abort(), 2500);
      const r = await fetch(C.sidecarUrl + "/metrics", { signal: c.signal }); clearTimeout(to);
      if (!r.ok) throw 0;
      applyMetrics(await r.json());
    } catch { $("sampleHz").textContent = "sidecar ✕"; }
  }
  poll(); setInterval(poll, 1000 / (C.pollHz || 2));

  /* ---------------- persistence (separate, saved per-mode conversations) ---------------- */
  const LS = { chat: "ds4:chatMsgs", agent: "ds4:agentMsgs", amode: "ds4:agentMode" };
  function saveChat() { try { localStorage.setItem(LS.chat, JSON.stringify(messages)); } catch (e) {} }
  function saveAgent() { try { localStorage.setItem(LS.agent, JSON.stringify(agentMsgs)); } catch (e) {} }

  /* ---------------- conversation logging (sidecar → /logs) + in-memory history cap ---------------- */
  const LOGCFG = C.logging || {};
  const logOn = LOGCFG.enabled !== false;
  const maxHistChars = LOGCFG.maxHistoryChars || 4000000;
  const logState = { chat: { name: null, logged: 0 }, agent: { name: null, logged: 0 } };
  const NL = String.fromCharCode(10);
  const pad2 = (n) => String(n).padStart(2, "0");
  function buildLogName(m) {
    const d = new Date(), secs = d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds();
    return (m === "agent" ? "Agent" : "Chat") + "_" + pad2(d.getDate()) + "-" + pad2(d.getMonth() + 1) + "-" + String(d.getFullYear()).slice(-2) + "_" + secs + ".md";
  }
  function msgsOf(m) { return m === "agent" ? agentMsgs : messages; }
  function fmtMsgs(arr) {
    let out = "";
    for (const m of arr) {
      if (m.role === "user") out += NL + "### User" + NL + NL + (m.content || "") + NL;
      else if (m.role === "assistant") {
        out += NL + "### Assistant" + NL + NL + (m.content || "");
        if (m.tool_calls) for (const tc of m.tool_calls) out += NL + "- tool: " + tc.function.name + "(" + (tc.function.arguments || "") + ")";
        out += NL;
      } else if (m.role === "tool") out += NL + "> tool result: " + String(m.content || "").slice(0, 4000) + NL;
    }
    return out;
  }
  function saveLogState() { try { localStorage.setItem("ds4:logState", JSON.stringify(logState)); } catch (e) {} }
  function flushLog(m, beacon) {
    if (!logOn) return;
    const arr = msgsOf(m), st = logState[m];
    if (!st.name) { st.name = buildLogName(m); st.logged = 0; }
    if (arr.length <= st.logged) return;
    const first = st.logged === 0;
    const head = first ? ("# " + (m === "agent" ? "Agent" : "Chat") + " conversation - " + new Date().toLocaleString() + NL) : "";
    const body = head + fmtMsgs(arr.slice(st.logged));
    const prev = st.logged;
    st.logged = arr.length; saveLogState();
    const payload = JSON.stringify({ name: st.name, content: body, append: !first });
    if (beacon && navigator.sendBeacon) { try { navigator.sendBeacon(C.sidecarUrl + "/log", new Blob([payload], { type: "text/plain" })); } catch (e) {} }
    else fetch(C.sidecarUrl + "/log", { method: "POST", headers: { "Content-Type": "application/json" }, body: payload }).then((r) => { if (!r.ok) throw 0; }).catch(() => { st.logged = prev; saveLogState(); });
  }
  function capHistory(m) {
    const arr = msgsOf(m);
    let chars = 0; for (const x of arr) chars += (x.content || "").length;
    if (chars <= maxHistChars) return;
    flushLog(m);                              // persist before dropping the oldest from RAM
    let drop = 0;
    while (arr.length - drop > 2 && chars > maxHistChars) { chars -= (arr[drop].content || "").length; drop++; }
    if (drop > 0) { arr.splice(0, drop); logState[m].logged = Math.max(0, logState[m].logged - drop); saveLogState(); m === "agent" ? saveAgent() : saveChat(); }
  }
  function logFinishTurn(m) { flushLog(m); capHistory(m); }
  function resetLog(m) { flushLog(m); logState[m] = { name: null, logged: 0 }; saveLogState(); }
  /* ---------- UI state file: resume across runs even when the browser profile is fresh (sidecar /state) ---------- */
  let lastStateJson = "";
  function snapshotState() {
    const o = {};
    for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (k && k.indexOf("ds4:") === 0) o[k] = localStorage.getItem(k); }
    return o;
  }
  function saveState(beacon) {
    if (!logOn) return;
    const snap = snapshotState();
    if (!Object.keys(snap).length) return;                  // never clobber saved state with an empty snapshot (fresh profile / failed load)
    let json; try { json = JSON.stringify(snap); } catch (e) { return; }
    if (!beacon && json === lastStateJson) return;          // only write when something actually changed
    lastStateJson = json;
    if (beacon && navigator.sendBeacon) { try { navigator.sendBeacon(C.sidecarUrl + "/state", new Blob([json], { type: "text/plain" })); } catch (e) {} }
    else fetch(C.sidecarUrl + "/state", { method: "POST", headers: { "Content-Type": "application/json" }, body: json }).catch(() => { lastStateJson = ""; });
  }
  async function loadState() {
    try {
      const c = new AbortController(); const to = setTimeout(() => c.abort(), 1500);
      const r = await fetch(C.sidecarUrl + "/state", { signal: c.signal }); clearTimeout(to);
      if (!r.ok) return;
      const o = await r.json();
      if (o && typeof o === "object") for (const k in o) { if (k.indexOf("ds4:") === 0 && typeof o[k] === "string") { try { localStorage.setItem(k, o[k]); } catch (e) {} } }
    } catch (e) {}
  }

  const flushAllLogs = () => { flushLog("chat", true); flushLog("agent", true); saveState(true); };   // persist conversation log + UI state on close
  window.addEventListener("pagehide", flushAllLogs);
  window.addEventListener("beforeunload", flushAllLogs);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushAllLogs(); });

  function renderChatHistory() {
    messagesEl.innerHTML = "";
    for (const m of messages) {
      if (m.role === "user") { const e = document.createElement("div"); e.className = "msg msg--user"; e.textContent = m.content; messagesEl.appendChild(e); }
      else if (m.role === "assistant") { const e = document.createElement("div"); e.className = "msg msg--assistant"; const b = document.createElement("div"); b.className = "md"; b.innerHTML = mdRender(m.content || ""); addCopy(b); e.appendChild(b); messagesEl.appendChild(e); }
    }
  }
  function renderAgentHistory() {
    agentMessagesEl.innerHTML = "";
    const results = {};
    for (const m of agentMsgs) if (m.role === "tool") { try { results[m.tool_call_id] = JSON.parse(m.content); } catch (e) { results[m.tool_call_id] = m.content; } }   // a stubbed/capped result is plain text, not JSON — keep it as text, not a fake error
    for (const m of agentMsgs) {
      if (m.role === "user") { agUser(m.content); }
      else if (m.role === "assistant") {
        if (m.content) { const e = document.createElement("div"); e.className = "msg msg--assistant"; const b = document.createElement("div"); b.className = "md"; b.innerHTML = mdRender(m.content); addCopy(b); e.appendChild(b); agentMessagesEl.appendChild(e); }
        if (m.tool_calls) for (const tc of m.tool_calls) {
          let args = {}; try { args = JSON.parse(tc.function.arguments || "{}"); } catch (e) { args = {}; }
          const card = agToolCard(tc.function.name, args);
          const res = results[tc.id];
          if (res !== undefined) card.result(res, res && res.error ? "error" : "ok");
        }
      }
    }
  }
  function restoreConversations() {
    try { messages = JSON.parse(localStorage.getItem(LS.chat) || "[]") || []; } catch (e) { messages = []; }
    try { agentMsgs = JSON.parse(localStorage.getItem(LS.agent) || "[]") || []; } catch (e) { agentMsgs = []; }
    try { const ls = JSON.parse(localStorage.getItem("ds4:logState") || "null"); if (ls && ls.chat && ls.agent) { logState.chat = ls.chat; logState.agent = ls.agent; } } catch (e) {}
    agentMode = localStorage.getItem(LS.amode) || "ask";
    amToggle.classList.toggle("is-auto", agentMode === "auto");
    amToggle.querySelectorAll(".seg2__btn").forEach((x) => x.classList.toggle("is-active", x.dataset.am === agentMode));
    const tv = localStorage.getItem("ds4:thinking");           // AGENT: migrate old boolean "1"/"0" -> "on"/"off"/"auto"
    thinkMode = tv === "1" ? "on" : tv === "0" ? "off" : (tv === "on" || tv === "off" || tv === "auto") ? tv : thinkMode;
    const cv = localStorage.getItem("ds4:chatThink");          // CHAT: plain on/off toggle
    chatThink = (cv === "on" || cv === "off") ? cv : chatThink;
    reflectThink();
    mode = localStorage.getItem("ds4:mode") || "chat";           // restore Chat/Agent
    const savedStick = localStorage.getItem("ds4:stick");        // read Follow before render
    rendering = true; renderChatHistory(); renderAgentHistory(); rendering = false;   // rebuild without firing the Follow auto-toggle
    stick = savedStick !== "0"; reflectStick(); saveStick();     // apply restored Follow
    railResyncs.forEach((f) => f());                             // re-apply sidebar collapse (localStorage may have just been seeded by loadState)
  }

  function boot() {
    restoreConversations();
    updateView();
    syncWorkspace();                             // re-lock the saved folder if we restored into Agent mode (agent-only)
    setInterval(() => saveState(false), 3000);   // mirror UI state to the state file as it changes
  }
  let hasLocal = false;
  for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (k && k.indexOf("ds4:") === 0) { hasLocal = true; break; } }
  if (hasLocal) boot();                          // persistent browser profile already holds our state → restore immediately (no flash)
  else loadState().then(boot).catch(boot);       // fresh profile / different browser → seed from the state file first
})();
