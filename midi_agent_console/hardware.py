"""External hardware the agent can drive, over a tiny line protocol.

Commands (one per line, ASCII, newline-terminated; device answers one line):
    PING                   -> PONG
    LED <i> <r> <g> <b>    i 0-63, colour 0-255 each -> OK
    SERVO <i> <deg>        i 0-15, angle 0-180       -> OK
    STATE                  -> device-defined one-line summary

Commands are validated here before anything reaches the wire, so an agent
cannot send arbitrary bytes to the board.
"""
from __future__ import annotations

import threading
from typing import Protocol

LIMITS = {"LED": [(0, 63), (0, 255), (0, 255), (0, 255)], "SERVO": [(0, 15), (0, 180)],
          "PING": [], "STATE": []}


def validate(command: str) -> str:
    parts = command.strip().split()
    if not parts:
        raise ValueError("empty command")
    op = parts[0].upper()
    if op not in LIMITS:
        raise ValueError(f"unknown command {op!r}; allowed: {sorted(LIMITS)}")
    spec, args = LIMITS[op], parts[1:]
    if len(args) != len(spec):
        raise ValueError(f"{op} takes {len(spec)} integer argument(s), got {len(args)}")
    vals = []
    for a, (lo, hi) in zip(args, spec):
        v = int(a)
        if not lo <= v <= hi:
            raise ValueError(f"{op}: {v} outside {lo}-{hi}")
        vals.append(str(v))
    return " ".join([op, *vals])


class Device(Protocol):
    name: str

    def send(self, command: str) -> str: ...

    def close(self) -> None: ...


class SimulatedDevice:
    """Stands in for a board so everything works with no hardware attached."""

    def __init__(self):
        self.name = "simulated"
        self.leds: dict[int, tuple[int, int, int]] = {}
        self.servos: dict[int, int] = {}
        self.log: list[str] = []
        self._lock = threading.Lock()

    def send(self, command: str) -> str:
        cmd = validate(command)
        op, *a = cmd.split()
        with self._lock:
            self.log.append(cmd)
            if op == "PING":
                return "PONG"
            if op == "LED":
                self.leds[int(a[0])] = (int(a[1]), int(a[2]), int(a[3]))
            elif op == "SERVO":
                self.servos[int(a[0])] = int(a[1])
            elif op == "STATE":
                return f"leds={self.leds} servos={self.servos}"
            return "OK"

    def close(self) -> None:
        pass


class SerialDevice:
    """A board on a serial port (Arduino / ESP32 running firmware/esp32_line_protocol)."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        import serial  # pyserial

        self.name = f"serial:{port}"
        self._ser = serial.Serial(port, baud, timeout=timeout)
        self._lock = threading.Lock()

    def send(self, command: str) -> str:
        cmd = validate(command)
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write((cmd + "\n").encode("ascii"))
            reply = self._ser.readline().decode("ascii", "replace").strip()
        return reply or "(no reply before timeout)"

    def close(self) -> None:
        self._ser.close()


def open_device(spec: str | None) -> Device:
    """None / 'sim' -> simulated; 'COM5' or '/dev/ttyUSB0', optionally '@baud' -> serial."""
    if not spec or spec == "sim":
        return SimulatedDevice()
    port, _, baud = spec.partition("@")
    return SerialDevice(port, int(baud) if baud else 115200)
