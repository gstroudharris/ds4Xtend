# DS4 Web Frontend — Specification & Plan

A single-page HTML frontend for chatting with **ds4-server**, styled for **Pop!_OS
COSMIC**, recreating the reference UI (DGX Spark demo build) and adapting its
telemetry to *this* machine. Talks to `ds4-server` on **`localhost:8080`**.

> Status: **Phases 1–3 implemented and live** (see README). Now a **standalone repo**
> (`ds4Xtend`), kept separate from the upstream antirez/ds4 checkout it talks to over
> HTTP. The reference screenshot was a DGX Spark (GB10, unified memory); §7.6 explains how
> we adapt its memory panel to our discrete-GPU + system-RAM architecture.

---

## 1. Goals

1. Recreate **every feature visible in the reference image** (§7, coverage table in §7.0).
2. Look native on **Pop!_OS COSMIC** — dark, warm, rounded, periwinkle accent (§5).
3. Stream chat from `ds4-server` at `localhost:8080`, with **thinking traces** shown above each reply.
4. Show **live GPU + memory telemetry** — adapted to RTX 3090 (24 GB VRAM) + 128 GB system RAM.
5. Fold in the levers we discovered (RAM warmth, expert-stream I/O, thinking/sampling controls) — §10.
6. **No build step.** Plain HTML/CSS/JS, runnable by opening a file or a one-line static server.

## 2. Target hardware vs. the reference image

| | Reference image (DGX Spark) | **This machine** |
|---|---|---|
| GPU | NVIDIA GB10 | **NVIDIA RTX 3090**, 24 GB VRAM, 370 W cap |
| Memory model | 128 GB **unified** (one bar: "Used 107 / 119.7 GB") | **Split**: 24 GB VRAM **+** ~126 GB system RAM |
| Backend label | "q2-imatrix · DGX Spark GB10" | **"q2-imatrix · RTX 3090 · CUDA, SSD-streaming"** |
| Memory panel | single "UNIFIED MEMORY" bar | **two bars: VRAM + System RAM (model cache)** (§7.6) |
| Speed | ~13.75 t/s (model fully GPU-resident) | ~10 t/s warm / ~3.7 cold (experts stream over PCIe) |

Live snapshot used for example values below: 3090, 24576 MiB VRAM, 370 W cap;
RAM 126.3 GB total / ~93 GB cached; model 80.76 GiB (`ds4flash.gguf`).

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Browser SPA (index.html + app.js + styles.css)               │
│  ├─ Chat/streaming  ──HTTP/SSE──►  ds4-server   :8080  (--cors)│
│  │     POST /v1/chat/completions (stream), GET /v1/models      │
│  └─ Telemetry poll  ──HTTP────────►  metrics-sidecar :8081     │
│        GET /metrics @ 2 Hz   (nvidia-smi + /proc/* as JSON)     │
└──────────────────────────────────────────────────────────────┘
```

**Two backends.** The browser cannot run `nvidia-smi` (sandbox), and `ds4-server`
exposes **only** inference endpoints (`/v1/chat/completions`, `/v1/responses`,
`/v1/completions`, `/v1/messages`, `/v1/models`) — **no GPU/RAM metrics**. So the
GPU/memory panel requires a tiny local **metrics sidecar** (§9). This is the one
non-obvious architectural requirement.

- **Inference** → `ds4-server` on `:8080`. Must be launched with **`--cors`** (the
  SPA is a different origin) and **`--port 8080`**.
- **Telemetry** → metrics sidecar on `:8081` (configurable), polled at 2 Hz.
- **Serving the SPA**: `python3 -m http.server 8090` in `frontend/`, or open
  `index.html` as `file://`. Either way it's cross-origin to both backends, so both
  must send permissive CORS headers (ds4-server `--cors`; sidecar built-in).
- Both backends bind **127.0.0.1 only** (local-only; see §9 security).

## 4. Tech stack & constraints

- **Vanilla HTML/CSS/JS**, ES modules, no framework, no bundler.
- **Vendored** (not CDN — this box runs local/offline-friendly): `marked` (markdown),
  `highlight.js` (code), `morphdom` optional. Tiny; checked into `frontend/vendor/`.
- Streaming via **`fetch()` + `ReadableStream`** (not `EventSource`, which is GET-only;
  chat is POST). Manual SSE line parsing (`data:` / `[DONE]`).
- Charts (the 60 s utilization sparkline) drawn on a **`<canvas>`** — no chart lib.
- State in-memory + **`localStorage`** for conversation history and settings.
- Config (server URL, sidecar URL, poll rate, model alias) in a small settings panel,
  persisted to `localStorage`, defaults: `http://localhost:8080`, `http://localhost:8081`, 2 Hz.

---

## 5. Pop!_OS COSMIC design system

Dark, warm, rounded, layered surfaces, periwinkle accent — matching the image.
All tokens as CSS custom properties so the accent is swappable.

```css
:root {
  /* surfaces (near-black, slightly warm) */
  --bg:            #0E0E11;   /* app background */
  --bg-vignette:   radial-gradient(120% 80% at 50% -10%, #2a0f14 0%, transparent 55%); /* warm maroon glow, like the image */
  --surface:       #16161A;   /* panels */
  --surface-2:     #1E1E24;   /* raised tiles / cards */
  --surface-3:     #26262E;   /* hover */
  --border:        rgba(255,255,255,0.07);
  --divider:       rgba(255,255,255,0.05);
  /* text */
  --text:          #ECECEF;
  --text-dim:      #9A9AA6;
  --text-faint:    #6B6B76;
  /* accent (COSMIC periwinkle, matches the DS badge + Send button) */
  --accent:        #7C8CFF;
  --accent-hover:  #8E9CFF;
  --accent-press:  #6675F0;
  --on-accent:     #0E0E11;
  /* semantic */
  --online:        #4ADE80;   /* server-online dot */
  --warn:          #F0A35E;   /* memory bar fill (orange, like the image) */
  --danger:        #F26D6D;
  /* radii / spacing */
  --r-card: 14px; --r-tile: 12px; --r-btn: 10px; --r-pill: 999px;
  --gap: 12px; --pad: 16px;
}
```

- **Typography:** `Fira Sans` (Pop!_OS signature) → fallback `Inter, system-ui, sans-serif`.
  Numbers/metrics/code in `Fira Mono` → fallback `"JetBrains Mono", ui-monospace, monospace`.
  Section labels (THIS TURN, GPU, etc.): uppercase, letter-spaced, `--text-faint`, ~11px.
- **Components:** segmented control (Chat/Agent) = rounded pill track with sliding
  accent thumb; status pill = rounded with colored dot; metric tiles = `--surface-2`
  with hairline `--border`, `--r-tile`; suggestion cards = `--surface-2`, hover lifts to
  `--surface-3` + accent border; primary button = `--accent` fill; secondary = ghost/outline.
- **Motion:** 120–180 ms ease for hovers, thumb slide, message fade-in; cursor "blink" on
  streaming; respect `prefers-reduced-motion`.
- **Layout chrome:** thin scrollbars; focus rings in `--accent`; subtle inner glow on the
  app frame edges (the image's vignette) via `--bg-vignette` overlay.

---

## 6. Layout (wireframe)

```
┌───────────────────────────────────────────────┬───────────────────────────┐
│ [DS] DeepSeek V4 Flash        ( Chat | Agent )  │ THIS TURN                 │
│      q2-imatrix · RTX 3090       ● ds4 online   │ ┌TTFT──┐ ┌TOTAL─┐          │
├───────────────────────────────────────────────┤ ┌PREFILL┐┌DECODE┐          │
│                                                 │ ┌PROMPT┐ ┌OUTPUT┐         │
│              Start a conversation               │                           │
│   Streaming from ds4-server on localhost:8080   │ GPU · NVIDIA RTX 3090     │
│   · thinking traces shown above each reply      │ Utilization        41 %   │
│                                                 │ [▁▂▃▅▂▁ sparkline 60s]    │
│   ┌ EXPLAIN ─────┐  ┌ CODE ─────────┐           │ ┌TEMP─┐ ┌POWER─┐          │
│   │ Unified mem… │  │ Reverse words…│           │ ┌SMCLK┐ ┌SAMPLE┐          │
│   └──────────────┘  └───────────────┘           │                           │
│   ┌ DISCUSS ─────┐  ┌ PLAN ─────────┐           │ VRAM         15.5/24 GB   │
│   │ V4 vs V3     │  │ Benchmark…    │           │ [████████░░░░░░░]         │
│   └──────────────┘  └───────────────┘           │ SYSTEM RAM   93/126 GB    │
│                                                 │ [██████████░░] model warm │
│ ┌─────────────────────────────────────────────┐│                           │
│ │ Message DS4…            [ Clear ] [ Send ]   ││ ▶ BACKEND                 │
│ └─────────────────────────────────────────────┘│                           │
│   Enter to send · Shift+Enter for newline       │                           │
└───────────────────────────────────────────────┴───────────────────────────┘
```

Right rail is fixed-width (~340 px), independently scrollable; main column flexes;
responsive: telemetry collapses into a drawer below ~1100 px wide.

---

## 7. Core features (everything in the image)

### 7.0 Image-element coverage checklist

| # | Image element | Feature | Data source |
|---|---|---|---|
| 1 | "DS" badge + "DeepSeek V4 Flash" + "q2-imatrix · DGX Spark GB10" | Header identity | `GET /v1/models` + sidecar `gpu.name` |
| 2 | "Chat \| Agent" segmented toggle | Mode switch (§7.1) | client |
| 3 | "● ds4-server online" pill | Server health | `GET /v1/models` heartbeat |
| 4 | "Start a conversation" + subtitle | Empty state | client |
| 5 | 4 suggestion cards (EXPLAIN/CODE/DISCUSS/PLAN) | Quick prompts | client config |
| 6 | Message box + Clear + Send + key hint | Composer (§7.3) | client |
| 7 | THIS TURN: TTFT, TOTAL, PREFILL, DECODE, PROMPT/OUTPUT TOKENS | Per-turn metrics (§7.4) | client timing + `usage` |
| 8 | GPU util + "last 60 s" sparkline + "peak %" | GPU util chart (§7.5) | sidecar |
| 9 | TEMP, POWER, SM CLOCK, SAMPLE (2 Hz) | GPU tiles (§7.5) | sidecar |
| 10 | "UNIFIED MEMORY · Used 107/119.7 GB" bar | → **VRAM + System RAM bars** (§7.6) | sidecar |
| 11 | "▶ BACKEND" collapsible | Backend details (§7.7) | `/v1/models` + sidecar |

### 7.1 Header
- **Identity:** model display name from `GET /v1/models` (alias `deepseek-v4-flash`);
  subtitle = quant (`q2-imatrix`, static/config) + backend string from sidecar
  (`NVIDIA RTX 3090`) + "CUDA · SSD-streaming".
- **Chat / Agent toggle:** segmented control. **Chat** = `/v1/chat/completions`
  (fully supported). **Agent** = **display-only tool-call preview** (decided): the tab
  shows an illustrative turn with rendered tool-call cards and a banner stating they are
  *shown, not executed* — a deliberate reminder that wiring a sandboxed tool executor is
  planned future work. ds4-server already supports `tools`/`tool_choice`, so the later
  upgrade path is to execute these via the sidecar.
- **Status pill:** green dot + "ds4-server online" when the heartbeat (`GET /v1/models`
  every ~5 s) succeeds; amber "connecting…"; red "offline" with retry.

### 7.2 Conversation area
- **Empty state:** "Start a conversation" + "Streaming from ds4-server on
  localhost:8080 · thinking traces shown above each reply" + the 4 suggestion cards.
- **Suggestion cards:** label (EXPLAIN/CODE/DISCUSS/PLAN) + prompt; click → fill composer
  and send. Config-driven array so they're easy to edit.
- **Messages:** user (right/neutral) and assistant (left) bubbles; **markdown rendering**
  (headings, lists, tables), **fenced code with syntax highlight + copy button**.
- **Thinking traces (core):** DeepSeek V4 streams reasoning separately. Render a
  collapsible **"Thinking"** block *above* each assistant reply (matches the image's
  subtitle), auto-expanded while streaming, collapsible when done. Parse
  `delta.reasoning_content` (OpenAI shape) — or use `/v1/messages` thinking blocks.
- **Streaming:** token-by-token append with a blinking cursor; smooth autoscroll
  (pause autoscroll if the user scrolls up).
- **Per-message actions:** copy, regenerate (last), and a small stats line (t/s, tokens).

### 7.3 Composer
- Multiline textarea, placeholder "Message DS4… (Enter to send, Shift+Enter for newline)".
- **Enter** = send, **Shift+Enter** = newline (hint line below, as in image).
- **Send** (accent) and **Clear** (ghost) buttons. While streaming, Send becomes
  **Stop** (aborts the fetch via `AbortController`).
- Disabled with tooltip when server offline.

### 7.4 "This turn" metrics (the 6 tiles) — derivation

Measured client-side from stream timestamps + the final `usage` chunk (request with
`stream_options:{include_usage:true}`):

| Tile | Computation |
|---|---|
| **TTFT** | `t(first token) − t(request sent)` |
| **TOTAL** | `t(done) − t(request sent)` |
| **PROMPT TOKENS** | `usage.prompt_tokens` |
| **OUTPUT TOKENS** | `usage.completion_tokens` |
| **PREFILL** (t/s) | `prompt_tokens / TTFT` (prompt processed before first token) |
| **DECODE** (t/s) | `completion_tokens / (TOTAL − TTFT)` |

These mirror ds4's own `prefill: X t/s, generation: Y t/s` line. Show "—" before the
first turn (as in image). Keep a small **history** of recent turns (§10).

### 7.5 GPU telemetry (from sidecar @ 2 Hz)
- **Utilization** big % + **60 s sparkline** (`<canvas>`, ring buffer of 120 samples) +
  **peak %** label. Matches "util · last 60 s / peak X %".
- Tiles: **TEMP** (°C), **POWER** (W; show `draw / limit`, e.g. `45 / 370 W` — richer than
  the image's single value), **SM CLOCK** (MHz), **SAMPLE** (poll rate, default 2 Hz; settable).

### 7.6 Memory — **adapted from "Unified Memory" to two bars**

The image shows one unified bar because the DGX shares one pool. We have two physical
pools, and (per our tuning work) **system RAM is the performance-critical one** — the
81 GB model lives in the page cache so experts stream from RAM, not NVMe. So:

- **VRAM bar:** `used / 24576 MB` (e.g. `15.5 / 24 GB`), accent fill.
- **System RAM bar:** `(total − available) / total` (e.g. `93 / 126 GB`), **orange**
  (`--warn`) fill like the image, with a **"model warm: 81/81 GB ✓"** sublabel derived
  from page-cache residency (§9). This bar *is* the 2.7× lever, made visible.

### 7.7 Backend (collapsible "▶ BACKEND")
Expands to: model file + quant, ctx size, backend (`cuda`), streaming mode + expert-cache
size, server version/endpoints from `/v1/models`, sidecar status, and the active
env tuning (`DS4_CUDA_NO_DIRECT_IO`, `KEEP_MODEL_PAGES`) if the sidecar reports them.

---

## 8. ds4-server API integration

- **Base URL:** `http://localhost:8080` (configurable). Launch server with
  `--cors --port 8080` (see §13).
- **Health/identity:** `GET /v1/models` → model list (`deepseek-v4-flash`, `deepseek-v4-pro`).
- **Chat:** `POST /v1/chat/completions`, body:
  ```json
  { "model":"deepseek-v4-flash", "stream":true,
    "stream_options":{"include_usage":true},
    "messages":[...], "max_tokens":N, "temperature":T, "top_p":P,
    "thinking":{"type":"enabled"|"disabled"} }
  ```
- **SSE parse:** read `ReadableStream`, split on `\n\n`, strip `data: `, stop on `[DONE]`;
  accumulate `choices[0].delta.content` (answer) and `delta.reasoning_content` (thinking);
  capture `usage` from the final chunk.
- **Thinking control:** map the UI thinking selector (Off / Think / Think-Max) to
  `thinking:{type:"disabled"}` / default / `reasoning_effort:"max"`. Note: **Think-Max needs
  `--ctx ≥ 393216`** (impractical on 24 GB VRAM — disable that option or warn). In thinking
  mode the server **ignores client sampling** (mirror that in the UI by greying temp/top-p).
- **Abort:** `AbortController` on Stop.
- **Alt endpoints (stretch):** `/v1/messages` (Anthropic; cleaner thinking blocks) and
  `/v1/responses` (Codex-style) selectable in settings for testing all of ds4's APIs.

---

## 9. Metrics sidecar (`metrics_sidecar.py`)

A ~60–80 line `python3` `http.server` (stdlib only; `nvidia-smi` already present).

- **`GET /metrics`** → JSON (one sample). **`GET /healthz`**. Optional **`GET /metrics/stream`** (SSE push).
- Sends `Access-Control-Allow-Origin: *`; binds **127.0.0.1** only.
- Sources: `nvidia-smi --query-gpu=name,utilization.gpu,temperature.gpu,power.draw,
  power.limit,clocks.sm,memory.used,memory.total --format=csv,noheader,nounits`;
  `/proc/meminfo` (MemTotal/MemAvailable/Cached); `/proc/diskstats` delta for the model's
  block device → `disk.read_mb_s`.
- **Model RAM-residency** (the "warm" sublabel): exact value needs `mincore(2)` over the
  mmap'd model (small `ctypes` helper) — `fincore`/`vmtouch` are **not installed**. v1 may
  approximate from `Cached`; v2 adds the `mincore` helper for an exact "81/81 GB warm".

```json
{ "ts":1750000000.0,
  "gpu":{"name":"NVIDIA GeForce RTX 3090","util_pct":41,"temp_c":38,
         "power_w":45.3,"power_limit_w":370,"sm_clock_mhz":1020,
         "vram_used_mb":1008,"vram_total_mb":24576},
  "ram":{"total_mb":126343,"available_mb":109563,"cached_mb":92988},
  "model":{"path":"ds4flash.gguf","size_mb":82703,"resident_mb":81000,"warm_pct":98},
  "disk":{"read_mb_s":0.0} }
```

- **Optional `POST /warm`** → runs `cat ds4flash.gguf > /dev/null` to warm the page cache
  (drives a "Warm cache" button, §10). Local-only; document the shell-exec risk.

**Security:** both backends 127.0.0.1-bound; the sidecar shells out (`nvidia-smi`, optional
`cat`) — never expose it off-host; `/warm` should be opt-in (flag).

---

## 10. Value-add features (driven by our conversation)

| Feature | Why (from our work) | Source |
|---|---|---|
| **Model-in-RAM "warm" indicator** | The 81 GB page-cache residency *is* the 2.7× lever (3.74→9.97 t/s) | sidecar `model.warm_pct` |
| **"Warm cache" button** | After reboot the cache is cold; re-warm without a terminal | sidecar `POST /warm` |
| **Expert-stream disk I/O meter** | High NVMe read during decode = cold cache (experts from disk, the bottleneck) | sidecar `disk.read_mb_s` |
| **Cold/warm t/s context** | Show decode t/s against the ~10 (warm) / ~3.7 (cold) reference | client + history |
| **Thinking selector (Off/Think/Think-Max)** | Core DeepSeek V4 control; Think-Max gated by ctx | API `thinking`/`reasoning_effort` |
| **Sampling controls** (temp, top-p, max tokens) | Exposed but auto-greyed in thinking mode (server ignores them) | API |
| **Context/KV usage meter** | Large ctx eats VRAM on a 24 GB card; watch headroom | client tokens vs ctx |
| **Endpoint switcher** (chat/messages/responses) | Exercise all of ds4's APIs | settings |
| **Stop / Regenerate / Export (md+json)** | Standard chat ergonomics | client |
| **Conversation history** (localStorage) | Persist sessions client-side | client |

**Explicitly excluded / noted:**
- **No MTP toggle** — `--mtp` is incompatible with `--ssd-streaming` (mandatory here); unusable.
- **`--power` is launch-time only** (no runtime API) — surface current power in telemetry,
  but document power-capping as a server launch flag, not a UI control.

---

## 11. Priority matrix

- **Core (P0 — matches the image, ship first):** header identity + status, Chat streaming,
  thinking traces, suggestion cards, composer + key handling, the 6 THIS-TURN tiles, GPU
  tiles + sparkline, VRAM + RAM bars, backend panel, metrics sidecar, markdown/code render.
- **Nice-to-have (P1):** thinking/sampling controls, stop/regenerate, model-warm indicator +
  warm button, disk-I/O meter, ctx/KV meter, conversation history, export.
- **Stretch (P2):** Agent mode *execution* (sandboxed tool executor — the display-only
  preview ships in P1), endpoint switcher, SSE telemetry push, exact `mincore` residency,
  multi-conversation tabs, light theme + COSMIC accent picker.

## 12. Deliverables / file structure

```
frontend/
  frontend_specs.md        # this document
  index.html               # markup + right-rail structure
  styles.css               # COSMIC theme (§5 tokens)
  app.js                   # chat/SSE, telemetry poll, metrics math, rendering
  config.js                # server/sidecar URLs, suggestion cards, defaults
  metrics_sidecar.py       # GPU/RAM/disk JSON on :8081 (§9)
  vendor/                  # marked.min.js, highlight.min.js (+css)
  run-frontend.sh          # launches sidecar + static server, prints URLs
  README.md                # quickstart
```

## 13. Launch / run (target workflow)

```bash
# 1) ds4-server on :8080 with CORS, RAM-optimized (see ../run.sh rationale)
cd ../ds4   # the ds4 checkout (sibling of this repo)
DS4_CUDA_NO_DIRECT_IO=1 DS4_CUDA_KEEP_MODEL_PAGES=1 \
  ./ds4-server --cuda --ssd-streaming --ctx 100000 --cors --port 8080 \
  --kv-disk-dir ~/.ds4/server-kv --kv-disk-space-mb 8192
# (warm once: cat ds4flash.gguf > /dev/null)
# AMD / ROCm: use --rocm + HSA_OVERRIDE_GFX_VERSION=11.0.0; full residency needs --ctx 32768 (100000
# OOMs). In practice just run ../ds4Xtend — it auto-detects backend + ctx, or runs your ds4-server.sh.

# 2) metrics sidecar on :8081
python3 metrics_sidecar.py --port 8081   # from this repo

# 3) serve the SPA
python3 -m http.server 8090   # repo root → http://localhost:8090
```
`run-frontend.sh` will wrap steps 2–3.

## 14. Implementation phases

1. **Shell & theme** — static `index.html` + `styles.css` with the COSMIC tokens, full
   layout (header, empty state, cards, composer, right rail with placeholder "—" tiles).
2. **Chat MVP** — `/v1/chat/completions` streaming, markdown/code, thinking traces, status
   heartbeat, composer key handling, suggestion cards, THIS-TURN metrics.
3. **Sidecar + telemetry** — `metrics_sidecar.py`, 2 Hz poll, GPU tiles + sparkline, VRAM +
   RAM bars, backend panel.
4. **Conversation value-adds** — thinking/sampling controls, stop/regenerate, model-warm +
   warm button, disk-I/O meter, history/export.
5. **Polish & stretch** — responsive drawer, reduced-motion, Agent mode, endpoint switcher,
   exact `mincore` residency.

## 15. Open questions / assumptions

- **Assumed defaults** (proceed unless told otherwise): single-page vanilla JS, vendored libs
  (offline), sidecar on `:8081`, SPA on `:8090`, poll 2 Hz, suggestion cards copied from the
  image.
- **Agent mode**: **decided — display-only tool-call preview** in P1 (reminder of planned
  work); real sandboxed execution is deferred to P2.
- **Light theme / COSMIC accent picker**: P2 — the browser can't read the user's chosen
  COSMIC accent, so we'd ship a fixed periwinkle with an optional picker.
- **`--ctx` for the server**: 100000 assumed; affects VRAM headroom and Think-Max availability.
