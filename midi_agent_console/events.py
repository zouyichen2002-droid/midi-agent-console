"""MIDI 1.0 short-message decoding into small, JSON-friendly events."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n: int) -> str:
    """60 -> 'C4' (middle C, scientific pitch notation)."""
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


@dataclass
class MidiEvent:
    kind: str  # note_on | note_off | cc | pitchbend | aftertouch | program | other
    channel: int = 1  # 1-16
    note: int | None = None
    velocity: int | None = None
    control: int | None = None
    value: int | None = None
    raw: tuple[int, ...] = ()
    t: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if v is not None and k != "raw"}
        if self.note is not None:
            d["name"] = note_name(self.note)
        return d


def decode(status: int, d1: int = 0, d2: int = 0, t: float | None = None) -> MidiEvent:
    kind, ch = status & 0xF0, (status & 0x0F) + 1
    raw = (status, d1, d2)
    kw = {"t": t} if t is not None else {}
    if kind == 0x90 and d2 > 0:
        return MidiEvent("note_on", ch, note=d1, velocity=d2, raw=raw, **kw)
    if kind in (0x80, 0x90):  # note_on with velocity 0 is a note_off
        return MidiEvent("note_off", ch, note=d1, velocity=0, raw=raw, **kw)
    if kind == 0xB0:
        return MidiEvent("cc", ch, control=d1, value=d2, raw=raw, **kw)
    if kind == 0xE0:
        return MidiEvent("pitchbend", ch, value=((d2 << 7) | d1) - 8192, raw=raw, **kw)
    if kind == 0xD0:
        return MidiEvent("aftertouch", ch, value=d1, raw=raw, **kw)
    if kind == 0xC0:
        return MidiEvent("program", ch, value=d1, raw=raw, **kw)
    return MidiEvent("other", ch, raw=raw, **kw)


def decode_packed(msg: int, t: float | None = None) -> MidiEvent:
    """winmm packs a short message into one DWORD: status | d1 << 8 | d2 << 16."""
    return decode(msg & 0xFF, (msg >> 8) & 0xFF, (msg >> 16) & 0xFF, t)
