#!/usr/bin/env bash
# The whole comparison in one go: an idle baseline, then 1..MAX cameras on
# each arm, then the report.
#
#   ./run_matrix.sh 5                  # decode + box, 1..5 cameras
#   ENCODE=h264 ./run_matrix.sh 5      # decode + box + re-encode
#   FPS=5 ./run_matrix.sh 5            # both arms at 5 fps instead of 10
#
#   QTI_DRAW=composer ./run_matrix.sh 5   # previous qti design, for comparison
#                                       # (needs RES=WxH; use a separate OUT)
#
# Every run_videoloop.sh setting applies. Extra:
#   BASELINE=20   seconds of idle measurement before the first run
#   COOLDOWN=15   seconds between runs, so one run's heat does not tax the next
set -euo pipefail

MAX="${1:-5}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MON="$HERE/../metrics_monitor"
export OUT="${OUT:-$HERE/vlogs/matrix_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT"

echo "== idle baseline (${BASELINE:-20}s) -> $OUT"
python3 "$MON/metrics_logger.py" --duration "${BASELINE:-20}" --quiet \
    --output "$OUT/idle_0cam.jsonl" --meta arm=idle cams=0 2>/dev/null

for n in $(seq 1 "$MAX"); do
  # Alternate which arm goes first, so neither always runs on a warmer board.
  arms="opencv qti"
  [ $((n % 2)) -eq 0 ] && arms="qti opencv"
  for arm in $arms; do
    sleep "${COOLDOWN:-15}"
    "$HERE/run_videoloop.sh" "$arm" "$n" || echo "!! $arm with $n cameras failed -- see $OUT" >&2
  done
done

python3 "$MON/compare_metrics.py" "$OUT" | tee "$OUT/report.txt"
