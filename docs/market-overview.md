# Market overview and further documents

> Compiled by Claude (Anthropic) from research chats in May 2026.
> Prices are samples from eBay searches in May 2026, not a market analysis.
> See [Authorship](../README.md#authorship).

Many used 77 GHz car radars are cheap. What differs is how much is publicly
known about reading them outside their car.

## Used 77 GHz automotive radars

| Sensor | Typical source | Used price | Protocol |
|---|---|---|---|
| Continental ARS408-21 | industrial variant | approx. 500–1500 € | openly documented, ROS driver |
| Continental ARS4-A/B | BMW, Nissan Qashqai, among others | from approx. 50 € | nothing public found |
| Bosch LRR3/LRR4 | VW/Audi, BMW, Mercedes | cheap (no price noted) | contradictory information* |
| Toyota/Denso TSS-P (88210-07010, -47090) | Corolla, Prius, RAV4 2016–2019 | approx. 60–200 € | reverse-engineered (opendbc, frk2); the one used here |

\* An early assessment described the Bosch protocol as well documented; later
research found hardly any open documentation. Not verified.

## The Toyota/Denso radar

### Protocol

The DBC files are in [commaai/opendbc](https://github.com/commaai/opendbc),
the messages that wake the radar in
[frk2/toyoyta_radar_control_can](https://github.com/frk2/toyoyta_radar_control_can).
Others had already run it on the bench.

### Connection

It has two separate CAN buses, and it expects vehicle data (own speed, yaw
rate) on the car-side bus.

### Variants

According to the frk2 README, its code is fully tested only on the 2017 Denso
units; 2018 Toyotas might have a Continental unit instead (hearsay there, not
checked here). The radar used here is an 88210-47090 from a Prius XW50.

## Further documents (external, not in the repo)

Material collected during research. For copyright reasons it is not
included, only linked.

**Continental ARS4-B** (automotive sensor, used by Nissan among others)
- FCC certification OAYARS4B with datasheet and manual:
  <https://fccid.io/OAYARS4B>
- User manual directly:
  <https://fccid.io/OAYARS4B/User-Manual/User-Manual-2412021>
- Teardown article (Advanced Structures India, archived):
  <https://web.archive.org/web/20230630091656/https://advancedstructures.in/continental-ars-4-b-radar-sensor-tesla/>

**Continental ARS4-A**
- Datasheet from the FCC filing: <https://apps.fcc.gov/els/GetAtt.html?id=144885>

**Continental ARS 404-21 / 408-21** (industrial variant)
- "Short Description – Technical Data" V1.01 (third-party mirror, not an
  official source; not reachable when checked in November 2026):
  <http://ael.chungbuk.ac.kr/lectures/graduate/seminar/archive/continental(br)-automotive%20radar%2077ghz.pdf>
- ROS driver with configuration docs:
  <https://github.com/VT-ASIM-LAB/continental_radar_driver>
- Example use of the ARS-408 on a drone (open access, CC BY 4.0):
  Weber et al., *Transforming 2D Radar Remote Sensor Information from a
  UAV into a 3D World-View*, Remote Sens. 2022, 14, 1633,
  <https://doi.org/10.3390/rs14071633>

**Toyota/Denso**
- Spoof messages: <https://github.com/frk2/toyoyta_radar_control_can>
- DBC files: <https://github.com/commaai/opendbc>
- Early research (treat the pinout with caution, see [bench-setup](hardware/bench-setup.md)):
  <https://github.com/wocsor/Toyota-Radar-Spoofer>

**Fundamentals**
- M. S. Greco, *Automotive Radar*, tutorial, 2012 IEEE Radar Conference,
  Atlanta. No stable public source found.
