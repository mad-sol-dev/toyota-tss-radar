# Tools

> Written by Claude (Anthropic) from conversations with the maintainer and the
> measurements; see [Authorship](../README.md#authorship).

All tools live in `tools/` and are called from the repository root, e.g.
`tools/roadreport.py logs/<session>`. They run on Linux; the recording tools
need SocketCAN. Setup: `uv pip install -r requirements.txt` and `tools/get_dbc.sh`
once (see the [README](../README.md#try-it-without-hardware)). `roadlog.sh`
uses `.venv/bin/python` if the project venv exists; the Python scripts called
directly need the venv activated or the packages installed system-wide. The
interface names `can_radar` / `can_car` come from the udev rule described in
[bench-setup.md](hardware/bench-setup.md). `dbc/` and `logs/` are looked up in
the repository root, not in `tools/`.

| Tool | Purpose |
|------|---------|
| [`spoof_dsu.py`](#spoof_dsupy--wake-up-the-radar) | DSU spoof — wakes the radar from standby |
| [`canwatch.py`](#canwatchpy--live-frame-change-detector) | Live view of CAN frames with change highlighting |
| [`roadlog.sh`](#roadlogsh--one-command-recording) | Drive recording: spoof + CAN log (+ GNSS) |
| [`roadstatus.py`](#roadstatuspy--live-status-line) | Live status line during recording (started by `roadlog.sh`) |
| [`gnsslog.py`](#gnsslogpy--gnss-logger-emlid-reach-or-nmea) | GNSS logger: Emlid Reach (ERB) or NMEA → raw + CSV |
| [`canlog_stats.py`](#canlog_statspy--quick-health-check) | Streaming statistics of a recording |
| [`roadreport.py`](#roadreportpy--drive-analysis) | Drive analysis: time series, dropout vs. speed, Doppler check, radar yaw |
| [`radarmap.py`](#radarmappy--birds-eye-view-of-the-tracks) | Bird's-eye view of the tracks (movie / PNG / GIF / live) |
| [`make_test_log.py`](#make_test_logpy--testing-without-a-vehicle) | Synthetic radar log for testing without hardware |
| [`make_test_gnss.py`](#make_test_gnsspy--gnss-without-a-receiver) | Synthetic GNSS stream (ERB or NMEA) for testing without a receiver |
| `get_dbc.sh`, `radar_dbc.py` | Fetch the Toyota DBCs into `dbc/` (checksum-verified); shared DBC loading used by the other scripts |

---

## spoof_dsu.py — wake up the radar

Sends the CAN messages that the Toyota DSU (Driving Support Unit) normally
sends to the radar. Without them the sensor stays in standby and reports only
invalid tracks. Prints valid tracks (distance, lateral offset, v_rel) as they
come in. The radar is told a constant speed of 5 km/h (`EGO_SPEED_KPH`);
it measures relative speed on its own ([first-drive.md](drives/first-drive.md)).
Based on [frk2/toyoyta_radar_control_can](https://github.com/frk2/toyoyta_radar_control_can);
the changes are listed in the file header.

> This version has not been run with the real radar yet (November 2026). The
> bench tests and the first drive used an earlier version. Offline, with a
> fake CAN bus, both send the same 4000 frames in the same order; the cyclic
> frames also match the CAN log of the drive. A bench run is still open.

```bash
tools/spoof_dsu.py
```

## canwatch.py — live frame change detector

Reads `candump` from stdin and shows the current payload per CAN ID,
highlights byte changes in color and dims automatically detected
counter/checksum bytes. Baseline snapshot via the `f` key — useful for
reflection/motion tests in front of the sensor.

```bash
candump can_radar | tools/canwatch.py
```

Keys: `f` freeze baseline, `r` release, `c` reset state, `q` quit.

---

## Drive workflow: record → analyze

Recording and analysis run on the same laptop: log to disk during the drive,
analyze locally afterwards — no uploading, no copying around.

### roadlog.sh — one-command recording

> In this repository `roadlog.sh` has been tested offline only (fake
> `candump`, GNSS from a pseudo-terminal, `spoof_dsu.py` without CAN: paths,
> restarts, status line, Ctrl+C). A run with the real radar and CAN sticks
> is still open; do a bench run first. The first drive was recorded with an
> earlier version.

Starts the DSU spoof (unless it's already running), logs `can_radar` **and**
`can_car` in the canonical SocketCAN log format (replayable with `canplayer`)
and — if a GNSS source is given or unambiguously detected — GNSS via
[`gnsslog.py`](#gnsslogpy--gnss-logger-emlid-reach-or-nmea). Everything ends up
in `logs/<timestamp>_<label>/`.
A single `Ctrl+C` stops everything cleanly and writes a summary to the
manifest.

```bash
tools/roadlog.sh country-road --gnss reach              # Emlid Reach on USB
tools/roadlog.sh motorway --gnss tcp:192.168.2.15:9001     # Reach TCP output / phone
tools/roadlog.sh serial-test --gnss nmea:/dev/ttyUSB0@9600          # serial NMEA receiver
tools/roadlog.sh standstill-test --no-spoof                # spoof is already running elsewhere
```

Without `--gnss`, every `/dev/ttyACM*` port is probed for 3 s and GNSS is
logged only if **exactly one** of them sends valid ERB or NMEA; otherwise the
recording runs with CAN only (perfectly adequate as a baseline) and
`session.md` says why. `/dev/ttyUSB*` is never probed, because opening such a
port can reset the board behind it — give serial NMEA receivers explicitly.
The old `--gps <dev> --baud <n>` still works and means `--gnss nmea:<dev>@<n>`.

Before setting off, do a **stationary check**: point the radar at a
parked car and check that the status line shows `valid` tracks and a
plausible `next` distance. Only then drive off.

`candump` and the spoof are supervised: if one exits — a USB reset of the
CAN sticks, or the spoof failing with "No buffer space" while the radar has no
power — it is restarted after 2 s and the log continues in the same file
(each restart is noted in `restarts.log`; candump's error messages go to
`candump.err`, not into the log). A restarted spoof re-sends its one-time
init messages; its behavior is otherwise unchanged. `gnsslog.py` reconnects on
its own.

While recording, `roadlog.sh` shows a [live status line](#roadstatuspy--live-status-line).
Python tools are started with `.venv/bin/python` if the project venv exists,
otherwise with the system `python3` (which then needs the packages from
`requirements.txt`).

### roadstatus.py — live status line

Started by `roadlog.sh`; reads the growing `can_radar.log` and `gnss.csv`
(no second CAN socket, the recording is not touched) and rewrites one line per
second:

```
spoof ok | valid  3 | next  23.4 m  -1.2 m/s | GNSS fix1  47 km/h | 00:12:31
```

`valid` = track slots whose latest frame is valid; `next` = nearest valid
track in the driving corridor (|lat| < 1.5 m, `--lane`), distance and v_rel
(negative = closing in). If the spoof process is gone or no frame came in on
`can_radar` for more than 5 s, the line turns red, is framed by `!!` and the
terminal bell rings every 5 s. Restarts by the supervisor show up in yellow
as `restarts n`. Standalone, for a session recording elsewhere:
`tools/roadstatus.py logs/<session>`.

### gnsslog.py — GNSS logger (Emlid Reach or NMEA)

Reads a GNSS stream and writes `gnss.raw` (every chunk exactly as received,
with host arrival time) and `gnss.csv`, one row per navigation epoch:

```
t_host,t_gps,lat,lon,speed_mps,heading_deg,fix,n_sats,h_acc
```

`t_host` is the laptop clock, the same clock `candump -L` stamps with, so CAN
and GNSS line up directly. `fix`: 0 none, 1 single, 2 RTK float/DGPS, 3 RTK
fix; without a fix the position/speed fields stay empty. `heading_deg` comes
from the velocity vector and is noise when standing still. The format (ERB or
NMEA) is detected from the data; a dropped source is reopened automatically.

```bash
tools/gnsslog.py --source reach --out-dir /tmp/g               # /dev/ttyACM0, ERB
tools/gnsslog.py --source tcp:192.168.2.15:9001 --out-dir /tmp/g
tools/gnsslog.py --replay logs/<session>/gnss.raw --csv again.csv   # re-parse raw data
tools/gnsslog.py --probe /dev/ttyACM0                           # prints ERB/NMEA, exit 0 if GNSS
```

**Emlid Reach (1st gen.):** on USB the Reach shows up as `/dev/ttyACM0` and
sends ERB (Emlid Reach Binary) out of the box (ReachView → Position output →
Output 1: USB-to-PC, ERB). ReachView itself is at `http://192.168.2.15/` over
the USB network; for that the laptop needs `192.168.2.1/24` on the `enx…`
interface — not needed for the serial port. The ERB layout follows Emlid's own
encoder, [`src/erb.c` in emlid/RTKLIB (branch `reach`)](https://github.com/emlid/RTKLIB/blob/reach/src/erb.c),
cross-checked against ArduPilot's [`AP_GPS_ERB.h`](https://github.com/ArduPilot/ardupilot/blob/master/libraries/AP_GPS/AP_GPS_ERB.h)
and frames recorded from a real Reach; details in the file header.

### make_test_gnss.py — GNSS without a receiver

Synthetic 120 s drive (stand, accelerate, straight, 90° turn, 100 km/h,
slow down) at a made-up location, as ERB byte-exact to Emlid's encoder or as NMEA.
Offline as a `gnss.raw` file, or in real time on a pseudo-terminal or TCP port
to stand in for the receiver.

```bash
python3 tools/make_test_gnss.py --raw-out /tmp/g.raw && tools/gnsslog.py --replay /tmp/g.raw
python3 tools/make_test_gnss.py --pty             # prints e.g. /dev/pts/5
tools/roadlog.sh dry-run --no-spoof --gnss serial:/dev/pts/5
python3 tools/make_test_gnss.py --tcp 9001 --format nmea --garbage
```

### canlog_stats.py — quick health check

Streams a recording (constant RAM usage, whether 1 MB or 1 GB; reads `.log`
and `.log.gz`) and distills it into one screen: frames per CAN ID + rate,
valid-track ratio per slot, distance/lateral/v_rel ranges.

```bash
tools/canlog_stats.py logs/<session>/can_radar.log
```

### roadreport.py — drive analysis

Combines `can_radar.log` and `gnss.csv` of a session (same laptop clock) and
writes `logs/<session>/report/` — Markdown plus three PNGs, headless:

```bash
tools/roadreport.py logs/<session>
tools/roadreport.py logs/<session> --lane 1.5 --out /tmp/report
```

- **Time series** over the whole drive: valid tracks, distance and v_rel of the
  nearest object in the driving corridor (|LAT_DIST| < 1.5 m), GNSS speed;
  gaps in the CAN log are shaded red.
- **Dropout vs. speed:** share of 0.1-s scans without any valid track (and
  without one in the corridor), detections per second and share of stationary
  detections per GNSS speed class — answers whether tracks are dropped when
  the real speed is far above the 5 km/h sent to the radar. An empty road also gives
  "no track"; look for jumps between classes, not the level.
- **Doppler plausibility:** for stationary objects `v_rel = −v_ego·cos(bearing)`.
  Histogram of `v_rel + v_GNSS·cos(bearing)` (peak at 0 = stationary targets
  seen with the right sign and scale) and the radar/GNSS speed scale.
- **Which side is positive LAT_DIST:** with right-hand traffic, oncoming
  vehicles (v_rel clearly below −v_ego) are on the left; their side in
  LAT_DIST re-checks the sign convention. The bench test says positive
  LAT_DIST = right in the driving direction (bench, then confirmed on the
  first drive, see [first-drive.md](drives/first-drive.md)).
- **Radar yaw:** on straight sections (heading change < 1° over 4 s,
  > 29 km/h) vehicles driving ahead (moving our way, so not oncoming traffic)
  are fitted with `LAT_DIST = a + b·LONG_DIST`; yaw = atan b with a 95 %
  bootstrap interval over track episodes.

Without GNSS only the time series and the overall dropout are available.

### radarmap.py — bird's-eye view of the tracks

Shows the tracks from above: x = lateral offset, y = forward distance,
ego at the origin (direction of travel pointing up), color by v_rel
(red = approaching, blue = receding).

```bash
tools/radarmap.py logs/<session>/can_radar.log                 # offline movie (display)
tools/radarmap.py logs/<session>/can_radar.log --png map.png   # headless overview
tools/radarmap.py logs/<session>/can_radar.log --gif clip.gif  # headless animation
candump can_radar | tools/radarmap.py                          # live (display)
```

Each track gets an arrow to where it will be in 1 s (along the line of sight)
and its v_rel as a number — color alone is hard to read at small speeds.
`--vmax` narrows the color scale (default ±15 m/s; e.g. `--vmax 2` and
`--max-range 6` on the bench).

In the movie and live window:

| Key | Effect |
|-----|--------|
| `v` | toggle top view / front view (`--view front` starts there) |
| `+` / `−`, ↑ / ↓, mouse wheel | range 6 → 10 → 20 → 40 → 80 → 150 m |

The **front view** looks like the windscreen: each track is a box at its
bearing that shrinks with distance, with the driving corridor (±1.5 m) and
range marks drawn as on the road. Box size (1.8 × 1.5 m) and eye height
(1.2 m) are assumed — the radar reports no height, so up/down in this view
carries no measurement.

PNG/GIF are rendered headless (no X) and suit short clips and sharing; for a
whole drive the interactive movie is more economical.

### make_test_log.py — testing without a vehicle

Generates a synthetic `can_radar` log with two moving tracks (one approaching,
one receding object), DBC-encoded. This lets you check `radarmap.py` and
`canlog_stats.py` without a bench and without a drive.

```bash
python3 tools/make_test_log.py --out /tmp/synth.log && tools/radarmap.py /tmp/synth.log
```

`--drive DIR` instead writes a complete synthetic road session (16 track slots
at 20 Hz plus matching `gnss.raw`/`gnss.csv`) with known answers for
`roadreport.py`: radar yaw +1.5°, a car ahead, stationary posts at the road
edge, oncoming cars on the left, dropouts that become more frequent above
40 km/h, one 90° turn.

```bash
python3 tools/make_test_log.py --drive /tmp/synth_drive && tools/roadreport.py /tmp/synth_drive
```

### Data volumes

Raw ~135 MB/h for `can_radar` alone (~180–200 MB/h with `can_car` + spoof).
The log format compresses ~6.6× (measured) → ~20–30 MB/h gzipped. Recordings
are gitignored and stay local. Analysis is streamed, never loaded in one
piece — file size doesn't limit what can be processed.
