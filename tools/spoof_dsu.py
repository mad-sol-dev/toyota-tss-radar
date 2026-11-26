#!/usr/bin/env python3
"""
spoof_dsu.py — wakes the Denso DNMWR008 radar from standby (bench and van)
Based on frk2/toyoyta_radar_control_can
(https://github.com/frk2/toyoyta_radar_control_can)
Copyright (c) 2018 Faraz Khan, MIT License (full text: LICENSES/MIT-frk2.txt)
Modifications: see below. Distributed here under GPL-3.0 (see LICENSE); the
MIT notice of the original stays with the adapted code.

Purpose:
  Sends the CAN messages that the radar expects from the Toyota
  Driving Support Unit on its car-side bus and on its radar bus. Without
  them the DNMWR008 stays in standby (tracks show FF F8 00 00 … = invalid
  sentinel). With this running and metal in front of the sensor, valid
  distance/v_rel values appear in 0x210–0x21F.

Changes vs. the frk2 original:
  - Interfaces:      can0/can1          →  can_car/can_radar (stable udev names)
  - bustype:         socketcan_native   →  socketcan  (python-can ≥4.x)
  - cantools API:    cantools.db        →  cantools.database  (not deprecated)
  - DBC load:        strict=True        →  strict=False  (overlap in STEERING_IPAS_COMMA)
  - DBC paths:       relative           →  dbc/ or $RADAR_DBC_DIR (radar_dbc.py, tools/get_dbc.sh)
  - Message param:   extended_id=False  →  is_extended_id=False  (python-can 4.x)
  - Payloads:        string literals + map(ord,…)  →  bytes literals (b'\\xNN')
  - Track bug:       0x210 <= id < 0x21F   →  <= 0x21F  (0x21F was excluded)
  - Output:          silent on invalid      →  info counter every 100 frames
  - Except for the speed message, the car-side frames are sent as constant
    payloads (byte-identical to what the original's DBC encoding produced),
    and the table of car variants is reduced to the one radar used here.

Setup:
  can_car   = car-side bus (radar pins 2/3, green/white pair)
  can_radar = radar bus    (radar pins 5/6, black/white pair)
  Both CANable Pro sticks come up at 500 kbit/s automatically via udev rule.

Start (in the repo directory):
  tools/spoof_dsu.py
  (in parallel, in a second terminal: candump can_radar | tools/canwatch.py)

Status: bench-tested with the radar on 26 Nov 2026 (see docs/tools.md).
"""

import time

import can

import radar_dbc

# ── Paths ─────────────────────────────────────────────────────────────────────
DBC_ADAS   = radar_dbc.path(radar_dbc.ADAS)
DBC_PT     = radar_dbc.path(radar_dbc.PT)

# ── Config ────────────────────────────────────────────────────────────────────
CAN_CAR       = 'can_car'    # car-side bus (pins 2/3 on the radar, green/white)
CAN_RADAR     = 'can_radar'  # radar bus (pins 5/6, black/white)
EGO_SPEED_KPH = 5.0          # Speed value sent once to the radar at start
                              # frk2 original: 1.44 kph — possibly below radar threshold


# ── Classes ───────────────────────────────────────────────────────────────────

class OnCan(can.Listener):
    """
    Listens on can_radar.
    Prints valid tracks from the track frames (0x210–0x21F) with distance,
    lateral offset and relative speed.
    Shows a warning if not a single valid track arrived for >1 s.
    """
    def __init__(self):
        self.db = radar_dbc.load(radar_dbc.ADAS)
        self._last_valid_ts = time.time()   # timestamp of the last valid track
        self._warned        = False          # so the warning doesn't flood the output

    def on_message_received(self, msg):
        if 0x210 <= msg.arbitration_id <= 0x21F:
            try:
                dec = self.db.decode_message(msg.arbitration_id, msg.data)
            except Exception:
                return
            if dec.get('VALID') == 1:
                self._last_valid_ts = time.time()
                self._warned = False
                print(
                    f"  ✓ VALID 0x{msg.arbitration_id:03X}: "
                    f"dist={dec['LONG_DIST']:6.2f} m  "
                    f"lat={dec.get('LAT_DIST', 0.0):6.2f} m  "
                    f"v_rel={dec.get('REL_SPEED', 0.0):+6.2f} m/s"
                )
            else:
                # Warn only if no valid track for >1 s — and only once per dropout
                if not self._warned and (time.time() - self._last_valid_ts) > 1.0:
                    elapsed = time.time() - self._last_valid_ts
                    print(f"  … no valid track for {elapsed:.1f} s (no object in detection range?)")
                    self._warned = True


# ── Static messages ───────────────────────────────────────────────────────────
# Format: (CAN ID, bus (0=car side, 1=radar), rate divisor of the 100 Hz loop, payload)
#
# Bus=1 → messages go to can_radar
# Bus=0 → messages go to can_car

STATIC_MSGS = [
    # 50 Hz — radar bus
    (0x141, 1, 2, b'\x00\x00\x00\x46'),

    # ~33 Hz — radar bus
    (0x128, 1, 3, b'\xf4\x01\x90\x83\x00\x37'),

    # ~33 Hz — car side
    (0x283, 0, 3, b'\x00\x00\x00\x00\x00\x00\x8c'),

    # 20 Hz — car side
    (0x344, 0, 5, b'\x00\x00\x01\x00\x00\x00\x00\x50'),

    # ~14 Hz — radar bus (constant yaw/lateral data frames)
    (0x160, 1, 7, b'\x00\x00\x08\x12\x01\x31\x9c\x51'),
    (0x161, 1, 7, b'\x00\x1e\x00\x00\x00\x80\x07'),

    # 5 Hz — car side
    (0x365, 0, 20, b'\x00\x00\x00\x80\xfc\x00\x08'),
    (0x366, 0, 20, b'\x00\x72\x07\xff\x09\xfe\x00'),

    # 1 Hz — car side
    (0x4CB, 0, 100, b'\x0c\x00\x00\x00\x00\x00\x00\x00'),
]

# Further car-side frames as in the original (constant payloads, 8 bytes).
# Whether the radar needs each of them has not been tested individually.
INIT_FRAMES = [                                   # sent once at start
    (0x1D2, bytes.fromhex('0000000000009000')),
    (0x1D3, bytes.fromhex('0000000000000000')),
    (0x343, bytes.fromhex('0000000000000000')),
    (0x399, bytes.fromhex('0000000000000000')),
]
CYCLIC_CAR_FRAME = (0x343, bytes.fromhex('000063c000000071'))   # every loop (100 Hz)


# ── Main program ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"[spoof_dsu] DBC PT  : {DBC_PT}")
    print(f"[spoof_dsu] DBC ADAS: {DBC_ADAS}")

    db = radar_dbc.load(radar_dbc.PT)

    print(f"[spoof_dsu] Opening {CAN_CAR} (car-side bus) …")
    can_car = can.interface.Bus(bustype='socketcan', channel=CAN_CAR, fd=False)
    print(f"[spoof_dsu] Opening {CAN_RADAR} (radar bus) …")
    can_radar = can.interface.Bus(bustype='socketcan', channel=CAN_RADAR, fd=False)

    notifier = can.Notifier(can_radar, [OnCan()], timeout=0.1)

    # ── One-time init messages on the car-side bus ────────────────────────────
    print("[spoof_dsu] Sending one-time init messages on can_car …")

    msg_speed = db.get_message_by_name("SPEED")
    can_car.send(can.Message(
        arbitration_id=msg_speed.frame_id,
        data=msg_speed.encode({"ENCODER": 0, "SPEED": EGO_SPEED_KPH, "CHECKSUM": 0}),
        is_extended_id=False))

    for fid, payload in INIT_FRAMES:
        can_car.send(can.Message(arbitration_id=fid, data=payload, is_extended_id=False))

    print(
        f"[spoof_dsu] Loop @ 100 Hz, speed value {EGO_SPEED_KPH} kph sent to the radar. "
        f"Ctrl+C to quit."
    )
    print("[spoof_dsu] Waiting for valid tracks on can_radar (0x210–0x21F) …\n")

    frame = 0
    try:
        while True:
            # one constant car-side frame every loop (100 Hz)
            can_car.send(can.Message(
                arbitration_id=CYCLIC_CAR_FRAME[0],
                data=CYCLIC_CAR_FRAME[1],
                is_extended_id=False))

            # STATIC_MSGS according to their individual rate divisor
            for (addr, bus, fr_step, payload) in STATIC_MSGS:
                if frame % fr_step == 0:
                    target = can_radar if bus == 1 else can_car
                    target.send(can.Message(
                        arbitration_id=addr,
                        data=payload,
                        is_extended_id=False))

            frame += 1
            time.sleep(1.0 / 100.0)

    except KeyboardInterrupt:
        print("\n[spoof_dsu] Stopped (KeyboardInterrupt).")
    finally:
        notifier.stop()
        can_car.shutdown()
        can_radar.shutdown()
        print("[spoof_dsu] CAN interfaces closed.")
