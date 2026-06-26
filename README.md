# DS4 Web Frontend

A COSMIC-styled HTML frontend for **ds4-server** (antirez/ds4 · DwarfStar). Standalone repo —
it talks to ds4-server over HTTP, with no source dependency on ds4. See
[docs/frontend_specs.md](docs/frontend_specs.md) for the full plan.

## Status: Phases 1–3 complete (live)

- **Phase 1 — shell + theme:** full layout, Pop!_OS COSMIC styling, Chat/Agent toggle (Agent
  shows a display-only tool-call preview), suggestion cards, composer.
- **Phase 2 — live chat:** streaming from ds4-server (`/v1/chat/completions` SSE), thinking
  traces, markdown + code blocks with copy, Stop/abort, server-status heartbeat, and the six
  per-turn metrics (TTFT / total / prefill / decode / prompt / output) computed from the stream.
- **Phase 3 — live telemetry:** `metrics_sidecar.py` serves real GPU (rocm-smi / nvidia-smi), RAM, disk-rate,
  and **model page-cache residency (mincore)** as JSON; the right rail polls it at 2 Hz.

**Remaining (P2 stretch):** Agent-mode tool *execution*, endpoint switcher, light theme.

## Run

**One command** (from the repo root):

```bash
./ds4Service      # prompts for the ds4 dir (remembered), launches everything, Ctrl+C stops it all
```

`ds4Service` starts ds4-server + the metrics sidecar + the web UI, streams their logs, and tears
everything down by name on Ctrl+C. Then open http://localhost:8090.

**Manual** (three processes) if you prefer:

```bash
# 1) ds4-server — in the ds4 checkout (CORS + port 8080). Normally ds4Service does this for you,
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

**Per-machine launch (optional).** `ds4Service` auto-detects the GPU backend (AMD/ROCm vs
NVIDIA/CUDA), so it works on either box with no config. To pin custom flags per machine, drop an
executable `ds4-server.sh` in the ds4 dir (forwarding `"$@"`) — `ds4Service` runs it instead,
appending only `--cors --port`; the script owns backend/env/model/ctx. See the `ds4Service` header
for the precedence ladder and the `DS4_SERVER_SCRIPT` / `DS4_NO_SERVER_SCRIPT` knobs.

**Two example `ds4-server.sh` setups.** Pick the one matching your hardware, save it as
`ds4-server.sh` in your ds4 dir, `chmod +x` it, then launch that box with `./ds4Service`. Both `cd`
to the script's own dir and forward `"$@"`, so they work whether `ds4Service` runs them or you run
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

On either box, launch the whole stack with `./ds4Service`, then open http://localhost:8090.

**Setting up a second machine (known gaps).** Per-box config is machine-local and never committed —
do it once per box: the ds4 dir is remembered under `~/.config/ds4service/`, and any `ds4-server.sh`
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
- `ds4Service` — one-command launcher for the whole stack (repo root)
- `code/index.html` · `styles.css` · `app.js` — the SPA (markup / COSMIC theme / chat + telemetry)
- `code/config.js` — server & sidecar URLs, suggestion cards
- `code/metrics_sidecar.py` — GPU/RAM/disk/model-residency JSON on :8081
- `code/run-frontend.sh` — sidecar + static server (not ds4-server)
- `code/bench_thinking.py` — measures whether "Auto" thinking-mode saves time on this box (think on/off savings + the cost of switching); run against a live ds4-server, esp. on the slow box
- `docs/frontend_specs.md` — full specification & roadmap
