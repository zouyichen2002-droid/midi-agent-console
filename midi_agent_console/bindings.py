"""Live bindings: a physical control drives a hardware command continuously.

e.g. bind mod_strip -> "SERVO 0 {v}" and the touch strip becomes a hand slider for the
servo, with no agent round-trip per move.

Device I/O never runs on the MIDI thread: events only record the latest value per
binding, and a worker thread sends them (coalesced, rate-limited by real send time).
A slow or silent device therefore cannot delay the emergency stop, which is sent on its
own thread, retried until the board answers OK, and re-sent on every new e-stop request.
Bindings pause while the keyboard is busy (teleop) and DRIVE templates are refused:
motors are only driven by teleop or by explicit, approved commands.
"""
from __future__ import annotations

import threading
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
    def __init__(self, console: Console, device: Device, estop_backoff_max: float = 2.0):
        self.console, self.device = console, device
        self.bindings: dict[str, Binding] = {}
        self.errors: list[str] = []
        self.board_estop_ack: bool | None = None  # None: never asked; True: board answered OK
        self.estop_backoff_max = estop_backoff_max
        self._pending: dict[str, int] = {}  # control -> latest value not yet sent
        self._cv = threading.Condition()
        self._estop_wanted = threading.Event()
        self._estop_gen = 0  # bumped by device_reset: an e-stop request from before a reset is void
        self._stop = threading.Event()
        threading.Thread(target=self._worker, name="bindings-io", daemon=True).start()
        threading.Thread(target=self._estop_sender, name="estop", daemon=True).start()
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
        if b.template.strip().upper().startswith("DRIVE"):
            raise ValueError("DRIVE cannot be bound to a control; use teleop for driving")
        validate(b.template.format(v=b.out_min), self.device.caps)  # reject bad templates before any movement
        validate(b.template.format(v=b.out_max), self.device.caps)
        with self._cv:
            if self.console.estopped:  # re-check under the lock: an e-stop may have just cleared everything
                raise ValueError("emergency stop is latched")
            self.bindings[b.control] = b
        return f"{b.control} -> {b.template} over {b.out_min}..{b.out_max}"

    def remove(self, control: str) -> bool:
        with self._cv:
            self._pending.pop(control, None)
            return self.bindings.pop(control, None) is not None

    def describe(self) -> list[dict]:
        with self._cv:
            return [{"control": b.control, "template": b.template, "range": [b.out_min, b.out_max]}
                    for b in self.bindings.values()]

    def device_reset(self) -> str:
        with self._cv:
            self._estop_gen += 1  # drop any e-stop still being retried from before this reset
            self._estop_wanted.clear()
        try:
            reply = self.device.send("RESET")
        except Exception as exc:
            return f"reset failed: {exc}"
        if reply.startswith("OK"):
            self.board_estop_ack = None
        return reply

    # ------------------------------------------------------------ e-stop
    def _on_estop(self) -> None:
        with self._cv:
            self.bindings.clear()
            self._pending.clear()
            self.board_estop_ack = False
        self._estop_wanted.set()  # never block the MIDI thread on the device

    def _estop_sender(self) -> None:
        """One thread for all e-stops: send ESTOP until the board answers OK (backing off up
        to estop_backoff_max), however long that takes, unless a reset voids the request."""
        while not self._stop.is_set():
            if not self._estop_wanted.wait(0.5):
                continue
            with self._cv:
                gen = self._estop_gen
            delay, attempt = 0.05, 0
            while not self._stop.is_set():
                with self._cv:
                    if gen != self._estop_gen or not self._estop_wanted.is_set():
                        break
                attempt += 1
                try:
                    reply = self.device.send("ESTOP")
                except Exception as exc:
                    reply = f"error: {exc}"
                if reply.startswith("OK"):
                    with self._cv:
                        if gen == self._estop_gen:
                            self.board_estop_ack = True
                            self._estop_wanted.clear()
                    break
                self._error(f"estop attempt {attempt}: {reply}")
                self._stop.wait(delay)
                delay = min(delay * 2, self.estop_backoff_max)

    # ------------------------------------------------------------ live values
    def _on_event(self, ev: MidiEvent) -> None:
        if self.console.estopped or self.console.busy:
            return
        with self._cv:
            for b in self.bindings.values():
                spec = self.console.profile.controls[b.control]
                if spec == "pitchbend" and ev.kind == "pitchbend":
                    raw = ev.value
                elif spec.startswith("cc:") and ev.kind == "cc" and ev.control == int(spec[3:]):
                    raw = ev.value
                else:
                    continue
                self._pending[b.control] = b.scale(spec, raw)
                self._cv.notify()

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while not self._pending and not self._stop.is_set():
                    self._cv.wait(0.5)
                now = time.monotonic()
                due, wait = [], 1.0
                for control, v in list(self._pending.items()):
                    b = self.bindings.get(control)
                    if b is None or v == b._last_value:
                        self._pending.pop(control)
                        continue
                    left = b.min_interval - (now - b._last_sent)
                    if left > 0:
                        wait = min(wait, left)
                        continue
                    self._pending.pop(control)
                    b._last_sent, b._last_value = now, v
                    due.append((b, v))
            for b, v in due:  # device I/O outside the lock
                if self.console.estopped or self.console.busy:
                    break
                try:
                    reply = self.device.send(b.template.format(v=v))
                    if not reply.startswith("OK"):
                        self._error(f"{b.control}: {reply}")
                except Exception as exc:
                    self._error(f"{b.control}: {exc}")
            if not due:
                time.sleep(min(wait, 0.05))

    def _error(self, msg: str) -> None:
        self.errors.append(msg)
        del self.errors[:-20]

    def close(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
