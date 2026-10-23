"""Live bindings: a physical control drives a hardware command continuously.

e.g. bind mod_strip -> "SERVO 0 {v}" over 0..180 and the touch strip
becomes a hand slider for the servo, with no agent round-trip per move.
An emergency stop drops every binding and sends ESTOP to the board.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .console import Console
from .events import MidiEvent
from .hardware import Device, servo_range, validate


@dataclass
class Binding:
    control: str  # profile control name, e.g. "mod_strip"
    template: str  # command with {v}, e.g. "SERVO 0 {v}" or "LED 0 {v} 0 0"
    out_min: int = 0
    out_max: int = 180
    min_interval: float = 0.03  # seconds between sends; serial links choke on 1 kHz CC floods
    _last_sent: float = field(default=0.0, repr=False)
    _last_value: int | None = field(default=None, repr=False)

    def scale(self, spec: str, raw: int) -> int:
        frac = (raw + 8192) / 16383 if spec == "pitchbend" else raw / 127
        return round(self.out_min + frac * (self.out_max - self.out_min))


class BindingManager:
    def __init__(self, console: Console, device: Device):
        self.console, self.device = console, device
        self.bindings: dict[str, Binding] = {}
        self.errors: list[str] = []
        console.add_listener(self._on_event)
        console.add_estop_listener(self._on_estop)

    def default_range(self, template: str) -> tuple[int, int]:
        """SERVO i {v} -> that servo's own limits from the device; otherwise 0..255."""
        parts = template.split()
        if len(parts) == 3 and parts[0].upper() == "SERVO" and parts[1].isdigit():
            return servo_range(self.device.caps, int(parts[1]))
        return 0, 255

    def add(self, b: Binding) -> str:
        if b.control not in self.console.profile.controls:
            raise ValueError(f"unknown control {b.control!r}; profile has {list(self.console.profile.controls)}")
        validate(b.template.format(v=b.out_min), self.device.caps)  # reject bad templates before any movement
        validate(b.template.format(v=b.out_max), self.device.caps)
        self.bindings[b.control] = b
        return f"{b.control} -> {b.template} over {b.out_min}..{b.out_max}"

    def remove(self, control: str) -> bool:
        return self.bindings.pop(control, None) is not None

    def describe(self) -> list[dict]:
        return [{"control": b.control, "template": b.template, "range": [b.out_min, b.out_max]}
                for b in self.bindings.values()]

    def device_reset(self) -> str:
        try:
            return self.device.send("RESET")
        except Exception as exc:
            return f"reset failed: {exc}"

    def _on_estop(self) -> None:
        self.bindings.clear()
        try:
            self.device.send("ESTOP")
        except Exception as exc:
            self.errors.append(f"estop: {exc}")

    def _on_event(self, ev: MidiEvent) -> None:
        if self.console.estopped:
            return
        for b in list(self.bindings.values()):
            spec = self.console.profile.controls[b.control]
            if spec == "pitchbend" and ev.kind == "pitchbend":
                raw = ev.value
            elif spec.startswith("cc:") and ev.kind == "cc" and ev.control == int(spec[3:]):
                raw = ev.value
            else:
                continue
            v = b.scale(spec, raw)
            now = time.monotonic()
            if v == b._last_value or now - b._last_sent < b.min_interval:
                continue
            b._last_sent, b._last_value = now, v
            try:
                self.device.send(b.template.format(v=v))
            except Exception as exc:
                self.errors.append(f"{b.control}: {exc}")
                del self.errors[:-20]
