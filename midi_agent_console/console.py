"""Live state of a MIDI controller, plus blocking "ask the human" primitives.

Events arrive on the MIDI driver's thread; agents read state and wait for
answers from other threads. Everything shared goes through one Condition.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from .events import MidiEvent, note_name
from .profiles import Profile

WHITE_KEYS = {0: 1, 2: 2, 4: 3, 5: 4, 7: 5, 9: 6, 11: 7}  # pitch class -> option number (C=1 ... B=7)


class Console:
    def __init__(self, profile: Profile, history: int = 500):
        self.profile = profile
        self._cond = threading.Condition()
        self._events: deque[MidiEvent] = deque(maxlen=history)
        self._seq = 0  # total events seen; lets waiters ignore anything older than their call
        self._cc: dict[tuple[int, int], int] = {}
        self._pitchbend = 0
        self._held: dict[int, int] = {}  # note -> velocity
        self._listeners: list[Callable[[MidiEvent], None]] = []

    # ------------------------------------------------------------ input side
    def feed(self, ev: MidiEvent) -> None:
        with self._cond:
            if ev.kind == "note_on":
                self._held[ev.note] = ev.velocity
            elif ev.kind == "note_off":
                self._held.pop(ev.note, None)
            elif ev.kind == "cc":
                self._cc[(ev.channel, ev.control)] = ev.value
            elif ev.kind == "pitchbend":
                self._pitchbend = ev.value
            self._events.append(ev)
            self._seq += 1
            self._cond.notify_all()
            listeners = list(self._listeners)
        for fn in listeners:
            fn(ev)

    def add_listener(self, fn: Callable[[MidiEvent], None]) -> None:
        with self._cond:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[MidiEvent], None]) -> None:
        with self._cond:
            if fn in self._listeners:
                self._listeners.remove(fn)

    # ------------------------------------------------------------ reading
    def control_value(self, spec: str) -> int | None:
        """'pitchbend' -> -8192..8191, 'cc:N' -> 0..127 (any channel, most recent), None if never moved."""
        with self._cond:
            if spec == "pitchbend":
                return self._pitchbend
            if spec.startswith("cc:"):
                num = int(spec[3:])
                vals = [v for (_ch, c), v in self._cc.items() if c == num]
                return vals[-1] if vals else None
        raise ValueError(f"unknown control spec {spec!r}")

    def control_normalized(self, spec: str) -> float | None:
        """0.0..1.0 for CCs, -1.0..1.0 for pitchbend."""
        v = self.control_value(spec)
        if v is None:
            return None
        return v / 8192 if spec == "pitchbend" else v / 127

    def snapshot(self) -> dict:
        controls = {}
        for name, spec in self.profile.controls.items():
            controls[name] = {"spec": spec, "value": self.control_value(spec),
                              "normalized": self.control_normalized(spec)}
        with self._cond:
            held = [{"note": n, "name": note_name(n), "velocity": v} for n, v in sorted(self._held.items())]
            return {"profile": self.profile.name, "controls": controls, "held_notes": held,
                    "events_seen": self._seq}

    def recent(self, n: int = 20) -> list[dict]:
        with self._cond:
            return [e.to_dict() for e in list(self._events)[-n:]]

    # ------------------------------------------------------------ waiting
    def wait_for(self, match: Callable[[MidiEvent], object], timeout: float) -> tuple[MidiEvent, object] | None:
        """Block until an event *newer than this call* makes `match` return something truthy."""
        deadline = time.monotonic() + timeout
        with self._cond:
            seen = self._seq
            while True:
                fresh = self._seq - seen
                if fresh:
                    evs = list(self._events)
                    for ev in evs[-min(fresh, len(evs)):]:
                        r = match(ev)
                        if r:
                            return ev, r
                    seen = self._seq
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)

    def wait_choice(self, n_options: int, timeout: float) -> dict | None:
        """White keys answer: C=1, D=2, E=3, F=4, G=5, A=6, B=7, in any octave."""
        if not 1 <= n_options <= 7:
            raise ValueError("1-7 options (one per white key in an octave)")

        def match(ev: MidiEvent):
            if ev.kind == "note_on":
                k = WHITE_KEYS.get(ev.note % 12)
                if k and k <= n_options:
                    return k
            return None

        hit = self.wait_for(match, timeout)
        if hit is None:
            return None
        ev, k = hit
        return {"choice": k, "key": note_name(ev.note), "velocity": ev.velocity}

    def wait_confirm(self, timeout: float) -> dict | None:
        """Yes = approve control (e.g. Hold) or a key at/above split_note; No = a key below it."""
        approve = self.profile.controls.get(self.profile.approve_control or "", None)

        def match(ev: MidiEvent):
            if approve and approve.startswith("cc:") and ev.kind == "cc" \
                    and ev.control == int(approve[3:]) and ev.value >= 64:
                return ("yes", self.profile.approve_control)
            if ev.kind == "note_on":
                return ("yes", note_name(ev.note)) if ev.note >= self.profile.split_note \
                    else ("no", note_name(ev.note))
            return None

        hit = self.wait_for(match, timeout)
        if hit is None:
            return None
        ev, (answer, via) = hit
        return {"answer": answer, "via": via, "velocity": ev.velocity}

    def wait_any_note(self, timeout: float) -> dict | None:
        hit = self.wait_for(lambda e: e.kind == "note_on", timeout)
        return hit[0].to_dict() if hit else None
