#!/usr/bin/env python3
"""
canwatch.py — live CAN-frame change detector.

Pipe `candump` into it and watch which bytes in which IDs actually move.
Counter and checksum bytes (those that change in nearly every frame) are
auto-detected and dimmed so they don't drown out the real signal.

Usage:
    candump can0          | tools/canwatch.py
    candump -ta can0      | tools/canwatch.py        # timestamped, also works
    stdbuf -oL candump can0 | tools/canwatch.py      # if pipe is block-buffered

Keys (while running):
    f   freeze current payloads as a reference baseline (deviations -> red)
    r   release the baseline
    c   clear all change history (re-learns which bytes are counters)
    q   quit
"""

import sys
import time
import curses
import select
import argparse
from collections import defaultdict


def parse_candump_line(line):
    """Parse a candump line in either default or -ta format.

    Returns (can_id_int, [byte_int, ...]) or None.
    Robust to leading timestamp '(123.456)' and to varying DLC.
    """
    parts = line.split()
    try:
        for i, tok in enumerate(parts):
            if len(tok) >= 2 and tok[0] == '[' and tok[-1] == ']':
                can_id = int(parts[i - 1], 16)
                data = [int(b, 16) for b in parts[i + 1:]]
                return can_id, data
    except (ValueError, IndexError):
        pass
    return None


def main(stdscr, fade_s, mask_threshold):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)    # very recent change
    curses.init_pair(2, curses.COLOR_YELLOW, -1)   # older change (fading)
    curses.init_pair(4, curses.COLOR_RED, -1)      # differs from baseline
    curses.init_pair(5, curses.COLOR_CYAN, -1)     # header

    last_payload = {}                                          # id -> [bytes]
    byte_change_time = defaultdict(dict)                       # id -> {byte_idx: ts}
    byte_change_count = defaultdict(lambda: defaultdict(int))  # id -> {byte_idx: n}
    total_frames = defaultdict(int)
    first_seen = {}
    baseline = None  # frozen snapshot of last_payload

    DRAW_HZ = 10
    draw_interval = 1.0 / DRAW_HZ
    last_draw = 0.0

    while True:
        # Drain available input without blocking the redraw
        rlist, _, _ = select.select([sys.stdin], [], [], 0.01)
        if rlist:
            line = sys.stdin.readline()
            if line:
                parsed = parse_candump_line(line)
                if parsed:
                    cid, data = parsed
                    now = time.monotonic()
                    total_frames[cid] += 1
                    if cid not in first_seen:
                        first_seen[cid] = now
                    prev = last_payload.get(cid)
                    if prev is not None and len(prev) == len(data):
                        for i, (a, b) in enumerate(zip(prev, data)):
                            if a != b:
                                byte_change_time[cid][i] = now
                                byte_change_count[cid][i] += 1
                    last_payload[cid] = data

        now = time.monotonic()
        if now - last_draw < draw_interval:
            # quick poll for keys then loop
            try:
                c = stdscr.getch()
            except Exception:
                c = -1
            if c == ord('q'):
                break
            elif c == ord('f'):
                baseline = {k: list(v) for k, v in last_payload.items()}
            elif c == ord('r'):
                baseline = None
            elif c == ord('c'):
                last_payload.clear()
                byte_change_time.clear()
                byte_change_count.clear()
                total_frames.clear()
                first_seen.clear()
            continue
        last_draw = now

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        title = (f" canwatch | fade={fade_s:.1f}s "
                 f"| baseline={'SET' if baseline else 'off'} "
                 f"| f=freeze r=release c=clear q=quit ")
        stdscr.addstr(0, 0, title[:w - 1], curses.color_pair(5) | curses.A_BOLD)
        stdscr.addstr(1, 0, "  ID    DLC  payload"
                            + " " * 26
                            + "Hz    frames", curses.A_BOLD)

        row = 2
        for cid in sorted(last_payload.keys()):
            if row >= h - 1:
                break
            data = last_payload[cid]
            dt = now - first_seen.get(cid, now)
            hz = total_frames[cid] / dt if dt > 0 else 0.0
            n = total_frames[cid]

            # Auto-detect counter/checksum bytes: those that change in
            # >= threshold of frames after the first one.
            mask = set()
            if n >= 8:
                for i in range(len(data)):
                    cnt = byte_change_count[cid].get(i, 0)
                    if cnt >= mask_threshold * (n - 1):
                        mask.add(i)

            stdscr.addstr(row, 0, f" {cid:03X}   [{len(data)}]  ")
            x = 15
            base = baseline.get(cid) if baseline else None
            for i, b in enumerate(data):
                attr = curses.A_NORMAL
                # Baseline diff (red) wins over counter-mask, so a signal
                # byte that just happens to be wiggling shows up clearly.
                if (base is not None and i < len(base)
                        and base[i] != b and i not in mask):
                    attr = curses.color_pair(4) | curses.A_BOLD
                else:
                    t_ch = byte_change_time[cid].get(i)
                    if t_ch is not None and i not in mask:
                        age = now - t_ch
                        if age < fade_s * 0.25:
                            attr = curses.color_pair(1) | curses.A_BOLD
                        elif age < fade_s:
                            attr = curses.color_pair(2)
                    if i in mask:
                        attr = curses.A_DIM
                stdscr.addstr(row, x, f"{b:02X}", attr)
                x += 3
            stdscr.addstr(row, max(x + 2, 45), f"{hz:5.1f}  {n:7d}")
            row += 1

        stdscr.refresh()

        try:
            c = stdscr.getch()
        except Exception:
            c = -1
        if c == ord('q'):
            break
        elif c == ord('f'):
            baseline = {k: list(v) for k, v in last_payload.items()}
        elif c == ord('r'):
            baseline = None
        elif c == ord('c'):
            last_payload.clear()
            byte_change_time.clear()
            byte_change_count.clear()
            total_frames.clear()
            first_seen.clear()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Live CAN-frame change detector. Pipe candump into it.")
    ap.add_argument("--fade", type=float, default=1.5,
                    help="seconds for a change highlight to fade (default 1.5)")
    ap.add_argument("--mask-threshold", type=float, default=0.8,
                    help="fraction of frames a byte must change in to be "
                         "treated as counter/checksum (default 0.8)")
    args = ap.parse_args()
    try:
        curses.wrapper(main, args.fade, args.mask_threshold)
    except KeyboardInterrupt:
        pass
