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

  // Context-window management — prevents HTTP 400 "context_length_exceeded" on long (esp. agent) sessions.
  // serverCtx is learned live from the sidecar; the rest tune how the conversation is trimmed to fit BEFORE
  // sending (and the frontend auto-retries harder if the server still 400s). Everything scales to the live ctx.
  contextReserveTokens: 2048,   // tokens held back for the model's reply
  contextSafety: 0.9,           // fraction of (ctx - reserve) the input may use (absorbs token-estimate error)
  contextRecentKeep: 6,         // newest messages never dropped (only stubbed as a last resort)
  contextStubChars: 800,        // size OLD tool outputs are trimmed to (head+tail) + a re-read hint
  agentToolOutputChars: "auto", // cap on tool output the MODEL sees per call (drives prefill cost). "auto" scales
                                //   to hardware — small on a slow iGPU, large on CUDA; set a number to pin it.
  toolPrefillTargetSec: 8,      // "auto" aims to keep one tool-output prefill under ~this many seconds on this box
  agentWriteEchoChars: "auto",  // separate, LARGER cap on write_file/edit_file content echoed back into context
                                //   (the model's own recent work product). "auto" = ~3× the output cap, ≥12k,
                                //   ≤30% of ctx — keeps typical files whole (fewer re-reads) while bounding the worst case.
  toolTimeoutMs: 30000,         // hard ceiling on a single tool call (file I/O is bounded, but a wedged backend or
                                //   pathological scan must not hang the agent loop). Also abortable mid-flight by Stop.

  // Thinking mode. The switch has 3 positions: "on" (always think), "off" (never), "auto" (a local heuristic
  // skips thinking on trivial turns — the headline feature). Auto is BALANCED + biased to think, because
  // under-thinking a hard task is the costly, unrecoverable error while over-thinking only wastes time.
  thinkDefault: "auto",         // initial switch position when nothing is saved
  thinkOnWords: ["why","how","explain","prove","derive","analyze","analyse","compare","debug","fix","refactor",
    "optimize","optimise","design","plan","implement","algorithm","complexity","edge case","step by step","reason",
    "architect","trade-off","tradeoff","root cause","investigate","diagnose","figure out","what's wrong","whats wrong"],
  thinkOffWords: ["hi","hello","hey","yo","sup","thanks","thank you","ok","okay","yes","yep","got it","cool",
    "rename","lowercase","uppercase","capitalize","format this","what time","what's the date","whats the date"],
  thinkShortWords: 6,           // auto: a prompt with this few words and no cue is treated as trivial -> skip thinking
  thinkSkipMaxWords: 14,        // auto: a skip-word only skips when the whole prompt is at most this many words
  diffLogMax: 200,              // turns of the difficulty log kept in localStorage (seeds future learned weighting)

  contextWarnPct: 0.80,         // agent gets a "wrap up" notice at this fill
  contextDangerPct: 0.92,       // agent gets a stronger "stop exploring" notice at this fill
  // maxOutputTokens: 2048,     // optional hard output cap (default: omit -> server default, auto-clamped to fit)
  serverCtx: 32768,             // conservative fallback (the small-box limit) used ONLY when the sidecar can't
                                //   report --ctx; the live sidecar value always wins, so the safety net never goes dark

  model:  "deepseek-v4-flash",
  quant:  "q2-imatrix",                  // your model variant — shown in the header
  hardware: "",                          // leave blank: GPU name + backend are auto-detected live by the sidecar

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
