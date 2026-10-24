"""Live state of a MIDI controller, plus "ask the human" primitives.

Waiting is built from two pieces:
  * a Matcher decides what counts as an answer (choice, confirm, long-press...);
  * a _WaitState applies the safety rules every wait shares:
      - keys already held when the question starts never count,
      - nothing counts during the first `arm_delay` seconds (no "I was already
        reaching for the key" approvals),
      - an emergency stop ends the wait immediately.
The same state machine is driven synchronously (CLI / hook) or with asyncio
(MCP server, cancellable, reports progress).
"""
from __future__ import annotations

import asyncio
import queue
import random
import sys
import threading
import time
from collections import deque
from typing import Any, Awaitable, Callable

from .events import MidiEvent, note_name
from .profiles import Profile

WHITE_KEYS = {0: 1, 2: 2, 4: 3, 5: 4, 7: 5, 9: 6, 11: 7}  # pitch class -> option number (C=1 ... B=7)
_ESTOP = object()
_TICK = 0.05
CHORD_WINDOW = 0.06  # an answer is held this long so a slammed e-stop chord wins over its first key


# ------------------------------------------------------------------ matchers
class Matcher:
    def feed(self, ev: MidiEvent) -> Any:
        return None

    def poll(self, now: float) -> Any:
        return None


class AnyNote(Matcher):
    def feed(self, ev):
        return ev.to_dict() if ev.kind == "note_on" else None


class Choice(Matcher):
    """White keys answer: C=1, D=2, E=3, F=4, G=5, A=6, B=7, in any octave."""

    def __init__(self, n_options: int):
        if not 1 <= n_options <= 7:
            raise ValueError("1-7 options (one per white key in an octave)")
        self.n = n_options

    def feed(self, ev):
        if ev.kind == "note_on":
            k = WHITE_KEYS.get(ev.note % 12)
            if k and k <= self.n:
                return {"choice": k, "key": note_name(ev.note), "velocity": ev.velocity}
        return None


class Confirm(Matcher):
    """No = any key below split_note, at once (saying no must be easy).
    Yes (normal)    = a key at/above split_note, or the approve control (a sustain pedal).
    Yes (dangerous) = a key at/above split_note held for `long_press` seconds;
                      a quick tap does nothing, so a stray touch cannot approve; a slam
                      (velocity >= panic_velocity) is read as panic and answers no, like
                      squeezing a three-position enabling switch. While the key is held a
                      tick sounds every `tick_every` s; the yes sound only plays on commit."""

    def __init__(self, profile: Profile, dangerous: bool = False, long_press: float = 1.5,
                 panic_velocity: int = 124, tick_every: float = 0.4):
        self.p, self.dangerous, self.long_press = profile, dangerous, long_press
        self.panic_velocity, self.tick_every = panic_velocity, tick_every
        spec = profile.controls.get(profile.approve_control or "", "")
        self.approve_cc = int(spec[3:]) if spec.startswith("cc:") else None
        self._pressed: dict[int, float] = {}  # note -> press time (yes side, dangerous mode)
        self._last_tick = 0.0
        self.on_tick: Callable[[], None] | None = None

    def feed(self, ev):
        if ev.kind == "note_on" and ev.note < self.p.split_note:
            return {"answer": "no", "via": note_name(ev.note)}
        if not self.dangerous:
            if ev.kind == "note_on":
                return {"answer": "yes", "via": note_name(ev.note), "velocity": ev.velocity}
            if ev.kind == "cc" and ev.control == self.approve_cc and ev.value >= 64:
                return {"answer": "yes", "via": self.p.approve_control}
            return None
        if ev.kind == "note_on":
            if ev.velocity >= self.panic_velocity:
                return {"answer": "no", "via": f"slam {note_name(ev.note)} (velocity {ev.velocity})"}
            self._pressed[ev.note] = ev.t
            self._last_tick = ev.t
        elif ev.kind == "note_off":
            self._pressed.pop(ev.note, None)
        return None

    def poll(self, now):
        for note, t0 in self._pressed.items():
            if now - t0 >= self.long_press:
                return {"answer": "yes", "via": f"long-press {note_name(note)}", "held_s": round(now - t0, 2)}
        if self._pressed and self.on_tick and now - self._last_tick >= self.tick_every:
            self._last_tick = now
            self.on_tick()
        return None


# ------------------------------------------------------------------ wait state
class _WaitState:
    def __init__(self, matcher: Matcher, held_at_start: set[int], arm_delay: float):
        self.m = matcher
        self.start = time.monotonic()
        self.armed_at = self.start + arm_delay
        self.ignore = set(held_at_start)  # notes down before the question: ignore until released
        self.pending: tuple[Any, float] | None = None

    def feed(self, ev: MidiEvent) -> Any:
        if ev.kind in ("note_on", "note_off") and ev.note in self.ignore:
            if ev.kind == "note_off":
                self.ignore.discard(ev.note)
            return None
        if ev.t < self.armed_at:
            if ev.kind == "note_on":
                self.ignore.add(ev.note)  # pressed too early: this press never counts
            return None
        return self.m.feed(ev)

    def poll(self, now: float) -> Any:
        return self.m.poll(now) if now >= self.armed_at else None

    def step(self, item: MidiEvent | None, now: float) -> Any:
        """Feed one event (or None for a tick); returns the answer once it has survived
        CHORD_WINDOW without an emergency stop arriving."""
        if item is not None:
            r = self.feed(item)
            if r and self.pending is None:
                self.pending = (r, now)
        if self.pending is None:
            r = self.poll(now)
            if r:
                self.pending = (r, now)
        if self.pending and now - self.pending[1] >= CHORD_WINDOW:
            return self.pending[0]
        return None


# ------------------------------------------------------------------ console
class Console:
    def __init__(self, profile: Profile, history: int = 500, feedback=None,
                 arm_delay: float = 0.3, estop_keys: int = 3):
        self.profile = profile
        self.feedback = feedback
        self.arm_delay = arm_delay
        self.estop_keys = estop_keys  # this many keys held at once on the "no" side = emergency stop
        self._lock = threading.Lock()
        self._events: deque[MidiEvent] = deque(maxlen=history)
        self._seq = 0
        self._cc: dict[int, int] = {}
        self._pitchbend = 0
        self._held: dict[int, int] = {}  # note -> velocity
        self._listeners: list[Callable[[MidiEvent], None]] = []
        self._estop_listeners: list[Callable[[], None]] = []
        self.estopped = False
        self.busy: str | None = None  # e.g. "teleop": keys are driving, so they answer nothing
        self._chord_down = False
        self.waiting: str | None = None  # what the keyboard is being asked right now, if anything

    # ------------------------------------------------------------ input side
    def feed(self, ev: MidiEvent) -> None:
        estop_now = False
        with self._lock:
            key = (ev.channel, ev.note)  # per channel: a virtual release must not free a real key
            if ev.kind == "note_on":
                self._held[key] = ev.velocity
                low = sum(1 for _c, n in self._held if n < self.profile.split_note)
                # every new slam counts, even when already latched: it re-sends ESTOP and
                # cancels a reset that is waiting for its long-press
                if self.estop_keys and low >= self.estop_keys and not self._chord_down:
                    self._chord_down = True
                    self.estopped = estop_now = True
            elif ev.kind == "note_off":
                self._held.pop(key, None)
                if sum(1 for _c, n in self._held if n < self.profile.split_note) < self.estop_keys:
                    self._chord_down = False
            elif ev.kind == "cc":
                self._cc[ev.control] = ev.value
            elif ev.kind == "pitchbend":
                self._pitchbend = ev.value
            self._events.append(ev)
            self._seq += 1
            listeners = list(self._listeners)
            estop_listeners = list(self._estop_listeners) if estop_now else []
        if estop_now:
            self._fire_estop(estop_listeners)
        for fn in listeners:  # one failing listener must not starve the rest (e.g. teleop's STOP)
            try:
                fn(ev)
            except Exception as exc:
                print(f"[console] listener error: {exc!r}", file=sys.stderr)

    def trigger_estop(self) -> None:
        """Latch the stop and (re-)notify everyone, even if already latched: a stop request
        must always reach the board again in case the first ESTOP was lost."""
        with self._lock:
            self.estopped = True
            estop_listeners = list(self._estop_listeners)
        self._fire_estop(estop_listeners)

    def _fire_estop(self, listeners) -> None:
        if self.feedback:
            self.feedback.play("estop")
        for fn in listeners:
            try:
                fn()
            except Exception as exc:
                print(f"[console] estop listener error: {exc!r}", file=sys.stderr)

    def clear_estop(self) -> None:
        with self._lock:
            self.estopped = False

    def add_listener(self, fn: Callable[[MidiEvent], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[MidiEvent], None]) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def add_estop_listener(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._estop_listeners.append(fn)

    def remove_estop_listener(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if fn in self._estop_listeners:
                self._estop_listeners.remove(fn)

    # ------------------------------------------------------------ reading
    def control_value(self, spec: str) -> int | None:
        """'pitchbend' -> -8192..8191, 'cc:N' -> 0..127 (most recent on any channel), None if never moved."""
        with self._lock:
            if spec == "pitchbend":
                return self._pitchbend
            if spec.startswith("cc:"):
                return self._cc.get(int(spec[3:]))
        raise ValueError(f"unknown control spec {spec!r}")

    def control_normalized(self, spec: str) -> float | None:
        """0.0..1.0 for CCs, -1.0..1.0 for pitchbend."""
        v = self.control_value(spec)
        if v is None:
            return None
        return v / 8192 if spec == "pitchbend" else v / 127

    def held_notes(self) -> set[int]:
        with self._lock:
            return {n for _c, n in self._held}

    def snapshot(self) -> dict:
        controls = {name: {"spec": spec, "value": self.control_value(spec),
                           "normalized": self.control_normalized(spec)}
                    for name, spec in self.profile.controls.items()}
        with self._lock:
            held = [{"note": n, "name": note_name(n), "velocity": v, "channel": c}
                    for (c, n), v in sorted(self._held.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
            return {"profile": self.profile.name, "controls": controls, "held_notes": held,
                    "events_seen": self._seq, "estopped": self.estopped, "waiting": self.waiting}

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            return [e.to_dict() for e in list(self._events)[-n:]]

    # ------------------------------------------------------------ waiting (sync)
    def wait(self, matcher: Matcher, timeout: float) -> dict | None:
        """Block until `matcher` answers; None on timeout; {'answer': 'estop'} on emergency stop."""
        q: queue.SimpleQueue = queue.SimpleQueue()
        return self._run_wait(matcher, timeout, q.put, lambda: q.put(_ESTOP),
                              lambda left: _get(q, min(_TICK, left)))

    def wait_choice(self, n_options: int, timeout: float) -> dict | None:
        return self.wait(Choice(n_options), timeout)

    def wait_confirm(self, timeout: float, dangerous: bool = False) -> dict | None:
        return self.wait(Confirm(self.profile, dangerous), timeout)

    def wait_any_note(self, timeout: float) -> dict | None:
        return self.wait(AnyNote(), timeout)

    def _run_wait(self, matcher, timeout, put, put_estop, get):
        if self.busy:
            return {"answer": "busy", "reason": f"keyboard is in {self.busy} mode"}
        if self.estopped:
            return {"answer": "estop", "reason": "emergency stop is latched; reset it first"}
        state = _WaitState(matcher, self.held_notes(), self.arm_delay)
        if hasattr(matcher, "on_tick") and matcher.on_tick is None:
            matcher.on_tick = lambda: self._sound("hold")
        self.add_listener(put)
        self.add_estop_listener(put_estop)
        self.waiting = self._describe(matcher)
        self._sound(self._ask_motif(matcher))
        try:
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    self._sound("timeout")
                    return None
                item = get(left)
                if item is _ESTOP:
                    return {"answer": "estop"}
                r = state.step(item, time.monotonic())
                if r:
                    self._sound("no" if r.get("answer") == "no" else "yes")
                    return r
        finally:
            self.waiting = None
            self.remove_listener(put)
            self.remove_estop_listener(put_estop)

    # ------------------------------------------------------------ waiting (async)
    async def await_answer(self, matcher: Matcher, timeout: float,
                           progress: Callable[[float, float], Awaitable[None]] | None = None,
                           while_estopped: bool = False, ask_motif: str | None = None) -> dict | None:
        """Async, cancellable version of wait(). Events arrive on the MIDI dispatcher
        thread and are handed to this event loop with call_soon_threadsafe.
        while_estopped=True is only for asking the human to clear the stop itself."""
        if self.busy:
            return {"answer": "busy", "reason": f"keyboard is in {self.busy} mode"}
        if self.estopped and not while_estopped:
            return {"answer": "estop", "reason": "emergency stop is latched; reset it first"}
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        put = lambda ev: loop.call_soon_threadsafe(q.put_nowait, ev)  # noqa: E731
        put_estop = lambda: loop.call_soon_threadsafe(q.put_nowait, _ESTOP)  # noqa: E731
        state = _WaitState(matcher, self.held_notes(), self.arm_delay)
        if hasattr(matcher, "on_tick") and matcher.on_tick is None:
            matcher.on_tick = lambda: self._sound("hold")
        self.add_listener(put)
        self.add_estop_listener(put_estop)
        self.waiting = self._describe(matcher)
        self._sound(ask_motif or self._ask_motif(matcher))
        try:
            start = time.monotonic()
            next_report = start + 1.0
            while True:
                now = time.monotonic()
                left = start + timeout - now
                if left <= 0:
                    self._sound("timeout")
                    return None
                if progress and now >= next_report:
                    await progress(now - start, timeout)
                    next_report = now + 1.0
                try:
                    item = await asyncio.wait_for(q.get(), min(_TICK, left))
                except asyncio.TimeoutError:
                    item = None
                if item is _ESTOP:
                    return {"answer": "estop"}
                r = state.step(item, time.monotonic())
                if r:
                    self._sound("no" if r.get("answer") == "no" else "yes")
                    return r
        finally:
            self.waiting = None
            self.remove_listener(put)
            self.remove_estop_listener(put_estop)

    @staticmethod
    def _describe(matcher) -> str:
        if isinstance(matcher, Confirm):
            return "confirm-dangerous" if matcher.dangerous else "confirm"
        if isinstance(matcher, Choice):
            return f"choice-{matcher.n}"
        return type(matcher).__name__.lower()

    @staticmethod
    def _ask_motif(matcher) -> str:
        """Dangerous questions rotate between warning phrases; everything else uses "ask"."""
        return f"danger{random.randrange(3)}" if getattr(matcher, "dangerous", False) else "ask"

    def _sound(self, motif: str) -> None:
        if self.feedback:
            self.feedback.play(motif)


def _get(q: queue.SimpleQueue, timeout: float):
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None
