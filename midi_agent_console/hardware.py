"""External hardware the agent can drive, over a small strict line protocol (proto 2).

Request:  <id> <VERB> [int ...]\\n      id 1-65535, ints are -?\\d{1,6}, line <= 63 bytes
Reply:    <id> OK [payload] | <id> ERR <CODE> [detail]
Event:    ! <EVENT> [k=v ...]           unsolicited, no id (BOOT, WDT, DRIVE_DONE)

Verbs
    HELLO               -> OK HELLO proto=2 fw=... hb_ms=1000
    DESCRIBE            -> OK {json capability document}
    HB                  -> OK HB st=RUN|SAFE|ESTOP        heartbeat; the board goes SAFE after hb_ms without frames
    STATE               -> OK STATE st=... s0=cur>tgt ... led0=r,g,b drv=l,r,ms_left
    PING                -> OK PONG
    STOP                -> OK          motors off, servos hold, not latched
    ESTOP               -> OK          latched: motors off, servos detached, motion refused until RESET
    RESET               -> OK          clear ESTOP
    SERVO i deg         -> OK          refused (RANGE) outside that servo's own min/max; the board slews at max_dps
    LED i r g b         -> OK
    DRIVE l r ms        -> OK          -max..max each side (180 by default), 1..max_ms; stops by itself

Limits live in the firmware (DESCRIBE reports them) and are checked again here,
so neither a model nor a bug on one side can push hardware out of range.
"""
from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
from collections import deque
from typing import Any, Protocol

_INT = re.compile(r"-?\d{1,6}", re.ASCII)  # int() would accept "+5", "1_0" and full-width digits

DEFAULT_CAPS: dict[str, Any] = {
    "proto": 2, "fw": "sim-0.2", "hb_timeout_ms": 1000, "max_line": 63, "max_rate_hz": 100,
    "servos": [{"i": 0, "min": 10, "max": 170, "safe": 90, "max_dps": 180, "unit": "deg"},
               {"i": 1, "min": 10, "max": 170, "safe": 90, "max_dps": 180, "unit": "deg"}],
    "leds": [{"i": 0, "min": 0, "max": 255}],
    "drive": {"min": -180, "max": 180, "max_ms": 2000, "unit": "pwm"},
    "safe_state": "motors 0, servos -> safe, leds off",
    "user_only": ["RESET"],
}

NO_ARGS = ("HELLO", "DESCRIBE", "HB", "STATE", "PING", "STOP", "ESTOP", "RESET")
MOTION = ("SERVO", "LED", "DRIVE")


def _ints(op: str, args: list[str], n: int) -> list[int]:
    if len(args) != n:
        raise ValueError(f"{op} takes {n} integer argument(s), got {len(args)}")
    out = []
    for a in args:
        if not _INT.fullmatch(a):
            raise ValueError(f"{op}: {a!r} is not a plain integer")
        out.append(int(a))
    return out


def _within(op: str, what: str, v: int, lo: int, hi: int) -> None:
    if not lo <= v <= hi:
        raise ValueError(f"{op}: {what} {v} outside {lo}..{hi}")


def validate(command: str, caps: dict[str, Any] | None = None) -> str:
    """Normalize and range-check one command against the device's capabilities."""
    caps = caps or DEFAULT_CAPS
    parts = command.strip().split()
    if not parts:
        raise ValueError("empty command")
    op, args = parts[0].upper(), parts[1:]
    if op in NO_ARGS:
        _ints(op, args, 0)
        return op
    if op == "SERVO":
        i, deg = _ints(op, args, 2)
        servos = {s["i"]: s for s in caps["servos"]}
        if i not in servos:
            raise ValueError(f"SERVO: no servo {i}; have {sorted(servos)}")
        _within(op, f"servo {i} angle", deg, servos[i]["min"], servos[i]["max"])
        return f"SERVO {i} {deg}"
    if op == "LED":
        i, r, g, b = _ints(op, args, 4)
        leds = {d["i"]: d for d in caps["leds"]}
        if i not in leds:
            raise ValueError(f"LED: no led {i}; have {sorted(leds)}")
        for name, v in (("r", r), ("g", g), ("b", b)):
            _within(op, name, v, leds[i]["min"], leds[i]["max"])
        return f"LED {i} {r} {g} {b}"
    if op == "DRIVE":
        if not caps.get("drive"):
            raise ValueError("DRIVE: this device has no motors")
        left, right, ms = _ints(op, args, 3)
        d = caps["drive"]
        _within(op, "left", left, d["min"], d["max"])
        _within(op, "right", right, d["min"], d["max"])
        _within(op, "ms", ms, 1, d["max_ms"])
        return f"DRIVE {left} {right} {ms}"
    raise ValueError(f"unknown command {op!r}; allowed: {sorted(NO_ARGS + MOTION)}")


def servo_range(caps: dict[str, Any], i: int) -> tuple[int, int]:
    for s in caps["servos"]:
        if s["i"] == i:
            return s["min"], s["max"]
    raise ValueError(f"no servo {i}")


class Device(Protocol):
    name: str
    caps: dict[str, Any]

    def send(self, command: str) -> str: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------- simulator
class SimulatedDevice:
    """Behaves like the reference firmware, so everything works with no board attached."""

    def __init__(self, caps: dict[str, Any] | None = None):
        self.name = "simulated"
        self.caps = json.loads(json.dumps(caps or DEFAULT_CAPS))
        self.servos = {s["i"]: s["safe"] for s in self.caps["servos"]}
        self.leds = {d["i"]: (0, 0, 0) for d in self.caps["leds"]}
        self.drive = (0, 0, 0.0)  # left, right, deadline (monotonic)
        self.estopped = False
        self.log: list[str] = []
        self._lock = threading.Lock()
        # a little differential-drive car, so demos show something moving (metres, radians)
        self.x = self.y = self.theta = 0.0
        self.trail: deque[tuple[float, float]] = deque([(0.0, 0.0)], maxlen=600)
        self._t_last = time.monotonic()

    VMAX = 0.30   # m/s at full PWM
    TRACK = 0.12  # m between the wheels

    def _advance(self, now: float | None = None) -> None:
        """Integrate the pose up to `now` (caller holds the lock)."""
        now = time.monotonic() if now is None else now
        left, right, deadline = self.drive
        pmax = (self.caps.get("drive") or {}).get("max") or 1
        t = self._t_last
        end = min(now, deadline) if (left or right) else t
        while t < end:
            dt = min(0.02, end - t)
            vl, vr = left / pmax * self.VMAX, right / pmax * self.VMAX
            v, w = (vl + vr) / 2, (vr - vl) / self.TRACK
            self.x += v * math.cos(self.theta) * dt
            self.y += v * math.sin(self.theta) * dt
            self.theta = (self.theta + w * dt + math.pi) % (2 * math.pi) - math.pi
            t += dt
            if math.dist(self.trail[-1], (self.x, self.y)) > 0.01:
                self.trail.append((self.x, self.y))
        self._t_last = now

    def pose(self) -> dict[str, Any]:
        with self._lock:
            self._advance()
            left, right, dl = self.drive
            moving = (left or right) and time.monotonic() < dl
            return {"x": round(self.x, 3), "y": round(self.y, 3), "deg": round(math.degrees(self.theta), 1),
                    "drive": [left, right] if moving else [0, 0], "servos": dict(self.servos),
                    "leds": {i: list(c) for i, c in self.leds.items()}, "estopped": self.estopped,
                    "trail": [[round(a, 3), round(b, 3)] for a, b in self.trail]}

    def send(self, command: str) -> str:
        cmd = validate(command, self.caps)
        op, *a = cmd.split()
        v = [int(x) for x in a]
        with self._lock:
            self._advance()
            self.log.append(cmd)
            if op == "PING":
                return "OK PONG"
            if op == "HB":
                return f"OK HB st={self._st()}"
            if op == "HELLO":
                return f"OK HELLO proto=2 fw={self.caps['fw']} hb_ms={self.caps['hb_timeout_ms']}"
            if op == "DESCRIBE":
                return "OK " + json.dumps(self.caps, separators=(",", ":"))
            if op == "STATE":
                return "OK STATE " + self.state_line()
            if op == "STOP":
                self.drive = (0, 0, 0.0)
                return "OK"
            if op == "ESTOP":
                self.estopped, self.drive = True, (0, 0, 0.0)
                return "OK"
            if op == "RESET":
                self.estopped = False
                return "OK"
            if self.estopped:
                return "ERR ESTOP"
            if op == "SERVO":
                self.servos[v[0]] = v[1]
            elif op == "LED":
                self.leds[v[0]] = (v[1], v[2], v[3])
            elif op == "DRIVE":
                self.drive = (v[0], v[1], time.monotonic() + v[2] / 1000)
            return "OK"

    def _st(self) -> str:
        return "ESTOP" if self.estopped else "RUN"

    def state_line(self) -> str:
        left, right, dl = self.drive
        ms_left = max(0, int((dl - time.monotonic()) * 1000))
        if not ms_left:
            left = right = 0
        servos = " ".join(f"s{i}={d}" for i, d in sorted(self.servos.items()))
        leds = " ".join(f"led{i}={r},{g},{b}" for i, (r, g, b) in sorted(self.leds.items()))
        pose = f"pose={self.x:.3f},{self.y:.3f},{math.degrees(self.theta):.1f}"
        return f"st={self._st()} {servos} {leds} drv={left},{right},{ms_left} {pose}"

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- serial board
class DeviceError(RuntimeError):
    pass


class SerialDevice:
    """A board on a serial port running firmware/line_protocol (proto 2).

    A reader thread matches replies to request ids; a heartbeat thread sends HB
    every hb_period so the board's watchdog stays fed only while this process
    is alive. If this process dies or the cable is pulled, the board goes SAFE."""

    def __init__(self, port: str, baud: int = 115200, reply_timeout: float = 0.5,
                 hb_period: float = 0.3):
        import serial  # pyserial

        self.name = f"serial:{port}"
        self.reply_timeout = reply_timeout
        self.caps: dict[str, Any] = DEFAULT_CAPS
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout, ser.write_timeout = port, baud, 0.05, 0.5
        if sys.platform != "win32":
            ser.exclusive = True  # no other process may write RESET / DRIVE to our board
        ser.dtr = ser.rts = False  # avoid the auto-reset many boards do when the port opens
        ser.open()
        self._ser = ser
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, list] = {}  # id -> [Event, reply]
        self.events: deque[str] = deque(maxlen=100)
        self.connected = True
        self._stop = threading.Event()
        threading.Thread(target=self._reader, name="serial-reader", daemon=True).start()
        time.sleep(0.1)
        self._raw("HELLO")
        self.caps = json.loads(self._raw("DESCRIBE")[3:])
        threading.Thread(target=self._heartbeat, args=(hb_period,), name="serial-hb", daemon=True).start()

    def send(self, command: str) -> str:
        return self._raw(validate(command, self.caps))

    def _raw(self, cmd: str) -> str:
        with self._lock:
            rid = self._next_id
            self._next_id = self._next_id % 65535 + 1
            slot = [threading.Event(), None]
            self._pending[rid] = slot
            self._ser.write(f"{rid} {cmd}\n".encode("ascii"))
        if not slot[0].wait(self.reply_timeout):
            self._pending.pop(rid, None)
            raise DeviceError(f"no reply to {cmd!r} within {self.reply_timeout}s")
        return slot[1]

    def _reader(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception as exc:
                self.connected = False
                print(f"[hw] serial read failed: {exc}", file=sys.stderr)
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("ascii", "replace").strip()
                if not text:
                    continue
                if text.startswith("!"):
                    self.events.append(text)
                    continue
                rid, _, rest = text.partition(" ")
                slot = self._pending.pop(int(rid), None) if rid.isdigit() else None
                if slot:
                    slot[1] = rest
                    slot[0].set()

    def _heartbeat(self, period: float) -> None:
        """HB every `period` while the board answers. After 3 missed replies, back off to
        one HB per 2 s, longer than the board's 1 s watchdog: a half-broken link (commands
        arrive, replies do not) must let the board fall SAFE instead of feeding it."""
        misses = 0
        while not self._stop.wait(period if misses < 3 else 2.0):
            try:
                self._raw("HB")
                misses, self.connected = 0, True
            except Exception:
                misses += 1
                if misses >= 3:
                    self.connected = False

    def close(self) -> None:
        try:
            self._raw("STOP")
        except Exception:
            pass
        self._stop.set()
        self._ser.close()


def open_device(spec: str | None) -> Device:
    """None / 'sim' -> simulated; 'COM5' or '/dev/ttyUSB0', optionally '@baud' -> serial;
    'ws://host:9090#tb3-jazzy' -> a ROS 2 robot through rosbridge (profile after '#')."""
    if not spec or spec == "sim":
        return SimulatedDevice()
    if spec.startswith(("ws://", "wss://")):
        from .rosbridge import RosbridgeDevice

        url, _, profile = spec.partition("#")
        return RosbridgeDevice(url, profile or "turtlesim")
    port, _, baud = spec.partition("@")
    return SerialDevice(port, int(baud) if baud else 115200)
