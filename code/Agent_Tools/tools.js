// DS4 agent tool CONTRACT — the agent system prompt + a thin loader that fetches the model-facing tool
// catalog from the backend registry at runtime. Kept here (out of app.js) for clarity. Loaded before app.js.
//   app.js consumes   window.DS4_AGENT   (calls .load(agentUrl) once, then reads .TOOLS/.ENDPOINTS/.MUTATING)
//   the backend that REGISTERS + EXECUTES the tools is   ./agent_tools.py   (sandboxed file I/O on :8082)
//   each tool is a folder (spec.json + tool.py); the backend auto-discovers them and serves the catalog at
//   GET /tools, so adding a tool needs NO change here — to add one, follow   ./TOOL_TEMPLATE.md
window.DS4_AGENT = {

  // Prepended as the system message on every agent turn. It states the PROCEDURE + environment quirks
  // (not the tool catalog — the tools describe themselves). Keep it short: it is prefilled every turn.
  SYSTEM:
    "You are DS4, a coding agent working inside one locked project folder. Use the provided tools to " +
    "inspect and modify files. All paths are relative to the workspace root (use '.' for the root); you " +
    "cannot access anything outside it. Prefer edit_file for small changes; read or search before editing, " +
    "and give edit_file a 'find' that matches exactly one place (include surrounding lines) or set replace_all. " +
    "When writing a file, provide its full new contents. When done, briefly summarize your changes. " +
    "Context is limited: read large files in ranges with read_file offset/limit instead of whole, and note that " +
    "older tool outputs may be trimmed to fit - re-read the specific range you need. If you get an automatic " +
    "context notice, wrap up and summarize promptly.",

  // Populated by load() from the backend's GET /tools. Empty until then — app.js calls load() before the
  // first agent turn, so any earlier read sees harmless empties rather than a stale hard-coded list.
  TOOLS: [],            // OpenAI function defs sent to ds4 each turn as `tools`
  ENDPOINTS: {},        // tool name -> backend HTTP path (plus the built-in `tree`)
  MUTATING: {},         // tool name -> 1 for tools that change the workspace (gated in Ask mode)
  RISK: {},             // tool name -> risk level ("medium"/"high"); high-risk tools force approval even in Auto
  loaded: false,

  // Fetch the live tool contract from the backend registry. The backend (agent_tools.py) auto-discovers
  // each tool folder, so it is the single source of truth: a tool added there shows up here with no edit.
  // Fetched once and cached (the registry is fixed at backend startup); call load(url, true) to force a refresh.
  async load(agentUrl, force) {
    if (this.loaded && !force) return this.TOOLS;
    const base = String(agentUrl || "").replace(/\/+$/, "");
    const r = await fetch(base + "/tools", { cache: "no-store" });
    if (!r.ok) throw new Error("GET /tools -> " + r.status);
    const p = await r.json();
    const tools = Array.isArray(p.tools) ? p.tools : [];
    const endpoints = { tree: "/tools/tree" };   // built-in UI endpoint (not a model tool); per-tool paths below
    for (const t of tools) {
      const n = t && t.function && t.function.name;
      if (n) endpoints[n] = "/tools/" + n;
    }
    const mutating = {};
    for (const n of (p.mutating || [])) mutating[n] = 1;
    this.TOOLS = tools;
    this.ENDPOINTS = endpoints;
    this.MUTATING = mutating;
    this.RISK = (p && p.risk && typeof p.risk === "object") ? p.risk : {};   // {name: "high"|"medium"} for above-default tools
    this.loaded = true;
    return this.TOOLS;
  },
};
