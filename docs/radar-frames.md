# What the radar sends

> Text drafted with Claude (Anthropic); see [Authorship](../README.md#authorship).

Frames of the Denso DNMWR008 (88210-47090) as seen on its two CAN buses, both
at 500 kbit/s. IDs, lengths, rates, counters and checksums were checked
against the complete CAN log of the [first drive](drives/first-drive.md)
(110 min). Signal names come from the public DBC
(`toyota_prius_2017_adas.dbc`, fetched by `tools/get_dbc.sh`).

## Radar bus (`can_radar`)

| ID | Bytes | Rate | Content |
|---|---|---|---|
| `0x210`–`0x21F` | 8 | 20 Hz | `TRACK_A`: 16 track slots, distance, lateral offset, relative speed |
| `0x220`–`0x227` | 8 | 20 Hz | `TRACK_B`, slots 0–7: `REL_ACCEL`, the rest is not in the DBC |
| `0x228`–`0x22F` | 6 | 20 Hz | `TRACK_B`, slots 8–15, shorter |
| `0x230`–`0x232` | 7 / 8 / 8 | 20 Hz | not in the DBC; presumably status |
| `0x3D0`–`0x3D3` | 8 | 10 Hz | not in the DBC; presumably diagnostics |
| `0x123` | 7 | 10 Hz | constant during the drive, looks like a heartbeat |

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
([first-drive.md](drives/first-drive.md)).

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

## On the bench

A baking tray in front of the radar, spoof running (May 2026):

| | Relative speed | Distance |
|---|---|---|
| standing still | about 0.00 m/s (±0.05) | about 2.2 m, steady |
| moved away | up to +1.00 m/s | 1.2 → 3.7 m |
| moved closer | up to −0.75 m/s | 2.4 → 1.1 m |

Too close, the tray's track became invalid. The shortest valid distance in a
later bench log (November 2026) is 0.60 m.
