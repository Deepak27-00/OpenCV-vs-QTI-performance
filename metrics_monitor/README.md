# metrics_monitor

Stdlib-only tools that measure CPU during the video-loop benchmark
(`../rtsp_video_loop`). Usable for any process.

| file                 | what it does |
|----------------------|--------------|
| `metrics_logger.py`  | samples once per second and writes JSON lines: system CPU, CPU of the matched processes, per-cluster load and clock, cycles (GHz), GPU load, RSS |
| `compare_metrics.py` | turns a directory of runs into the hardware vs software table, per-camera slopes and ratios |
| `monitor_metrics.sh` | follows a running log from another terminal |

```bash
python3 metrics_logger.py --match qti_videoloop.py --duration 60 \
    --output qti_3cam.jsonl --meta arm=qti cams=3
python3 compare_metrics.py ../rtsp_video_loop/vlogs/<run_dir>
./monitor_metrics.sh ../rtsp_video_loop/vlogs/<run_dir>/qti_3cam.jsonl
```

## What is measured

- `sys_cpu`: from `/proc/stat`, in % of one core (8 cores = 800%). Covers
  everything, including kernel threads and interrupts serving the video and
  GPU drivers.
- `proc_cpu`: utime + stime of every process whose script argument matches
  `--match`, over all of its threads, from `/proc/<pid>/stat`.
- `busy_ghz`: the sum over cores of busy fraction x average clock. The average
  clock comes from cpufreq `time_in_state`, or the current clock if stats are
  off. It counts cycles, so it does not change when the governor raises or
  lowers the clock.
- `clusters`: busy % and MHz per cpufreq policy (little / big / prime).
- `gpu`: `/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage`.

The first line of each file is `{"meta": ...}` (labels, cores, governors) and
the last is `{"summary": ...}` (mean / p50 / p95 / max).
