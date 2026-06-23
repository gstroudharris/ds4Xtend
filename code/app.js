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
  let workspace = null, agentMode = "ask", agentMsgs = [], agentBusy = false, agentAbort = null;

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
    agentPanel.hidden = !agent;
    messagesEl.hidden = agent;
    emptyState.hidden = agent || messages.length > 0;
    const ae = $("agentEmpty"); if (ae) ae.hidden = agentMsgs.length > 0;
    input.placeholder = agent
      ? (workspace ? "Ask the agent to read or edit files in the folder…" : "Choose a folder for the agent first…")
      : "Message DS4… (Enter to send, Shift+Enter for newline)";
  }

  /* ---------------- composer ---------------- */
  const input = $("input");
  let thinkingOn = true;
  $("thinkToggle").addEventListener("click", (e) => {
    thinkingOn = !thinkingOn;
    try { localStorage.setItem("ds4:thinking", thinkingOn ? "1" : "0"); } catch (err) {}
    e.target.classList.toggle("is-on", thinkingOn);
  });
  input.addEventListener("input", () => autosize(input));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  $("sendBtn").addEventListener("click", () => { if (streaming) return stop(); if (agentBusy) return stopAgent(); send(); });
  $("clearBtn").addEventListener("click", () => {
    if (mode === "agent") {                       // Clear only the active mode's conversation
      if (agentBusy) stopAgent();
      agentMsgs = []; agentMessagesEl.innerHTML = ""; saveAgent(); $("agentEmpty").hidden = false;
    } else {
      if (streaming) stop();
      messages = []; messagesEl.innerHTML = ""; resetTurnMetrics(); saveChat();
    }
    updateView(); input.focus();
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
    if (!text) return;
    if (mode === "agent") { if (agentBusy) return; input.value = ""; autosize(input); return runAgent(text); }
    if (streaming) return;
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
    let outTokEst = 0;
    const estPrompt = estimateTokens(messages);
    beginLiveMetrics();
    const liveTimer = setInterval(() => renderLiveMetrics({ t0, tFirst, outTokEst, estPrompt }), 150);
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
      finalizeTurnMetrics({ t0, tFirst, tEnd, usage, outTokEst, estPrompt });
      if (usage) {
        const decSecs = (tEnd - (tFirst == null ? tEnd : tFirst)) / 1000;
        const tps = usage.completion_tokens && decSecs > 0 ? (usage.completion_tokens / decSecs).toFixed(1) : "?";
        ui.meta(`${tps} t/s · ${usage.completion_tokens == null ? "?" : usage.completion_tokens} tokens · ${((tEnd - t0) / 1000).toFixed(1)} s`);
      }
    } catch (e) {
      if (e.name === "AbortError") { ui.finalize(content || "_(stopped)_"); finalizeTurnMetrics({ t0, tFirst, tEnd: performance.now(), usage, outTokEst, estPrompt }); }
      else ui.error(String(e.message || e));
    } finally {
      clearInterval(liveTimer);
      streaming = false; setSendStop(false); abortCtrl = null;
      saveChat();
    }
  }

  function setSendStop(s) {
    const b = $("sendBtn");
    b.textContent = s ? "Stop" : "Send";
    b.classList.toggle("btn--stop", s);
    b.classList.toggle("btn--primary", !s);
  }

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
  }
  function finalizeTurnMetrics({ t0, tFirst, tEnd, usage, outTokEst, estPrompt }) {
    const ttft = tFirst != null ? (tFirst - t0) / 1000 : null;
    const total = (tEnd - t0) / 1000;
    const pt = (usage && usage.prompt_tokens != null) ? usage.prompt_tokens : estPrompt;
    const ot = (usage && usage.completion_tokens != null) ? usage.completion_tokens : outTokEst;
    setTile("mTtft", ttft != null ? ttft.toFixed(2) + " s" : "—", false);
    setTile("mTotal", total.toFixed(2) + " s", false);
    setTile("mPrefill", (pt && ttft) ? (pt / ttft).toFixed(1) + " t/s" : "—", false);
    setTile("mDecode", (ot && ttft != null && total - ttft > 0) ? (ot / (total - ttft)).toFixed(1) + " t/s" : "—", false);
    setTile("mPrompt", pt != null ? String(pt) : "—", false);
    setTile("mOutput", ot != null ? String(ot) : "—", false);
  }
  function resetTurnMetrics() {
    TURN_IDS.forEach((id) => { const e = $(id); e.textContent = "—"; e.classList.add("is-empty"); e.classList.remove("is-live"); });
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
  const AGENT_SYSTEM =
    "You are DS4, a coding agent working inside a single locked project folder. Use the provided tools " +
    "(list_dir, read_file, search, write_file, edit_file, mkdir, delete) to inspect and modify files. " +
    "All paths are relative to the workspace root (use '.' for the root); you cannot access anything " +
    "outside the folder. Prefer edit_file for small changes; read or search before editing. When you " +
    "write a file, provide its full new contents. When the task is done, briefly summarize what you changed.";

  const TOOLS = [
    { type: "function", function: { name: "list_dir", description: "List files and folders in a directory within the workspace.", parameters: { type: "object", properties: { path: { type: "string", description: "Directory relative to the workspace root; use '.' for the root." } }, required: ["path"] } } },
    { type: "function", function: { name: "read_file", description: "Read a UTF-8 text file within the workspace.", parameters: { type: "object", properties: { path: { type: "string", description: "File path relative to the workspace root." } }, required: ["path"] } } },
    { type: "function", function: { name: "search", description: "Search file contents within the workspace for a substring.", parameters: { type: "object", properties: { query: { type: "string" }, path: { type: "string", description: "Optional subdirectory to limit the search." } }, required: ["query"] } } },
    { type: "function", function: { name: "write_file", description: "Create or overwrite a UTF-8 text file within the workspace with full new contents.", parameters: { type: "object", properties: { path: { type: "string" }, content: { type: "string" } }, required: ["path", "content"] } } },
    { type: "function", function: { name: "edit_file", description: "Find-and-replace in an existing text file (replaces every occurrence of 'find' with 'replace').", parameters: { type: "object", properties: { path: { type: "string" }, find: { type: "string" }, replace: { type: "string" } }, required: ["path", "find", "replace"] } } },
    { type: "function", function: { name: "mkdir", description: "Create a directory (and parents) within the workspace.", parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] } } },
    { type: "function", function: { name: "delete", description: "Delete a file, or an empty directory, within the workspace.", parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] } } },
  ];
  const TOOL_EP = { list_dir: "/tools/list_dir", read_file: "/tools/read_file", write_file: "/tools/write_file", search: "/tools/search", edit_file: "/tools/edit_file", mkdir: "/tools/mkdir", delete: "/tools/delete", tree: "/tools/tree" };

  /* ----- folder picker ----- */
  const picker = $("picker"), pickerList = $("pickerList"), pickerPath = $("pickerPath"), agentWs = $("agentWs"), agentEmpty = $("agentEmpty");
  let pickerCwd = "", pickerParent = null;
  $("agentPick").addEventListener("click", () => { picker.hidden = false; browseTo(workspace || ""); });
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
      workspace = d.root; agentWs.textContent = d.root; picker.hidden = true;
      agentEmpty.textContent = "Locked to " + d.root + " — ask the agent to read or edit files here.";
      updateView(); refreshTree();
    } catch (e) { toast("Couldn't lock folder: " + (e.message || e)); }
  }

  // adopt an already-locked workspace (e.g. via --workspace) on load
  fetch(C.agentUrl + "/healthz").then((r) => r.json()).then((d) => {
    if (d && d.workspace) { workspace = d.workspace; agentWs.textContent = d.workspace; agentEmpty.textContent = "Locked to " + d.workspace + " — ask the agent to read or edit files here."; refreshTree(); }
  }).catch(() => { agentWs.textContent = "agent-tools offline (:8082)"; });

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

  function stopAgent() { if (agentAbort) agentAbort.abort(); }

  /* ----- agent rendering ----- */
  function agScroll() { conversationEl.scrollTop = conversationEl.scrollHeight; }
  function agUser(text) { const e = document.createElement("div"); e.className = "msg msg--user"; e.textContent = text; agentMessagesEl.appendChild(e); agScroll(); }
  function agAssistant() {
    const root = document.createElement("div"); root.className = "msg msg--assistant";
    const think = document.createElement("details"); think.className = "think"; think.open = true; think.hidden = true;
    think.innerHTML = '<summary>Thinking</summary><div class="think__body"></div>';
    const body = document.createElement("div"); body.className = "md is-streaming";
    const cur = document.createElement("span"); cur.className = "cursor";
    root.append(think, body, cur); agentMessagesEl.appendChild(root); agScroll();
    return {
      thinking(t) { think.hidden = false; think.querySelector(".think__body").textContent = t; agScroll(); },
      stream(t) { body.textContent = t; agScroll(); },
      finalize(t) { cur.remove(); body.classList.remove("is-streaming"); if (t) { body.innerHTML = mdRender(t); addCopy(body); } else { root.remove(); } think.open = false; },
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
        if (res && res.error) txt = "error: " + res.error;
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

  async function callTool(name, args) {
    const ep = TOOL_EP[name];
    if (!ep) return { error: "unknown tool: " + name };
    try {
      const r = await fetch(C.agentUrl + ep, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args || {}) });
      return await r.json();
    } catch (e) { return { error: "agent-tools unreachable: " + (e.message || e) }; }
  }

  const MUTATING = { write_file: 1, edit_file: 1, mkdir: 1, delete: 1 };
  function askConfirm(title, body) {
    return new Promise((resolve) => {
      const box = document.createElement("div"); box.className = "approve";
      box.innerHTML = '<div class="approve__head"></div><pre class="approve__body"></pre><div class="approve__btns"><button class="btn btn--ghost approve__no">Decline</button><button class="btn btn--primary approve__yes">Approve</button></div>';
      box.querySelector(".approve__head").textContent = title;
      const bodyEl = box.querySelector(".approve__body");
      if (body) { bodyEl.textContent = body.length > 4000 ? body.slice(0, 4000) + " …(truncated preview)" : body; } else { bodyEl.remove(); }
      agentMessagesEl.appendChild(box); agScroll();
      box.querySelector(".approve__yes").addEventListener("click", () => { box.remove(); resolve(true); });
      box.querySelector(".approve__no").addEventListener("click", () => { box.remove(); resolve(false); });
    });
  }
  async function approvalFor(name, args) {
    if (name === "write_file") {
      const cur = await callTool("read_file", { path: args.path });
      const exists = cur && typeof cur.content === "string";
      return { title: (exists ? "Overwrite " : "Create ") + args.path, body: args.content || "" };
    }
    if (name === "edit_file") {
      const cur = await callTool("read_file", { path: args.path });
      const old = cur && typeof cur.content === "string" ? cur.content : "";
      const hits = args.find ? old.split(args.find).length - 1 : 0;
      const next = args.find ? old.split(args.find).join(args.replace || "") : old;
      return { title: "Edit " + args.path + " (" + hits + " replacement" + (hits === 1 ? "" : "s") + ")", body: next };
    }
    if (name === "mkdir") return { title: "Create folder " + args.path, body: "" };
    if (name === "delete") return { title: "Delete " + args.path, body: "This permanently removes it from the workspace." };
    return { title: name, body: JSON.stringify(args) };
  }
  async function execToolCall(name, args, card) {
    if (MUTATING[name] && agentMode === "ask") {
      const a = await approvalFor(name, args);
      const ok = await askConfirm(a.title, a.body);
      if (!ok) { card.result({ error: "declined by user" }, "skip"); return { error: "user declined the " + name }; }
    }
    const res = await callTool(name, args);
    let display = res;
    if (!(res && res.error)) {
      if (name === "mkdir") display = { content: "📁 created " + (res.path || args.path) };
      else if (name === "delete") display = { content: "🗑 deleted " + (res.deleted || "") + " " + (res.path || args.path) };
    }
    card.result(display, res && res.error ? "error" : "ok");
    if (MUTATING[name] && !(res && res.error)) refreshTree();
    return res;
  }

  async function agentStreamTurn() {
    const ui = agAssistant();
    let content = "", reasoning = "", tcs = [];
    const res = await fetch(C.serverUrl + "/v1/chat/completions", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: agentAbort.signal,
      body: JSON.stringify({ model: C.model, stream: true, tools: TOOLS, tool_choice: "auto", messages: [{ role: "system", content: AGENT_SYSTEM }, ...agentMsgs], ...(thinkingOn ? {} : { thinking: { type: "disabled" } }) }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status} — ${(await res.text()).slice(0, 180)}`);
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n"); buf = lines.pop();
      for (const line of lines) {
        const s = line.trim(); if (!s.startsWith("data:")) continue;
        const data = s.slice(5).trim(); if (data === "[DONE]") continue;
        let j; try { j = JSON.parse(data); } catch { continue; }
        const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
        if (d.reasoning_content) { reasoning += d.reasoning_content; ui.thinking(reasoning); }
        if (d.content) { content += d.content; ui.stream(content); }
        if (d.tool_calls) for (const t of d.tool_calls) {
          const i = t.index || 0; tcs[i] = tcs[i] || { id: "", name: "", args: "" };
          if (t.id) tcs[i].id = t.id;
          if (t.function) { if (t.function.name) tcs[i].name = t.function.name; if (t.function.arguments) tcs[i].args += t.function.arguments; }
        }
      }
    }
    ui.finalize(content);
    return { content, toolCalls: tcs.filter(Boolean) };
  }

  async function runAgent(text) {
    if (!workspace) { toast("Choose a folder for the agent first."); $("agentPick").click(); return; }
    agentBusy = true; setSendStop(true); agentEmpty.hidden = true;
    agUser(text); agentMsgs.push({ role: "user", content: text });
    agentAbort = new AbortController();
    try {
      let guard = 0;
      while (guard++ < 25) {
        const turn = await agentStreamTurn();
        const asg = { role: "assistant", content: turn.content || "" };
        if (turn.toolCalls.length) asg.tool_calls = turn.toolCalls.map((tc) => ({ id: tc.id, type: "function", function: { name: tc.name, arguments: tc.args } }));
        agentMsgs.push(asg);
        if (!turn.toolCalls.length) break;
        for (const tc of turn.toolCalls) {
          let args = {}; try { args = JSON.parse(tc.args || "{}"); } catch { args = {}; }
          const card = agToolCard(tc.name, args);
          const res = await execToolCall(tc.name, args, card);
          agentMsgs.push({ role: "tool", tool_call_id: tc.id, content: JSON.stringify(res).slice(0, 100000) });
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") agError(String(e.message || e));
    } finally {
      agentBusy = false; setSendStop(false); agentAbort = null;
      saveAgent();
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

  /* ---------------- persistence (separate, saved per-mode conversations) ---------------- */
  const LS = { chat: "ds4:chatMsgs", agent: "ds4:agentMsgs", amode: "ds4:agentMode" };
  function saveChat() { try { localStorage.setItem(LS.chat, JSON.stringify(messages)); } catch (e) {} }
  function saveAgent() { try { localStorage.setItem(LS.agent, JSON.stringify(agentMsgs)); } catch (e) {} }

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
    for (const m of agentMsgs) if (m.role === "tool") { try { results[m.tool_call_id] = JSON.parse(m.content); } catch (e) { results[m.tool_call_id] = { error: "unparseable result" }; } }
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
    agentMode = localStorage.getItem(LS.amode) || "ask";
    amToggle.classList.toggle("is-auto", agentMode === "auto");
    amToggle.querySelectorAll(".seg2__btn").forEach((x) => x.classList.toggle("is-active", x.dataset.am === agentMode));
    thinkingOn = localStorage.getItem("ds4:thinking") !== "0";   // default on
    $("thinkToggle").classList.toggle("is-on", thinkingOn);
    renderChatHistory(); renderAgentHistory();
  }

  restoreConversations();
  updateView();
})();
