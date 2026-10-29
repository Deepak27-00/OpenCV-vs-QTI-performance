#!/usr/bin/env python3
"""Hardware arm: RTSP -> qtivdec -> qtioverlay (box) [-> qtic2venc] -> fakesink.

Every pixel operation runs on Qualcomm hardware, and the frames stay in GBM
(device) memory from the decoder to the sink, so the CPU never touches a
pixel:

  decode   qtivdec       hardware video decoder
  scale    qtivtransform only when --width/--height are given; by default the
                         camera's own size is kept and nothing scales
  box      qtioverlay    draws the box in place into the decoder's frame
  rate     videorate     drops frames down to --fps (drop-only: never duplicates)
  encode   qtic2venc     hardware H.264 encoder (--encode h264 only)

--draw composer keeps the previous design for comparison: qtivcomposer scales
the frame and blends a transparent RGBA box texture over it (needs an explicit
--width/--height). It was the costly part of the old pipeline.

The CPU is left with the RTSP transport (rtspsrc, depay, parse) -- the same
elements the OpenCV arm uses -- plus driving the hardware.

qtioverlay's blit engine is chosen system-wide, outside this script, and on
this board only 0 draws the box (checked at startup, see C2D_BLIT_OK):
  setprop persist.overlay.use_c2d_blit 0
run_videoloop.sh records the value with each run's metrics.
"""
import shutil
import signal
import subprocess
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
import numpy as np

from loop_common import BOX_RGBA, CODECS, FrameStats, box_error, parse_args, setup_logging

log = setup_logging("qti")

# persist.overlay.use_c2d_blit on the QCS6490 board (GStreamer 1.16, 2026-10-27):
#   0      draws the box
#   unset  and 1: blit a shrunken copy of the whole frame into the box instead
#   2      "GLES converter is not supported", qtioverlay fails to start
C2D_BLIT_OK = "0"


def c2d_blit():
    """persist.overlay.use_c2d_blit, '' when unset, None without getprop."""
    if shutil.which("getprop") is None:
        return None
    r = subprocess.run(["getprop", "persist.overlay.use_c2d_blit"], capture_output=True, text=True)
    return r.stdout.strip()


def add_args(p):
    p.add_argument("--draw", default="overlay", choices=["overlay", "composer"],
                   help="overlay = qtioverlay draws in place (default); "
                        "composer = qtivcomposer blends an RGBA texture (the previous design)")


def box_texture(w, h, t):
    """Yellow border on a fully transparent RGBA surface."""
    tex = np.zeros((h, w, 4), dtype=np.uint8)
    tex[:t] = tex[-t:] = BOX_RGBA
    tex[:, :t] = tex[:, -t:] = BOX_RGBA
    return tex.tobytes()


def bbox_spec(a):
    """qtioverlay's overlay-bbox value. The color is 0xRRGGBBAA.

    qtioverlay drops a bbox with an empty label (it reads back as ''), so the
    label is a single space at the smallest font: the box only, no text.
    """
    x, y, bw, bh = a.box
    r, g, b, alpha = BOX_RGBA
    color = (r << 24) | (g << 16) | (b << 8) | alpha
    return (f'bbox0, bbox=<{x}, {y}, {bw}, {bh}>, label=" ", color={color:#010x}, font-size=1;',
            color)


def source(a):
    depay, parse, _ = CODECS[a.codec]
    return (
        f'rtspsrc location="{a.url}" protocols=tcp latency={a.latency} drop-on-latency=true ! '
        f"{depay} ! {parse} ! video/x-{a.codec}, stream-format=byte-stream, alignment=au ! "
        # Keep decoder buffers in GBM. The board measured better with linear
        # GBM NV12 than with UBWC.
        "qtivdec name=vdec turbo=true ! video/x-raw(memory:GBM), format=NV12 ! "
    )


def overlay_stage(a):
    scale = ""
    if a.width:
        scale = (f"qtivtransform name=scale ! "
                 f"video/x-raw(memory:GBM), format=NV12, width={a.width}, height={a.height} ! ")
    return (
        f"{scale}"
        # overlay-bbox is set in configure(): its value has quotes of its own
        "qtioverlay name=overlay ! "
        # Rate after the overlay: videorate keeps a reference to the frame it
        # sent last, so a frame after it is shared, and drawing into a shared
        # frame makes GStreamer copy it to system memory, which qtioverlay
        # cannot draw on (gst_fd_memory_get_fd assertion, no frames out). The
        # cost: when the camera is faster than --fps, dropped frames are
        # drawn too. This camera sends 10 fps, the default --fps.
        f"videorate drop-only=true ! video/x-raw(memory:GBM), framerate={a.fps}/1 ! "
    )


def composer_stage(a):
    x, y, bw, bh = a.box
    return (
        # video -> sink_0
        "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! comp.sink_0 "
        # box -> sink_1: one buffer from appsrc, repeated by imagefreeze at --fps,
        # moved into GBM by qtivtransform (hardware) for the compositor
        "appsrc name=box format=time ! imagefreeze ! "
        f"video/x-raw, framerate={a.fps}/1 ! "
        "qtivtransform ! "
        f"video/x-raw(memory:GBM), format=RGBA, width={bw}, height={bh} ! "
        "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 ! comp.sink_1 "
        # compose; output stays in GBM so nothing maps it into CPU memory
        "qtivcomposer name=comp background=0 "
        f'sink_0::zorder=0 sink_0::position="<0, 0>" sink_0::dimensions="<{a.width}, {a.height}>" '
        f'sink_1::zorder=1 sink_1::crop="<0, 0, {bw}, {bh}>" '
        f'sink_1::position="<{x}, {y}>" sink_1::dimensions="<{bw}, {bh}>" sink_1::alpha=1.0 ! '
        f"video/x-raw(memory:GBM), format=NV12, width={a.width}, height={a.height}, "
        f"framerate={a.fps}/1 ! "
    )


def build_pipeline(a):
    encode = f"qtic2venc target-bitrate={a.bitrate * 1000} ! h264parse ! " if a.encode == "h264" else ""
    stage = composer_stage(a) if a.draw == "composer" else overlay_stage(a)
    return (
        source(a) + stage + encode +
        # Keep the sink unsynchronized: this is a processing benchmark, and
        # the actual output rate is measured at the sink.
        "fakesink name=out sync=false"
    )


def attach_box(pipeline, a):
    """Push the box texture once, then EOS. imagefreeze keeps repeating it."""
    _, _, bw, bh = a.box
    src = pipeline.get_by_name("box")
    # Fully specified: imagefreeze rejects caps that are less specific than
    # what it can accept, and a missing field makes appsrc fail to negotiate.
    src.set_property("caps", Gst.Caps.from_string(
        f"video/x-raw, format=RGBA, width={bw}, height={bh}, framerate={a.fps}/1, "
        "pixel-aspect-ratio=1/1, interlace-mode=progressive"))
    data = box_texture(bw, bh, a.thickness)
    pushed = [False]

    def on_need_data(appsrc, _length):
        if pushed[0]:
            return
        pushed[0] = True
        appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(data))
        appsrc.emit("end-of-stream")

    src.connect("need-data", on_need_data)


def set_bbox(pipeline, a):
    """Give qtioverlay the box, and fail the pipeline if the frame that
    arrives is too small for it (at the camera's size it is not known before)."""
    blit = c2d_blit()
    if blit is not None and blit != C2D_BLIT_OK:
        raise RuntimeError(
            f"persist.overlay.use_c2d_blit is {blit or 'unset'}; qtioverlay draws the box only "
            f"with {C2D_BLIT_OK} on this board. Run: setprop persist.overlay.use_c2d_blit {C2D_BLIT_OK}")
    overlay = pipeline.get_by_name("overlay")
    if overlay.find_property("overlay-bbox") is None:
        raise RuntimeError("this qtioverlay has no overlay-bbox property")
    spec, color = bbox_spec(a)
    if overlay.find_property("bbox-color") is not None:
        overlay.set_property("bbox-color", color)
    overlay.set_property("overlay-bbox", spec)
    accepted = overlay.get_property("overlay-bbox")
    log.info(f"overlay-bbox: {accepted!r}")
    # A string qtioverlay cannot parse is dropped with only a GST_DEBUG
    # warning, and the run would then measure a pipeline that draws nothing.
    if not accepted:
        raise RuntimeError(f"qtioverlay rejected overlay-bbox {spec!r} (it reads back empty)")

    def on_caps(pad, _pspec):
        caps = pad.get_current_caps()
        if caps is None:
            return
        s = caps.get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        log.info(f"overlay input: {caps.to_string()}")
        problem = box_error(a, w, h)
        if problem:
            err = GLib.Error.new_literal(Gst.core_error_quark(), problem, Gst.CoreError.NEGOTIATION)
            overlay.post_message(Gst.Message.new_error(overlay, err, problem))

    overlay.get_static_pad("sink").connect("notify::caps", on_caps)


def configure(pipeline, a):
    """Set up the box for --draw, after parse_launch and before PLAYING."""
    if a.draw == "composer":
        attach_box(pipeline, a)
    else:
        set_bbox(pipeline, a)


def frame_counter(pipeline):
    """Return a function giving the number of frames the sink has received.

    Reads the sink's own C counter, so Python is not called per frame. Falls
    back to a buffer probe on GStreamer older than 1.18 (no 'stats').
    """
    sink = pipeline.get_by_name("out")
    if sink.find_property("stats") is not None:
        return lambda: sink.get_property("stats").get_value("rendered")

    count = [0]

    def probe(_pad, _info):
        count[0] += 1
        return Gst.PadProbeReturn.OK

    sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)
    return lambda: count[0]


def main():
    a = parse_args(__doc__, add_args)
    if a.draw == "composer" and not a.width:
        log.error("--draw composer needs --width and --height (it always scales)")
        sys.exit(2)
    Gst.init(None)
    desc = build_pipeline(a)
    log.info(f"pipeline: {desc}")
    try:
        pipeline = Gst.parse_launch(desc)
        configure(pipeline, a)
    except (GLib.Error, RuntimeError, TypeError) as e:
        log.error(f"cannot build the pipeline: {getattr(e, 'message', e)}")
        sys.exit(1)

    frames = frame_counter(pipeline)
    stats = FrameStats(log, a.warmup)
    loop = GLib.MainLoop()
    status = {"code": 0}

    def on_message(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.error(f"{msg.src.get_name()}: {err.message} ({dbg})")
            status["code"] = 1
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            log.info("end of stream")
            loop.quit()

    def tick():
        stats.update(frames())
        if a.duration is not None and time.monotonic() - stats.start >= a.duration:
            loop.quit()
        return True

    def on_signal():
        loop.quit()
        return GLib.SOURCE_REMOVE

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, on_signal)
    GLib.timeout_add(500, tick)

    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        log.error("pipeline refused to start")
        sys.exit(1)
    size = f"{a.width}x{a.height}" if a.width else "camera size"
    log.info(f"streaming {size}@{a.fps} draw={a.draw} encode={a.encode} box={a.box}")
    try:
        loop.run()
    finally:
        stats.summary(frames())
        pipeline.set_state(Gst.State.NULL)
    sys.exit(status["code"])


if __name__ == "__main__":
    main()
