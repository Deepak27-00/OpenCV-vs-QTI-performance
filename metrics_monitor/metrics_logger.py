#!/usr/bin/env python3
"""Sample CPU, clocks and GPU while a benchmark runs. Writes JSON lines.

Every --interval seconds it records:

  sys_cpu    CPU used by the WHOLE system, in "% of one core" (8 cores = 800%).
             Includes kernel threads and interrupts, so work a hardware driver
             does on the CPU on the process's behalf is counted too.
  proc_cpu   CPU of the matched processes only (utime + stime, all threads).
  busy_ghz   sum over cores of (busy fraction x average clock). The CPU cycles
             actually spent, which does not move when the governor changes
             the clock -- CPU% does: the same work reads as more % at a lower
             clock.
  clusters   busy % (of the cluster's capacity) and average MHz per cpufreq
             policy, named little/big/prime by maximum clock.
  gpu        Adreno busy % (kgsl), if exposed.

The first line is {"meta": ...}, the last {"summary": ...}. Stdlib only.

  python3 metrics_logger.py --match qti_videoloop.py --duration 60 \\
      --output qti_3cam.jsonl --meta arm=qti cams=3
"""
import argparse
import glob
import json
import os
import signal
import sys
import time

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
CPU_DIR = "/sys/devices/system/cpu"
KGSL_BUSY = "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage"


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def cpu_list(text):
    """'0-3,7' -> [0, 1, 2, 3, 7]"""
    out = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo), int(hi or lo) + 1))
    return out


# --- CPU topology and clocks -------------------------------------------------

def cpufreq_policies():
    """One entry per cpufreq policy (a cluster of cores sharing a clock),
    slowest first, named little/big/prime when there are 2 or 3."""
    pols = []
    for d in sorted(glob.glob(f"{CPU_DIR}/cpufreq/policy*")):
        cpus = read(f"{d}/related_cpus") or read(f"{d}/affected_cpus")
        if not cpus:
            continue
        pols.append({
            "dir": d,
            "cpus": cpu_list(cpus.replace(" ", ",")),
            "max_mhz": int(read(f"{d}/cpuinfo_max_freq") or 0) // 1000,
            "governor": read(f"{d}/scaling_governor"),
        })
    pols.sort(key=lambda p: p["max_mhz"])
    names = {2: ["little", "big"], 3: ["little", "big", "prime"]}.get(
        len(pols), [f"cluster{i}" for i in range(len(pols))])
    for p, name in zip(pols, names):
        p["name"] = name
    return pols


def time_in_state(policy):
    """{kHz: cumulative 10ms ticks}, or None if cpufreq stats are not built in."""
    text = read(f"{policy['dir']}/stats/time_in_state")
    if not text:
        return None
    return {int(f): int(t) for f, t in (line.split() for line in text.splitlines())}


def avg_mhz(policy, prev, cur):
    """Time-weighted average clock over the interval from time_in_state,
    falling back to the instantaneous clock."""
    if prev and cur:
        dt = {f: cur[f] - prev.get(f, 0) for f in cur}
        total = sum(dt.values())
        if total > 0:
            return sum(f * t for f, t in dt.items()) / total / 1000
    return int(read(f"{policy['dir']}/scaling_cur_freq") or 0) / 1000


def read_proc_stat():
    """{core: (busy_ticks, total_ticks)}"""
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if not (line.startswith("cpu") and line[3].isdigit()):
                continue
            name, *vals = line.split()
            v = [int(x) for x in vals[:8]]      # user nice system idle iowait irq softirq steal
            idle = v[3] + v[4]
            out[int(name[3:])] = (sum(v) - idle, sum(v))
    return out


# --- processes ---------------------------------------------------------------

def matching_pids(match):
    """PIDs whose argv contains an argument named `match` (basename compare),
    e.g. --match qti_videoloop.py finds 'python3 /x/qti_videoloop.py --url ..'
    but not a shell that merely mentions it."""
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if any(os.path.basename(a.decode(errors="replace")) == match for a in argv[1:3]):
            yield int(entry)


def pid_stat(pid):
    """(cpu_ticks, rss_bytes) or None if the process is gone."""
    text = read(f"/proc/{pid}/stat")
    if not text:
        return None
    f = text[text.rfind(")") + 2:].split()   # f[0] is stat field 3
    return int(f[11]) + int(f[12]), int(f[21]) * PAGE


# --- sampling ------------------------------------------------------------------

class Sampler:
    def __init__(self, match):
        self.match = match
        self.pols = cpufreq_policies()
        self.prev_stat = read_proc_stat()
        self.prev_tis = [time_in_state(p) for p in self.pols]
        self.prev_pids = {}
        self.prev_t = time.monotonic()
        if match:
            self.prev_pids = {p: s for p in matching_pids(match) if (s := pid_stat(p))}

    def sample(self):
        now = time.monotonic()
        dt = now - self.prev_t
        stat = read_proc_stat()
        tis = [time_in_state(p) for p in self.pols]

        busy = {}
        for cpu, (b, t) in stat.items():
            pb, pt = self.prev_stat.get(cpu, (b, t))
            busy[cpu] = (b - pb) / (t - pt) if t > pt else 0.0

        rec = {"t": round(time.time(), 2), "sys_cpu": round(100 * sum(busy.values()), 1)}
        clusters, ghz = {}, 0.0
        for p, prev, cur in zip(self.pols, self.prev_tis, tis):
            mhz = avg_mhz(p, prev, cur)
            cb = [busy.get(c, 0.0) for c in p["cpus"]]
            ghz += sum(cb) * mhz / 1000
            clusters[p["name"]] = {"busy": round(100 * sum(cb) / len(cb), 1), "mhz": round(mhz)}
        if self.pols:                       # no cpufreq (VMs): clocks unknown
            rec["busy_ghz"] = round(ghz, 3)
        rec["clusters"] = clusters

        if self.match:
            pids, cpu_ticks, rss = {}, 0, 0
            for pid in matching_pids(self.match):
                s = pid_stat(pid)
                if s is None:
                    continue
                pids[pid] = s
                # a process that appeared this interval counts from its start
                cpu_ticks += s[0] - self.prev_pids.get(pid, (0,))[0]
                rss += s[1]
            rec["proc_cpu"] = round(100 * cpu_ticks / CLK_TCK / dt, 1) if dt > 0 else 0.0
            rec["procs"] = len(pids)
            rec["rss_mb"] = round(rss / 2**20, 1)
            self.prev_pids = pids

        gpu = read(KGSL_BUSY)
        if gpu:
            rec["gpu"] = float(gpu.split()[0])

        self.prev_stat, self.prev_tis, self.prev_t = stat, tis, now
        return rec


def stats(values):
    v = sorted(values)
    if not v:
        return None
    pick = lambda q: v[min(len(v) - 1, round(q * (len(v) - 1)))]
    return {"mean": round(sum(v) / len(v), 2), "p50": pick(0.5), "p95": pick(0.95), "max": v[-1]}


def summarize(records):
    out = {"samples": len(records)}
    for key in ("sys_cpu", "proc_cpu", "busy_ghz", "gpu", "rss_mb", "procs"):
        vals = [r[key] for r in records if key in r]
        if vals:
            out[key] = stats(vals)
    for name in records[0]["clusters"] if records else ():
        out[name] = {k: round(sum(r["clusters"][name][k] for r in records) / len(records), 1)
                     for k in ("busy", "mhz")}
    return out


def live_line(r):
    parts = [f"sys {r['sys_cpu']:6.1f}%"]
    if "busy_ghz" in r:
        parts.append(f"{r['busy_ghz']:5.2f} GHz")
    if "proc_cpu" in r:
        parts.append(f"proc {r['proc_cpu']:6.1f}% ({r['procs']})")
    if "gpu" in r:
        parts.append(f"gpu {r['gpu']:4.0f}%")
    parts += [f"{n} {c['busy']:3.0f}%@{c['mhz']}" for n, c in r["clusters"].items()]
    return "  ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--match", default=None,
                    help="script name to attribute proc_cpu to, e.g. qti_videoloop.py")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    ap.add_argument("--duration", type=float, default=None, help="seconds to run (default: until Ctrl-C)")
    ap.add_argument("--output", default="metrics.jsonl")
    ap.add_argument("--meta", nargs="*", default=[], metavar="KEY=VALUE",
                    help="labels stored in the header, e.g. arm=qti cams=3")
    ap.add_argument("--quiet", action="store_true", help="no live line per sample")
    a = ap.parse_args()

    stop = [False]
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__(0, True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__(0, True))

    sampler = Sampler(a.match)
    meta = dict(kv.split("=", 1) for kv in a.meta)
    meta.update({
        "match": a.match, "interval_s": a.interval, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kernel": os.uname().release,
        "clusters": {p["name"]: {"cpus": p["cpus"], "max_mhz": p["max_mhz"], "governor": p["governor"]}
                     for p in sampler.pols},
        "cpufreq_stats": bool(sampler.pols) and all(time_in_state(p) is not None for p in sampler.pols),
    })

    records = []
    end = time.monotonic() + a.duration if a.duration else None
    with open(a.output, "w") as fh:
        fh.write(json.dumps({"meta": meta}) + "\n")
        next_t = time.monotonic() + a.interval
        while not stop[0] and (end is None or time.monotonic() < end):
            time.sleep(max(0.0, next_t - time.monotonic()))
            next_t += a.interval
            rec = sampler.sample()
            records.append(rec)
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            if not a.quiet:
                print(live_line(rec), flush=True)
        summary = summarize(records)
        fh.write(json.dumps({"summary": summary}) + "\n")

    print(json.dumps({"summary": summary}, indent=1), file=sys.stderr)


if __name__ == "__main__":
    main()
