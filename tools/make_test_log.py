#!/usr/bin/env python3
"""
make_test_log.py — generates a SYNTHETIC can_radar log with two moving
tracks. For testing radarmap.py / canlog_stats.py without a vehicle, since the
bench setup provides no moving objects.

  python3 tools/make_test_log.py            # writes /tmp/synth.log
  python3 tools/make_test_log.py --out x.log
  tools/radarmap.py /tmp/synth.log        # animation with timeline (needs display)
  tools/radarmap.py /tmp/synth.log --png /tmp/m.png --max-range 70

Object A: 60 -> 8 m, approaching (v_rel -4.3 m/s)  -> red
Object B: 15 -> 50 m, receding (v_rel +2.9 m/s)    -> blue

--drive DIR writes a whole synthetic road SESSION instead (for roadreport.py):
  DIR/can_radar.log   all 16 track slots at 20 Hz
  DIR/gnss.raw/.csv   the same drive from make_test_gnss.py (ERB)
with known "answers" the report has to find:
  * radar yaw error +1.5° (objects straight ahead appear at LAT_DIST = d·tan 1.5°)
  * a car ahead in the own lane whenever the van is faster than 20 km/h
  * stationary posts at the right road edge: v_rel = -v_ego·cos(bearing)
  * oncoming cars in the left lane (60 km/h) every ~15 s while driving
  * dropouts: whole seconds without any valid track, more often the faster
    the van goes (0 % up to 40 km/h, rising to 80 % at 120 km/h)
  * one 90° turn, where the car ahead drifts sideways (must be excluded
    from the yaw estimate)

  python3 tools/make_test_log.py --drive /tmp/synth_drive && tools/roadreport.py /tmp/synth_drive
"""
import argparse
import math
import os
import random
import subprocess
import sys

import radar_dbc

ap = argparse.ArgumentParser(description='Synthetic can_radar log with two moving tracks.')
ap.add_argument('--out', default='/tmp/synth.log', help='Output file (default: /tmp/synth.log)')
ap.add_argument('--drive', metavar='DIR', help='write a synthetic road session (CAN + GNSS) into DIR')
ARGS = ap.parse_args()
OUT = ARGS.out

db = radar_dbc.load()
mA = db.get_message_by_frame_id(0x210)
mB = db.get_message_by_frame_id(0x211)

T, HZ = 12.0, 20.0
n = int(T * HZ)
t0 = 1748470000.0


def enc(msg, dist, lat, vrel, cnt, valid=1):
    d = {s.name: 0 for s in msg.signals}
    d.update(VALID=valid, LONG_DIST=dist, LAT_DIST=lat, REL_SPEED=vrel, COUNTER=cnt & 0xFF)
    return msg.encode(d, strict=False)


def two_tracks():
    with open(OUT, 'w') as f:
        for i in range(n):
            t = t0 + i / HZ
            frac = i / (n - 1)
            dA = 60 - 52 * frac; latA = -1.0 + 2.0 * frac; vA = -4.3
            dB = 15 + 35 * frac; latB = 3.0 - 1.0 * frac;  vB = +2.9
            for (m, d, la, vr) in [(mA, dA, latA, vA), (mB, dB, latB, vB)]:
                f.write(f"({t:.6f}) can_radar {m.frame_id:03X}#{enc(m, d, la, vr, i).hex().upper()}\n")
    print(f"{OUT}: {n * 2} frames, 2 tracks, {T:.0f}s")


# ── Synthetic road session ──────────────────────────────────────────────────
YAW_DEG = 1.5          # radar misalignment built into the data
POST_LAT = 4.0         # posts at the right road edge (LAT_DIST > 0 = right, bench, Nov 2026)
POST_SPACING = 30.0
ONCOMING_LAT = -3.5    # oncoming lane, left of the own lane (right-hand traffic)
ONCOMING_V = 60 / 3.6
ONCOMING_EVERY = 15.0  # s between oncoming cars


def drive(out_dir):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import make_test_gnss as g
    os.makedirs(out_dir, exist_ok=True)
    rnd = random.Random(7)
    msgs = [db.get_message_by_frame_id(0x210 + k) for k in range(16)]
    tan_y = math.tan(math.radians(YAW_DEG))

    # ego state at 20 Hz from the same profile the GNSS generator uses
    states = list(g.simulate(120.0, HZ))
    odo = 0.0                      # distance travelled
    prev_t, prev_v, prev_h = 0.0, 0.0, states[0][4]
    drop_until = -1.0
    lines = 0
    with open(os.path.join(out_dir, 'can_radar.log'), 'w') as f:
        for i, (t, _lat, _lon, v, hdg) in enumerate(states):
            dt = t - prev_t
            odo += (v + prev_v) / 2 * dt
            yaw_rate = math.radians(((hdg - prev_h + 180) % 360) - 180) / dt if dt else 0.0
            curv = yaw_rate / v if v > 1 else 0.0
            prev_t, prev_v, prev_h = t, v, hdg

            # dropouts: decided per whole second, probability grows with speed
            kmh = v * 3.6
            if int(t) != int(t - dt) or i == 0:
                p = min(0.8, max(0.0, (kmh - 40) / 100))
                if rnd.random() < p:
                    drop_until = int(t) + 1.0
            dropped = t < drop_until

            objs = []              # (true dist, true lat, v_rel)
            if kmh > 20:
                d_lead = 30 + 8 * math.sin(t / 7)
                vrel = 8 / 7 * math.cos(t / 7)          # d/dt of the distance
                objs.append((d_lead, 0.5 * curv * d_lead ** 2 + rnd.gauss(0, 0.2), vrel))
            if kmh > 20:
                # an oncoming car appears at 90 m every ONCOMING_EVERY s and passes
                age = t % ONCOMING_EVERY
                d_on = 90 - (v + ONCOMING_V) * age
                if 5 < d_on < 90:
                    cos_on = math.cos(math.atan2(ONCOMING_LAT, d_on))
                    objs.append((d_on, ONCOMING_LAT, -(v + ONCOMING_V) * cos_on))
            if v > 0.5:
                first = (math.floor(odo / POST_SPACING) + 1) * POST_SPACING
                for k in range(6):
                    d = first + k * POST_SPACING - odo
                    if 5 < d < 80:
                        objs.append((d, POST_LAT, -v * math.cos(math.atan2(POST_LAT, d))))

            for k, msg in enumerate(msgs):
                if k < len(objs) and not dropped:
                    d, la, vr = objs[k]
                    # rotate into the misaligned radar frame (small angle)
                    d_m = d + la * tan_y
                    la_m = la + d * tan_y
                    data = enc(msg, d_m + rnd.gauss(0, 0.1), la_m + rnd.gauss(0, 0.1),
                               vr + rnd.gauss(0, 0.1), i)
                else:
                    data = enc(msg, 0, 0, 0, i, valid=0)
                f.write(f"({t0 + t + k * 0.0002:.6f}) can_radar {msg.frame_id:03X}#{data.hex().upper()}\n")
                lines += 1

    here = os.path.dirname(os.path.abspath(__file__))
    raw = os.path.join(out_dir, 'gnss.raw')
    subprocess.run([sys.executable, os.path.join(here, 'make_test_gnss.py'), '--raw-out', raw,
                    '--rate', '5', '--t0', str(t0)], check=True)
    subprocess.run([sys.executable, os.path.join(here, 'gnsslog.py'), '--replay', raw,
                    '--csv', os.path.join(out_dir, 'gnss.csv')], check=True)
    print(f"{out_dir}: can_radar.log ({lines} frames, 120 s), gnss.raw, gnss.csv; "
          f"built-in yaw error {YAW_DEG:+.1f}°")


if ARGS.drive:
    drive(ARGS.drive)
else:
    two_tracks()
