/* ============================================================
   DS4 Web Frontend — app logic
   Phase 1: shell/theme · Phase 2: live chat streaming · Phase 3: live telemetry
   ============================================================ */
(() => {
  "use strict";
  const C = window.DS4_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const setText = (id, v) => { const e = $(id); if (e) { e.textContent = v; e.classList.remove("is-empty"); } };

  /* ---------------- header / cards / toggle ---------------- */
  if (C.hardware) $("brandSub").textContent = `${C.quant} · ${C.hardware}`;

  const messagesEl = $("messages");
  const emptyState = $("emptyState");
  const agentPreview = $("agentPreview");
  let mode = "chat";

  (C.suggestions || []).forEach((s) => {
    const el = document.createElement("button");
    el.className = "card";
    el.innerHTML = `<div class="card__tag">${esc(s.tag)}</div><div class="card__text">${esc(s.text)}</div>`;
    el.addEventListener("click", () => { input.value = s.text; autosize(input); send(); });
    $("suggestionCards").appendChild(el);
  });

  const toggle = $("modeToggle");
  toggle.querySelectorAll(".segmented__btn").forEach((btn) =>
    btn.addEventListener("click", () => { mode = btn.dataset.mode; updateView(); }));
  function updateView() {
    const agent = mode === "agent";
    toggle.classList.toggle("is-agent", agent);
    toggle.querySelectorAll(".segmented__btn").forEach((b) => b.classList.toggle("is-active", b.dataset.mode === mode));
    agentPreview.hidden = !agent;
    messagesEl.hidden = agent;
    emptyState.hidden = agent || messages.length > 0;
  }

  /* ---------------- composer ---------------- */
  const input = $("input");
  let thinkingOn = true;
  $("thinkToggle").addEventListener("click", (e) => {
    thinkingOn = !thinkingOn;
    e.target.classList.toggle("is-on", thinkingOn);
  });
  input.addEventListener("input", () => autosize(input));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  $("sendBtn").addEventListener("click", () => (streaming ? stop() : send()));
  $("clearBtn").addEventListener("click", () => {
    if (streaming) stop();
    messages = []; messagesEl.innerHTML = ""; resetTurnMetrics(); updateView(); input.focus();
  });
  function autosize(t) { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }

  /* ---------------- chat ---------------- */
  let messages = [];          // API history [{role, content}]
  let streaming = false;
  let abortCtrl = null;

  function nearBottom() { const c = messagesEl.parentElement; return c.scrollHeight - c.scrollTop - c.clientHeight < 120; }
  function scrollDown(force) { const c = messagesEl.parentElement; if (force || nearBottom()) c.scrollTop = c.scrollHeight; }

  function appendUser(text) {
    const el = document.createElement("div");
    el.className = "msg msg--user"; el.textContent = text;
    messagesEl.appendChild(el); scrollDown(true);
  }

  function appendAssistant() {
    const root = document.createElement("div"); root.className = "msg msg--assistant";
    const think = document.createElement("details"); think.className = "think"; think.open = true; think.hidden = true;
    think.innerHTML = '<summary>Thinking</summary><div class="think__body"></div>';
    const thinkBody = think.querySelector(".think__body");
    const body = document.createElement("div"); body.className = "md is-streaming";
    const cursor = document.createElement("span"); cursor.className = "cursor";
    const meta = document.createElement("div"); meta.className = "msg__meta"; meta.hidden = true;
    root.append(think, body, cursor, meta);
    messagesEl.appendChild(root); scrollDown(true);
    return {
      thinking(t) { think.hidden = false; thinkBody.textContent = t; scrollDown(); },
      stream(t) { body.textContent = t; scrollDown(); },
      finalize(content) { cursor.remove(); body.classList.remove("is-streaming"); body.innerHTML = mdRender(content); addCopy(body); think.open = false; },
      meta(s) { meta.hidden = false; meta.textContent = s; },
      error(msg) { cursor.remove(); root.classList.add("msg--error"); body.classList.remove("is-streaming"); body.textContent = "⚠ " + msg; },
    };
  }

  async function send() {
    const text = input.value.trim();
    if (!text || streaming) return;
    input.value = ""; autosize(input);
    appendUser(text);
    messages.push({ role: "user", content: text });
    updateView();
    await runStream();
  }
  function stop() { if (abortCtrl) abortCtrl.abort(); }

  async function runStream() {
    streaming = true; setSendStop(true);
    const ui = appendAssistant();
    const t0 = performance.now();
    let tFirst = null, content = "", reasoning = "", usage = null;
    abortCtrl = new AbortController();
    try {
      const res = await fetch(C.serverUrl + "/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: abortCtrl.signal,
        body: JSON.stringify({
          model: C.model, stream: true, stream_options: { include_usage: true },
          messages, ...(thinkingOn ? {} : { thinking: { type: "disabled" } }),
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} — ${(await res.text()).slice(0, 180)}`);
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
          const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
          if (d.reasoning_content) { if (tFirst === null) tFirst = performance.now(); reasoning += d.reasoning_content; ui.thinking(reasoning); }
          if (d.content) { if (tFirst === null) tFirst = performance.now(); content += d.content; ui.stream(content); }
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
      updateTurnMetrics({ t0, tFirst, tEnd, usage });
      if (usage) {
        const decSecs = (tEnd - (tFirst == null ? tEnd : tFirst)) / 1000;
        const tps = usage.completion_tokens && decSecs > 0 ? (usage.completion_tokens / decSecs).toFixed(1) : "?";
        ui.meta(`${tps} t/s · ${usage.completion_tokens == null ? "?" : usage.completion_tokens} tokens · ${((tEnd - t0) / 1000).toFixed(1)} s`);
      }
    } catch (e) {
      if (e.name === "AbortError") { ui.finalize(content || "_(stopped)_"); }
      else ui.error(String(e.message || e));
    } finally {
      streaming = false; setSendStop(false); abortCtrl = null;
    }
  }

  function setSendStop(s) {
    const b = $("sendBtn");
    b.textContent = s ? "Stop" : "Send";
    b.classList.toggle("btn--stop", s);
    b.classList.toggle("btn--primary", !s);
  }

  /* ---------------- per-turn metrics ---------------- */
  function updateTurnMetrics({ t0, tFirst, tEnd, usage }) {
    const ttft = tFirst != null ? (tFirst - t0) / 1000 : null;
    const total = (tEnd - t0) / 1000;
    const pt = usage && usage.prompt_tokens, ot = usage && usage.completion_tokens;
    setText("mTtft", ttft != null ? ttft.toFixed(2) + " s" : "—");
    setText("mTotal", total.toFixed(2) + " s");
    setText("mPrefill", pt && ttft ? (pt / ttft).toFixed(1) + " t/s" : "—");
    setText("mDecode", ot && ttft != null && total - ttft > 0 ? (ot / (total - ttft)).toFixed(1) + " t/s" : "—");
    setText("mPrompt", pt != null ? String(pt) : "—");
    setText("mOutput", ot != null ? String(ot) : "—");
  }
  function resetTurnMetrics() {
    ["mTtft", "mTotal", "mPrefill", "mDecode", "mPrompt", "mOutput"].forEach((id) => {
      const e = $(id); e.textContent = "—"; e.classList.add("is-empty");
    });
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

  function applyMetrics(m) {
    if (m.gpu) {
      const gp = m.gpu;
      if (gp.name) $("gpuLabel").textContent = "GPU · " + gp.name;
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

  updateView();
})();
