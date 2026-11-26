# What the radar sends

> Written by Claude (Anthropic) from conversations with the maintainer and the
> measurements; see [Authorship](../README.md#authorship).

Frames of the Denso DNMWR008 (88210-47090) as seen on its two CAN buses, both
at 500 kbit/s. IDs, lengths, rates, counters and checksums were checked
against the complete CAN log of the [first drive](drives/first-drive.md)
(110 min). Signal names come from the public DBC
(`toyota_prius_2017_adas.dbc`, fetched by `tools/get_dbc.sh`); `SCORE` and
`STATUS_MSG` only from the current
[opendbc `toyota_adas.dbc`](https://github.com/commaai/opendbc/blob/master/opendbc/dbc/toyota_adas.dbc),
which `tools/get_dbc.sh` also fetches (pinned to the state of June 2026).
In Python: `radar_dbc.load(radar_dbc.ADAS_CURRENT)`.

## Radar bus (`can_radar`)

| ID | Bytes | Rate | Content |
|---|---|---|---|
| `0x210`–`0x21F` | 8 | 20 Hz | `TRACK_A`: 16 track slots, distance, lateral offset, relative speed |
| `0x220`–`0x227` | 8 | 20 Hz | `TRACK_B`, slots 0–7: `REL_ACCEL`, `SCORE`; the other bytes are unknown |
| `0x228`–`0x22F` | 6 | 20 Hz | `TRACK_B`, slots 8–15, shorter |
| `0x230`–`0x232` | 7 / 8 / 8 | 20 Hz | not in the DBC; `0x230` bytes 3–4 count since power-on (see below) |
| `0x3D0`–`0x3D3` | 8 | 10 Hz | not in the DBC; meaning unknown |
| `0x123` | 7 | 10 Hz | `STATUS_MSG` (see below) |

Without the DSU messages the radar sends the track frames too, but every slot
is invalid (see below). With the spoof running, its own four frames appear on
this bus as well: `0x128`, `0x141`, `0x160`, `0x161`.

## Car-side bus (`can_car`)

The radar itself sends only `0x4FF` here, about every 1.3 s, always
`3F 00 00 02 00 00 00 00`. Everything else on this bus comes from the spoof.

## A track (`TRACK_A`)

| Signal | Bits | Scale |
|---|---|---|
| `COUNTER` | byte 0 | +1 per scan, the same for all slots |
| `LONG_DIST` | 13 bits from byte 1 | 0.04 m |
| `LAT_DIST` | 11 bits signed from byte 3 | 0.04 m, positive = right |
| `NEW_TRACK` | byte 4, bit 4 | slot was handed to a new object |
| `REL_SPEED` | 12 bits signed from byte 5 | 0.025 m/s, negative = approaching |
| `VALID` | byte 6, bit 0 | |
| `CHECKSUM` | byte 7 | see below |

Big-endian throughout. An example from the drive, slot 0:

```
0x210   61 01 A2 00 C0 00 01 DF
```

| Bytes | Value |
|---|---|
| `61` | counter |
| `01 A2` | upper 13 bits = 52 → 2.08 m |
| `00 C0` | upper 11 bits = 6 → +0.24 m; `NEW_TRACK` = 0 |
| `00 01` | upper 12 bits = 0 → 0 m/s; lowest bit: `VALID` = 1 |
| `DF` | checksum |

An empty slot looks like this between counter and checksum:

```
FF F8 00 00 00 00
```

`FF F8` is the largest 13-bit distance. `VALID` is 0. On the drive, 87 % of
the invalid track frames looked like this; the rest carried values with
`VALID` = 0.

The distance scale deviates slightly from GNSS
([first-drive.md](drives/first-drive.md)). The current opendbc defines
`LONG_DIST` as 15 bits at 0.01 m: the 13-bit value from above with the two
lowest bits of byte 2 added, so 0 to 0.03 m larger (checked on the bench logs
of 26 Nov 2026). The tools here still use the 13-bit form.

## `SCORE` (`TRACK_B`, byte 2)

Values 0–100. For valid tracks of vehicles ahead on the first drive (moving
our way, by GNSS), the median depends on the lateral offset:

| `LAT_DIST` (absolute) | median `SCORE` |
|---|---:|
| 0–0.5 m | 100 |
| 0.5–1 m | 99 |
| 1–1.5 m | 75 |
| 1.5–2.5 m | 25 |
| 2.5 m and more | 0 |

For stationary objects and oncoming traffic the median is 0. openpilot also
takes a track with `VALID` = 0 if its `SCORE` is above 50 and the slot had
valid frames shortly before
([`radar_interface.py`](https://github.com/commaai/opendbc/blob/master/opendbc/car/toyota/radar_interface.py)).

## Status (`0x123`, `STATUS_MSG`)

`RADAR_STATUS` is the two bits 4–3 of byte 2. Observed:

- 0 after power-on, until the spoof frames arrive;
- 1 while they arrive;
- 0 again about 0.8 s after they stop, 1 again about 0.3 s after they resume.

Other values were not seen. `RADAR_PRE_FAULT` was always 0. On this unit the
`INVALID_COUNTER` field of the DBC overlaps the checksum byte and changes only
with it.

## Power-on counter (`0x230`, bytes 3–4)

16 bits, +1 per frame (20 Hz), 4 in the first frame after power-on. Over
about 160 000 frames (first drive and bench) it never went backwards except at
a power-on; while the CAN sticks were gone for 10 s it advanced by 206, as at
20 Hz. It therefore shows whether the radar has restarted.

## Counter and checksum

- Byte 0 of the track frames, of `0x230`–`0x232` and of `0x3D0`–`0x3D3` counts
  up by one per frame, without a gap in the whole drive.
- The last byte of every radar-bus frame is the usual Toyota checksum: the sum
  of both ID bytes, the length and all bytes before it, modulo 256. It matched
  in all 5.69 million frames of the drive, including the spoof frames on this bus.

```python
def checksum(can_id, data):          # data includes the checksum byte
    return ((can_id >> 8) + (can_id & 0xFF) + len(data) + sum(data[:-1])) & 0xFF
```

On the car-side bus it also holds for the constant frames `0x283`, `0x343` and
`0x344`. It does not hold for `0x365`, `0x366`, `0x4CB` and `0x4FF`.

## What the radar needs to wake up

Bench series, November 2026: radar on its back facing an office ceiling (four
stationary targets), switched off and on by relay before every run, spoof
started 1 s after power-on, 30 s per run, each variant 3 times in shuffled
order. All runs started from a fresh power-on (counter in `0x230` = 4).

| Variant | Result |
|---|---|
| full spoof | first valid track 0.31 s after the spoof starts, 80 valid track frames per second |
| without `0x160` or without `0x161` | `RADAR_STATUS` back to 0 after 0.9 s, all slots empty from then on |
| any other single frame left out, `0xB4` (speed) included | same as the full spoof |
| only `0x160` and `0x161`, nothing on the car-side bus | same as the full spoof |
| speed 0, 5 or 50 km/h, sent once or at 50 Hz | same as the full spoof |
| spoof paused for 2, 10 or 30 s | tracks invalid about 0.7 s after the pause starts, valid again 0.36–0.42 s after it ends, with or without resending the start frames |

Not covered: moving targets, the road, and running without the second CAN
stick (it was connected in all runs and acknowledges the radar's `0x4FF`).
`tools/spoof_dsu.py` still sends all frames.

## On the bench

A baking tray in front of the radar, spoof running (May 2026):

| | Relative speed | Distance |
|---|---|---|
| standing still | about 0.00 m/s (±0.05) | about 2.2 m, steady |
| moved away | up to +1.00 m/s | 1.2 → 3.7 m |
| moved closer | up to −0.75 m/s | 2.4 → 1.1 m |

Too close, the tray's track became invalid. The shortest valid distance in a
later bench log (November 2026) is 0.60 m.
