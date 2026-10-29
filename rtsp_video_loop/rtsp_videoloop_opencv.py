#!/usr/bin/env python3
"""Software arm: RTSP -> avdec_h264 -> cv2 box [-> x264enc] -> fakesink.

Every pixel operation runs on the CPU, and no qti* element is used anywhere
(stock GStreamer software plugins only):

  decode   avdec_h264      FFmpeg software decoder
  rate     videorate       repeats/drops camera frames to exactly --fps
  scale    videoscale      to --width x --height; passes frames through
                           untouched at the camera's size (the default)
  convert  videoconvert    to BGR, the format OpenCV works in
  box      cv2.rectangle   four filled bars drawn into the frame
  encode   x264enc         software H.264 encoder (--encode h264 only)

The RTSP transport (rtspsrc, depay, parse) is identical to the QTI arm, so the
two arms differ only in where the codec and pixel work runs.
"""
import signal
import sys
import time

import cv2

from loop_common import BOX_BGR, CODECS, FrameStats, box_error, parse_args, setup_logging

log = setup_logging("opencv")


def add_args(p):
    p.add_argument("--snapshot", default=None, metavar="PNG",
                   help="save the first boxed frame here, to compare with the QTI arm's "
                        "(diag_qti.py --only snapshot)")


def capture_pipeline(a):
    depay, parse, decoder = CODECS[a.codec]
    size = f"width={a.width}, height={a.height}, " if a.width else ""
    return (
        f'rtspsrc location="{a.url}" protocols=tcp latency={a.latency} drop-on-latency=true ! '
        f"{depay} ! {parse} ! {decoder} ! "
        "videorate ! videoscale ! videoconvert ! "
        f"video/x-raw, format=BGR, {size}framerate={a.fps}/1 ! "
        "appsink drop=true max-buffers=1 sync=false"
    )


def writer_pipeline(a):
    return (
        "appsrc ! videoconvert ! video/x-raw, format=I420 ! "
        f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={a.bitrate} "
        f"key-int-max={2 * a.fps} ! h264parse ! fakesink sync=false"
    )


def has_gstreamer():
    for line in cv2.getBuildInformation().splitlines():
        if line.strip().startswith("GStreamer:"):
            return "YES" in line
    return False


def box_bars(a):
    """The border as four filled bars (corner pairs). A single stroked
    cv2.rectangle rasterises wider than its thickness; the bars cover exactly
    the pixels of the QTI arm's box texture."""
    x, y, w, h = a.box
    t = a.thickness
    return [
        ((x, y), (x + w - 1, y + t - 1)),                   # top
        ((x, y + h - t), (x + w - 1, y + h - 1)),           # bottom
        ((x, y + t), (x + t - 1, y + h - t - 1)),           # left
        ((x + w - t, y + t), (x + w - 1, y + h - t - 1)),   # right
    ]


def main():
    a = parse_args(__doc__, add_args)
    if not has_gstreamer():
        log.error("this cv2 build has no GStreamer support")
        sys.exit(1)

    desc = capture_pipeline(a)
    log.info(f"capture: {desc}")
    cap = cv2.VideoCapture(desc, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        log.error("cannot open the capture pipeline (check the URL and that "
                  "gst-libav provides avdec_h264)")
        sys.exit(1)

    writer = None

    def open_writer(w, h):
        nonlocal writer
        desc = writer_pipeline(a)
        log.info(f"writer: {desc}")
        writer = cv2.VideoWriter(desc, cv2.CAP_GSTREAMER, 0, a.fps, (w, h), True)
        if not writer.isOpened():
            log.error("cannot open the writer pipeline (is x264enc installed?)")
            return False
        return True

    stop = [False]

    def on_signal(_sig, _frame):
        stop[0] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    bars = box_bars(a)
    stats = FrameStats(log, a.warmup)
    frames = misses = code = 0
    size = f"{a.width}x{a.height}" if a.width else "camera size"
    log.info(f"streaming {size}@{a.fps} encode={a.encode} box={a.box}")
    try:
        while not stop[0]:
            ok, frame = cap.read()
            if not ok:
                misses += 1
                if misses >= 50:
                    log.error("no frames from the camera")
                    code = 1
                    break
                time.sleep(0.1)
                continue
            misses = 0
            if frames == 0:
                # The first frame gives the size: the writer needs it, and at
                # the camera's size the box could not be checked before.
                h, w = frame.shape[:2]
                log.info(f"frame size {w}x{h}")
                problem = box_error(a, w, h)
                if problem:
                    log.error(problem)
                    code = 1
                    break
                if a.encode == "h264" and not open_writer(w, h):
                    code = 1
                    break
            for pt1, pt2 in bars:
                cv2.rectangle(frame, pt1, pt2, BOX_BGR, cv2.FILLED)
            if frames == 0 and a.snapshot:
                cv2.imwrite(a.snapshot, frame)
                log.info(f"snapshot saved to {a.snapshot}")
            if writer is not None:
                writer.write(frame)
            frames += 1
            stats.update(frames)
            if a.duration is not None and time.monotonic() - stats.start >= a.duration:
                break
    finally:
        stats.summary(frames)
        cap.release()
        if writer is not None:
            writer.release()
    sys.exit(code)


if __name__ == "__main__":
    main()
