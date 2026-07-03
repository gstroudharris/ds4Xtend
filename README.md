# ds4Xtend

A UIXtend-styled HTML frontend for **ds4-server** (antirez/ds4 · DwarfStar). Standalone repo —
it talks to ds4-server over HTTP, with no source dependency on ds4.

## Features

**Chat**
- Live **streaming** from ds4-server (`/v1/chat/completions`, SSE), with inline **thinking traces** above each reply
- **Markdown + code blocks** with one-click copy
- **Thinking switch — on / off / auto:** the "auto" heuristic skips thinking on trivial turns and thinks on hard
  ones (the headline feature); a per-turn difficulty log seeds future tuning
- **Six per-turn metrics** computed from the stream: TTFT, total, prefill, decode, prompt tokens, output tokens
- **Stop/abort** mid-stream, and **Send vs Loop** (re-run a prompt until you press Stop)
- **Automatic context-window management:** trims long conversations to fit `--ctx` before sending, learns the
  server's real tokenizer live, and auto-retries harder if the server still 400s; a context meter shows tokens left
- **Transient-error resilience:** retries 5xx / 429 / ROCm hiccups with abort-aware backoff instead of killing the run
- Server-status heartbeat, stick-to-bottom toggle, and quick-start suggestion cards
- **Light / dark theme** (UIXtend light-acrylic · dark-glass), remembered across sessions
- Conversation **logging** to `logs/` with an in-memory history cap

**Agent mode (sandboxed)**
- Turn any chat into an **agent loop** with a **workspace folder** you pick and lock (file-tree browser in the left rail)
- **Ask vs Auto:** Ask shows a diff and waits before each write; Auto applies writes immediately — always inside the locked folder
- **14 sandboxed tools** served by `agent_tools.py` (bound to `127.0.0.1`): `read_file`, `write_file`, `edit_file`,
  `delete`, `list_dir`, `mkdir`, `search`, `run_command`, `execute`, `list_processes`, `process_output`,
  `stop_process`, `web_search`, `web_scrape`
- **Keyless web search** (ddgs) and **clean HTML→markdown scraping** (trafilatura, SSRF-guarded)
- Project commands from `.ds4/commands.json` surfaced to the model; tool-output sizes auto-scaled to your hardware
- Context force-clear on long agent loops to avoid prefill thrash on slow boxes

**Live telemetry** (right rail, 2 Hz · `metrics_sidecar.py`)
- Real **GPU** stats via rocm-smi / nvidia-smi: utilization, peak, temp, power draw/limit, clock, VRAM
- **System RAM** and disk I/O rate
- **Model page-cache residency** ("model warm", via `mincore`) — how much of the model is hot in RAM
- Live backend readout: backend, context size, streaming / expert-cache mode

**Launcher & tooling**
- **One-command `ds4Xtend`** launcher: starts ds4-server + sidecar + UI, streams their logs, tears it all down on Ctrl+C
- **Auto-detects** AMD/ROCm vs NVIDIA/CUDA; optional per-box `ds4-server.sh` hook for custom flags
- `bench_thinking.py` — measures whether "auto" thinking actually saves time on your box

> **Not yet:** switching the ds4-server endpoint from the UI — set `serverUrl` in `code/config.js` for now.

## Prerequisites — set up ds4 first

ds4Xtend is **only a frontend**: it does not bundle, build, or download the model. It talks to a running
**ds4-server** over HTTP, so you must set up [antirez/ds4](https://github.com/antirez/ds4) **before** using ds4Xtend.

1. **Clone ds4.** The launcher asks for its path the first time and remembers it (under `~/.config/ds4xtend/`);
   a sibling `../ds4` is the default the manual commands below assume.
   ```bash
   git clone https://github.com/antirez/ds4   # e.g. next to this repo
   ```
2. **Build it for your GPU backend** (see ds4's own README): `make rocm` (AMD) or `make cuda-generic` (NVIDIA).
3. **Download the model** ds4 runs (e.g. `ds4flash.gguf`) into the ds4 dir, per ds4's instructions.

You also need **Python 3** (for the sidecar, static server, and Agent-mode tools) and a **Linux / POSIX** shell —
the launcher is a bash script and telemetry reads `rocm-smi` / `nvidia-smi`. Agent-mode web tools install into a
local venv on first use. ds4Xtend launches and auto-detects ds4-server for you; you only set ds4 up once.

## Run

**One command** (from the repo root):

```bash
./ds4Xtend      # prompts for the ds4 dir (remembered), launches everything, Ctrl+C stops it all
```

`ds4Xtend` starts ds4-server + the metrics sidecar + the web UI, streams their logs, and tears
everything down by name on Ctrl+C. Then open http://localhost:8090.

**Manual** (three processes) if you prefer:

```bash
# 1) ds4-server — in the ds4 checkout (CORS + port 8080). Normally ds4Xtend does this for you,
#    auto-detecting the backend. To run it by hand use YOUR box's flags (see the two example setups
#    below); the AMD/ROCm full-residency variant is shown here:
cd ../ds4   # the ds4 checkout (sibling of this repo)
HSA_OVERRIDE_GFX_VERSION=11.0.0 LD_LIBRARY_PATH=/opt/rocm/lib \
  ./ds4-server --rocm --ctx 32768 --cors --port 8080

# 2 + 3) sidecar (:8081) + static frontend (:8090)
code/run-frontend.sh
```

The "model warm" gauge defaults to the sibling `../ds4/ds4flash.gguf`; override with
`DS4_MODEL=/path/to/model.gguf`.

**Per-machine launch (optional).** `ds4Xtend` auto-detects the GPU backend (AMD/ROCm vs
NVIDIA/CUDA), so it works on either box with no config. To pin custom flags per machine, drop an
executable `ds4-server.sh` in the ds4 dir (forwarding `"$@"`) — `ds4Xtend` runs it instead,
appending only `--cors --port`; the script owns backend/env/model/ctx. See the `ds4Xtend` header
for the precedence ladder and the `DS4_SERVER_SCRIPT` / `DS4_NO_SERVER_SCRIPT` knobs.

**Two example `ds4-server.sh` setups.** Pick the one matching your hardware, save it as
`ds4-server.sh` in your ds4 dir, `chmod +x` it, then launch that box with `./ds4Xtend`. Both `cd`
to the script's own dir and forward `"$@"`, so they work whether `ds4Xtend` runs them or you run
them by hand.

*AMD APU / iGPU — full residency (fastest when the whole model fits in unified memory).* An APU like
the Radeon 780M (gfx1103) carves system RAM into a large GPU aperture, so the entire model stays
GPU-resident — no SSD streaming, ~2× the decode. `HSA_OVERRIDE_GFX_VERSION` is required on gfx1103
(rocBLAS ships no native kernels for it), and the context is capped so the KV cache can't evict the
resident model:

```bash
#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
exec env HSA_OVERRIDE_GFX_VERSION=11.0.0 LD_LIBRARY_PATH=/opt/rocm/lib \
  ./ds4-server --rocm --ctx 32768 "$@"
```

*NVIDIA discrete GPU — SSD streaming (best when the model is larger than VRAM).* Experts stream from
the SSD on demand, so the resident footprint is small and a much larger context fits:

```bash
#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
exec env DS4_CUDA_NO_DIRECT_IO=1 DS4_CUDA_KEEP_MODEL_PAGES=1 LD_LIBRARY_PATH=/usr/local/cuda/lib64 \
  ./ds4-server --cuda --ssd-streaming --ctx 100000 "$@"
```

On either box, launch the whole stack with `./ds4Xtend`, then open http://localhost:8090.

**Setting up a second machine (known gaps).** Per-box config is machine-local and never committed —
do it once per box: the ds4 dir is remembered under `~/.config/ds4xtend/`, and any `ds4-server.sh`
lives in (and is ignored from) your ds4 checkout. Auto-detect sniffs the *GPU*, not its *libraries* or
*build*, so if ds4-server won't start: point `DS4_LD_PATH` at your CUDA/ROCm libs (defaults
`/usr/local/cuda/lib64` and `/opt/rocm/lib`), and make sure ds4-server was built for that backend
(`make cuda-generic` / `make rocm` in ds4). The AMD/ROCm full-residency path is tuned on a 780M; the
NVIDIA/CUDA path uses ds4's documented streaming defaults — pin a `ds4-server.sh` to adjust either.

**Keep your ds4 checkout clean.** This frontend needs **nothing committed to ds4** — a fresh
`git clone` of upstream `antirez/ds4` (built + model downloaded as usual) just works, and
`ds4-server.sh` is optional. So don't add your local files to ds4's *tracked* `.gitignore` (that
diverges your clone from upstream). Ignore them locally instead, in **`ds4/.git/info/exclude`**
(git's per-repo, never-committed ignore file):

```
/ds4-server.sh        # the optional per-box launch hook above
/run.sh               # plus any other local helpers/notes you keep in ds4
```

## Layout / what each file does
- `ds4Xtend` — one-command launcher for the whole stack (repo root)
- `code/index.html` · `styles.css` · `app.js` — the SPA (markup / COSMIC theme / chat + telemetry)
- `code/config.js` — server & sidecar URLs, suggestion cards
- `code/Agent_Tools/` — Agent-mode file tools: `tools.js` (the model-facing tool contract + system prompt, consumed by app.js), `agent_tools.py` (the sandboxed executor on :8082), `TOOL_TEMPLATE.md` (how to add a tool + best practices)
- `code/metrics_sidecar.py` — GPU/RAM/disk/model-residency JSON on :8081
- `code/run-frontend.sh` — sidecar + static server (not ds4-server)
- `code/bench_thinking.py` — measures whether "Auto" thinking-mode saves time on this box (think on/off savings + the cost of switching); run against a live ds4-server, esp. on the slow box
- `docs/LICENSING.md` — licensing guide: what GPLv3 means here, third-party components, and the SPDX header convention for new files

## Known issues

### AMD 780M / gfx1103 (ROCm): GPU MES hang → ds4-server crash

On the Radeon 780M (Phoenix, gfx1103) — and other Phoenix/Hawk-Point APUs — heavy ROCm prefill can
wedge the GPU's **MES** (command scheduler) firmware. Under memory pressure the kernel does an SVM
invalidation → queue eviction → `MES REMOVE_QUEUE` never completes → the amdgpu driver force-resets the
GPU (MODE2). The reset destroys ds4-server's compute context, so ds4-server dies with `unspecified launch
failure` (HIP 719) mid-prefill. This is a known, **still-open upstream AMD/kernel bug** — not a ds4Xtend
or ds4 bug; gfx1103 is unofficial ROCm (hence the required `HSA_OVERRIDE_GFX_VERSION=11.0.0`):
[ROCm#6386](https://github.com/ROCm/ROCm/issues/6386) ·
[ROCm#6273](https://github.com/ROCm/ROCm/issues/6273) ·
[ROCm#4444](https://github.com/ROCm/ROCm/issues/4444) ·
[Ubuntu#2147367](https://www.mail-archive.com/ubuntu-bugs@lists.ubuntu.com/msg6259954.html).

**Symptoms:** a run/Loop stalls; the dashboard shows `GPU busy — letting it reset…`, a connection error,
or `ds4-server … restart it`. The desktop usually survives (the driver recovers the GPU), but the compute
context is gone.

**Verify it was a GPU reset** (`dmesg` is often root-restricted; the systemd journal is not):

```bash
journalctl -k -b 0 | grep -iE 'amdgpu.*(reset|wedged)|MES .*unrecoverable'
grep -iE 'launch failure|synchronize failed' /tmp/ds4xtend.*/ds4-server.log
cat /sys/class/drm/card0/device/devcoredump/data   # driver crash dump; exists for ~5 min after a reset
```

**What ds4Xtend does about it (automatic):**
- Recoverable `rocm prefill state reset failed` 503s → a patient GPU cooldown retry (≈2→4→8→12 s, up to
  `gpuStateRetries`, default 6) instead of hammering the still-resetting GPU.
- ds4-server killed outright → the launcher **auto-restarts** it (bounded by `DS4_MAX_SERVER_RESTARTS`,
  default 5, with a cooldown) and the dashboard **waits and resumes** the agent Loop (up to
  `serverDownWaitMs`, default 120 s), instead of dying with a cryptic error.

**Reduce how often it fires** (the trigger is memory pressure / SVM churn):
- Prefer a **newer kernel + `linux-firmware`** (newer amdgpu driver + MES blobs): `sudo apt full-upgrade`,
  then reboot. Reversible — old kernels stay in GRUB, so boot the previous one if a new one regresses.
- **Lower `--ctx`** (smaller KV cache → less GTT/VRAM pressure). The frontend already tightens context on
  ROCm; go smaller if it still crashes. Full residency is fastest but stresses SVM hardest.
- **Don't bump ROCm to fix this** — the bug is in the kernel (KFD/SVM/MES), not the ROCm runtime. A ROCm
  version change won't help and would force a ds4-server rebuild (gfx1103 is unofficial).

## License

Copyright (C) 2026 Grant Harris

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the [LICENSE](LICENSE) file for details.

Every source file carries an SPDX header (`SPDX-License-Identifier: GPL-3.0-or-later`); see [docs/LICENSING.md](docs/LICENSING.md) for the licensing guide and the header convention for new files.

## Attribution

The visual theme — blue/white palette, square radii, acrylic/glass surfaces, and the bold "X" logo treatment — is adapted from [UIXtend](https://github.com/gstroudharris/UIXtend) © Grant Harris, GPLv3.

Web tools build on [ddgs](https://github.com/deedy5/ddgs) (MIT) for keyless web search and [trafilatura](https://github.com/adbar/trafilatura) (Apache-2.0) for HTML→text extraction. Both are GPLv3-compatible and installed at runtime into a local venv — not bundled or redistributed here.

Talks to **ds4-server** ([antirez/ds4](https://github.com/antirez/ds4) · DeepSeek V4 Flash inference) over HTTP — a separate component, not bundled.

Project and dashboard inspiration from **Prompt Engineering** and his [YouTube video](https://www.youtube.com/watch?v=9gHcmhUDJfw) — thank you.

Vibe coding assistance from [Claude](https://claude.ai) by Anthropic.
