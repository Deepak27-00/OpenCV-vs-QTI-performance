#!/usr/bin/env python3
"""Find which part of the QTI pipeline spends the CPU.

Runs the hardware pipeline in stages, one after another in this process and
pinned like run_videoloop.sh, and prints for each stage: output fps, process
CPU, and the busiest threads. GStreamer names each streaming thread after its
element and pad (e.g. comp:src), so the thread list says which element it is.

  stage           what runs
  decode          rtspsrc -> qtivdec -> fakesink (fps here = the camera's real rate)
  rate            + videorate drop-only to --fps
  overlay_free    decode + qtioverlay box on every camera frame, no rate limit
  overlay         the benchmark pipeline (--draw overlay) at the camera's size
  scale           decode + qtivtransform to --res + videorate, no box
  overlay_scaled  the benchmark pipeline (--draw overlay) at --res
  composer        the previous design (--draw composer) at --res
  snapshot        one boxed frame of 'overlay_free' saved as PNG (snapshot_qti.png), to
                  check the box against the OpenCV arm's (rtsp_videoloop_opencv.py --snapshot).
                  No videorate: it does not change pixels, and videorate followed by
                  the qtivtransform read-back delivered no frames on the board.
  snapshot_decode decoder frame, no box, read back the same way (snapshot_qti_decode.png):
                  if this one is blank too, the read-back is broken, not the overlay
  Snapshots skip the first 2 s of frames; the decoder's first frames can be blank.

  python3 diag_qti.py                       # first URL in cams.txt, every stage
  python3 diag_qti.py --only overlay composer --secs 30
  python3 diag_qti.py --only decode         # the camera's size and frame rate

qtioverlay's engine is the system property persist.overlay.use_c2d_blit; on
this board only 0 draws the box, and the overlay stages refuse to run without it.
"""
import argparse
import copy
import os
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import qti_videoloop as q

CLK_TCK = os.sysconf("SC_CLK_TCK")
HERE = os.path.dirname(os.path.abspath(__file__))
SINK = "fakesink name=out sync=false"


def snap_path(a, name):
    """snapshot -> snapshot_qti.png, snapshot_decode -> snapshot_qti_decode.png"""
    root, ext = os.path.splitext(a.snapshot)
    return root + name[len("snapshot"):] + ext


def png(path):
    """PNG tail: copies the GBM frame to system memory, then encodes it."""
    return ("qtivtransform ! video/x-raw, format=NV12 ! videoconvert ! "
            f'pngenc name=png snapshot=true ! filesink name=file location="{path}"')


def thread_ticks():
    """{tid: (thread name, cpu ticks, last core)} for this process."""
    out = {}
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/stat") as f:
                s = f.read()
        except OSError:
            continue
        name = s[s.find("(") + 1:s.rfind(")")]
        fields = s[s.rfind(")") + 2:].split()
        out[tid] = (name, int(fields[11]) + int(fields[12]), int(fields[36]))
    return out


def stages(a):
    """{name: (pipeline description, args to configure() it with, or None)}"""
    native = copy.copy(a)
    native.draw, native.width, native.height = "overlay", 0, 0
    scaled = copy.copy(native)
    scaled.width, scaled.height = a.width, a.height
    composer = copy.copy(scaled)
    composer.draw = "composer"

    src = q.source(a)
    rate = f"videorate drop-only=true ! video/x-raw(memory:GBM), framerate={a.fps}/1 ! "
    scale = (f"qtivtransform name=scale ! "
             f"video/x-raw(memory:GBM), format=NV12, width={a.width}, height={a.height} ! ")
    full = q.build_pipeline(native)
    assert full.endswith(SINK), "qti_videoloop.build_pipeline sink changed"
    return {
        "decode": (src + SINK, None),
        "rate": (src + rate + SINK, None),
        "overlay_free": (src + "qtioverlay name=overlay ! " + SINK, native),
        "overlay": (full, native),
        "scale": (src + scale + rate + SINK, None),
        "overlay_scaled": (q.build_pipeline(scaled), scaled),
        "composer": (q.build_pipeline(composer), composer),
        "snapshot": (src + "qtioverlay name=overlay ! " + png(snap_path(a, "snapshot")), native),
        "snapshot_decode": (src + png(snap_path(a, "snapshot_decode")), None),
    }


def launch(desc, conf, a, setup=None):
    desc = desc.replace("turbo=true", f"turbo={'true' if a.turbo == 'on' else 'false'}")
    pipe = Gst.parse_launch(desc)
    if conf is not None:
        q.configure(pipe, conf)
    if setup is not None:
        setup(pipe)
    pipe.set_state(Gst.State.PLAYING)
    return pipe


def wait(bus, seconds):
    """None if the time ran out, else why the pipeline stopped."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        msg = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg is not None:
            if msg.type == Gst.MessageType.ERROR:
                err, _ = msg.parse_error()
                return f"{msg.src.get_name()}: {err.message}"
            return "end of stream"
    return None


def caps_of(pipe, name):
    element = pipe.get_by_name(name)
    caps = element.get_static_pad("src").get_current_caps() if element else None
    return caps.to_string() if caps else "-"


def run_snapshot(name, desc, conf, a):
    path = snap_path(a, name)
    if os.path.exists(path):
        os.remove(path)
    seen, written = [0], [False]

    def drop_first(_pad, _info):
        seen[0] += 1
        return Gst.PadProbeReturn.DROP if seen[0] <= 2 * a.fps else Gst.PadProbeReturn.OK

    def on_png(_pad, _info):
        written[0] = True
        return Gst.PadProbeReturn.OK

    def setup(pipe):
        pipe.get_by_name("png").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, drop_first)
        pipe.get_by_name("file").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, on_png)

    # Done when the PNG reaches filesink: pngenc's snapshot EOS does not
    # always come back out of rtspsrc as an EOS message.
    pipe = launch(desc, conf, a, setup)
    bus = pipe.get_bus()
    end = time.monotonic() + a.warmup + a.secs
    problem = None
    while not written[0] and problem is None and time.monotonic() < end:
        problem = wait(bus, 0.2)
    if problem == "end of stream":
        problem = None
    pipe.set_state(Gst.State.NULL)   # closes the file
    if written[0] and os.path.getsize(path) > 0:
        print(f"{name:15s} saved {path}", flush=True)
    else:
        why = problem or f"no PNG within {a.warmup + a.secs:g}s"
        print(f"{name:15s} FAILED: {why} ({seen[0]} frames reached the encoder)", flush=True)


def run_stage(name, desc, conf, a):
    pipe = launch(desc, conf, a)
    frames = q.frame_counter(pipe)
    bus = pipe.get_bus()

    problem = wait(bus, a.warmup)
    if problem is None:
        t0, f0, w0 = thread_ticks(), frames(), time.monotonic()
        problem = wait(bus, a.secs)
        t1, f1, w1 = thread_ticks(), frames(), time.monotonic()
        decode_caps, scale_caps = caps_of(pipe, "vdec"), caps_of(pipe, "scale")
    pipe.set_state(Gst.State.NULL)
    if problem:
        print(f"{name:15s} FAILED: {problem}", flush=True)
        return

    dt = w1 - w0
    per_thread = sorted(
        ((100 * (t1[t][1] - t0[t][1]) / CLK_TCK / dt, t1[t][0], t1[t][2]) for t in t1 if t in t0),
        reverse=True)
    total = sum(p for p, _, _ in per_thread)
    busiest = "  ".join(f"{n}={p:.0f}%@cpu{c}" for p, n, c in per_thread[:4] if p >= 0.5)
    print(f"{name:15s} fps {(f1 - f0) / dt:5.2f}  proc {total:5.1f}%  "
          f"decode_caps={decode_caps}  scale_caps={scale_caps}  "
          f"busiest: {busiest or '-'}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None, help="default: first URL in cams.txt")
    ap.add_argument("--secs", type=float, default=20, help="measured seconds per stage")
    ap.add_argument("--warmup", type=float, default=8, help="seconds before measuring each stage")
    ap.add_argument("--cores", default="4-7", help="cores to pin to, as run_videoloop.sh PIN=big")
    ap.add_argument("--only", nargs="*", default=None, help="run only these stages")
    ap.add_argument("--fps", type=int, default=10, help="target rate (default: 10, the camera's rate)")
    ap.add_argument("--res", default="1280x720",
                    help="output size for scale, overlay_scaled and composer (default: 1280x720)")
    ap.add_argument("--turbo", choices=("on", "off"), default="on",
                    help="qtivdec performance mode (default: on)")
    ap.add_argument("--snapshot", default=os.path.join(HERE, "snapshot_qti.png"),
                    help="where the snapshot stage writes its PNG; the other "
                         "snapshot stages add their suffix to this name")
    a = ap.parse_args()

    if a.url is None:
        cams = os.path.join(HERE, "cams.txt")
        if not os.path.exists(cams):
            sys.exit(f"{cams} not found: copy cams.example.txt to cams.txt and add your camera URL, or pass --url")
        with open(cams) as f:
            a.url = next((l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")), None)
        if a.url is None:
            sys.exit(f"no camera URL in {cams}")
    a.width, a.height = (int(v) for v in a.res.split("x"))
    # the benchmark's defaults (loop_common.py)
    a.codec, a.box, a.thickness = "h264", [40, 40, 220, 160], 3
    a.encode, a.bitrate, a.latency = "none", 4000, 200

    lo, _, hi = a.cores.partition("-")
    cores = set(range(int(lo), int(hi or lo) + 1))
    os.sched_setaffinity(0, cores)   # threads created from here on inherit it
    Gst.init(None)
    blit = q.c2d_blit()
    print(f"persist.overlay.use_c2d_blit = "
          f"{'unknown (no getprop)' if blit is None else blit or 'unset'}", flush=True)

    todo = stages(a)
    for name in a.only or todo:
        desc, conf = todo[name]
        try:
            if name.startswith("snapshot"):
                run_snapshot(name, desc, conf, a)
            else:
                run_stage(name, desc, conf, a)
        except Exception as e:       # one broken stage should not hide the rest
            print(f"{name:15s} FAILED: {getattr(e, 'message', e)}", flush=True)
        time.sleep(2)                # let the camera close the previous session


if __name__ == "__main__":
    main()
