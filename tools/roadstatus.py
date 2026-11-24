#!/usr/bin/env python3
"""
roadstatus.py — live status line for a running roadlog.sh session.

Reads the session's growing log files (can_radar.log, gnss.csv) like
`tail -f`; it opens no CAN socket and does not touch the recording.
Once per second it rewrites one line:

  spoof ok | valid 3 | next 23.4 m  -1.2 m/s | GNSS fix1 47 km/h | 00:12:31

  valid    radar track slots (0x210–0x21F) whose latest frame is VALID
  next     nearest valid track in the driving corridor (|lat| < --lane m):
           distance and v_rel (negative = closing in)
  GNSS     fix type (see gnsslog.py) and ground speed; "stale" if no epoch
           for 3 s, "off" if the session logs no GNSS

Restarts of candump or the spoof process by roadlog.sh's supervisor (restarts.log)
show up in yellow as "restarts n".

The line turns red, starts with "!!" and rings the terminal bell (every
5 s while it lasts) when
  * the spoof process is gone, or
  * no frame arrived on can_radar for more than 5 s.

roadlog.sh runs it in the foreground; standalone, for a session that is
recording in another terminal:
  tools/roadstatus.py logs/<session>
"""
import argparse
import csv
import io
import os
import re
import shutil
import sys
import time

import radar_dbc

LINE_RE = re.compile(r'\(([\d.]+)\)\s+(\S+)\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]*)')
TRACK_HOLD = 0.5      # s a slot counts as current after its last frame
CAN_SLOW = 2.0        # s without can_radar frames → shown in yellow
CAN_STALE = 5.0       # s without can_radar frames → warning
GNSS_STALE = 3.0      # s without a GNSS epoch → "stale"
BELL_EVERY = 5.0      # s between bells while a warning lasts

RED, YEL, GRN, DIM, OFF = '\033[1;97;41m', '\033[33m', '\033[32m', '\033[2m', '\033[0m'


class Tail:
    """Reads lines appended to a file since the last call (file may appear later)."""
    def __init__(self, path, from_start=False):
        self.path, self.f, self.buf = path, None, ''
        self.from_start = from_start

    def lines(self):
        if self.f is None:
            try:
                self.f = open(self.path, 'r', errors='replace')
            except OSError:
                return []
            if not self.from_start:
                self.f.seek(0, io.SEEK_END)
        data = self.f.read()
        if not data:
            return []
        data = self.buf + data
        parts = data.split('\n')
        self.buf = parts.pop()          # incomplete last line, finish next time
        return parts


class Radar:
    def __init__(self, path, lane):
        self.tail = Tail(path)
        self.db = radar_dbc.load()
        self.lane = lane
        self.slots = {}                 # id → (t, valid, dist, lat, vrel)
        self.last_frame = None          # host time of the newest can_radar frame

    def update(self):
        for line in self.tail.lines():
            m = LINE_RE.match(line)
            if not m:
                continue
            t = float(m.group(1))
            self.last_frame = t if self.last_frame is None else max(self.last_frame, t)
            cid = int(m.group(3), 16)
            if not 0x210 <= cid <= 0x21F:
                continue
            try:
                d = self.db.decode_message(cid, bytes.fromhex(m.group(4)))
            except Exception:
                continue
            self.slots[cid] = (t, d.get('VALID') == 1, float(d.get('LONG_DIST', 0.0)),
                               float(d.get('LAT_DIST', 0.0)), float(d.get('REL_SPEED', 0.0)))

    def summary(self):
        """(n_valid, nearest (dist, vrel) in the corridor or None)."""
        if self.last_frame is None:
            return 0, None
        cur = [s for s in self.slots.values() if s[1] and self.last_frame - s[0] <= TRACK_HOLD]
        lane = [s for s in cur if abs(s[3]) < self.lane]
        near = min(lane, key=lambda s: s[2]) if lane else None
        return len(cur), (near[2], near[4]) if near else None


class Gnss:
    def __init__(self, path):
        self.tail = Tail(path, from_start=True)
        self.exists = False
        self.last = None                # newest row as dict

    def update(self):
        self.exists = self.exists or os.path.exists(self.tail.path)
        for line in self.tail.lines():
            if line.startswith('t_host') or not line.strip():
                continue
            row = next(csv.reader([line]))
            if len(row) >= 9:
                self.last = row


def spoof_alive(pid):
    if pid:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    # No pid given (roadlog --no-spoof / standalone): look for a process that
    # *runs* the script — python … spoof_dsu.py, or ./spoof_dsu.py itself.
    # A plain substring match would also hit `vim spoof_dsu.py` or a shell
    # whose command line mentions the name.
    for p in os.listdir('/proc'):
        if not p.isdigit() or int(p) == os.getpid():
            continue
        try:
            with open(f'/proc/{p}/cmdline', 'rb') as f:
                args = [os.path.basename(a) for a in f.read().split(b'\0') if a]
        except OSError:
            continue
        if not args:
            continue
        if args[0] == b'spoof_dsu.py':
            return True
        if args[0].startswith(b'python') and len(args) > 1:
            script = next((a for a in args[1:] if not a.startswith(b'-')), b'')
            if script == b'spoof_dsu.py':
                return True
    return False


def count_restarts(session):
    """Lines in restarts.log (written by roadlog.sh's supervisor)."""
    try:
        with open(os.path.join(session, 'restarts.log'), 'rb') as f:
            return f.read().count(b'\n')
    except OSError:
        return 0


def fmt_dur(s):
    s = int(s)
    return f'{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}'


def status_line(radar, gnss, spoof_pid, t_start, now, restarts=0):
    warn = []
    parts = []

    if spoof_alive(spoof_pid):
        parts.append(f'{GRN}spoof ok{OFF}')
    else:
        parts.append('SPOOF DEAD')
        warn.append("spoof")

    age = None if radar.last_frame is None else now - radar.last_frame
    if (age is None and now - t_start > CAN_STALE) or (age is not None and age > CAN_STALE):
        warn.append('can')
        parts.append('NO CAN ' + ('yet' if age is None else f'{age:.0f} s'))
    else:
        n, near = radar.summary()
        if age is not None and age > CAN_SLOW:
            parts.append(f'{YEL}CAN {age:.0f} s old{OFF}')
        parts.append(f'valid {n:2d}')
        parts.append(f'next {near[0]:5.1f} m {near[1]:+5.1f} m/s' if near else f'next {DIM}  —{OFF}')

    if not gnss.exists:
        parts.append(f'{DIM}GNSS off{OFF}')
    elif gnss.last is None:
        parts.append(f'{YEL}GNSS waiting{OFF}')
    else:
        r = gnss.last
        g_age = now - float(r[0])
        fix = int(r[6] or 0)
        if g_age > GNSS_STALE:
            parts.append(f'{YEL}GNSS stale {g_age:.0f} s{OFF}')
        elif fix == 0:
            parts.append(f'{YEL}GNSS no fix ({r[7] or "?"} sat){OFF}')
        else:
            kmh = float(r[4]) * 3.6 if r[4] else float('nan')
            parts.append(f'GNSS fix{fix} {kmh:3.0f} km/h')

    if restarts:
        parts.append(f'{YEL}restarts {restarts}{OFF}')
    parts.append(fmt_dur(now - t_start))
    line = ' | '.join(parts)
    if warn:
        plain = re.sub(r'\033\[[0-9;]*m', '', line)
        line = f'{RED}!! {plain} !!{OFF}'
    return line, bool(warn)


def fit(line, warn, width):
    """Shorten to the terminal width: a wrapped line breaks the \\r overwrite
    and stacks up lines. Colors are dropped when cutting (simpler than
    cutting around escape codes); a warning stays red."""
    plain = re.sub(r'\033\[[0-9;]*m', '', line)
    if len(plain) <= width:
        return line
    plain = plain[:max(width, 10)]
    return f'{RED}{plain}{OFF}' if warn else plain


def main():
    ap = argparse.ArgumentParser(description='Live status line for a roadlog.sh session.')
    ap.add_argument('session', help='session folder (logs/<session>)')
    ap.add_argument('--spoof-pid', type=int, default=0, help='PID of spoof_dsu.py (else searched)')
    ap.add_argument('--start', type=float, default=None, help='recording start, Unix time (default: now)')
    ap.add_argument('--lane', type=float, default=1.5, help='driving corridor half-width in m (default 1.5)')
    ap.add_argument('--once', action='store_true', help='print one status line and exit (for tests)')
    a = ap.parse_args()

    radar = Radar(os.path.join(a.session, 'can_radar.log'), a.lane)
    gnss = Gnss(os.path.join(a.session, 'gnss.csv'))
    t_start = a.start or time.time()
    last_bell = 0.0
    tty = sys.stdout.isatty()
    try:
        while True:
            radar.update()
            gnss.update()
            now = time.time()
            line, warn = status_line(radar, gnss, a.spoof_pid, t_start, now,
                                     count_restarts(a.session))
            if a.once:
                print(line)
                return
            if tty:
                line = fit(line, warn, shutil.get_terminal_size().columns - 1)
            bell = ''
            if warn and now - last_bell >= BELL_EVERY:
                bell, last_bell = '\a', now
            if tty:
                sys.stdout.write(f'\r\033[K{bell}{line}')
            else:
                sys.stdout.write(f'{bell}{line}\n')
            sys.stdout.flush()
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        if tty:
            sys.stdout.write('\n')


if __name__ == '__main__':
    main()
