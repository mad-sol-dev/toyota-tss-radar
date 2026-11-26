#!/usr/bin/env python3
"""
radarmap.py — bird's-eye view of the Toyota radar tracks.

Three modes:
  1) Offline movie:  tools/radarmap.py logs/<s>/can_radar.log
                     (plays the recording back as an animation; needs display)
  2) PNG overview:   tools/radarmap.py logs/<s>/can_radar.log --png map.png
                     (all valid detections as a scatter plot; headless)
  3) Live:           candump -L can_radar | tools/radarmap.py
                     (candump without -L works too)
                     (real-time view; needs display)

Axes: x = lateral offset (LAT_DIST), y = forward distance (LONG_DIST).
Ego sits at the origin, direction of travel = up.
Color = v_rel:  red = approaching (v_rel<0),  blue = receding (v_rel>0).

Movie and live mode have two views and an adjustable range:
  v                      toggle top view / front view
  + / -, up / down,      next / previous range (6, 10, 20, 40, 80, 150 m)
  mouse wheel
The front view looks like the windscreen: each track is a box at its bearing,
shrinking with distance. The box size (1.8 x 1.5 m) and the eye height
(1.2 m) are ASSUMED — the radar reports no height. --view front starts there.

Accepts .log and .log.gz.
"""
import argparse
import gzip
import os
import re
import sys

import radar_dbc

# candump -L:            (1795656438.746083) can_radar 210#A6FFF800000000B7
LINE_RE = re.compile(r'\(([\d.]+)\)\s+(\S+)\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]*)')
# candump without -L:     can_radar  210   [8]  A6 FF F8 00 00 00 00 B7
# (optionally with a -t timestamp in front, e.g. -ta: (1795656438.746083))
PLAIN_RE = re.compile(r'\s*(?:\(([^)]*)\)\s+)?(\S+)\s+([0-9A-Fa-f]{3,8})\s+\[\d+\]\s+'
                      r'((?:[0-9A-Fa-f]{2}\s*)*)')
VMAX = 15.0          # m/s color scale clip (--vmax; e.g. 2 on the bench)
ARROW_S = 1.0        # s, the v_rel arrow points to where the track is in this time
HOLD = 0.30          # s, how long a track stays visible without an update
FRAME_DT = 0.10      # s, animation time step
RANGES = [6, 10, 20, 40, 80, 150]   # m, steps for + / - in the movie / live view
EYE_H = 1.2          # m, assumed eye height of the front view
OBJ_W, OBJ_H = 1.8, 1.5   # m, assumed box size in the front view (no height from the radar)
LANE = 1.5           # m, half-width of the driving corridor drawn in the front view


DB = radar_dbc.load()


def _open(path):
    return gzip.open(path, 'rt') if path.endswith('.gz') else open(path, 'rt')


def decode_line(line):
    """candump line -> (ts, cid, lat, lon, vrel) for a valid track, else None.

    Reads the -L log format and candump's default screen format. ts is None
    when the line carries no numeric timestamp (fine for the live view, which
    uses the arrival time)."""
    m = LINE_RE.match(line) or PLAIN_RE.match(line)
    if not m:
        return None
    cid = int(m.group(3), 16)
    if not (0x210 <= cid <= 0x21F):
        return None
    try:
        dec = DB.decode_message(cid, bytes.fromhex(m.group(4).replace(' ', '')))
    except Exception:
        return None
    if dec.get('VALID') != 1:
        return None
    try:
        ts = float(m.group(1))
    except (TypeError, ValueError):
        ts = None
    return (ts, cid,
            float(dec.get('LAT_DIST', 0.0)),
            float(dec.get('LONG_DIST', 0.0)),
            float(dec.get('REL_SPEED', 0.0)))


def parse_log(path):
    """Reads log file -> sorted list of valid detections (relative to ts0)."""
    recs = []
    with _open(path) as fh:
        for line in fh:
            r = decode_line(line)
            if r and r[0] is not None:
                recs.append(r)
    if not recs:
        print(f"[radarmap] no valid tracks with timestamps in {path} "
              "(log files need candump -L or a -t timestamp)", file=sys.stderr)
        return [], 0.0
    t0 = recs[0][0]
    recs = [(t - t0, cid, la, lo, vr) for (t, cid, la, lo, vr) in recs]
    return recs, recs[-1][0]


def _setup_axes(ax, max_range):
    ax.set_xlim(-12, 12)
    ax.set_ylim(0, max_range)
    # + = right in the driving direction (bench test, Nov 2026, radar upright;
    # roadreport.py re-checks it from oncoming traffic).
    ax.set_xlabel('Lateral  LAT_DIST [m]   (left – | + right)')
    ax.set_ylabel('Forward  LONG_DIST [m]  (direction of travel →)')
    ax.set_aspect('equal', adjustable='box')
    ax.grid(True, alpha=0.2)
    for r in range(10, int(max_range) + 1, 10):       # range rings
        ax.axhline(r, color='gray', lw=0.5, ls='--', alpha=0.4)
        ax.text(11.5, r, f'{r} m', va='center', ha='right', fontsize=7, color='gray')
    ax.plot(0, 0, marker='^', ms=14, color='black', zorder=5)   # Ego
    ax.text(0, -1.5, 'EGO', ha='center', fontsize=8, color='black')


class TrackLayer:
    """Dots colored by v_rel, plus per track an arrow to its position in
    ARROW_S seconds and the v_rel value as text — color alone is hard to read
    at small speeds."""
    def __init__(self, ax, vmax, colorbar=True):
        import numpy as np
        self.np, self.ax = np, ax
        self.sc = ax.scatter([], [], c=[], cmap='coolwarm_r', vmin=-vmax, vmax=vmax,
                             s=60, edgecolors='k', linewidths=0.4, zorder=3)
        self.labels = []                 # texts and the arrow set, redrawn per frame
        if colorbar:
            ax.figure.colorbar(self.sc, ax=ax, shrink=0.6, pad=0.02).set_label(
                'v_rel [m/s] (red = approaching)')

    def set(self, pts):
        """pts: list of (lat, lon, vrel)."""
        np = self.np
        for t in self.labels:
            t.remove()
        self.labels = []
        if not pts:
            self.sc.set_offsets(np.empty((0, 2)))
            return
        a = np.array(pts, dtype=float)
        self.sc.set_offsets(a[:, :2])
        self.sc.set_array(a[:, 2])
        # radial motion only: v_rel is along the line of sight
        r = np.hypot(a[:, 0], a[:, 1])
        r[r == 0] = 1
        u = a[:, 0] / r * a[:, 2] * ARROW_S
        v = a[:, 1] / r * a[:, 2] * ARROW_S
        # a quiver cannot change its number of arrows → new one per frame
        self.labels.append(self.ax.quiver(a[:, 0], a[:, 1], u, v, angles='xy', scale_units='xy',
                                          scale=1, width=0.006, color='k', zorder=4))
        for la, lo, vr in pts:
            self.labels.append(self.ax.annotate(
                f'{vr:+.1f}', (la, lo), xytext=(7, 4), textcoords='offset points',
                fontsize=9, fontweight='bold', zorder=5,
                color=_vcolor(vr)))


def _vcolor(vr):
    return 'firebrick' if vr < -0.3 else ('navy' if vr > 0.3 else 'dimgray')


def _nice_step(x):
    """1, 2, 5, 10, 20 … just above x."""
    import math
    e = 10 ** math.floor(math.log10(x))
    return next(m * e for m in (1, 2, 5, 10) if m * e >= x)


class Display:
    """Movie / live window: top view and front view on the same spot (one is
    hidden), shared colorbar, range steps via keys and mouse wheel."""

    def __init__(self, max_range, vmax, view='top'):
        import matplotlib.pyplot as plt
        from matplotlib import cm, colors
        # 'v' is matplotlib's "forward" key by default — take it for the view toggle
        plt.rcParams['keymap.forward'] = [k for k in plt.rcParams['keymap.forward'] if k != 'v']
        self.plt = plt
        self.fig = plt.figure(figsize=(8, 9))
        rect = [0.09, 0.08, 0.72, 0.82]
        self.ax_top = self.fig.add_axes(rect)
        self.ax_front = self.fig.add_axes(rect)
        self.norm = colors.Normalize(-vmax, vmax)
        self.cmap = plt.get_cmap('coolwarm_r')
        cax = self.fig.add_axes([0.86, 0.3, 0.025, 0.4])
        self.fig.colorbar(cm.ScalarMappable(norm=self.norm, cmap=self.cmap), cax=cax).set_label(
            'v_rel [m/s] (red = approaching)')
        self.top = TrackLayer(self.ax_top, vmax, colorbar=False)
        self.front_art = []
        self.static = []
        self.pts = []
        self.view = view
        self.range = max_range
        self.title = self.fig.suptitle('')
        self.title_text = ''
        self.fig.text(0.01, 0.01, 'keys:  v = top / front view    + / −  or wheel = range',
                      fontsize=8, color='gray')
        self.fig.canvas.mpl_connect('key_press_event', self._on_key)
        self.fig.canvas.mpl_connect('scroll_event', self._on_scroll)
        self._layout()

    # ── controls ──
    def _step_range(self, d):
        steps = sorted(set(RANGES) | {self.range})
        i = steps.index(self.range)
        self.range = steps[max(0, min(len(steps) - 1, i + d))]
        self._layout()

    def _on_key(self, ev):
        if ev.key == 'v':
            self.view = 'front' if self.view == 'top' else 'top'
            self._layout()
        elif ev.key in ('+', '=', 'up'):
            self._step_range(+1)
        elif ev.key in ('-', 'down'):
            self._step_range(-1)

    def _on_scroll(self, ev):
        self._step_range(-1 if ev.button == 'up' else +1)   # wheel up = zoom in

    # ── static parts ──
    def _layout(self):
        for a in self.static:
            a.remove()
        self.static = []
        self._layout_top()
        self._layout_front()
        self.ax_top.set_visible(self.view == 'top')
        self.ax_front.set_visible(self.view == 'front')
        self.set(self.pts)
        self.fig.canvas.draw_idle()

    def _layout_top(self):
        ax, rng = self.ax_top, self.range
        half = min(12.0, max(3.0, rng / 2))
        ax.set_xlim(-half, half)
        ax.set_ylim(-rng * 0.04, rng)
        ax.set_aspect('equal', adjustable='box')
        ax.set_xlabel('Lateral  LAT_DIST [m]   (left – | + right)')
        ax.set_ylabel('Forward  LONG_DIST [m]  (direction of travel →)')
        ax.grid(True, alpha=0.2)
        step = _nice_step(rng / 4)
        r = step
        while r <= rng:
            self.static.append(ax.axhline(r, color='gray', lw=0.5, ls='--', alpha=0.4))
            self.static.append(ax.text(half * 0.96, r, f'{r:g} m', va='bottom', ha='right',
                                       fontsize=7, color='gray'))
            r += step
        self.static += ax.plot(0, 0, marker='^', ms=14, color='black', zorder=5)

    def _layout_front(self):
        import math
        import numpy as np
        ax, rng = self.ax_front, self.range
        half = max(8.0, min(30.0, 600.0 / rng))              # horizontal field ±deg: 8° at 80 m, 30° at 20 m
        low = -min(35.0, math.degrees(math.atan(EYE_H / (rng * 0.15))))
        ax.set_xlim(-half, half)
        ax.set_ylim(low, -low * 0.6)
        ax.set_aspect('equal', adjustable='box')
        ax.set_xlabel('bearing [°]   (left – | + right)')
        ax.set_ylabel(f'elevation [°]   (eye height {EYE_H:g} m, assumed)')
        ax.set_facecolor('#f4f4f4')
        self.static.append(ax.axhline(0, color='0.4', lw=0.8))                      # horizon
        self.static.append(ax.axhspan(low, 0, color='0.88', zorder=0))              # road
        d = np.linspace(max(0.5, rng * 0.02), rng, 200)
        el = -np.degrees(np.arctan(EYE_H / d))
        for side in (-1, 1):                                                        # corridor
            self.static += ax.plot(side * np.degrees(np.arctan(LANE / d)), el,
                                   color='0.55', lw=1, ls='--')
        step = _nice_step(rng / 4)
        r = step
        while r <= rng:                                                             # range marks
            e = -math.degrees(math.atan(EYE_H / r))
            w = math.degrees(math.atan(LANE / r))
            self.static += ax.plot([-w, w], [e, e], color='0.55', lw=0.6)
            self.static.append(ax.text(w + 0.3, e, f'{r:g} m', fontsize=7, color='0.4', va='center'))
            r += step
        self.static.append(ax.text(-half * 0.97, -low * 0.55,
                                   f'boxes {OBJ_W:g} × {OBJ_H:g} m assumed — the radar reports no height',
                                   fontsize=7, color='0.4', va='top'))

    # ── per frame ──
    def set(self, pts):
        """pts: list of (lat, lon, vrel)."""
        self.pts = pts
        if self.view == 'top':
            self.top.set(pts)
            return
        import math
        from matplotlib.patches import Rectangle
        for a in self.front_art:
            a.remove()
        self.front_art = []
        ax = self.ax_front
        for la, lo, vr in sorted(pts, key=lambda p: -p[1]):     # far first, near on top
            if not 0.3 < lo <= self.range:
                continue
            az = math.degrees(math.atan2(la, lo))
            hw = math.degrees(math.atan(OBJ_W / 2 / lo))
            y0 = -math.degrees(math.atan(EYE_H / lo))
            y1 = math.degrees(math.atan((OBJ_H - EYE_H) / lo))
            self.front_art.append(ax.add_patch(Rectangle(
                (az - hw, y0), 2 * hw, y1 - y0, facecolor=self.cmap(self.norm(vr)),
                edgecolor='k', lw=0.8, alpha=0.9, zorder=3)))
            self.front_art.append(ax.annotate(
                f'{lo:.1f} m  {vr:+.1f}', (az, y1), xytext=(0, 3), textcoords='offset points',
                ha='center', fontsize=8, fontweight='bold', color=_vcolor(vr), zorder=4))

    def set_title(self, text):
        self.title.set_text(f'{text}   |   {"top" if self.view == "top" else "front"} view, '
                            f'range {self.range:g} m')


def render_png(recs, span, out, max_range):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 9))
    _setup_axes(ax, max_range)
    if recs:
        xs = [r[2] for r in recs]; ys = [r[3] for r in recs]; cs = [r[4] for r in recs]
        sc = ax.scatter(xs, ys, c=cs, cmap='coolwarm_r', vmin=-VMAX, vmax=VMAX,
                        s=10, alpha=0.5, edgecolors='none')
        cb = fig.colorbar(sc, ax=ax, shrink=0.6, pad=0.02)
        cb.set_label('v_rel [m/s]  (red = approaching)')
    n_valid = len(recs)
    ax.set_title(f'{os.path.basename(out)} — {n_valid} valid detections, {span:.0f} s\n'
                 f'(each point is one track measurement over the whole recording)')
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    print(f"[radarmap] PNG written: {out}  ({n_valid} detections)")


def play_file(recs, span, max_range, vmax=VMAX, view='top'):
    """Animated playback of the recording (needs display)."""
    from matplotlib.animation import FuncAnimation
    disp = Display(max_range, vmax, view)
    fig = disp.fig
    n_frames = max(1, int(span / FRAME_DT) + 1)

    def update(i):
        t = i * FRAME_DT
        active = {}                                   # cid -> (lat,lon,vrel) latest within hold window
        for (rt, cid, la, lo, vr) in recs:
            if t - HOLD <= rt <= t:
                active[cid] = (la, lo, vr)
            elif rt > t:
                break
        disp.set(list(active.values()))
        disp.set_title(f't = {t:5.1f} s / {span:.1f} s   |   {len(active)} active tracks')
        return []

    anim = FuncAnimation(fig, update, frames=n_frames, interval=FRAME_DT * 1000, blit=False)
    return fig, anim


def live_stdin(max_range, vmax=VMAX, view='top'):
    """Real-time view from 'candump -L can_radar | tools/radarmap.py' (needs display).

    A reader thread decodes stdin into a queue; the animation callback only
    drains that queue. (Reading sys.stdin in the callback itself blocks until
    EOF, since candump never ends its stream — the window froze.)"""
    import queue
    import threading
    import time
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    disp = Display(max_range, vmax, view)
    fig = disp.fig
    tracks = {}                                       # cid -> (lat,lon,vrel,last_seen)
    inbox = queue.SimpleQueue()
    eof = threading.Event()

    def reader():
        for line in sys.stdin:
            r = decode_line(line)
            if r:
                inbox.put((r, time.time()))
        eof.set()

    threading.Thread(target=reader, daemon=True).start()

    def update(_):
        while True:
            try:
                (_, cid, la, lo, vr), t = inbox.get_nowait()
            except queue.Empty:
                break
            tracks[cid] = (la, lo, vr, t)
        now = time.time()
        live = {c: v for c, v in tracks.items() if now - v[3] <= HOLD}
        tracks.clear(); tracks.update(live)
        disp.set([v[:3] for v in live.values()])
        disp.set_title(f'LIVE   |   {len(live)} active tracks' + ('   |   input ended' if eof.is_set() else ''))
        return []

    anim = FuncAnimation(fig, update, interval=80, blit=False, cache_frame_data=False)
    plt.show()

def main():
    ap = argparse.ArgumentParser(description="Radar tracks in bird's-eye view.")
    ap.add_argument('logfile', nargs='?', help='can_radar.log(.gz); without argument: live from stdin')
    ap.add_argument('--png', metavar='OUT', help='render PNG overview headless')
    ap.add_argument('--gif', metavar='OUT', help='save animation headless as GIF')
    ap.add_argument('--max-range', type=float, default=80.0, help='y-axis maximum [m]')
    ap.add_argument('--vmax', type=float, default=VMAX,
                    help=f'color scale ±m/s (default {VMAX:g}; e.g. 2 on the bench)')
    ap.add_argument('--view', choices=['top', 'front'], default='top',
                    help='start view of movie / live / GIF (toggle with v)')
    args = ap.parse_args()

    if args.logfile is None:
        live_stdin(args.max_range, args.vmax, args.view)
        return

    recs, span = parse_log(args.logfile)
    print(f"[radarmap] {len(recs)} valid detections over {span:.1f} s")
    if args.png:
        render_png(recs, span, args.png, args.max_range)
    elif args.gif:
        import matplotlib
        matplotlib.use('Agg')
        fig, anim = play_file(recs, span, args.max_range, args.vmax, args.view)
        from matplotlib.animation import PillowWriter
        anim.save(args.gif, writer=PillowWriter(fps=int(1 / FRAME_DT)))
        print(f"[radarmap] GIF written: {args.gif}")
    else:
        import matplotlib.pyplot as plt
        fig, anim = play_file(recs, span, args.max_range, args.vmax, args.view)
        plt.show()


if __name__ == '__main__':
    main()
