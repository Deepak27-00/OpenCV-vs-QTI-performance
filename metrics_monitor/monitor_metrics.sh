#!/usr/bin/env bash
# Follow a metrics_logger.py file from another terminal, one line per sample.
#
#   ./monitor_metrics.sh ../rtsp_video_loop/vlogs/<run>/qti_3cam.jsonl
#   ./monitor_metrics.sh                       # newest .jsonl under rtsp_video_loop/vlogs
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FILE="${1:-}"
if [ -z "$FILE" ]; then
  FILE=$(ls -t "$HERE"/../rtsp_video_loop/vlogs/*/*.jsonl 2>/dev/null | head -1 || true)
  [ -n "$FILE" ] || { echo "usage: $0 <metrics.jsonl>" >&2; exit 1; }
fi
echo "following $FILE (Ctrl-C to stop)"

# Same formatting as the logger's own live output.
tail -n +1 -F "$FILE" | PYTHONPATH="$HERE" python3 -u -c '
import json, sys
from metrics_logger import live_line
for line in sys.stdin:
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if "meta" in r:
        m = r["meta"]
        print("-- %s, %s cam(s), %s fps, cores %s" % (m.get("arm"), m.get("cams"), m.get("fps"), m.get("cores")))
    elif "summary" in r:
        s = r["summary"]
        print("-- done: " + "  ".join("%s mean %s" % (k, s[k]["mean"]) for k in ("sys_cpu", "proc_cpu", "busy_ghz", "gpu") if k in s))
    else:
        print(live_line(r))
'
