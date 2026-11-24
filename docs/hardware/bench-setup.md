# Bench setup

> Text drafted with Claude (Anthropic); see [Authorship](../../README.md#authorship).

How the radar is wired and connected to the laptop. Everything here was
built and verified on the bench. Road results:
[first-drive.md](../drives/first-drive.md).

## Radar connector pinout

8-pin connector of the Denso DNMWR008, measured on the bench and confirmed
three times:

```
Pin 1 = GND (continuity to housing, black-white single wire)
Pin 8 = +12 V (green single wire)
Pin 2 = CANL car bus   (white in the green-white pair)
Pin 3 = CANH car bus   (green in the green-white pair)
Pin 5 = CANH radar bus (black in the black-white pair)
Pin 6 = CANL radar bus (white in the black-white pair)
```

The radar bus is terminated (124 Ω inside the sensor); the car bus is
unterminated (41 kΩ, the radar is a middle node).

> **Caution:** wocsor's table, for another unit (88210-07010), has power and
> ground on pins 1 and 8 the other way round. frk2's table agrees with the
> measurement here. Check with a multimeter before connecting power.

Power: 12 V from a current-limited bench supply; the radar draws ~0.3 A.

## CAN interfaces

Two CANable Pro sticks (candleLight firmware, kernel driver `gs_usb`) are
connected through a USB 2.0 hub. A udev rule renames them to stable names
based on their USB serial numbers and brings them up at 500 kbit/s when
plugged in:

* `can_radar` ↔ radar bus (pins 5/6, black-white pair)
* `can_car`   ↔ car bus   (pins 2/3, green-white pair)

This makes `candump can_radar` and `cansend can_car ...` usable without sudo.

### Installing the udev rule

The rule identifies the sticks by their USB serial number, which differs for
every stick. That's why the repo only contains a template:

```bash
cp udev/70-can-canable.rules.example udev/70-can-canable.rules
# Plug in only one stick and read its serial number:
udevadm info -a -p /sys/class/net/can0 | grep -m1 'ATTRS{serial}'
# → enter it in udev/70-can-canable.rules for SERIAL_RADAR_STICK or SERIAL_CAR_STICK,
#   then repeat with the second stick.
sudo install -m 0644 udev/70-can-canable.rules /etc/udev/rules.d/
sudo udevadm control --reload
# Then physically unplug and replug the USB hub once.
```

Without a matching serial number the rule doesn't apply — and it fails without
any error message. The sticks then keep their names `can0`/`can1`.

Verification: `ip -details link show can_radar` → `state UP, bitrate 500000`.

## Lessons learned

* `gs_usb` (candleLight) does **not** support `restart-ms` — omit the
  parameter, otherwise `ip link set up` fails silently and `udev RUN+=` aborts.
* udev `RUN+=` errors are not reported visibly; an interface that doesn't come
  up is the only symptom.
* `lsusb` device numbers change on every replug — bind via USB serial number,
  not device ID.
