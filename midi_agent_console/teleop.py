"""Drive a two-wheel car from the keyboard: the human is the controller, the firmware
is the safety net.

  * hold a key on the right half to drive: white keys forward, black keys reverse;
    the harder you hit, the faster (velocity -> PWM between pwm_min and pwm_max)
  * the pitch strip steers (differential mix, dead zone 5 %)
  * a 20 Hz timer resends DRIVE l r 300 while a key is held, so a lost link stops the
    car within 300 ms (and the firmware watchdog within 1 s)
  * releasing the last drive key sends STOP at once (once, like ROS teleop_twist_joy);
    speeding up is ramped (<= ramp PWM per tick, through 0 when reversing), stopping is not
  * the left-half e-stop chord still wins: teleop ends and the board gets ESTOP
While teleop is on, the keyboard answers no questions (Console.busy), so a drive key
can never approve something by accident.
"""
from __future__ import annotations

import threading

from .console import Console
from .events import MidiEvent
from .hardware import Device

BLACK = {1, 3, 6, 8, 10}


class Teleop:
    def __init__(self, console: Console, device: Device, period: float = 0.05, pulse_ms: int = 300,
                 pwm_min: int = 90, k: float = 0.6, ramp: int = 40, deadzone: float = 0.05,
                 steer: str = "pitchbend"):
        self.console, self.device = console, device
        self.period, self.pulse_ms, self.pwm_min, self.k = period, pulse_ms, pwm_min, k
        self.ramp, self.deadzone, self.steer = ramp, deadzone, steer
        drive = (device.caps or {}).get("drive") or {}
        if not drive:
            raise ValueError("this device has no motors (DESCRIBE reports no drive)")
        self.pwm_max = int(drive["max"])
        self.pwm_min = min(self.pwm_min, self.pwm_max)
        self._held: dict[int, int] = {}  # drive note -> velocity, in press order
        self._cur = (0, 0)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._stop_pending = False
        self._thread: threading.Thread | None = None
        self.sent: list[str] = []  # last commands, for status / tests

    # ------------------------------------------------------------ lifecycle
    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.active:
            return
        self._stop.clear()
        self.console.busy = "teleop"
        self.console.add_listener(self._on_event)
        self.console.add_estop_listener(self._on_estop)
        self._thread = threading.Thread(target=self._loop, name="teleop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.console.remove_listener(self._on_event)
        self.console.remove_estop_listener(self._on_estop)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        self._thread = None
        with self._lock:
            self._held.clear()
            self._cur = (0, 0)
        self._send("STOP")
        if self.console.busy == "teleop":
            self.console.busy = None

    # ------------------------------------------------------------ input
    def _on_event(self, ev: MidiEvent) -> None:
        if ev.kind not in ("note_on", "note_off") or ev.note < self.console.profile.split_note:
            return
        # MIDI thread: record and wake the loop; never touch the device here (a slow device
        # would delay the e-stop chord). The loop is the only sender, so STOP stays last.
        with self._lock:
            if ev.kind == "note_on":
                self._held.pop(ev.note, None)
                self._held[ev.note] = ev.velocity  # re-insert: the latest key decides direction
            elif ev.note in self._held:
                del self._held[ev.note]
                if not self._held:
                    self._cur = (0, 0)
                    self._stop_pending = True
        self._wake.set()

    def _on_estop(self) -> None:
        self._stop.set()  # the binding layer sends ESTOP; just leave the loop
        self._wake.set()

    # ------------------------------------------------------------ control
    def target(self) -> tuple[int, int]:
        with self._lock:
            return self._target_locked()

    def _target_locked(self) -> tuple[int, int]:
        if not self._held:
            return 0, 0
        last = next(reversed(self._held))
        vel = max(self._held.values())
        sign = -1 if last % 12 in BLACK else 1
        t = sign * (self.pwm_min + (self.pwm_max - self.pwm_min) * vel / 127) / self.pwm_max
        s = self.console.control_normalized(self.steer) or 0.0
        if abs(s) < self.deadzone:
            s = 0.0
        left, right = t + self.k * s, t - self.k * s
        m = max(abs(left), abs(right), 1.0)
        return round(left / m * self.pwm_max), round(right / m * self.pwm_max)

    def _step(self, cur: int, tgt: int) -> int:
        if cur and tgt and (cur > 0) != (tgt > 0):
            tgt = 0  # reversing: pass through zero first
        if tgt > cur:
            return min(tgt, cur + self.ramp)
        return max(tgt, cur - self.ramp)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.period)
            self._wake.clear()
            if self._stop.is_set() or self.console.estopped:
                break
            with self._lock:  # decide under the lock, send outside it
                cmd = None
                if self._stop_pending:
                    self._stop_pending, cmd = False, "STOP"
                elif self._held:
                    tl, tr = self._target_locked()
                    cl, cr = self._cur
                    nl, nr = self._step(cl, tl), self._step(cr, tr)
                    self._cur = (nl, nr)
                    cmd = f"DRIVE {nl} {nr} {self.pulse_ms}"
            if cmd:
                self._send(cmd)
        if self.console.busy == "teleop":
            self.console.busy = None

    def _send(self, cmd: str) -> None:
        try:
            reply = self.device.send(cmd)
        except Exception as exc:  # a dead link must not kill the loop; the firmware will stop
            reply = f"error: {exc}"
        self.sent.append(f"{cmd} -> {reply}")
        del self.sent[:-50]
