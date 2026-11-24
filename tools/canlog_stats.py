#!/usr/bin/env python3
"""
canlog_stats.py — Distills a can_radar recording into a summary.

Reads a candump -L log file (.log or .log.gz), decodes the radar tracks
via the Toyota ADAS DBC and prints aggregates: frames per ID + rate,
valid-track ratio, distance/lateral/v_rel ranges. Streams line by line —
constant RAM use, whether the log is 1 MB or 1 GB.

Usage:
  tools/canlog_stats.py logs/<session>/can_radar.log
  tools/canlog_stats.py logs/<session>/can_radar.log.gz
"""
import gzip
import os
import re
import sys
from collections import defaultdict

import radar_dbc

LINE_RE = re.compile(r'\(([\d.]+)\)\s+(\S+)\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]*)')


def opener(path):
    """Opens .log or .log.gz transparently."""
    if path.endswith('.gz'):
        return gzip.open(path, 'rt')
    return open(path, 'rt')


def main(path):
    db = radar_dbc.load()

    n_total = 0
    per_id = defaultdict(int)            # id -> frame count
    ts_first = ts_last = None
    track_valid = defaultdict(int)       # id -> valid frames
    track_total = defaultdict(int)       # id -> total track frames
    # Ranges over valid tracks:
    span = {'dist': [1e9, -1e9], 'lat': [1e9, -1e9], 'vrel': [1e9, -1e9]}
    n_valid_overall = 0

    with opener(path) as fh:
        for line in fh:
            m = LINE_RE.match(line)
            if not m:
                continue
            ts = float(m.group(1))
            can_id = int(m.group(3), 16)
            data = bytes.fromhex(m.group(4))

            n_total += 1
            per_id[can_id] += 1
            if ts_first is None:
                ts_first = ts
            ts_last = ts

            if 0x210 <= can_id <= 0x21F:
                track_total[can_id] += 1
                try:
                    dec = db.decode_message(can_id, data)
                except Exception:
                    continue
                if dec.get('VALID') == 1:
                    track_valid[can_id] += 1
                    n_valid_overall += 1
                    d = float(dec.get('LONG_DIST', 0.0))
                    la = float(dec.get('LAT_DIST', 0.0))
                    vr = float(dec.get('REL_SPEED', 0.0))
                    span['dist'][0] = min(span['dist'][0], d); span['dist'][1] = max(span['dist'][1], d)
                    span['lat'][0]  = min(span['lat'][0], la); span['lat'][1]  = max(span['lat'][1], la)
                    span['vrel'][0] = min(span['vrel'][0], vr); span['vrel'][1] = max(span['vrel'][1], vr)

    dur = (ts_last - ts_first) if (ts_first and ts_last and ts_last > ts_first) else 0.0
    return n_total, per_id, dur, track_total, track_valid, n_valid_overall, span


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(1)
    path = sys.argv[1]
    n_total, per_id, dur, track_total, track_valid, n_valid, span = main(path)

    print(f"\nFile       : {path}  ({os.path.getsize(path)/1024:.0f} KB)")
    print(f"Frames     : {n_total}")
    print(f"Duration   : {dur:.1f} s  ({n_total/dur:.0f} fps)" if dur else "Duration   : (unknown)")
    print(f"CAN IDs    : {len(per_id)} distinct")

    print("\nTop IDs by frame count:")
    for cid, cnt in sorted(per_id.items(), key=lambda kv: -kv[1])[:10]:
        hz = cnt/dur if dur else 0
        print(f"  0x{cid:03X}  {cnt:7d}  {hz:6.1f} Hz")

    print(f"\nTrack slots 0x210–0x21F  (valid frames: {n_valid}):")
    for cid in range(0x210, 0x220):
        if track_total.get(cid):
            v = track_valid.get(cid, 0)
            pct = 100*v/track_total[cid]
            flag = '  ← active' if v else ''
            print(f"  0x{cid:03X}  {track_total[cid]:5d} frames  {v:5d} valid ({pct:4.0f}%){flag}")

    if n_valid:
        print("\nRanges over valid tracks:")
        print(f"  LONG_DIST : {span['dist'][0]:6.2f} … {span['dist'][1]:6.2f} m")
        print(f"  LAT_DIST  : {span['lat'][0]:6.2f} … {span['lat'][1]:6.2f} m")
        print(f"  REL_SPEED : {span['vrel'][0]:+6.2f} … {span['vrel'][1]:+6.2f} m/s")
    else:
        print("\n(No valid tracks in the log — nothing reflective in front of the radar"
              " or the DSU spoof wasn't running. Decode path is identical to the valid case.)")
