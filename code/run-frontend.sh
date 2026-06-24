#!/usr/bin/env bash
# Launch the DS4 frontend: metrics sidecar (:8081) + static server (:8090).
# ds4-server must be started SEPARATELY (it needs the GPU + model). See below.
set -euo pipefail
cd "$(dirname "$0")"                              # → code/ (sidecar + web assets live here)
REPO="$(cd .. && pwd)"                            # → this repo root (ds4Frontend)
DS4_DIR="${DS4_DIR:-$(dirname "$REPO")/ds4}"      # sibling ds4 checkout — override with DS4_DIR=...
DS4_MODEL="${DS4_MODEL:-$DS4_DIR/ds4flash.gguf}"  # ds4's model — used by the sidecar's "model warm" gauge

cat <<EOF
Reminder — start ds4-server in another terminal (CORS + port 8080 + RAM-optimized):

  cd $DS4_DIR
  DS4_CUDA_NO_DIRECT_IO=1 DS4_CUDA_KEEP_MODEL_PAGES=1 LD_LIBRARY_PATH=/usr/local/cuda/lib64 ./ds4-server --cuda --ssd-streaming --ctx 100000 --cors --port 8080
  # first run after boot: cat $DS4_MODEL > /dev/null   (warm RAM)

EOF

python3 metrics_sidecar.py --port 8081 --model "$DS4_MODEL" &
SIDE=$!
trap 'kill "$SIDE" 2>/dev/null || true' EXIT
echo "metrics sidecar: pid $SIDE on http://localhost:8081  (model: $DS4_MODEL)"
echo "frontend:       http://localhost:8090   (Ctrl+C to stop both)"
echo
python3 -m http.server 8090
