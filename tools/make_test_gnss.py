#!/usr/bin/env python3
"""
make_test_gnss.py — SYNTHETIC GNSS stream for testing without a receiver.

Simulates a short drive (stand, accelerate, straight, 90° turn, faster
straight, slow down, stand) at a made-up location and emits it as
  * ERB, byte-exact as Emlid's encoder builds it (emlid/RTKLIB branch
    "reach", src/erb.c: VER, POS, STAT, DOPS, VEL, SVI, RTK per epoch), or
  * NMEA (RMC + GGA + GST), like a phone running gpsdRelay.

Outputs:
  --raw-out FILE   offline: a gnss.raw file as gnsslog.py writes it,
                   host time starting at --t0 (no waiting)
  --tcp PORT       real time: TCP server on localhost, one client at a time
  --pty            real time: creates a pseudo-terminal, prints its path
                   (stands in for /dev/ttyACM0)

  python3 tools/make_test_gnss.py --raw-out /tmp/g.raw && tools/gnsslog.py --replay /tmp/g.raw
  python3 tools/make_test_gnss.py --tcp 9001 &  tools/gnsslog.py --source tcp:127.0.0.1:9001 --out-dir /tmp/g
  python3 tools/make_test_gnss.py --pty --format nmea

--garbage mixes corrupt frames and random bytes into the stream (the
logger must skip them); --no-fix S makes the first S seconds fix-less.
"""
import argparse
import math
import os
import pty
import random
import socket
import struct
import sys
import time
import tty

GPS_EPOCH = 315964800
GPS_UTC_LEAP = 18
LAT0, LON0 = 50.0, 10.0          # made-up start point, not a real track
R_EARTH = 6371000.0


# ── Synthetic drive ─────────────────────────────────────────────────────────
# (t_end, speed at t_end in km/h, yaw rate during segment in deg/s)
PROFILE = [
    (10, 0, 0),      # standing
    (25, 50, 0),     # accelerate
    (50, 50, 0),     # straight
    (60, 50, 9),     # 90° right turn
    (75, 100, 0),    # accelerate
    (100, 100, 0),   # straight
    (112, 0, 0),     # slow down
    (120, 0, 0),     # standing
]


def simulate(duration, rate):
    """Yield (t, lat, lon, speed_mps, heading_deg) at `rate` Hz."""
    lat, lon, hdg = LAT0, LON0, 30.0
    t_prev, v_prev = 0.0, 0.0
    seg = 0
    t_start = 0.0
    v_start = 0.0
    dt = 1.0 / rate
    n = int(duration * rate)
    scale = duration / PROFILE[-1][0]
    for i in range(n + 1):
        t = i * dt
        tp = t / scale
        while seg < len(PROFILE) - 1 and tp > PROFILE[seg][0]:
            t_start, v_start = PROFILE[seg][0], PROFILE[seg][1]
            seg += 1
        t_end, v_end, yaw = PROFILE[seg]
        frac = 0 if t_end == t_start else min(1.0, (tp - t_start) / (t_end - t_start))
        v = (v_start + (v_end - v_start) * frac) / 3.6
        hdg = (hdg + yaw / scale * (t - t_prev)) % 360.0
        d = (v + v_prev) / 2 * (t - t_prev)
        lat += d * math.cos(math.radians(hdg)) / R_EARTH * 180 / math.pi
        lon += d * math.sin(math.radians(hdg)) / (R_EARTH * math.cos(math.radians(lat))) * 180 / math.pi
        t_prev, v_prev = t, v
        yield t, lat, lon, v, hdg


# ── ERB encoder (mirrors erb.c) ─────────────────────────────────────────────
def erb_frame(mid, payload):
    body = bytes([mid, len(payload) & 0xFF, len(payload) >> 8]) + payload
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return b'ER' + body + bytes([a, b])


def erb_epoch(gps_unix, lat, lon, v, hdg, fix):
    g = gps_unix + GPS_UTC_LEAP - GPS_EPOCH
    week, tow = int(g // 604800), g % 604800
    itow = int(round(tow * 1000))
    nsv = 14 if fix else 3
    ftype = 1 if fix else 0
    if not fix:                    # erb.c: no solution → rr = 0, std = 0
        lat, lon, v = 0.0, 0.0, 0.0
    hacc_mm = 2500 if fix else 0
    out = erb_frame(0x01, struct.pack('<IBBB', itow, 0, 1, 0))
    out += erb_frame(0x02, struct.pack('<IddddII', itow, lon, lat, 80.0, 35.0, hacc_mm, hacc_mm * 2))
    out += erb_frame(0x03, struct.pack('<IHBBB', itow, week, ftype, 1 if fix else 0, nsv))
    out += erb_frame(0x04, struct.pack('<IHHHH', itow, 180, 160, 120, 90))
    vn, ve = v * math.cos(math.radians(hdg)), v * math.sin(math.radians(hdg))
    head = int(math.degrees(math.atan2(ve, vn)) * 1e5)    # erb.c: atan2(E, N), may be negative
    out += erb_frame(0x05, struct.pack('<IiiiIiI', itow, int(100 * vn), int(100 * ve), 0,
                                       int(100 * math.hypot(vn, ve)), head, 0))
    svi = struct.pack('<IB', itow, nsv) + b''.join(
        struct.pack('<BBiiiHHH', i + 1, 0, 0, 0, 0, 40, 90, 45) for i in range(nsv))
    out += erb_frame(0x06, svi)
    out += erb_frame(0x07, struct.pack('<BHiiiHHI', 0, 0, 0, 0, 0, 0, week, itow))
    return out


# ── NMEA encoder ────────────────────────────────────────────────────────────
def nmea(body):
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f'${body}*{cs:02X}\r\n'.encode()


def nmea_pos(val, is_lat):
    hemi = ('N' if val >= 0 else 'S') if is_lat else ('E' if val >= 0 else 'W')
    val = abs(val)
    deg = int(val)
    minutes = (val - deg) * 60
    return (f'{deg:02d}{minutes:08.5f}' if is_lat else f'{deg:03d}{minutes:08.5f}'), hemi


def nmea_epoch(utc, lat, lon, v, hdg, fix):
    tm = time.gmtime(utc)
    hms = time.strftime('%H%M%S', tm) + f'.{int(round((utc % 1) * 100)) % 100:02d}'
    dmy = time.strftime('%d%m%y', tm)
    la, lah = nmea_pos(lat, True)
    lo, loh = nmea_pos(lon, False)
    if fix:
        rmc = f'GNRMC,{hms},A,{la},{lah},{lo},{loh},{v / 0.514444:.3f},{hdg:.2f},{dmy},,,A'
        gga = f'GNGGA,{hms},{la},{lah},{lo},{loh},1,12,0.9,80.0,M,45.0,M,,'
        gst = f'GNGST,{hms},1.5,2.0,1.4,30.0,1.8,1.7,3.5'
    else:
        rmc = f'GNRMC,{hms},V,,,,,,,{dmy},,,N'
        gga = f'GNGGA,{hms},,,,,0,03,,,M,,M,,'
        gst = None
    return nmea(rmc) + nmea(gga) + (nmea(gst) if gst else b'')


# ── Output ──────────────────────────────────────────────────────────────────
def corrupt(chunk, rnd):
    """Flip one byte (frame/sentence becomes invalid) and add random bytes."""
    b = bytearray(chunk)
    k = rnd.randrange(len(b))
    b[k] ^= 0x5A
    return bytes(b) + bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 40)))


def epochs(a):
    rnd = random.Random(1)
    enc = erb_epoch if a.format == 'erb' else nmea_epoch
    for t, lat, lon, v, hdg in simulate(a.duration, a.rate):
        fix = t >= a.no_fix
        chunk = enc(a.t0 + t, lat, lon, v, hdg, fix)
        if a.garbage and rnd.random() < 0.1:
            chunk = corrupt(chunk, rnd)
        yield t, chunk


def write_raw(a):
    rec = struct.Struct('<dI')
    with open(a.raw_out, 'wb') as f:
        f.write(b'GNSSRAW1\n')
        n = 0
        for t, chunk in epochs(a):
            # deliver in two pieces, as a serial read() would — tests resync
            k = len(chunk) // 3
            f.write(rec.pack(a.t0 + t + 0.020, k) + chunk[:k])
            f.write(rec.pack(a.t0 + t + 0.035, len(chunk) - k) + chunk[k:])
            n += 1
    print(f'{a.raw_out}: {n} epochs {a.format.upper()}, {a.duration:.0f} s @ {a.rate:g} Hz', file=sys.stderr)


def stream(a, write):
    start = time.time()
    a.t0 = start if a.t0 is None else a.t0
    for t, chunk in epochs(a):
        delay = start + t - time.time()
        if delay > 0:
            time.sleep(delay)
        write(chunk)


def main():
    ap = argparse.ArgumentParser(description='Synthetic GNSS stream (ERB or NMEA).')
    out = ap.add_mutually_exclusive_group(required=True)
    out.add_argument('--raw-out', help='write an offline gnss.raw file')
    out.add_argument('--tcp', type=int, metavar='PORT', help='real-time TCP server on 127.0.0.1')
    out.add_argument('--pty', action='store_true', help='real-time pseudo-terminal')
    ap.add_argument('--format', choices=['erb', 'nmea'], default='erb')
    ap.add_argument('--rate', type=float, default=5.0, help='epochs per second (default 5)')
    ap.add_argument('--duration', type=float, default=120.0, help='length of the drive in s (default 120)')
    ap.add_argument('--t0', type=float, default=None,
                    help='Unix time of the first epoch (default: now; raw-out: 1748470000, as make_test_log.py)')
    ap.add_argument('--no-fix', type=float, default=0.0, metavar='S', help='first S seconds without fix')
    ap.add_argument('--garbage', action='store_true', help='mix in corrupt frames and random bytes')
    a = ap.parse_args()

    if a.raw_out:
        if a.t0 is None:
            a.t0 = 1748470000.0
        write_raw(a)
    elif a.tcp:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', a.tcp))
        srv.listen(1)
        print(f'listening on 127.0.0.1:{a.tcp}', file=sys.stderr, flush=True)
        conn, _ = srv.accept()
        try:
            stream(a, conn.sendall)
        except (BrokenPipeError, ConnectionResetError):
            pass
        conn.close()
    else:
        master, slave = pty.openpty()
        tty.setraw(slave)
        print(os.ttyname(slave), flush=True)
        stream(a, lambda b: os.write(master, b))


if __name__ == '__main__':
    main()
