#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
# Launch the ds4Xtend frontend: metrics sidecar (:8081) + static server (:8090).
# ds4-server must be started SEPARATELY (it needs the GPU + model). See below.
set -euo pipefail
cd "$(dirname "$0")"                              # → code/ (sidecar + web assets live here)
REPO="$(cd .. && pwd)"                            # → this repo root (ds4Xtend)
DS4_DIR="${DS4_DIR:-$(dirname "$REPO")/ds4}"      # sibling ds4 checkout — override with DS4_DIR=...
DS4_MODEL="${DS4_MODEL:-$DS4_DIR/ds4flash.gguf}"  # ds4's model — used by the sidecar's "model warm" gauge

cat <<EOF
Reminder — start ds4-server in another terminal (it needs the GPU + model; CORS + port 8080):

  cd $DS4_DIR
  # AMD / ROCm (full residency — this box):
  HSA_OVERRIDE_GFX_VERSION=11.0.0 LD_LIBRARY_PATH=/opt/rocm/lib ./ds4-server --rocm --ctx 32768 --cors --port 8080
  # NVIDIA / CUDA (SSD streaming):
  DS4_CUDA_NO_DIRECT_IO=1 DS4_CUDA_KEEP_MODEL_PAGES=1 LD_LIBRARY_PATH=/usr/local/cuda/lib64 ./ds4-server --cuda --ssd-streaming --ctx 100000 --cors --port 8080
  # easier: run ../ds4Xtend (auto-detects backend), or drop an executable ds4-server.sh in $DS4_DIR
  # first run after boot: cat $DS4_MODEL > /dev/null   (warm RAM)

EOF

python3 metrics_sidecar.py --port 8081 --model "$DS4_MODEL" &
SIDE=$!
trap 'kill "$SIDE" 2>/dev/null || true' EXIT
echo "metrics sidecar: pid $SIDE on http://localhost:8081  (model: $DS4_MODEL)"
echo "frontend:       http://localhost:8090   (Ctrl+C to stop both)"
echo
python3 -m http.server 8090
