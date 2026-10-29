#!/usr/bin/env bash
# One measurement: N cameras through one arm, sampled by metrics_logger.py.
#
#   ./run_videoloop.sh qti 3        # hardware arm (qtivdec + qtioverlay), 3 cameras
#   ./run_videoloop.sh opencv 3     # software arm (avdec_h264 + cv2), 3 cameras
#
# Settings (environment). Every one is passed identically to both arms:
#   FPS=10          boxed frames per second each camera outputs (the camera sends 10)
#   RES=native      output size: native = the camera's own, nothing scales | WxH, e.g. 1280x720
#   ENCODE=none     none | h264 -- also re-encode the boxed video (qtic2venc vs x264enc)
#   CODEC=h264      h264 | h265 -- what the camera sends
#   QTI_DRAW=overlay  qti arm only: overlay (qtioverlay) | composer (qtivcomposer,
#                   the previous design; needs RES=WxH)
#   DURATION=60     seconds measured
#   WARMUP=10       seconds between launch and the start of measuring
#   PIN=big         cores the camera processes may use: big (every core faster
#                   than the slowest cluster -- 4-7 on QCS6490) | all | a list, e.g. 4-7
#   CAMS=cams.txt   camera URLs, one per line, reused round-robin for N cameras
#   OUT=vlogs/<timestamp>   where logs go
set -euo pipefail

ARM="${1:?usage: $0 <qti|opencv> <num_cameras>}"
N="${2:?usage: $0 <qti|opencv> <num_cameras>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGGER="$HERE/../metrics_monitor/metrics_logger.py"

FPS="${FPS:-10}"
RES="${RES:-native}"
ENCODE="${ENCODE:-none}"
CODEC="${CODEC:-h264}"
QTI_DRAW="${QTI_DRAW:-overlay}"
DURATION="${DURATION:-60}"
WARMUP="${WARMUP:-10}"
PIN="${PIN:-big}"
CAMS="${CAMS:-$HERE/cams.txt}"
OUT="${OUT:-$HERE/vlogs/$(date +%Y%m%d_%H%M%S)}"
PYTHON="${PYTHON:-python3}"

case "$ARM" in
  qti)    SCRIPT=qti_videoloop.py; ARM_ARGS=(--draw "$QTI_DRAW"); DRAW="$QTI_DRAW" ;;
  opencv) SCRIPT=rtsp_videoloop_opencv.py; ARM_ARGS=(); DRAW=cv2 ;;
  *)      echo "arm must be qti or opencv" >&2; exit 1 ;;
esac

case "$RES" in
  native) W=0; H=0 ;;
  *x*)    W="${RES%x*}"; H="${RES#*x}" ;;
  *)      echo "RES must be native or WxH" >&2; exit 1 ;;
esac

# qtioverlay's blit engine, a system-wide property; recorded with the metrics.
C2D_BLIT="$(getprop persist.overlay.use_c2d_blit 2>/dev/null || true)"
C2D_BLIT="${C2D_BLIT:-unset}"

if [ ! -f "$CAMS" ]; then
  echo "$CAMS not found: cp $HERE/cams.example.txt $HERE/cams.txt, then add your camera URL" >&2; exit 1
fi
mapfile -t URLS < <(grep -Ev '^[[:space:]]*(#|$)' "$CAMS" || true)
if [ "${#URLS[@]}" -eq 0 ]; then
  echo "no camera URL in $CAMS" >&2; exit 1
fi

# Cores faster than the slowest cluster. Pinning keeps every run on the same
# kind of core: unpinned, the scheduler keeps a light load on the slow cores
# and moves it to the fast ones as cameras are added, so CPU% per camera
# falls with camera count for the same work.
resolve_cores() {
  case "$1" in
    all) return 0 ;;
    big)
      local min f cpu out=""
      min=$(cat /sys/devices/system/cpu/cpu[0-9]*/cpufreq/cpuinfo_max_freq 2>/dev/null | sort -n | head -1)
      [ -n "$min" ] || return 0
      for f in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/cpuinfo_max_freq; do
        cpu="${f#/sys/devices/system/cpu/cpu}"; cpu="${cpu%%/*}"
        [ "$(cat "$f")" -gt "$min" ] && out="$out${out:+,}$cpu"
      done
      echo "$out" ;;
    *) echo "$1" ;;
  esac
}
CORES="$(resolve_cores "$PIN")"
TASKSET=()
if [ -n "$CORES" ]; then
  TASKSET=(taskset -c "$CORES")
elif [ "$PIN" != "all" ]; then
  echo "PIN=$PIN resolved to no cores; running unpinned" >&2
fi

mkdir -p "$OUT"
TAG="${ARM}_${N}cam"
PIDS=()
cleanup() {
  [ "${#PIDS[@]}" -gt 0 ] || return 0
  # TERM, not INT: bash starts background jobs with SIGINT ignored
  kill -TERM "${PIDS[@]}" 2>/dev/null || true
  for _ in $(seq 20); do
    kill -0 "${PIDS[@]}" 2>/dev/null || break
    sleep 0.5
  done
  kill -KILL "${PIDS[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
  PIDS=()
}
trap cleanup EXIT

echo "== $TAG: ${FPS}fps $RES draw=$DRAW encode=$ENCODE c2d_blit=$C2D_BLIT cores=${CORES:-all} -> $OUT"
for i in $(seq 1 "$N"); do
  url="${URLS[$(( (i - 1) % ${#URLS[@]} ))]}"
  ${TASKSET[@]+"${TASKSET[@]}"} "$PYTHON" -u "$HERE/$SCRIPT" --url "$url" --codec "$CODEC" \
      --fps "$FPS" --width "$W" --height "$H" --encode "$ENCODE" ${ARM_ARGS[@]+"${ARM_ARGS[@]}"} \
      --warmup "$WARMUP" > "$OUT/${TAG}_cam$i.log" 2>&1 &
  PIDS+=($!)
done

sleep "$WARMUP"
for i in "${!PIDS[@]}"; do
  if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
    log="$OUT/${TAG}_cam$((i + 1)).log"
    echo "camera $((i + 1)) exited during warmup -- not measuring. Its log ($log):" >&2
    tail -n 15 "$log" >&2
    exit 1
  fi
done

"$PYTHON" "$LOGGER" --match "$SCRIPT" --duration "$DURATION" --output "$OUT/$TAG.jsonl" \
    --meta arm="$ARM" cams="$N" fps="$FPS" res="$RES" encode="$ENCODE" codec="$CODEC" \
           cores="${CORES:-all}" draw="$DRAW" c2d_blit="$C2D_BLIT" 2> "$OUT/${TAG}_summary.json"

cleanup
grep -h "SUMMARY" "$OUT/${TAG}"_cam*.log | sed 's/^.*SUMMARY/  SUMMARY/' || true
missing=$(grep -L "SUMMARY" "$OUT/${TAG}"_cam*.log || true)
[ -z "$missing" ] || echo "no SUMMARY (crashed or killed): $missing" >&2
