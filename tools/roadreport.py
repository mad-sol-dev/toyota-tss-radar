#!/usr/bin/env python3
"""
roadreport.py — analysis of a road session (can_radar.log + gnss.csv)
---------------------------------------------------------------------------
  tools/roadreport.py logs/<session>            # writes logs/<session>/report/
  tools/roadreport.py logs/<session> --out /tmp/r --lane 1.5

Writes (headless, no display needed):
  roadreport.md          key figures as Markdown
  roadreport.png         time series over the whole drive: valid tracks,
                         distance and v_rel of the nearest object in the
                         driving corridor, GNSS speed — common time axis
  roadreport_doppler.png Doppler plausibility
  roadreport_yaw.png     radar yaw (misalignment) estimate

What the key figures answer:
  * Dropout vs. speed — share of time without any valid track, per GNSS
    speed class. The open question of the first drive: does the radar drop
    tracks when the real speed is far above the simulated 5 km/h?
  * Doppler plausibility — for stationary objects v_rel = -v_ego·cos(bearing).
    A peak of  v_rel + v_GNSS·cos(bearing)  at 0 shows that the radar sees
    stationary targets and that its Doppler scale matches GNSS.
  * Which side is positive LAT_DIST — with right-hand traffic, oncoming
    vehicles (v_rel clearly below -v_ego) are on the left. Their side in
    LAT_DIST settles the sign convention without a bench test.
  * Radar yaw — on straight sections, vehicles driving ahead sit on a line
    LAT_DIST = a + b·LONG_DIST; b = tan(yaw error). Given with a bootstrap
    confidence interval over track episodes, never as a bare number.

Time base: candump -L and gnsslog.py both stamp with the laptop clock, so
the two files are compared directly. Accepts can_radar.log or .log.gz.
"""
import argparse
import csv
import gzip
import math
import os
import re
import sys

import numpy as np

import radar_dbc

LINE_RE = re.compile(r'\(([\d.]+)\)\s+(\S+)\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]*)')
BIN = 0.1                                  # s, one radar scan is 50 ms
SPEED_CLASSES = [0, 5, 30, 50, 70, 90, 110, 1e9]   # km/h
STATIONARY_TOL = 1.0                       # m/s, |v_rel + v_ego·cos| below → stationary
MIN_EGO = 3.0                              # m/s, below this Doppler/yaw are not evaluated


# ── Loading ─────────────────────────────────────────────────────────────────
def load_radar(path):
    """All track frames (0x210–0x21F) as numpy arrays."""
    db = radar_dbc.load()
    opener = gzip.open if path.endswith('.gz') else open
    t, slot, valid, dist, lat, vrel = [], [], [], [], [], []
    with opener(path, 'rt') as fh:
        for line in fh:
            m = LINE_RE.match(line)
            if not m:
                continue
            cid = int(m.group(3), 16)
            if not 0x210 <= cid <= 0x21F:
                continue
            try:
                d = db.decode_message(cid, bytes.fromhex(m.group(4)))
            except Exception:
                continue
            t.append(float(m.group(1)))
            slot.append(cid - 0x210)
            valid.append(d.get('VALID') == 1)
            dist.append(float(d.get('LONG_DIST', 0.0)))
            lat.append(float(d.get('LAT_DIST', 0.0)))
            vrel.append(float(d.get('REL_SPEED', 0.0)))
    return dict(t=np.array(t), slot=np.array(slot, dtype=int), valid=np.array(valid, dtype=bool),
                dist=np.array(dist), lat=np.array(lat), vrel=np.array(vrel))


def load_gnss(path):
    """Epochs with a fix: t_host, speed (m/s), heading (deg)."""
    if not os.path.exists(path):
        return None
    t, v, h = [], [], []
    with open(path) as f:
        for r in csv.DictReader(f):
            if not r['fix'] or int(r['fix']) == 0 or not r['speed_mps']:
                continue
            t.append(float(r['t_host']))
            v.append(float(r['speed_mps']))
            h.append(float(r['heading_deg']) if r['heading_deg'] else np.nan)
    if len(t) < 2:
        return None
    o = np.argsort(t)
    return dict(t=np.array(t)[o], v=np.array(v)[o], h=np.array(h)[o])


def gnss_at(g, tq, max_gap=1.0):
    """GNSS speed and unwrapped heading at times tq; NaN where GNSS has a gap."""
    if g is None:
        nan = np.full(len(tq), np.nan)
        return nan, nan.copy()
    v = np.interp(tq, g['t'], g['v'])
    # heading only where moving (it is noise at standstill), unwrapped for interpolation
    ok = (g['v'] > 2.0) & ~np.isnan(g['h'])
    if ok.sum() >= 2:
        hu = np.degrees(np.unwrap(np.radians(g['h'][ok])))
        h = np.interp(tq, g['t'][ok], hu)
    else:
        h = np.full(len(tq), np.nan)
    idx = np.clip(np.searchsorted(g['t'], tq), 1, len(g['t']) - 1)
    gap = np.minimum(np.abs(tq - g['t'][idx - 1]), np.abs(g['t'][idx] - tq))
    bad = (gap > max_gap) | (tq < g['t'][0] - max_gap) | (tq > g['t'][-1] + max_gap)
    v[bad] = np.nan
    h[bad] = np.nan
    return v, h


# ── Per-scan time series ────────────────────────────────────────────────────
def scans(r, lane):
    """Bins of BIN s: number of valid slots, nearest corridor object, CAN gaps."""
    t0 = r['t'].min()
    nb = int((r['t'].max() - t0) / BIN) + 1
    b = ((r['t'] - t0) / BIN).astype(int)
    has_data = np.bincount(b, minlength=nb) > 0
    v = r['valid']
    key = np.unique(b[v] * 16 + r['slot'][v])
    n_valid = np.bincount(key // 16, minlength=nb).astype(float)
    near_d = np.full(nb, np.nan)
    near_v = np.full(nb, np.nan)
    sel = v & (np.abs(r['lat']) < lane)
    if sel.any():
        bi, di, vi = b[sel], r['dist'][sel], r['vrel'][sel]
        o = np.lexsort((di, bi))                # by bin, then distance
        bi, di, vi = bi[o], di[o], vi[o]
        first = np.r_[True, bi[1:] != bi[:-1]]
        near_d[bi[first]] = di[first]
        near_v[bi[first]] = vi[first]
    n_valid[~has_data] = np.nan
    tc = t0 + (np.arange(nb) + 0.5) * BIN
    return dict(t=tc, t0=t0, has_data=has_data, n_valid=n_valid, near_d=near_d, near_v=near_v)


# ── Key figures ─────────────────────────────────────────────────────────────
def dropout_table(s, det, lane):
    """Per speed class: time, % scans without any valid track / none in corridor,
    detections per second, share of stationary detections."""
    rows = []
    v_kmh = s['v_ego'] * 3.6
    data = s['has_data']
    classes = list(zip(SPEED_CLASSES[:-1], SPEED_CLASSES[1:])) + [(None, None)]
    for lo, hi in classes:
        if lo is None:
            m = data & np.isnan(v_kmh)
            label = 'no GNSS'
        else:
            m = data & (v_kmh >= lo) & (v_kmh < hi)
            label = f'{lo}–{hi} km/h' if hi < 1e8 else f'≥ {lo} km/h'
        n = int(m.sum())
        if n == 0:
            continue
        none_any = np.mean(s['n_valid'][m] == 0) * 100
        none_lane = np.mean(np.isnan(s['near_d'][m])) * 100
        dm = det['bin_ok'] & m[det['bin']]
        secs = n * BIN
        stat = det['stationary'][dm]
        rows.append(dict(label=label, secs=secs, none_any=none_any, none_lane=none_lane,
                         det_rate=dm.sum() / secs,
                         stat_share=(stat.mean() * 100) if len(stat) and lo is not None and lo >= 5 else np.nan))
    return rows


def doppler(det):
    """Residual v_rel + v_ego·cos(bearing) for detections while moving."""
    m = det['moving']
    res = det['res'][m]
    out = dict(n=int(m.sum()))
    if out['n'] < 20:
        return out
    hist, edges = np.histogram(res, bins=np.arange(-35, 35.5, 0.5))
    k = np.argmax(hist)
    # refine the histogram peak: median of the residuals within ±2 m/s of it
    c = (edges[k] + edges[k + 1]) / 2
    near = res[np.abs(res - c) < 2.0]
    out.update(res=res, peak=float(np.median(near)),
               share_stat=np.mean(np.abs(res) < STATIONARY_TOL) * 100)
    st = m & det['stationary']
    if st.sum() >= 20:
        ratio = -det['vrel'][st] / (det['v_ego'][st] * det['cos'][st])
        q25, q50, q75 = np.percentile(ratio, [25, 50, 75])
        out.update(scale=q50, scale_iqr=(q25, q75), n_stat=int(st.sum()))
    return out


def side_check(det):
    """Which LAT_DIST sign is left? Oncoming vehicles (approaching faster than
    a stationary object would, by > 3 m/s) are on the left in right-hand
    traffic. Counted per track episode, so one long track is one vote."""
    m = (det['moving'] & (det['res'] < -3.0) & (det['dist'] > 10) & (det['dist'] < 100)
         & (np.abs(det['lat']) < 10))
    out = dict(n=int(m.sum()))
    if not m.any():
        return out
    ep, la = det['episode'][m], det['lat'][m]
    med = np.array([np.median(la[ep == e]) for e in np.unique(ep)])
    pos, neg = int((med > 0.5).sum()), int((med < -0.5).sum())
    out.update(n_ep=len(med), pos=pos, neg=neg)
    if pos + neg >= 5:
        share = max(pos, neg) / (pos + neg)
        if share >= 0.75:
            out['left'] = '+' if pos > neg else '-'
            out['share'] = share
    return out


def episodes(t, slot, gap=0.5):
    """Track episode id: same slot, frames less than `gap` s apart."""
    o = np.lexsort((t, slot))
    ep = np.empty(len(t), dtype=int)
    new = np.r_[True, (slot[o][1:] != slot[o][:-1]) | (np.diff(t[o]) > gap)]
    ep[o] = np.cumsum(new)
    return ep


def yaw(det, straight_tol=1.0, n_boot=500, seed=1):
    """Robust line LAT = a + b·DIST through vehicles driving ahead on straight
    sections — moving our way (residual > 3 m/s); oncoming traffic in the
    neighbouring lane would pull the line sideways."""
    m = (det['straight'] & det['moving'] & (det['res'] > 3.0)
         & (det['dist'] > 8) & (det['dist'] < 100))
    d, la, ep = det['dist'][m], det['lat'][m], det['episode'][m]
    out = dict(n_cand=int(m.sum()))
    if len(d) < 30:
        return out
    inl = np.abs(la) < 2.5 + 0.05 * d          # own lane, allowing up to ~3° tilt
    a = b = 0.0
    for _ in range(4):
        if inl.sum() < 30:
            return out
        b, a = np.polyfit(d[inl], la[inl], 1)
        inl = np.abs(la - (a + b * d)) < 1.0
    rnd = np.random.default_rng(seed)
    eps = np.unique(ep[inl])
    bs = []
    if len(eps) >= 3:
        by_ep = {e: np.flatnonzero(inl & (ep == e)) for e in eps}
        for _ in range(n_boot):
            pick = np.concatenate([by_ep[e] for e in rnd.choice(eps, len(eps))])
            if np.ptp(d[pick]) > 5:
                bs.append(np.polyfit(d[pick], la[pick], 1)[0])
    deg = math.degrees(math.atan(b))
    out.update(d=d, lat=la, inl=inl, a=a, b=b, deg=deg, n=int(inl.sum()), n_ep=len(eps),
               d_range=(d[inl].min(), d[inl].max()))
    if len(bs) >= 50:
        lo, hi = np.percentile(np.degrees(np.arctan(bs)), [2.5, 97.5])
        out.update(ci=(lo, hi), sd=float(np.std(np.degrees(np.arctan(bs)))))
    return out


# ── Plots ───────────────────────────────────────────────────────────────────
def plot_series(s, path, lane):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    tm = (s['t'] - s['t0']) / 60
    fig, ax = plt.subplots(4, 1, figsize=(14, 10), sharex=True,
                           gridspec_kw=dict(height_ratios=[1, 1.4, 1.4, 1]))
    # 1-s means for the track count, otherwise 20 Hz noise fills the panel
    k = int(round(1 / BIN))
    nv = s['n_valid']
    n1 = len(nv) // k * k
    ax[0].plot(tm[:n1].reshape(-1, k).mean(1), np.nanmean(nv[:n1].reshape(-1, k), 1), lw=0.8, color='C0')
    ax[0].set_ylabel('valid tracks\n(1 s mean)')
    ax[1].plot(tm, s['near_d'], '.', ms=1.5, color='C1')
    ax[1].set_ylabel(f'nearest in corridor\n|lat|<{lane:g} m: dist [m]')
    ax[2].plot(tm, s['near_v'], '.', ms=1.5, color='C2', label='v_rel nearest')
    ax[2].plot(tm, -s['v_ego'], lw=0.8, color='k', alpha=0.6, label='−v_GNSS (stationary target)')
    ax[2].set_ylabel('v_rel [m/s]')
    ax[2].legend(loc='upper right', fontsize=8)
    ax[3].plot(tm, s['v_ego'] * 3.6, lw=0.8, color='C3')
    ax[3].set_ylabel('GNSS speed\n[km/h]')
    ax[3].set_xlabel('time since start of can_radar.log [min]')
    gaps = ~s['has_data']
    for a in ax:
        a.grid(alpha=0.3)
        if gaps.any():
            a.fill_between(tm, 0, 1, where=gaps, color='red', alpha=0.15, lw=0,
                           transform=a.get_xaxis_transform())
    ax[0].set_title('Road session — red bands: no can_radar frames')
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_doppler(det, dop, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(13, 5))
    ax[0].hist(dop['res'], bins=np.arange(-35, 35.5, 0.5), color='C0')
    ax[0].axvline(0, color='k', lw=0.8)
    ax[0].set_xlabel('v_rel + v_GNSS·cos(bearing)  [m/s]')
    ax[0].set_ylabel('detections')
    ax[0].set_title('0 = stationary, > 0 = moving our way (traffic ahead), < 0 = oncoming')
    m = det['moving']
    ax[1].plot(det['v_ego'][m] * 3.6, det['vrel'][m], '.', ms=1, alpha=0.4)
    vmax = np.nanmax(det['v_ego'][m]) if m.any() else 1
    xs = np.linspace(0, vmax, 10)
    ax[1].plot(xs * 3.6, -xs, 'k--', lw=1, label='v_rel = −v_ego (stationary)')
    ax[1].set_xlabel('GNSS speed [km/h]')
    ax[1].set_ylabel('v_rel [m/s]')
    ax[1].legend()
    for a in ax:
        a.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_yaw(y, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.plot(y['d'][~y['inl']], y['lat'][~y['inl']], '.', ms=2, color='0.75', label='not used')
    ax.plot(y['d'][y['inl']], y['lat'][y['inl']], '.', ms=2, color='C0', label='used (moving, ahead, straight)')
    xs = np.linspace(0, y['d_range'][1], 50)
    ax.plot(xs, y['a'] + y['b'] * xs, 'C3', label=f"fit: {y['deg']:+.2f}°")
    if 'ci' in y:
        for e in y['ci']:
            ax.plot(xs, y['a'] + math.tan(math.radians(e)) * xs, 'C3:', lw=0.8)
    ax.axhline(0, color='k', lw=0.6)
    ax.set_xlabel('LONG_DIST [m]')
    ax.set_ylabel('LAT_DIST [m]')
    ax.set_ylim(-6, 6)
    ax.legend()
    ax.grid(alpha=0.3)
    ax.set_title('Vehicles ahead on straight sections; dotted: 95 % CI')
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


# ── Main ────────────────────────────────────────────────────────────────────
def find_log(session):
    for name in ('can_radar.log', 'can_radar.log.gz'):
        p = os.path.join(session, name)
        if os.path.exists(p):
            return p
    sys.exit(f'{session}: no can_radar.log(.gz)')


def main():
    ap = argparse.ArgumentParser(description='Analysis of a road session (radar + GNSS).')
    ap.add_argument('session', help='session folder (logs/<session>)')
    ap.add_argument('--out', help='output folder (default: <session>/report)')
    ap.add_argument('--lane', type=float, default=1.5, help='corridor half-width in m (default 1.5)')
    a = ap.parse_args()
    out = a.out or os.path.join(a.session, 'report')
    os.makedirs(out, exist_ok=True)

    log = find_log(a.session)
    print(f'[roadreport] reading {log} …', file=sys.stderr)
    r = load_radar(log)
    if len(r['t']) == 0:
        sys.exit(f'{log}: no radar track frames')
    g = load_gnss(os.path.join(a.session, 'gnss.csv'))

    s = scans(r, a.lane)
    s['v_ego'], s['head'] = gnss_at(g, s['t'])

    # per valid detection: ego speed, bearing, Doppler residual, straight-road flag
    v = r['valid']
    det = {k: r[k][v] for k in ('t', 'slot', 'dist', 'lat', 'vrel')}
    det['bin'] = ((det['t'] - s['t0']) / BIN).astype(int)
    det['bin_ok'] = np.ones(len(det['t']), bool)
    det['v_ego'], head = gnss_at(g, det['t'])
    det['cos'] = np.cos(np.arctan2(det['lat'], np.maximum(det['dist'], 0.1)))
    det['res'] = det['vrel'] + det['v_ego'] * det['cos']
    det['moving'] = det['v_ego'] > MIN_EGO
    det['stationary'] = det['moving'] & (np.abs(det['res']) < STATIONARY_TOL)
    _, h_m = gnss_at(g, det['t'] - 2.0)
    _, h_p = gnss_at(g, det['t'] + 2.0)
    det['straight'] = (np.abs(h_p - h_m) < 1.0) & (det['v_ego'] > 8.0)   # < 1° over 4 s, > 29 km/h
    det['episode'] = episodes(det['t'], det['slot'])

    side = side_check(det)
    drop = dropout_table(s, det, a.lane)
    dop = doppler(det)
    y = yaw(det)

    plot_series(s, os.path.join(out, 'roadreport.png'), a.lane)
    if 'res' in dop:
        plot_doppler(det, dop, os.path.join(out, 'roadreport_doppler.png'))
    if 'b' in y:
        plot_yaw(y, os.path.join(out, 'roadreport_yaw.png'))

    md = report_md(a, log, r, g, s, drop, dop, y, side)
    with open(os.path.join(out, 'roadreport.md'), 'w') as f:
        f.write(md)
    print(md)
    print(f'[roadreport] written to {out}/', file=sys.stderr)


def report_md(a, log, r, g, s, drop, dop, y, side):
    dur = r['t'].max() - r['t'].min()
    gap_s = (~s['has_data']).sum() * BIN
    L = [f'# Road report — {os.path.basename(os.path.normpath(a.session))}', '']
    L += ['## Data', '',
          f'- Radar: `{os.path.basename(log)}`, {len(r["t"])} track frames over {dur / 60:.1f} min; '
          f'scans without any can_radar frame: {gap_s:.1f} s',
          f'- GNSS: ' + ('none (`gnss.csv` missing or no fix) — speed-dependent figures not available'
                         if g is None else
                         f'{len(g["t"])} epochs with fix, covering '
                         f'{np.mean(~np.isnan(s["v_ego"][s["has_data"]])) * 100:.0f} % of the radar time; '
                         f'max {np.nanmax(g["v"]) * 3.6:.0f} km/h'),
          f'- Corridor for "nearest object": |LAT_DIST| < {a.lane:g} m', '']

    L += ['## Dropout vs. speed', '',
          'Share of radar scans (0.1 s) without valid tracks, by GNSS speed. A road with nothing',
          'ahead also gives "no track" — what matters is a jump between classes, not the level.',
          '"stationary" = share of detections with v_rel ≈ −v_ego·cos(bearing) (±1 m/s).', '',
          '| speed | time | no valid track | none in corridor | detections/s | stationary |',
          '|---|---:|---:|---:|---:|---:|']
    for d in drop:
        st = '—' if np.isnan(d['stat_share']) else f"{d['stat_share']:.0f} %"
        L.append(f"| {d['label']} | {d['secs'] / 60:.1f} min | {d['none_any']:.0f} % | "
                 f"{d['none_lane']:.0f} % | {d['det_rate']:.1f} | {st} |")
    L.append('')

    L += ['## Doppler plausibility', '']
    if 'res' not in dop:
        L += [f'Not enough detections while moving ({dop["n"]}; needs GNSS and v > {MIN_EGO:g} m/s).', '']
    else:
        L += [f'- Detections while moving: {dop["n"]}',
              f'- Peak of v_rel + v_GNSS·cos(bearing): **{dop["peak"]:+.2f} m/s** '
              f'(0 expected: stationary objects)',
              f'- Share within ±{STATIONARY_TOL:g} m/s (stationary): {dop["share_stat"]:.0f} %']
        if 'scale' in dop:
            L.append(f'- Speed scale radar/GNSS on stationary objects: **{dop["scale"]:.3f}** '
                     f'(IQR {dop["scale_iqr"][0]:.3f}–{dop["scale_iqr"][1]:.3f}, n = {dop["n_stat"]})')
        L += ['', '![Doppler](roadreport_doppler.png)', '']

    L += ['## Which side is positive LAT_DIST?', '']
    if 'n_ep' not in side:
        L += ['No oncoming traffic detected (needs GNSS and two-way roads).', '']
    else:
        L += [f'- Oncoming vehicles (v_rel < −v_ego − 3 m/s): {side["n_ep"]} track episodes, '
              f'{side["pos"]} at LAT_DIST > 0, {side["neg"]} at LAT_DIST < 0']
        if 'left' in side:
            word = 'left' if side['left'] == '+' else 'right'
            L += [f'- With right-hand traffic: **positive LAT_DIST = {word}** '
                  f'({side["share"] * 100:.0f} % of the episodes agree)', '']
        else:
            L += ['- Not conclusive (fewer than 5 clear episodes or no clear majority).', '']

    L += ['## Radar yaw (misalignment)', '']
    if 'b' not in y:
        L += [f'Not enough data: {y["n_cand"]} candidate detections of vehicles driving ahead on '
              'straight sections (needs GNSS, > 29 km/h, heading change < 1° over 4 s).', '']
    else:
        ci = (f'95 % CI {y["ci"][0]:+.2f}° … {y["ci"][1]:+.2f}° (bootstrap over {y["n_ep"]} track episodes)'
              if 'ci' in y else f'no CI: only {y["n_ep"]} track episodes — not meaningful yet')
        L += [f'- Yaw: **{y["deg"]:+.2f}°**, {ci}',
              f'- Lateral offset at 0 m: {y["a"]:+.2f} m (mounting offset + own lane position)',
              f'- {y["n"]} detections used, distance {y["d_range"][0]:.0f}–{y["d_range"][1]:.0f} m',
              '- Sign: positive = objects straight ahead appear at positive LAT_DIST'
              + ((', i.e. the radar looks to the ' + ('right' if side['left'] == '+' else 'left')
                  + ' of the driving direction (see side check above).')
                 if 'left' in side else
                 '; left/right follows from the side check above once it is conclusive.'),
              '', '![Yaw](roadreport_yaw.png)', '']

    L += ['## Time series', '', '![Time series](roadreport.png)', '']
    return '\n'.join(L)


if __name__ == '__main__':
    main()
