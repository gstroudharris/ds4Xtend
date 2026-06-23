# DS4 Web Frontend

A COSMIC-styled HTML frontend for **ds4-server** (antirez/ds4 · DwarfStar). Standalone repo —
it talks to ds4-server over HTTP, with no source dependency on ds4. See
[frontend_specs.md](frontend_specs.md) for the full plan.

## Status: Phases 1–3 complete (live)

- **Phase 1 — shell + theme:** full layout, Pop!_OS COSMIC styling, Chat/Agent toggle (Agent
  shows a display-only tool-call preview), suggestion cards, composer.
- **Phase 2 — live chat:** streaming from ds4-server (`/v1/chat/completions` SSE), thinking
  traces, markdown + code blocks with copy, Stop/abort, server-status heartbeat, and the six
  per-turn metrics (TTFT / total / prefill / decode / prompt / output) computed from the stream.
- **Phase 3 — live telemetry:** `metrics_sidecar.py` serves real GPU (nvidia-smi), RAM, disk-rate,
  and **model page-cache residency (mincore)** as JSON; the right rail polls it at 2 Hz.

**Remaining (P2 stretch):** Agent-mode tool *execution*, endpoint switcher, light theme.

## Run (three processes)

```bash
# 1) ds4-server — in the ds4 checkout (CORS + port 8080, RAM-optimized)
cd /home/grant/Dev/ds4
DS4_CUDA_NO_DIRECT_IO=1 DS4_CUDA_KEEP_MODEL_PAGES=1 LD_LIBRARY_PATH=/usr/local/cuda/lib64 \
  ./ds4-server --cuda --ssd-streaming --ctx 100000 --cors --port 8080

# 2 + 3) sidecar (:8081) + static frontend (:8090), from THIS repo
./run-frontend.sh          # → open http://localhost:8090
```

`run-frontend.sh` points the sidecar at `/home/grant/Dev/ds4/ds4flash.gguf` for the "model warm"
gauge; override with `DS4_MODEL=/path/to/model.gguf ./run-frontend.sh`.

Static-only preview (no telemetry/chat): `python3 -m http.server 8090`, or `xdg-open index.html`.

## Config
Server/sidecar URLs and the prompt cards live in `config.js`.

## Files
- `index.html` · `styles.css` · `app.js` — the SPA (markup / COSMIC theme / chat + telemetry logic)
- `config.js` — server & sidecar URLs, suggestion cards
- `metrics_sidecar.py` — GPU/RAM/disk/model-residency JSON on :8081
- `run-frontend.sh` — launches the sidecar + static server
- `frontend_specs.md` — full specification & roadmap
