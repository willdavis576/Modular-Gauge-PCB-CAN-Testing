"""
Assetto Corsa → CAN bus bridge via OpenFFBoard HID interface.

Reads AC telemetry from Windows shared memory and transmits it as CAN
frames through an OpenFFBoard USB device (VID=0x1209, PID=0xFFB0).

Dependencies:
    pip install pywinusb

CAN frame layout
─────────────────────────────────────────────────────
0x100  Engine / Motion  (DLC=8)
  [0:1]  RPM             uint16 LE   0-65535 RPM
  [2:3]  Speed × 10      uint16 LE   e.g. 1505 = 150.5 km/h
  [4]    Gear            uint8       0=R  1=N  2=1st … 9=8th
  [5]    Throttle        uint8       0-255 = 0-100%
  [6]    Brake           uint8       0-255 = 0-100%
  [7]    Clutch          uint8       0-255 = 0-100%

0x101  Temperatures     (DLC=8)
  [0]    Air temp        uint8       °C + 40  (range −40 to +215 °C)
  [1]    Road temp       uint8       °C + 40
  [2]    Tyre FL core    uint8       °C (0-255)
  [3]    Tyre FR core    uint8
  [4]    Tyre RL core    uint8
  [5]    Tyre RR core    uint8
  [6:7]  reserved        0x0000

0x102  Session          (DLC=8)
  [0:1]  Current lap ms÷10  uint16 LE   0-65535 = 0-655.35 s
  [2:3]  Best lap ms÷10     uint16 LE
  [4]    Completed laps      uint8
  [5]    Position            uint8
  [6:7]  Fuel × 10           uint16 LE   0-65535 = 0-6553.5 L
"""

import mmap
import ctypes
import struct
import time
import sys

try:
    import pywinusb.hid as hid
    HID_AVAILABLE = True
except ImportError:
    HID_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

VENDOR_ID        = 0x1209
PRODUCT_ID       = 0xFFB0
CAN_SPEED_PRESET = 2      # OpenFFBoard preset: 0=125k 1=250k 2=500k 3=1M
REFRESH_HZ       = 30     # telemetry + CAN send rate

# ── Assetto Corsa shared memory structs ───────────────────────────────────────

class Physics(ctypes.Structure):
    _fields_ = [
        ('packetId',              ctypes.c_int),
        ('gas',                   ctypes.c_float),
        ('brake',                 ctypes.c_float),
        ('fuel',                  ctypes.c_float),
        ('gear',                  ctypes.c_int),
        ('rpms',                  ctypes.c_int),
        ('steerAngle',            ctypes.c_float),
        ('speedKmh',              ctypes.c_float),
        ('velocity',              ctypes.c_float * 3),
        ('accG',                  ctypes.c_float * 3),
        ('wheelSlip',             ctypes.c_float * 4),
        ('wheelLoad',             ctypes.c_float * 4),
        ('wheelsPressure',        ctypes.c_float * 4),
        ('wheelAngularSpeed',     ctypes.c_float * 4),
        ('tyreWear',              ctypes.c_float * 4),
        ('tyreDirtyLevel',        ctypes.c_float * 4),
        ('tyreCoreTemperature',   ctypes.c_float * 4),
        ('camberRAD',             ctypes.c_float * 4),
        ('suspensionTravel',      ctypes.c_float * 4),
        ('drs',                   ctypes.c_float),
        ('tc',                    ctypes.c_float),
        ('heading',               ctypes.c_float),
        ('pitch',                 ctypes.c_float),
        ('roll',                  ctypes.c_float),
        ('cgHeight',              ctypes.c_float),
        ('carDamage',             ctypes.c_float * 5),
        ('numberOfTyresOut',      ctypes.c_int),
        ('pitLimiterOn',          ctypes.c_int),
        ('abs',                   ctypes.c_float),
        ('kersCharge',            ctypes.c_float),
        ('kersInput',             ctypes.c_float),
        ('autoShifterOn',         ctypes.c_int),
        ('rideHeight',            ctypes.c_float * 2),
        ('turboBoost',            ctypes.c_float),
        ('ballast',               ctypes.c_float),
        ('airDensity',            ctypes.c_float),
        ('airTemp',               ctypes.c_float),
        ('roadTemp',              ctypes.c_float),
        ('localAngularVelocity',  ctypes.c_float * 3),
        ('finalFF',               ctypes.c_float),
        ('performanceMeter',      ctypes.c_float),
        ('engineBrake',           ctypes.c_int),
        ('ersRecoveryLevel',      ctypes.c_int),
        ('ersPowerLevel',         ctypes.c_int),
        ('ersHeatCharging',       ctypes.c_int),
        ('ersIsCharging',         ctypes.c_int),
        ('kersCurrentKJ',         ctypes.c_float),
        ('drsAvailable',          ctypes.c_int),
        ('drsEnabled',            ctypes.c_int),
        ('brakeTemp',             ctypes.c_float * 4),
        ('clutch',                ctypes.c_float),
        ('tyreTempI',             ctypes.c_float * 4),
        ('tyreTempM',             ctypes.c_float * 4),
        ('tyreTempO',             ctypes.c_float * 4),
        ('isAIControlled',        ctypes.c_int),
        ('tyreContactPoint',      ctypes.c_float * 4 * 3),
        ('tyreContactNormal',     ctypes.c_float * 4 * 3),
        ('tyreContactHeading',    ctypes.c_float * 4 * 3),
        ('brakeBias',             ctypes.c_float),
        ('localVelocity',         ctypes.c_float * 3),
    ]


class Graphics(ctypes.Structure):
    _fields_ = [
        ('packetId',              ctypes.c_int),
        ('status',                ctypes.c_int),
        ('session',               ctypes.c_int),
        ('currentTime',           ctypes.c_wchar * 15),
        ('lastTime',              ctypes.c_wchar * 15),
        ('bestTime',              ctypes.c_wchar * 15),
        ('split',                 ctypes.c_wchar * 15),
        ('completedLaps',         ctypes.c_int),
        ('position',              ctypes.c_int),
        ('iCurrentTime',          ctypes.c_int),
        ('iLastTime',             ctypes.c_int),
        ('iBestTime',             ctypes.c_int),
        ('sessionTimeLeft',       ctypes.c_float),
        ('distanceTraveled',      ctypes.c_float),
        ('isInPit',               ctypes.c_int),
        ('currentSectorIndex',    ctypes.c_int),
        ('lastSectorTime',        ctypes.c_int),
        ('numberOfSectors',       ctypes.c_int),
        ('tyreCompound',          ctypes.c_wchar * 4 * 33),
        ('replayTimeMultiplier',  ctypes.c_float),
        ('normalizedCarPosition', ctypes.c_float),
        ('activeCars',            ctypes.c_int),
        ('carCoordinates',        ctypes.c_float * 60 * 3),
        ('carID',                 ctypes.c_int * 60),
        ('playerCarID',           ctypes.c_int),
        ('penaltyTime',           ctypes.c_float),
        ('flag',                  ctypes.c_int),
        ('penalty',               ctypes.c_int),
        ('idealLineOn',           ctypes.c_int),
        ('isInPitLane',           ctypes.c_int),
        ('surfaceGrip',           ctypes.c_float),
        ('mandatoryPitDone',      ctypes.c_int),
        ('windSpeed',             ctypes.c_float),
        ('windDirection',         ctypes.c_float),
    ]


class Static(ctypes.Structure):
    _fields_ = [
        ('smVersion',             ctypes.c_wchar * 15),
        ('acVersion',             ctypes.c_wchar * 15),
        ('numberOfSessions',      ctypes.c_int),
        ('numCars',               ctypes.c_int),
        ('carModel',              ctypes.c_wchar * 33),
        ('track',                 ctypes.c_wchar * 33),
        ('playerName',            ctypes.c_wchar * 33),
        ('playerSurname',         ctypes.c_wchar * 33),
        ('playerNick',            ctypes.c_wchar * 33),
        ('sectorCount',           ctypes.c_int),
        ('maxTorque',             ctypes.c_float),
        ('maxPower',              ctypes.c_float),
        ('maxRpm',                ctypes.c_int),
        ('maxFuel',               ctypes.c_float),
        ('suspensionMaxTravel',   ctypes.c_float * 4),
        ('tyreRadius',            ctypes.c_float * 4),
        ('maxTurboBoost',         ctypes.c_float),
        ('deprecated_1',          ctypes.c_float),
        ('deprecated_2',          ctypes.c_float),
        ('penaltiesEnabled',      ctypes.c_int),
        ('aidFuelRate',           ctypes.c_float),
        ('aidTireRate',           ctypes.c_float),
        ('aidMechanicalDamage',   ctypes.c_float),
        ('aidAllowTyreBlankets',  ctypes.c_int),
        ('aidStability',          ctypes.c_float),
        ('aidAutoBrake',          ctypes.c_int),
        ('aidAutoShifter',        ctypes.c_int),
        ('aidAutoClutch',         ctypes.c_int),
        ('isSuper',               ctypes.c_int),
        ('pitWindowStart',        ctypes.c_int),
        ('pitWindowEnd',          ctypes.c_int),
        ('isOnline',              ctypes.c_int),
    ]


# ── OpenFFBoard CAN sender ────────────────────────────────────────────────────

class CANSender:
    """Sends CAN frames via the OpenFFBoard HID interface."""

    _CAN_CLASS   = 0xC01
    _CMD_SPEED   = 0x0
    _CMD_SEND    = 0x1
    _CMD_LEN     = 0x2
    _TYPE_WRITE  = 0
    _TYPE_WR_ADR = 3   # write with address — uses both data and addr fields

    def __init__(self):
        self.device = None
        self.report = None
        self.tx_count = 0

    def connect(self) -> bool:
        if not HID_AVAILABLE:
            return False
        try:
            devices = hid.HidDeviceFilter(
                vendor_id=VENDOR_ID, product_id=PRODUCT_ID
            ).get_devices()
            if not devices:
                return False
            self.device = devices[0]
            self.device.open()
            reports = self.device.find_output_reports(0xff00, 0x01)
            if not reports:
                self.device.close()
                self.device = None
                return False
            self.report = reports[0]
            # Set DLC=8 for all frames; do NOT touch speed (configure in OpenFFBoard UI)
            self._cmd(self._TYPE_WRITE, self._CMD_LEN, 8)
            return True
        except Exception:
            self.device = None
            self.report = None
            return False

    def _cmd(self, type_: int, cmd: int, data: int = 0, adr: int = 0):
        r = self.report
        r[hid.get_full_usage_id(0xff00, 0x01)] = type_
        r[hid.get_full_usage_id(0xff00, 0x02)] = self._CAN_CLASS
        r[hid.get_full_usage_id(0xff00, 0x03)] = 0
        r[hid.get_full_usage_id(0xff00, 0x04)] = cmd
        r[hid.get_full_usage_id(0xff00, 0x05)] = data
        r[hid.get_full_usage_id(0xff00, 0x06)] = adr
        r.send()

    def send_frame(self, can_id: int, payload: bytes):
        """Transmit one CAN frame onto the bus."""
        if self.report is None:
            return
        # Pad to 8 bytes; interpret as signed int64 (what the firmware expects)
        padded = (bytes(payload)[:8] + b'\x00' * 8)[:8]
        data_int = struct.unpack('<q', padded)[0]   # signed LE int64
        self._cmd(self._TYPE_WR_ADR, self._CMD_SEND, data_int, can_id)
        self.tx_count += 1

    def is_connected(self) -> bool:
        return self.device is not None and self.device.is_plugged()

    def disconnect(self):
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
        self.device = None
        self.report = None


# ── Telemetry → CAN frame packers ─────────────────────────────────────────────

def frame_engine(phys: Physics) -> tuple:
    """0x100: RPM, speed, gear, throttle, brake, clutch."""
    rpm      = min(max(int(phys.rpms),            0), 0xFFFF)
    speed    = min(max(int(phys.speedKmh * 10),   0), 0xFFFF)
    gear     = min(max(int(phys.gear),             0), 0xFF)   # 0=R 1=N 2=1st…
    throttle = min(max(int(phys.gas    * 100), 0), 100)
    brake    = min(max(int(phys.brake  * 100), 0), 100)
    clutch   = min(max(int(phys.clutch * 100), 0), 100)
    return 0x100, struct.pack('<HHBBBB', rpm, speed, gear, throttle, brake, clutch)


def frame_temps(phys: Physics) -> tuple:
    """0x101: Air/road/tyre core temperatures."""
    air  = min(max(int(phys.airTemp  + 40), 0), 255)
    road = min(max(int(phys.roadTemp + 40), 0), 255)
    tc   = [min(max(int(t), 0), 255) for t in phys.tyreCoreTemperature]
    return 0x101, bytes([air, road, tc[0], tc[1], tc[2], tc[3], 0, 0])


def frame_session(phys: Physics, gfx: Graphics) -> tuple:
    """0x102: Lap times, lap count, position, fuel."""
    cur  = min(max(gfx.iCurrentTime // 10, 0), 0xFFFF)
    best = min(max(gfx.iBestTime    // 10, 0), 0xFFFF)
    laps = min(max(gfx.completedLaps,      0), 0xFF)
    pos  = min(max(gfx.position,           0), 0xFF)
    fuel = min(max(int(phys.fuel * 10),    0), 0xFFFF)
    return 0x102, struct.pack('<HHBBH', cur, best, laps, pos, fuel)


# ── Shared memory helpers ─────────────────────────────────────────────────────

def open_shm(name: str, size: int) -> mmap.mmap:
    return mmap.mmap(-1, size, tagname=name, access=mmap.ACCESS_READ)


def read_struct(mm: mmap.mmap, struct_type):
    mm.seek(0)
    return struct_type.from_buffer_copy(mm.read(ctypes.sizeof(struct_type)))


# ── Terminal display ──────────────────────────────────────────────────────────

STATUS_LABELS = {0: 'OFF', 1: 'REPLAY', 2: 'LIVE', 3: 'PAUSED'}
GEAR_LABELS   = {0: 'N', -1: 'R'}

def _gear(g: int) -> str:
    return GEAR_LABELS.get(g, str(g))

def _rpm_bar(rpms: int, max_rpm: int, width: int = 20) -> str:
    ratio  = min(rpms / max_rpm, 1.0) if max_rpm > 0 else 0
    filled = int(ratio * width)
    return '[' + '#' * filled + '-' * (width - filled) + ']'

_display_init = False

def print_telemetry(phys: Physics, gfx: Graphics, sta: Static,
                    can_status: str, tx_count: int):
    global _display_init
    rpm_pct = (phys.rpms / sta.maxRpm * 100) if sta.maxRpm > 0 else 0.0
    speed_presets = {0: '125k', 1: '250k', 2: '500k', 3: '1M'}

    lines = [
        f"  Car    : {sta.carModel or '---'}  |  Track: {sta.track or '---'}",
        f"  Status : {STATUS_LABELS.get(gfx.status,'?')}  |  Lap: {gfx.completedLaps + 1}  |  Pos: {gfx.position}",
        f"  Time   : {gfx.currentTime}  |  Best: {gfx.bestTime}",
        f"  CAN    : {can_status}  {speed_presets.get(CAN_SPEED_PRESET,'?')}bps  |  TX frames: {tx_count}",
        "  ──────────────────────────────────────────────",
        f"  Speed  : {phys.speedKmh:6.1f} km/h  ({phys.speedKmh * 0.621371:.1f} mph)",
        f"  RPM    : {phys.rpms:5d} / {sta.maxRpm}  ({rpm_pct:.0f}%)  {_rpm_bar(phys.rpms, sta.maxRpm)}",
        f"  Gear   : {_gear(phys.gear - 1)}",
        f"  Throttle: {phys.gas * 100:5.1f}%   Brake: {phys.brake * 100:5.1f}%   Clutch: {phys.clutch * 100:.1f}%",
        f"  Fuel   : {phys.fuel:5.1f} L",
        f"  Air    : {phys.airTemp:.1f}°C   Road: {phys.roadTemp:.1f}°C",
        "  Tyre °C : FL={:.0f}  FR={:.0f}  RL={:.0f}  RR={:.0f}".format(
            *phys.tyreCoreTemperature),
        "  0x100   : {:04X}h  0x101 : {:02X}h  0x102 : {:04X}h".format(
            min(int(phys.rpms), 0xFFFF),
            min(int(phys.speedKmh * 10), 0xFFFF),
            min(int(phys.fuel * 10), 0xFFFF),
        ),
    ]

    if _display_init:
        sys.stdout.write(f'\033[{len(lines)}A')
    _display_init = True

    for line in lines:
        sys.stdout.write(line + '\033[K\n')
    sys.stdout.flush()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    if not HID_AVAILABLE:
        print("WARNING: pywinusb not installed — CAN output disabled.")
        print("         Install with:  pip install pywinusb\n")

    print("Assetto Corsa → CAN Bridge")
    print("Ctrl+C to stop\n")

    sender   = CANSender()
    phys_mm  = gfx_mm = sta_mm = None
    interval = 1.0 / REFRESH_HZ

    try:
        while True:
            t0 = time.monotonic()

            # ── Reconnect to AC shared memory ──
            if phys_mm is None:
                try:
                    phys_mm = open_shm('Local\\acpmf_physics',  ctypes.sizeof(Physics))
                    gfx_mm  = open_shm('Local\\acpmf_graphics', ctypes.sizeof(Graphics))
                    sta_mm  = open_shm('Local\\acpmf_static',   ctypes.sizeof(Static))
                    print("Connected to Assetto Corsa.\n")
                    print('\n' * 13)
                except Exception:
                    time.sleep(2)
                    continue

            # ── Reconnect to HID device ──
            if not sender.is_connected():
                sender.connect()

            can_status = 'CONNECTED' if sender.is_connected() else 'NOT FOUND'

            # ── Read telemetry ──
            try:
                phys = read_struct(phys_mm, Physics)
                gfx  = read_struct(gfx_mm,  Graphics)
                sta  = read_struct(sta_mm,   Static)
            except Exception as e:
                print(f"\nShared memory read error: {e}")
                phys_mm = gfx_mm = sta_mm = None
                time.sleep(2)
                continue

            # ── Transmit CAN frames ──
            if sender.is_connected():
                try:
                    sender.send_frame(*frame_engine(phys))
                    sender.send_frame(*frame_temps(phys))
                    sender.send_frame(*frame_session(phys, gfx))
                except Exception:
                    sender.disconnect()

            # ── Update display ──
            print_telemetry(phys, gfx, sta, can_status, sender.tx_count)

            # ── Sleep remainder of interval ──
            elapsed = time.monotonic() - t0
            sleep_t = interval - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        sender.disconnect()
        for mm in (phys_mm, gfx_mm, sta_mm):
            if mm:
                mm.close()


if __name__ == '__main__':
    main()
