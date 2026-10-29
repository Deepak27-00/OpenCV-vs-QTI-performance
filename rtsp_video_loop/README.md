# RTSP video loop: hardware vs software codec

Each camera's RTSP stream is decoded, cut to `--fps`, given a yellow bounding
box, and (optionally) re-encoded to H.264. By default nothing is scaled: both
arms work at the camera's own size (`RES=native`). The same job runs two ways:

| step      | `qti_videoloop.py` (all hardware) | `rtsp_videoloop_opencv.py` (all software) |
|-----------|-----------------------------------|--------------------------------------------|
| transport | rtspsrc, rtph264depay, h264parse  | the same elements                          |
| decode    | `qtivdec turbo=true`              | `avdec_h264`                               |
| rate      | `videorate drop-only=true`        | `videorate`                                |
| scale     | `qtivtransform`, only with `RES=WxH` | `videoscale` (passes through at native) |
| box       | `qtioverlay overlay-bbox`, in place | `cv2.rectangle` (4 filled bars) on BGR   |
| encode    | `qtic2venc` (`--encode h264`)     | `x264enc` (`--encode h264`)                |
| memory    | GBM end to end                    | system memory                              |

The QTI path keeps decoded video in linear GBM NV12 (forcing UBWC raised CPU
use on this board). `qtioverlay` draws the box into the decoder's frame in
place, so there is no second video stream and no composition. `qtioverlay`
only draws on GBM/ION (fd) memory, so `videorate` comes after it: `videorate`
keeps a reference to its last frame, and drawing into a shared frame makes
GStreamer copy it to system memory first. If the camera is faster than `FPS`,
the overlay therefore also draws the frames that are then dropped (this
camera sends 10 fps = the default `FPS`). `drop-only` never duplicates, so if
the camera sends fewer than `FPS` frames, the QTI arm outputs what it gets and
the report shows it. The sink is unsynchronized (`sync=false`), so the benchmark measures
processing throughput.

`qtioverlay`'s blit engine is a system-wide, persistent property. On this board
only `0` draws the box: unset and `1` blit a shrunken copy of the whole frame
into the box rectangle, and `2` fails ("GLES converter is not supported"). Set
it once (no root needed), and the QTI arm refuses to start without it:

```bash
setprop persist.overlay.use_c2d_blit 0
```

Each run records the value, and the report prints it.

The previous QTI design, in which `qtivcomposer` scales the frame and blends an
RGBA box texture over it, is still available as `QTI_DRAW=composer` (it needs
`RES=WxH`). The composer was the costly part of the old pipeline;
`diag_qti.py --only overlay_scaled composer` compares the two at the same size.

The software arm uses no `qti*` element, only stock GStreamer software
plugins. Both arms import `loop_common.py`, so they have
the same defaults: same box, same output size, and the same `--fps` (default 10, the camera's rate; above it the QTI arm
cannot reach the target, because drop-only never repeats frames). `qtioverlay`'s
line width is fixed at 3 px, so `--thickness` defaults to 3 and both arms draw
about the same ~2,250 border pixels. Measured on the board: `qtioverlay`'s box
comes out 218x159 for a 220x160 `--box`, and its yellow is (255,236,0) after
the trip through YUV. Re-check with `python3 diag_qti.py --only snapshot` and
`rtsp_videoloop_opencv.py --snapshot snapshot_opencv.png`.

## Run it on the board

Put the camera URL(s) in `cams.txt` (one per line, reused for every camera).
`cams.txt` is git-ignored; start from the example:

```bash
cp cams.example.txt cams.txt       # then edit it

./run_matrix.sh 5                  # idle baseline, then 1..5 cameras on each arm, then the report
ENCODE=h264 ./run_matrix.sh 5      # the same, plus re-encoding
FPS=5 ./run_matrix.sh 5            # both arms at 5 fps instead of 10

./run_videoloop.sh qti 3           # one run
```

Before a matrix, check the QTI pipeline stage by stage (fps, CPU, busiest
threads), and compare the overlay with the old composer:

```bash
python3 diag_qti.py --only decode             # the camera's size and real frame rate
python3 diag_qti.py --only overlay composer   # repeat per use_c2d_blit value
```

Logs go to `vlogs/<timestamp>/`; the report is printed and saved as
`report.txt`. Re-print it with
`python3 ../metrics_monitor/compare_metrics.py vlogs/<timestamp>`.

Settings (environment variables, applied to both arms): `FPS` `RES` `ENCODE`
`CODEC` `DURATION` `WARMUP` `PIN` `CAMS` `OUT`; `QTI_DRAW` for the QTI arm; for the matrix also
`BASELINE` and `COOLDOWN`. See the top of each script.

## Reading the report

- **fps/cam**: the boxed frames each camera actually produced. Both arms
  should read `FPS`. If the software arm cannot keep up, this is where it shows,
  and the row is flagged.
- **proc CPU%**: CPU of the benchmark processes. 100% = one core.
- **sys CPU% / sys-idle**: CPU of the whole system, before and after
  subtracting the idle baseline. This counts kernel and driver work done on the
  hardware codec's behalf, which proc CPU% does not, so it is the fair total.
- **GHz-idle**: cycles spent (busy x clock, summed over cores). Unlike CPU%,
  this does not change when the governor changes the clock.
- **GPU%**: Adreno load. The overlay's blit (C2D) and `qtivtransform` run on the
  GPU, so the hardware arm is not free; this column shows where its work went.
- **Cost of each added camera**: the slope of each column against the camera count.

Camera processes are pinned to the fast cores (`PIN=big`, cores 4-7 on
QCS6490). Without pinning, the scheduler moves the load from the slow A55 cores
to the A78 cores as cameras are added, so CPU% per camera falls as the camera
count rises, although the work is the same. `PIN=all` turns pinning off.
