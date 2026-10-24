"""Guided MIDI learn: prompt for one control at a time, watch what it actually sends,
and write a profile JSON — so mappings come from the device, not from guesses.

Steps: lowest key, highest key, each strip / wheel, the Hold-type button pressed twice
(to tell a LATCHING button from a momentary one: a latching button must never be the
approve control), and the sustain pedal (optional). Anything the device did not show
is left out and listed under "unverified".
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from .events import MidiEvent, note_name

Collect = Callable[[str, float], list[MidiEvent]]


def console_collector(console, say: Callable[[str], None] = print) -> Collect:
    """Prompt, then record everything the keyboard sends for `seconds`."""
    def collect(prompt: str, seconds: float) -> list[MidiEvent]:
        got: list[MidiEvent] = []
        lock = threading.Lock()

        def on(ev):
            with lock:
                got.append(ev)

        say(f"\n>> {prompt}  ({seconds:.0f} s)")
        console.add_listener(on)
        try:
            time.sleep(seconds)
        finally:
            console.remove_listener(on)
        return list(got)

    return collect


def _continuous(events: list[MidiEvent], exclude: set[str]) -> str | None:
    """The control that sent the most distinct values: 'pitchbend' or 'cc:N'."""
    seen: dict[str, set[int]] = {}
    for e in events:
        spec = "pitchbend" if e.kind == "pitchbend" else f"cc:{e.control}" if e.kind == "cc" else None
        if spec and spec not in exclude:
            seen.setdefault(spec, set()).add(e.value)
    best = max(seen.items(), key=lambda kv: len(kv[1]), default=None)
    return best[0] if best and len(best[1]) >= 3 else None


def _button(events: list[MidiEvent], presses: int = 2, exclude: set[str] = frozenset()) -> dict | None:
    """Classify a button from `presses` presses: which CC, latching or momentary. A control
    that sends anything but on/off values (a strip brushed by accident) is not a button."""
    ccs = Counter(e.control for e in events if e.kind == "cc" and f"cc:{e.control}" not in exclude)
    if not ccs:
        return None
    cc = ccs.most_common(1)[0][0]
    values = [e.value for e in events if e.kind == "cc" and e.control == cc]
    if len(set(values)) > 2 or not set(values) <= {0, 127}:
        return {"spec": f"cc:{cc}", "kind": "continuous", "values": values}
    ons = sum(1 for v in values if v >= 64)
    offs = sum(1 for v in values if v < 64)
    # momentary: every press sends on AND off (2 presses -> ~2 ons, 2 offs)
    # latching : each press toggles (2 presses -> 1 on, 1 off)
    kind = "momentary" if ons >= presses and offs >= presses else "latching" if ons and offs else "unknown"
    return {"spec": f"cc:{cc}", "kind": kind, "values": values}


def _split(lo: int, hi: int) -> int:
    mid = (lo + hi) // 2
    near_c = round(mid / 12) * 12
    return near_c if abs(near_c - mid) <= 2 and lo < near_c <= hi else mid


def run_wizard(collect: Collect, name: str, port_match: str, strips: int = 2) -> dict:
    report: dict = {"steps": {}}
    unverified: list[str] = []

    lows = [e.note for e in collect("Press the LOWEST key once", 5) if e.kind == "note_on"]
    highs = [e.note for e in collect("Press the HIGHEST key once", 5) if e.kind == "note_on"]
    lo = min(lows) if lows else None
    hi = max(highs) if highs else None
    report["steps"]["range"] = [lo, hi]
    if lo is not None and hi is not None and hi > lo:
        split = _split(lo, hi)
        report["steps"]["range_names"] = [note_name(lo), note_name(hi), note_name(split)]
    else:
        split = 60
        unverified.append("key range (split defaulted to 60)")

    controls: dict[str, str] = {}
    labels = ["strip_1", "strip_2", "strip_3", "strip_4"][:strips]
    for label in labels:
        spec = _continuous(collect(f"Move {label.replace('_', ' ')} (touch strip / wheel) up and down a few times", 5),
                           set(controls.values()))
        report["steps"][label] = spec
        if spec:
            controls["pitch_strip" if spec == "pitchbend" and "pitch_strip" not in controls else label] = spec
        else:
            unverified.append(f"{label}: nothing continuous seen")

    continuous = set(controls.values())  # strips / wheels found above: never a button
    hold = _button(collect("Press the HOLD (or sustain) BUTTON twice, slowly", 6), exclude=continuous)
    report["steps"]["hold"] = hold
    if hold and hold["kind"] != "continuous":
        controls["hold"] = hold["spec"]
        if hold["kind"] != "momentary":
            unverified.append(f"hold is {hold['kind']}: not used to approve")
    else:
        unverified.append("hold button sent no MIDI")

    pedal = _button(collect("Press the SUSTAIN PEDAL twice (skip: wait)", 6), exclude=continuous)
    report["steps"]["pedal"] = pedal
    approve = None
    if pedal and pedal["kind"] == "momentary":
        controls["pedal"] = pedal["spec"]
        approve = "pedal"
    elif hold and hold["kind"] == "momentary":
        approve = "hold"
    if approve is None:
        unverified.append("no momentary approve control: yes = right half of the keyboard only")

    profile = {"name": name, "port_match": port_match, "controls": controls,
               "approve_control": approve, "split_note": split, "unverified": unverified}
    return {"profile": profile, "report": report}


def save_profile(result: dict, path: str | Path) -> Path:
    p = Path(path)
    p.write_text(json.dumps(result["profile"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p
