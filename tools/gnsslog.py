#!/usr/bin/env python3
"""
gnsslog.py — GNSS logger for road sessions (Emlid Reach ERB or NMEA)
---------------------------------------------------------------------------
Reads a GNSS stream and writes two files into the output directory:

  gnss.raw   every chunk exactly as received, with the host time of arrival
             (re-readable with --replay; format see RawWriter below)
  gnss.csv   one row per navigation epoch:
             t_host,t_gps,lat,lon,speed_mps,heading_deg,fix,n_sats,h_acc

Both parsers (ERB and NMEA) run over every byte of the stream: the ERB parser
skips text, the NMEA parser rejects binary garbage via its checksum. The stream format therefore never
has to be configured.

Sources (--source):
  reach                 Emlid Reach on USB serial (/dev/ttyACM0, ERB, 57600 Bd)
  serial:<dev>[@baud]   any serial device, e.g. serial:/dev/ttyUSB0@9600
  nmea:<dev>[@baud]     same as serial: (name kept for roadlog.sh --gnss)
  tcp:<host>:<port>     TCP stream, e.g. Reach "TCP server" output or a phone
                        running gpsdRelay (NMEA)

Serial and TCP sources are reopened automatically if they drop (USB hiccup,
phone hotspot), the raw log simply has a gap.

Usage:
  tools/gnsslog.py --source reach --out-dir logs/<session>
  tools/gnsslog.py --source tcp:192.168.2.15:9001 --out-dir /tmp/g
  tools/gnsslog.py --replay logs/<session>/gnss.raw [--csv out.csv]
  tools/gnsslog.py --probe /dev/ttyACM0      # exit 0 if GNSS data arrives (3 s)

CSV columns:
  t_host       host clock (Unix s) when the epoch's last message arrived —
               same clock as candump -L, so directly comparable
  t_gps        receiver time as UTC (Unix s); empty if not known yet
  lat, lon     degrees (WGS84)
  speed_mps    horizontal ground speed in m/s
  heading_deg  course over ground, 0 = north, clockwise. Derived from the
               velocity vector, so it is noise when standing still
  fix          0 none, 1 single (autonomous), 2 RTK float / DGPS, 3 RTK fix
  n_sats       satellites used
  h_acc        horizontal accuracy estimate in m (ERB: from the solution
               covariance; NMEA: from GST if sent, else empty)

ERB format — source: Emlid's own encoder, emlid/RTKLIB branch "reach",
src/erb.c (commit 92bf649f71), https://github.com/emlid/RTKLIB/blob/reach/src/erb.c
Cross-checked against ArduPilot's reader libraries/AP_GPS/AP_GPS_ERB.h and
against frames recorded from a first-generation Reach (ReachView v2.16.0,
02.10.2026): all payload lengths match, all checksums verify.
  frame:   'E' 'R' id len(u16 LE) payload ck_a ck_b
           Fletcher-8 over id, len and payload
  epoch:   VER, POS, STAT, DOPS, VEL, SVI, RTK — all with the same iTOW
  0x02 POS  (44): iTOW u32 ms, lon f64, lat f64, h_ell f64, h_msl f64,
                  accHor u32 mm, accVer u32 mm
  0x03 STAT  (9): iTOW u32, week u16, fixType u8 (0 none, 1 single,
                  2 float, 3 fix), fixStatus u8, numSV u8
  0x05 VEL  (28): iTOW u32, velN/velE/velD i32 cm/s (D = down),
                  speed u32 cm/s (2-D), heading i32 1e-5 deg (north = 0),
                  accS u32 (always 0 in erb.c)
"""
import argparse
import calendar
import math
import os
import signal
import socket
import struct
import sys
import termios
import time
import tty

GPS_EPOCH = 315964800          # 1980-01-06 00:00:00 UTC as Unix time
GPS_UTC_LEAP = 18              # GPS - UTC in s, valid since 2017-01-01
REACH_DEV = '/dev/ttyACM0'
REACH_BAUD = 57600

CSV_HEADER = 't_host,t_gps,lat,lon,speed_mps,heading_deg,fix,n_sats,h_acc'


# ── Epoch record + CSV ───────────────────────────────────────────────────────
def fmt(v, nd):
    return '' if v is None else f'{v:.{nd}f}'


def csv_row(e):
    if not e.get('fix'):
        # No solution: position/velocity fields are meaningless (ERB still
        # sends POS/VEL then). The row stays, so a reader sees "alive, no fix".
        e = {k: v for k, v in e.items() if k not in ('lat', 'lon', 'speed_mps', 'heading_deg', 'h_acc')}
    return ','.join([
        fmt(e['t_host'], 3), fmt(e.get('t_gps'), 3),
        fmt(e.get('lat'), 8), fmt(e.get('lon'), 8),
        fmt(e.get('speed_mps'), 3), fmt(e.get('heading_deg'), 2),
        str(e.get('fix', 0)), '' if e.get('n_sats') is None else str(e['n_sats']),
        fmt(e.get('h_acc'), 3),
    ])


# ── ERB parser ───────────────────────────────────────────────────────────────
class ErbParser:
    """Streaming ERB parser. feed() returns finished epochs (dicts)."""
    MAX_LEN = 1024

    def __init__(self):
        self.buf = bytearray()
        self.cur = None            # epoch being assembled, keyed by iTOW
        self.frames = 0
        self.errors = 0

    @staticmethod
    def checksum(data):
        a = b = 0
        for x in data:
            a = (a + x) & 0xFF
            b = (b + a) & 0xFF
        return a, b

    def _epoch(self, itow, t_host):
        """Return the epoch for itow; flush the previous one if itow changed."""
        done = None
        if self.cur is not None and self.cur['itow'] != itow:
            done = self.cur
            self.cur = None
        if self.cur is None:
            self.cur = {'itow': itow}
        self.cur['t_host'] = t_host
        return done

    def feed(self, data, t_host):
        out = []
        self.buf += data
        buf = self.buf
        i = 0
        while True:
            j = buf.find(b'ER', i)
            if j < 0:
                i = max(i, len(buf) - 1)
                break
            if len(buf) - j < 5:
                i = j
                break
            mid = buf[j + 2]
            ln = buf[j + 3] | buf[j + 4] << 8
            if ln > self.MAX_LEN:
                i = j + 1
                continue
            end = j + 5 + ln + 2
            if len(buf) < end:
                i = j
                break
            ck = self.checksum(buf[j + 2:j + 5 + ln])
            if ck != (buf[end - 2], buf[end - 1]):
                self.errors += 1
                i = j + 1
                continue
            self.frames += 1
            done = self._handle(mid, bytes(buf[j + 5:j + 5 + ln]), t_host)
            if done is not None:
                out.append(done)
            i = end
        del buf[:i]
        return out

    def _handle(self, mid, p, t_host):
        if mid == 0x02 and len(p) >= 44:
            itow, lon, lat, _h, _hmsl, acch, _accv = struct.unpack_from('<IddddII', p)
            done = self._epoch(itow, t_host)
            self.cur.update(lat=lat, lon=lon, h_acc=acch / 1000.0)
            return done
        if mid == 0x03 and len(p) >= 9:
            itow, week, fixtype, _fixstatus, nsv = struct.unpack_from('<IHBBB', p)
            done = self._epoch(itow, t_host)
            self.cur.update(fix=fixtype if fixtype <= 3 else 0, n_sats=nsv)
            if week > 0:
                self.cur['t_gps'] = GPS_EPOCH + week * 604800 + itow / 1000.0 - GPS_UTC_LEAP
            return done
        if mid == 0x05 and len(p) >= 28:
            itow, _vn, _ve, _vd, speed, heading, _accs = struct.unpack_from('<IiiiIiI', p)
            done = self._epoch(itow, t_host)
            self.cur.update(speed_mps=speed / 100.0, heading_deg=(heading / 1e5) % 360.0)
            return done
        if mid == 0x07:
            # RTK is the last message of an epoch in erb.c — flush now instead
            # of waiting for the next epoch (saves one epoch of latency).
            if self.cur is not None:
                self.cur['t_host'] = t_host
                done, self.cur = self.cur, None
                return done
        return None

    def flush(self):
        done, self.cur = self.cur, None
        return [done] if done is not None else []


# ── NMEA parser ──────────────────────────────────────────────────────────────
GGA_FIX = {0: 0, 1: 1, 2: 2, 3: 1, 4: 3, 5: 2, 6: 0, 7: 0, 8: 0}


def nmea_deg(v, hemi):
    if not v:
        return None
    d = float(v)
    deg = int(d // 100)
    val = deg + (d - deg * 100) / 60.0
    return -val if hemi in ('S', 'W') else val


class NmeaParser:
    """Streaming NMEA parser (RMC, GGA, GST, any talker). Epochs are grouped
    by the UTC time field; an epoch is emitted when the time changes."""
    MAX_LINE = 120

    def __init__(self):
        self.line = bytearray()
        self.cur = None
        self.date = None            # (y, m, d) from the last RMC
        self.frames = 0
        self.errors = 0

    def feed(self, data, t_host):
        out = []
        for byte in data:
            if byte == 0x24:                       # '$' starts a sentence
                self.line = bytearray(b'$')
            elif byte in (0x0A, 0x0D):
                if len(self.line) > 6:
                    out += self._sentence(bytes(self.line), t_host)
                self.line = bytearray()
            elif self.line:
                if 0x20 <= byte < 0x7F and len(self.line) < self.MAX_LINE:
                    self.line.append(byte)
                else:
                    self.line = bytearray()        # binary garbage
        return out

    def _sentence(self, s, t_host):
        try:
            txt = s.decode('ascii')
        except UnicodeDecodeError:
            return []
        star = txt.rfind('*')
        if star < 0 or len(txt) < star + 3:
            self.errors += 1
            return []
        cs = 0
        for ch in txt[1:star]:
            cs ^= ord(ch)
        try:
            if cs != int(txt[star + 1:star + 3], 16):
                self.errors += 1
                return []
        except ValueError:
            self.errors += 1
            return []
        self.frames += 1
        f = txt[1:star].split(',')
        kind = f[0][2:]
        try:
            if kind == 'RMC' and len(f) >= 10:
                return self._rmc(f, t_host)
            if kind == 'GGA' and len(f) >= 10:
                return self._gga(f, t_host)
            if kind == 'GST' and len(f) >= 8:
                return self._gst(f, t_host)
        except ValueError:
            self.errors += 1
        return []

    def _epoch(self, hms, t_host):
        done = []
        if self.cur is not None and self.cur['hms'] != hms:
            done.append(self._finish(self.cur))
            self.cur = None
        if self.cur is None:
            self.cur = {'hms': hms}
        self.cur['t_host'] = t_host
        return done

    def _finish(self, e):
        if self.date and e['hms']:
            h, m, sec = int(e['hms'][0:2]), int(e['hms'][2:4]), float(e['hms'][4:])
            y, mo, d = self.date
            e['t_gps'] = calendar.timegm((y, mo, d, h, m, 0, 0, 0, 0)) + sec
        e.setdefault('fix', 0)
        return e

    def _rmc(self, f, t_host):
        done = self._epoch(f[1], t_host)
        if len(f[9]) == 6:
            self.date = (2000 + int(f[9][4:6]), int(f[9][2:4]), int(f[9][0:2]))
        if f[2] == 'A':
            self.cur['lat'] = nmea_deg(f[3], f[4])
            self.cur['lon'] = nmea_deg(f[5], f[6])
            if f[7]:
                self.cur['speed_mps'] = float(f[7]) * 0.514444
            if f[8]:
                self.cur['heading_deg'] = float(f[8]) % 360.0
            self.cur.setdefault('fix', 1)
        return done

    def _gga(self, f, t_host):
        done = self._epoch(f[1], t_host)
        q = int(f[6] or 0)
        self.cur['fix'] = GGA_FIX.get(q, 0)
        if f[7]:
            self.cur['n_sats'] = int(f[7])
        if q > 0 and f[2]:
            self.cur.setdefault('lat', nmea_deg(f[2], f[3]))
            self.cur.setdefault('lon', nmea_deg(f[4], f[5]))
        return done

    def _gst(self, f, t_host):
        done = self._epoch(f[1], t_host)
        if f[6] and f[7]:
            self.cur['h_acc'] = math.hypot(float(f[6]), float(f[7]))
        return done

    def flush(self):
        done, self.cur = self.cur, None
        return [self._finish(done)] if done is not None else []


# ── Raw file: magic line, then records <t_host f64><len u32><bytes> ──────────
RAW_MAGIC = b'GNSSRAW1\n'
REC = struct.Struct('<dI')


class RawWriter:
    def __init__(self, path):
        self.f = open(path, 'ab')
        if self.f.tell() == 0:
            self.f.write(RAW_MAGIC)

    def write(self, t, data):
        self.f.write(REC.pack(t, len(data)) + data)
        self.f.flush()

    def close(self):
        self.f.close()


def raw_records(path):
    with open(path, 'rb') as f:
        if f.read(len(RAW_MAGIC)) != RAW_MAGIC:
            raise SystemExit(f'{path}: not a gnsslog raw file')
        while True:
            h = f.read(REC.size)
            if len(h) < REC.size:
                return
            t, n = REC.unpack(h)
            data = f.read(n)
            if len(data) < n:
                return                       # truncated last record (power loss)
            yield t, data


# ── Sources ──────────────────────────────────────────────────────────────────
BAUDS = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
         57600: termios.B57600, 115200: termios.B115200, 230400: termios.B230400}


def parse_source(spec):
    if spec == 'reach':
        return ('serial', REACH_DEV, REACH_BAUD)
    kind, _, rest = spec.partition(':')
    if kind in ('serial', 'nmea') and rest:
        dev, _, baud = rest.partition('@')
        return ('serial', dev, int(baud) if baud else 9600)
    if kind == 'tcp' and rest:
        host, _, port = rest.rpartition(':')
        if host and port.isdigit():
            return ('tcp', host, int(port))
    raise SystemExit(f'unknown --source "{spec}" '
                     '(reach | serial:<dev>[@baud] | nmea:<dev>[@baud] | tcp:<host>:<port>)')


def open_serial(dev, baud):
    fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY)
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    b = BAUDS.get(baud)
    if b is not None:                       # CDC-ACM ignores the baud rate anyway
        attrs[4] = attrs[5] = b
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 5             # read() returns after 0.5 s idle
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return lambda: os.read(fd, 4096), lambda: os.close(fd)


def open_tcp(host, port):
    s = socket.create_connection((host, port), timeout=5)
    s.settimeout(0.5)

    def rd():
        try:
            d = s.recv(4096)
        except socket.timeout:
            return b''
        if not d:
            raise ConnectionError('peer closed the connection')
        return d
    return rd, s.close


def open_source(src):
    kind, a, b = src
    return open_serial(a, b) if kind == 'serial' else open_tcp(a, b)


def describe(src):
    kind, a, b = src
    return f'{a} @ {b} Bd' if kind == 'serial' else f'tcp {a}:{b}'


class Stop(Exception):
    pass


def _stop(*_):
    raise Stop()


# ── Modes ────────────────────────────────────────────────────────────────────
def log(msg):
    print(f'[gnsslog {time.strftime("%H:%M:%S")}] {msg}', file=sys.stderr, flush=True)


class Epochs:
    """Runs both parsers and keeps track of which format is seen."""
    def __init__(self):
        self.erb, self.nmea = ErbParser(), NmeaParser()

    def feed(self, data, t):
        return self.erb.feed(data, t) + self.nmea.feed(data, t)

    def flush(self):
        return self.erb.flush() + self.nmea.flush()

    def fmt(self):
        if self.erb.frames and self.nmea.frames:
            return 'ERB+NMEA'
        return 'ERB' if self.erb.frames else ('NMEA' if self.nmea.frames else '—')


def run_logger(src, out_dir, stale_s):
    os.makedirs(out_dir, exist_ok=True)
    raw = RawWriter(os.path.join(out_dir, 'gnss.raw'))
    csv_path = os.path.join(out_dir, 'gnss.csv')
    new = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    csv = open(csv_path, 'a', buffering=1)
    if new:
        csv.write(CSV_HEADER + '\n')
    ep = Epochs()
    # SIGTERM/SIGINT raise Stop, so a blocking read or a retry sleep is left
    # at once (roadlog.sh sends SIGKILL 0.5 s after SIGTERM). Every raw
    # record and CSV row is a single write, so nothing is left half-written.
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log(f'source {describe(src)} → {out_dir}/gnss.raw, gnss.csv')
    try:
        _log_loop(src, ep, raw, csv, stale_s)
    except Stop:
        pass
    for e in ep.flush():
        csv.write(csv_row(e) + '\n')
    csv.close()
    raw.close()
    log(f'stopped: format {ep.fmt()}, '
        f'ERB {ep.erb.frames} ok / {ep.erb.errors} bad, NMEA {ep.nmea.frames} ok / {ep.nmea.errors} bad')


def _log_loop(src, ep, raw, csv, stale_s):
    """Read, log and parse until Stop; reopen the source whenever it drops."""
    last_fmt = None
    while True:
        try:
            read, close = open_source(src)
        except OSError as e:
            log(f'cannot open {describe(src)}: {e} — retry in 2 s')
            time.sleep(2)
            continue
        log('connected')
        last_data = time.time()
        try:
            while True:
                data = read()
                t = time.time()
                if data:
                    last_data = t
                    raw.write(t, data)
                    for e in ep.feed(data, t):
                        csv.write(csv_row(e) + '\n')
                    if ep.fmt() != last_fmt:
                        last_fmt = ep.fmt()
                        log(f'format: {last_fmt}')
                elif t - last_data > stale_s:
                    raise ConnectionError(f'no data for {stale_s:.0f} s')
        except (OSError, ConnectionError) as e:
            log(f'lost source: {e} — reconnecting')
        finally:
            close()
        time.sleep(1)


def run_replay(path, out):
    ep = Epochs()
    f = open(out, 'w') if out else sys.stdout
    f.write(CSV_HEADER + '\n')
    n = 0
    for t, data in raw_records(path):
        for e in ep.feed(data, t):
            f.write(csv_row(e) + '\n')
            n += 1
    for e in ep.flush():
        f.write(csv_row(e) + '\n')
        n += 1
    if out:
        f.close()
    log(f'replay: {n} epochs, format {ep.fmt()}, ERB {ep.erb.frames} ok / {ep.erb.errors} bad, '
        f'NMEA {ep.nmea.frames} ok / {ep.nmea.errors} bad')


def run_probe(spec, seconds):
    """Exit 0 and print the format if valid GNSS frames arrive within `seconds`."""
    src = parse_source(spec) if ':' in spec or spec == 'reach' else ('serial', spec, REACH_BAUD)
    try:
        read, close = open_source(src)
    except OSError:
        return 1
    ep = Epochs()
    t_end = time.time() + seconds
    try:
        while time.time() < t_end:
            ep.feed(read(), time.time())
            if ep.erb.frames >= 3 or ep.nmea.frames >= 2:
                print(ep.fmt())
                return 0
    except (OSError, ConnectionError):
        return 1
    finally:
        close()
    return 1


def main():
    ap = argparse.ArgumentParser(description='GNSS logger (Emlid Reach ERB or NMEA) → gnss.raw + gnss.csv')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--source', help='reach | serial:<dev>[@baud] | nmea:<dev>[@baud] | tcp:<host>:<port>')
    g.add_argument('--replay', metavar='RAW', help='re-parse a gnss.raw file into CSV')
    g.add_argument('--probe', metavar='DEV', help='check whether GNSS data arrives on DEV')
    ap.add_argument('--out-dir', default='.', help='directory for gnss.raw / gnss.csv (logger mode)')
    ap.add_argument('--csv', help='CSV output for --replay (default: stdout)')
    ap.add_argument('--seconds', type=float, default=3.0, help='probe duration (default 3 s)')
    ap.add_argument('--stale', type=float, default=5.0,
                    help='reconnect after this many seconds without data (default 5)')
    a = ap.parse_args()
    if a.replay:
        run_replay(a.replay, a.csv)
    elif a.probe:
        sys.exit(run_probe(a.probe, a.seconds))
    else:
        run_logger(parse_source(a.source), a.out_dir, a.stale)


if __name__ == '__main__':
    main()
