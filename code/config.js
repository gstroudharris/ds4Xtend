// DS4 frontend configuration.
// Plain global (no ES module) so the app also works opened directly as file://.
// Phase 2+ reads these for live wiring; Phase 1 uses `demo` for static styling.
window.DS4_CONFIG = {
  serverUrl:  "http://localhost:8080",   // ds4-server — launch with: --cors --port 8080
  sidecarUrl: "http://localhost:8081",   // metrics sidecar (added in Phase 3)
  agentUrl:   "http://localhost:8082",   // sandboxed agent file-tools (Agent mode)
  pollHz: 2,                             // telemetry sample rate

  // Conversation logging (sidecar writes <repo>/logs) + in-memory history cap. Tune freely.
  logging: {
    enabled: true,
    maxHistoryChars: 4000000,            // per conversation kept in RAM (~8 MB UTF-16); older messages are
                                         // appended to the log file and dropped from memory past this.
  },
  model:  "deepseek-v4-flash",
  quant:  "q2-imatrix",                  // your model variant — shown in the header
  hardware: "",                          // leave blank: GPU name + backend are auto-detected live by the sidecar

  // Context window (the --ctx the server launched with). The sidecar reports it live for the headroom
  // meter; ds4Service only knows it on the built-in launch path. If you use a per-box ds4-server.sh that
  // owns --ctx, the sidecar can't report it — set serverCtx here so the meter still works.
  serverCtx: null,                       // fallback ctx (tokens) when the sidecar doesn't report one
  contextWarnPct: 0.8,                   // meter turns orange at/above this fraction of ctx
  contextDangerPct: 0.92,                // meter turns red at/above this fraction (start a fresh run soon)
  maxOutputTokens: null,                 // optional cap on reply length; always clamped to remaining ctx

  // Quick-start prompt cards (from the reference image).
  suggestions: [
    { tag: "EXPLAIN", text: "Unified memory in three sentences" },
    { tag: "CODE",    text: "Reverse words, keep characters" },
    { tag: "DISCUSS", text: "DeepSeek V4 Flash vs V3" },
    { tag: "PLAN",    text: "Benchmark a long-context LLM" },
  ],

  // Phase-1 placeholder telemetry so the right rail reads correctly before the
  // sidecar exists. Replaced by live rocm-smi/nvidia-smi/proc data in Phase 3.
  demo: {
    gpuName: "GPU",
    util: 41, peak: 62, temp: 38, powerDraw: 45, powerLimit: 370, smClock: 1920,
    vramUsed: 15.5, vramTotal: 24,
    ramUsed: 93, ramTotal: 126, modelWarm: 81, modelSize: 81,
    backend: { backend: "auto", ctx: 32768, streaming: "auto",
               expertCache: "auto", noDirectIO: false, keepPages: false },
  },
};
