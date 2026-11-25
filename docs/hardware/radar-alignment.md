# Which way is the radar looking?

> Written by Claude (Anthropic) from conversations with the maintainer and the
> measurements; see [Authorship](../../README.md#authorship).

**Status:** Plan (November 2026). No reflector built yet, no static
measurement done. The numbers below are calculated or taken from Toyota
service documents for other models; none of them has been checked on this
radar. The only yaw value so far comes from the first drive: +0.21° with a
95 % interval of 0.14–0.31° ([first-drive.md](../drives/first-drive.md)),
which this static check is meant to cross-check.

`LAT_DIST` is only as good as the direction the radar points in. A yaw error
of 1° moves everything at 60 m sideways by about 1 m, so before a lateral
position from a test drive means anything, the direction has to be known. It
does not have to be zero, only known.

In the Prius the dealer does this with a reflector and Techstream. On the test
vehicle (a VW T3) the radar sits on a home-made bracket, so yaw and pitch are
whatever the bracket gives. `roadreport.py` estimates the yaw from vehicles
driving ahead on straight roads, but that needs a drive and traffic. A corner
reflector in front of the parked vehicle gives a second, static measurement
that can be repeated after every change to the bracket.

---

## The target: a trihedral corner reflector

Three flat plates meeting at right angles, like the inside corner of a cube.
Any ray entering the opening leaves it in the direction it came from, so the
reflector sends a strong echo straight back to the radar without having to
be aimed precisely. It acts like a point target located at its apex.

### Size

The radar transmits at about 76.5 GHz, so λ = c / f ≈ 3.9 mm.

For the usual shape with triangular faces, the peak radar cross section
(RCS) is σ = 4π·a⁴ / (3·λ²), with *a* the length of the three edges that
meet at the apex:

| Edge a | σ (far field) | Comparison |
|-------:|--------------:|------------|
| 5 cm   | 1.7 m² (2 dBsm)  | weak; easily lost next to ground clutter |
| 7.5 cm | 8.6 m² (9 dBsm)  | about the rear of a car |
| 10 cm  | 27 m² (14 dBsm)  | proposed size |
| 15 cm  | 140 m² (21 dBsm) | far stronger than any car |

A bigger reflector does not help much at alignment distances. The far field
starts at about 2·D²/λ, with D the widest dimension of the opening (≈ a·√2):
about 10 m for a = 10 cm. At 3–5 m the reflector is in its near field and
returns less than the table says, but it is still a clean point target.
I don't know how strongly the Denso firmware suppresses very strong or very
close echoes; if 15 cm saturates, smaller is the way to go.

### Build tolerances

These are rules of thumb, not derived for this case:

- **Right angles within about ±0.5°** at a = 10 cm (the allowed error scales
  roughly with λ/a, so a larger reflector needs tighter angles). An angle
  error splits the returned beam and weakens the echo; it does not shift the
  measured direction.
- **Faces flat to about 0.3 mm** (≈ λ/10).
- Material: 1–1.5 mm aluminium sheet, or copper-clad FR4 soldered along the
  seams. Check the angles with a precision square against light.

### Stand

Wooden or plastic tripod, no metal clamps near the reflector. The apex is the
reference point: it goes on the measurement axis, at the height of the radar.
Pointing the opening at the radar within ±15° is enough.

---

## What the mounting height does to the measurement

For the first drive the radar sat on the front bumper (about 600 mm above
ground, not measured). The points below are estimates, not measurements.

### What the firmware is used to

In the Prius the radar sits behind the grille emblem; I estimate that at
0.5–0.7 m without having measured it. The Tundra manual gives 931–986 mm for
its TSS-P radar, so 600 mm is probably within what the firmware knows. Whether
it assumes a mounting height anywhere is unknown. One way it could: some
radars estimate the height of a target (to reject overhead signs and bridges)
from how its echo fades with distance due to the ground reflection, and that
pattern depends on the radar's own height.

### Near range

The vertical beam width of the DNMWR008 is unknown. Assuming ±4°, the beam
covers about 0.7 m of height at 5 m distance. At 600 mm it is centred on car
rears. At 1200 mm the rear bumper of a car (about 0.5 m) would be below the
beam centre, seen only through side lobes and the rear window; from 10–15 m
on this no longer matters.

### Ground reflection

The direct path and the path reflected off the road interfere, so the echo
fades in and out with distance. With a target at 0.5 m, the fades are about
16 m apart at 50 m distance for a radar at 600 mm, about 8 m apart at
1200 mm (spacing ≈ R²·λ / (2·h_radar·h_target)). Real vehicles have many
reflection points, which smears this out.

### Clear view

Nothing may reach into the beam: no sheet metal edge, bumper lip or number
plate in front of the radome. Low on the vehicle the radar also collects more
dirt and spray.

---

## What Toyota does

I could not find the repair manual section for the Prius XW50. What the
sources show:

- Toyota's floor slope bulletin T-SB-0088-21 lists the models with a
  *manually* adjusted front radar. The Prius is listed for 2010–2015 only,
  so the XW50 (2016 on, Toyota Safety Sense P) is presumably adjusted in
  software. This is my inference from the list, not stated in the bulletin.
- The Tundra service manual (TSS-P, same radar generation; read on a
  third-party mirror) describes:
  - Level floor, tyre pressure to spec, vehicle empty, doors closed, nobody
    in the area.
  - Vehicle centreline from two plumb lines (centre of the grille, centre of
    the tailgate), extended forward with a string.
  - **Reflector 3000 mm in front of the grille plumb point**, on the
    centreline.
  - **Reflector centre at the height of the sensor centre.**
  - Metal-free zones of several metres in front of the vehicle; metal parts
    lower than 50 mm don't matter.
  - Techstream: *Body Electrical / Pre-Collision 2 / Utility / Front Beam
    Axis Adjustment*. The radar learns the offset itself; no adjustment
    screws are mentioned.
  - Accepted result: **horizontal ±0.3°, vertical ±0.4°**.
  - Special tools: reflector SST 09870-60000, -60010, -60040 (the page does
    not say which part is which).

Whether Techstream can reach the radar without the rest of a Toyota around
it is untested, and I don't expect it to. The procedure below does the same
measurement with the project's own tools.

---

## Procedure

### Horizontal (yaw)

1. Vehicle on level ground, loaded as usual, tyre pressure to spec.
2. Mark the **thrust line**, not the body centreline: it is perpendicular to
   the rear axle and is the direction the vehicle actually drives. Two plumb
   lines from the rear wheel centres give the axle; the thrust line goes
   through its midpoint.
3. Place the reflector apex on the extended thrust line, 3–5 m in front of
   the radar, at radar height. Keep the area free of metal.
4. Start the DSU spoof and record a minute (`roadlog.sh`, or `candump` alone);
   look at the track in `radarmap.py`.
5. Yaw = atan(LAT_DIST / LONG_DIST) of the reflector track, corrected for
   the lateral offset of the radar from the thrust line if it is not mounted
   on it.

±0.3° is only ±16 mm sideways at 3 m and ±26 mm at 5 m, so the apex has to
be placed to a few millimetres. 5 m is the more forgiving distance, as long
as the track stays clean.

The result is the offset to keep in mind when reading `LAT_DIST`. It should
agree with the yaw `roadreport.py` fits from a drive.

### Which side is positive LAT_DIST

The bench test of November 2026 (radar mounted upright, moving reflector)
gave **positive LAT_DIST = right** in the driving direction, and oncoming
traffic on the first drive agreed (569 of 622 track episodes at LAT_DIST < 0).
A static check on the vehicle is therefore only a confirmation for the mounted
radar: same setup, move the reflector 0.5 m to the left and check that the
track goes negative.

### Vertical (pitch)

The radar does not report elevation: a track carries only LONG_DIST,
LAT_DIST, REL_SPEED and VALID, plus REL_ACCEL in TRACK_B. There is no echo
amplitude in the DBC. Target is the Toyota tolerance of ±0.4°.

#### Reference surface

The front of the housing is slanted by roughly 5–10° (estimated by eye). It
is not a reference: the beam axis is set by the antenna board inside, not by
the radome. Why the radome is slanted is not known. A likely reason is that
the share of the wave reflected by the plastic should not run straight back
into the receive antenna, which would cause multiple reflections between
antenna and radome and clutter at short range. It may also just follow the
installation angle behind the Prius emblem.

The provisional reference is the rectangular body of the housing: top,
bottom or the mounting tabs. Toyota's bulletin places the angle gauge on a
"calibration surface" on top of the sensor; a small flat pad on top of the
DNMWR008 would be the candidate. Whether the antenna is parallel to the
housing is unknown, which is what the scan below measures. The radar is not
opened for this.

#### Height scan

Finds the beam axis from where a target drops out, without needing
amplitude:

1. Vehicle on level ground, loaded as usual. Radar set level on its housing with
   a digital inclinometer.
2. A **small** reflector (a = 5 cm) at 5 m in front of the radar. A small
   target drops out closer to the beam edge than a large one, so the edges
   are sharper.
3. Move it up in 5 cm steps until the track disappears, then down until it
   disappears again.
4. The midpoint of the two edges is the beam axis: pitch =
   atan((midpoint − radar height) / 5 m). 10 cm at 5 m are 1.1°; the
   ±0.4° tolerance is ±3.5 cm.

The result is the offset between housing and beam. From then on the
inclinometer on the housing is enough, corrected by that offset. If the
ground is not level, measure its slope along the vehicle and compensate by the
same amount.

Two limits: ground reflection and side lobes may smear the edges, so the
scan should be repeated a few times. And the undocumented bytes of TRACK_B
may contain a quality or score value that would be a better criterion than
VALID; not examined yet.

---

## Sources

- [T-SB-0088-21, Millimeter Wave Radar Sensor – Floor Slope Compensation](https://static.nhtsa.gov/odi/tsbs/2021/MC-10203533-9999.pdf)
  (Toyota, 2021): applicable models, slope measurement, angle gauge.
- [L-TT-0284-20, Millimeter Wave Radar Sensor Floor Slope Compensation](https://static.nhtsa.gov/odi/tsbs/2020/MC-10173987-9999.pdf)
  (Lexus, 2020): reflector height set with a laser level from the emblem centre.
- [Tundra service manual, Millimeter Wave Radar Sensor – Adjustment](https://www.ttundra.com/1292/adjustment.html)
  (third-party mirror): distance, height, Techstream path, tolerances.

The corner reflector formulas are textbook radar.
