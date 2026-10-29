"""Arguments and frame accounting shared by both arms of the benchmark.

qti_videoloop.py (all hardware) and rtsp_videoloop_opencv.py (all software)
import this module so they cannot drift apart: same defaults, same box, same
output rate, and the same SUMMARY line that compare_metrics.py reads.
"""
import argparse
import logging
import time

# depay, parse, software decoder -- the hardware arm swaps only the decoder.
CODECS = {
    "h264": ("rtph264depay", "h264parse", "avdec_h264"),
    "h265": ("rtph265depay", "h265parse", "avdec_h265"),
}

# Yellow. RGBA for qtioverlay (0xRRGGBBAA) and the composer texture, BGR for cv2.
BOX_RGBA = (255, 255, 0, 255)
BOX_BGR = (0, 255, 255)

REPORT_EVERY_S = 5.0


def setup_logging(name):
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {name} %(levelname)s %(message)s")
    return logging.getLogger(name)


def box_error(a, width, height):
    """Why --box cannot be drawn on a width x height frame, or None if it can."""
    x, y, bw, bh = a.box
    if x + bw > width or y + bh > height:
        return f"--box {x} {y} {bw} {bh} does not fit inside the {width}x{height} frame"
    return None


def parse_args(description, extra=None):
    """extra(parser), if given, adds the arm's own arguments."""
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--url", required=True, help="RTSP URL of the camera")
    p.add_argument("--codec", default="h264", choices=sorted(CODECS))
    p.add_argument("--width", type=int, default=0,
                   help="output width; 0 (default) = the camera's own size, no scaling")
    p.add_argument("--height", type=int, default=0,
                   help="output height; 0 (default) = the camera's own size, no scaling")
    p.add_argument("--fps", type=int, default=10,
                   help="output rate. Both arms emit exactly this many boxed frames per "
                        "second, repeating or dropping camera frames to get there")
    p.add_argument("--box", type=int, nargs=4, default=[40, 40, 220, 160],
                   metavar=("X", "Y", "W", "H"), help="bounding box in output pixels")
    p.add_argument("--thickness", type=int, default=3,
                   help="box border in pixels. 3 = what qtioverlay draws (it has no width setting)")
    p.add_argument("--encode", default="none", choices=["none", "h264"],
                   help="none = decode + box only; h264 = also re-encode the boxed video")
    p.add_argument("--bitrate", type=int, default=4000, help="encoder bitrate, kbit/s")
    p.add_argument("--latency", type=int, default=200, help="rtspsrc jitterbuffer, ms")
    p.add_argument("--warmup", type=float, default=10.0,
                   help="seconds excluded from the SUMMARY (RTSP setup, first keyframe)")
    p.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    if extra is not None:
        extra(p)
    a = p.parse_args()

    x, y, bw, bh = a.box
    if x < 0 or y < 0 or bw <= 2 * a.thickness or bh <= 2 * a.thickness:
        p.error("--box must be non-negative and larger than twice --thickness")
    if (a.width == 0) != (a.height == 0) or a.width < 0 or a.height < 0:
        p.error("--width and --height must both be positive, or both 0 for the camera's size")
    # At the camera's size the frame is only known once the stream starts;
    # each arm checks it then.
    if a.width and box_error(a, a.width, a.height):
        p.error(box_error(a, a.width, a.height))
    if a.fps <= 0:
        p.error("--fps must be positive")
    return a


class FrameStats:
    """Counts output frames. Prints a rate every few seconds, and on exit one
    SUMMARY line covering only the time after --warmup."""

    def __init__(self, log, warmup):
        self.log = log
        self.start = time.monotonic()
        self.warmup = warmup
        self.base = None            # (time, count) when the warmup ended
        self.last = (self.start, 0)

    def update(self, count):
        now = time.monotonic()
        if self.base is None and now - self.start >= self.warmup:
            self.base = (now, count)
        t0, c0 = self.last
        if now - t0 >= REPORT_EVERY_S:
            self.log.info(f"out_fps={(count - c0) / (now - t0):.2f} frames={count}")
            self.last = (now, count)

    def summary(self, count):
        now = time.monotonic()
        if self.base is None:
            self.log.info(f"SUMMARY frames=0 seconds=0 fps=0 -- stopped inside the "
                          f"{self.warmup:g}s warmup (total frames={count})")
            return
        t0, c0 = self.base
        secs = now - t0
        fps = (count - c0) / secs if secs > 0 else 0.0
        self.log.info(f"SUMMARY frames={count - c0} seconds={secs:.1f} fps={fps:.2f}")
