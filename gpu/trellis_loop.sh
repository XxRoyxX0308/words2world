#!/bin/bash
# Run trellis_batch.py until every queued item has a model, restarting a fresh process after a failure
# (only when CUDA is unusable after an error; ordinary failures are handled inside trellis_batch.py). Progress: python w2w.py progress <world>
# usage (inside WSL): bash trellis_loop.sh <world_dir> [max_attempts]
WORLD="$1"; MAX="${2:-8}"
HERE="$(cd "$(dirname "$0")" && pwd)"
source ~/miniconda3/etc/profile.d/conda.sh; conda activate trellis2; cd ~/TRELLIS.2
for attempt in $(seq 1 "$MAX"); do
  echo "[attempt $attempt] $(date '+%H:%M:%S')"
  python "$HERE/trellis_batch.py" "$WORLD" 2>&1 && { echo "[all done] $(date '+%H:%M:%S')"; exit 0; }
done
echo "[gave up after $MAX attempts]"; exit 1
