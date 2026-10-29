#!/usr/bin/env python3
"""Turn a directory of metrics_logger.py runs into the comparison report.

  python3 compare_metrics.py ../rtsp_video_loop/vlogs/matrix_20261023_101500
  python3 compare_metrics.py <dir> --csv report.csv

Reads every <arm>_<N>cam.jsonl in the directory, the matching
<arm>_<N>cam_cam*.log files for each camera's SUMMARY fps, and idle_0cam.jsonl
for the idle baseline. Nothing is adjusted except subtracting that baseline,
which is shown separately; every flagged problem is printed, not hidden.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys

SUMMARY_RE = re.compile(r"SUMMARY frames=(\d+) seconds=([\d.]+) fps=([\d.]+)")


def load_run(path):
    meta = summary = None
    with open(path) as f:
        for line in f:
            rec = json.loads(line)
            meta = rec.get("meta", meta)
            summary = rec.get("summary", summary)
    if meta is None or summary is None or not summary.get("samples"):
        return None
    stem = path[:-len(".jsonl")]
    fps = []
    for log in sorted(glob.glob(f"{stem}_cam*.log")):
        with open(log, errors="replace") as f:
            m = SUMMARY_RE.search(f.read())
        fps.append(float(m.group(3)) if m else None)
    return {"meta": meta, "s": summary, "fps": fps, "file": os.path.basename(path)}


def mean(summary, key):
    v = summary.get(key)
    return v["mean"] if v else None


def slope(points):
    """Least-squares slope of (x, y) points, or None with fewer than two."""
    pts = [(x, y) for x, y in points if y is not None]
    if len(pts) < 2:
        return None
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    den = sum((x - mx) ** 2 for x, _ in pts)
    return sum((x - mx) * (y - my) for x, y in pts) / den if den else None


def fmt(v, spec="6.1f"):
    return format(v, spec) if v is not None else " " * (int(spec.split(".")[0]) - 1) + "-"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", help="directory written by run_matrix.sh / run_videoloop.sh")
    ap.add_argument("--csv", default=None, help="also write the table as CSV")
    a = ap.parse_args()

    runs = [r for r in map(load_run, sorted(glob.glob(os.path.join(a.dir, "*.jsonl")))) if r]
    idle = next((r for r in runs if r["meta"].get("arm") == "idle"), None)
    runs = sorted((r for r in runs if r["meta"].get("arm") != "idle"),
                  key=lambda r: (int(r["meta"]["cams"]), r["meta"]["arm"]))
    if not runs:
        sys.exit(f"no runs in {a.dir}")

    idle_cpu = mean(idle["s"], "sys_cpu") if idle else None
    idle_ghz = mean(idle["s"], "busy_ghz") if idle else None
    m0 = runs[0]["meta"]
    print(f"Settings: {m0.get('fps')} fps, {m0.get('res')}, encode={m0.get('encode')}, "
          f"codec={m0.get('codec')}, cores={m0.get('cores')}")
    draws = sorted({f"{r['meta']['arm']}={r['meta'].get('draw', '?')}" for r in runs})
    print(f"Box drawn by: {', '.join(draws)}; persist.overlay.use_c2d_blit={m0.get('c2d_blit', '?')}")
    if idle:
        print(f"Idle baseline: sys CPU {idle_cpu:.1f}%"
              + (f", {idle_ghz:.2f} GHz" if idle_ghz is not None else "") + " (subtracted in the '-idle' columns)")
    else:
        print("No idle_0cam.jsonl: '-idle' columns are blank")
    print("CPU% = share of one core (100% = one core fully busy).\n")

    head = ("cams arm     fps/cam  proc CPU%  sys CPU%  sys-idle  GHz-idle  GPU%  RSS MB  notes")
    print(head)
    print("-" * len(head))
    rows = []
    for r in runs:
        m, s = r["meta"], r["s"]
        cams, target = int(m["cams"]), float(m.get("fps", 0) or 0)
        got = [f for f in r["fps"] if f is not None]
        fps = sum(got) / len(got) if got else None
        sys_cpu, ghz = mean(s, "sys_cpu"), mean(s, "busy_ghz")
        row = {
            "cams": cams, "arm": m["arm"], "fps_per_cam": fps,
            "proc_cpu": mean(s, "proc_cpu"), "sys_cpu": sys_cpu,
            "sys_minus_idle": sys_cpu - idle_cpu if idle_cpu is not None else None,
            "ghz_minus_idle": ghz - idle_ghz if None not in (ghz, idle_ghz) else None,
            "gpu": mean(s, "gpu"), "rss_mb": mean(s, "rss_mb"),
        }
        notes = []
        if len(got) < cams:
            notes.append(f"{cams - len(got)} cam(s) without SUMMARY")
        if s.get("procs") and s["procs"]["mean"] < cams - 0.01:
            notes.append(f"only {s['procs']['mean']:.1f}/{cams} processes alive")
        if target and any(f < 0.95 * target for f in got):
            notes.append(f"below {target:g} fps (min {min(got):.1f})")
        row["notes"] = "; ".join(notes)
        rows.append(row)
        print(f"{cams:4d} {m['arm']:7s} {fmt(fps, '7.2f')}  {fmt(row['proc_cpu'], '9.1f')}  "
              f"{fmt(sys_cpu, '8.1f')}  {fmt(row['sys_minus_idle'], '8.1f')}  "
              f"{fmt(row['ghz_minus_idle'], '8.2f')}  {fmt(row['gpu'], '4.0f')}  "
              f"{fmt(row['rss_mb'], '6.0f')}  {row['notes']}")

    print("\nCost of each added camera (least-squares slope over the camera counts run):")
    arms = sorted({r["arm"] for r in rows})
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm]
        parts = []
        for key, label, spec in (("proc_cpu", "proc", ".1f"), ("sys_minus_idle", "sys", ".1f"),
                                 ("ghz_minus_idle", "GHz", ".3f")):
            k = slope([(r["cams"], r[key]) for r in mine])
            if k is not None:
                parts.append(f"{label} {k:{spec}}{'' if label == 'GHz' else '%'}/cam")
        seq = [r["sys_minus_idle"] if r["sys_minus_idle"] is not None else r["proc_cpu"] for r in mine]
        mono = all(b >= a for a, b in zip(seq, seq[1:]))
        print(f"  {arm:7s} " + "   ".join(parts) + ("" if mono else "   ** not monotonic in camera count **"))

    if {"opencv", "qti"} <= set(arms):
        print("\nSoftware / hardware (opencv divided by qti):")
        for cams in sorted({r["cams"] for r in rows}):
            o = next((r for r in rows if r["cams"] == cams and r["arm"] == "opencv"), None)
            q = next((r for r in rows if r["cams"] == cams and r["arm"] == "qti"), None)
            if not (o and q):
                continue
            ratios = []
            for key, label in (("proc_cpu", "proc"), ("sys_minus_idle", "sys-idle")):
                if o[key] is not None and q[key]:
                    ratios.append(f"{label} x{o[key] / q[key]:.1f}")
            print(f"  {cams} cam: " + "   ".join(ratios))

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {a.csv}")


if __name__ == "__main__":
    main()
