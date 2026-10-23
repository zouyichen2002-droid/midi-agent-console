"""Controller profiles: which physical control means what.

Sources are the manufacturers' manuals (see docs/research-2026-10.md); anything a
manual does not state is listed in `unverified` — confirm it on the device with
`python -m midi_agent_console learn`, then edit here or pass --profile my.json.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Profile:
    name: str
    port_match: str
    # continuous controls exposed to agents: name -> "pitchbend" or "cc:<number>"
    controls: dict[str, str] = field(default_factory=dict)
    # a control that means "yes / approve" when it goes >= 64. Only use a MOMENTARY control
    # here (a sustain pedal), never a latching button: every second press would send 0.
    approve_control: str | None = None
    # notes >= split_note answer "yes", notes below answer "no" (middle of the default range)
    split_note: int = 60
    unverified: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


MICROLAB_MK3 = Profile(
    # Manual 1.0.0: strips send pitch bend and modulation (p.10); Hold works in LATCH mode
    # (p.9); the pedal jack sends sustain CC64 by default, momentary (p.20). No host LEDs.
    name="Arturia MicroLab mk3",
    port_match="MicroLab",
    controls={"pitch_strip": "pitchbend", "mod_strip": "cc:1", "pedal": "cc:64"},
    approve_control="pedal",
    split_note=60,
    unverified=["mod strip is CC1", "Hold sends CC64 or nothing", "default range 48-72 (split 60)"],
)

NANOKEY2 = Profile(
    # Parameter Guide p.7: SUSTAIN button = CC64, MOD = CC1, PITCH +/- = pitch bend.
    name="Korg nanoKEY2", port_match="nanoKEY2",
    controls={"pitch": "pitchbend", "mod": "cc:1", "sustain": "cc:64"},
    approve_control="sustain", split_note=60,
    unverified=["default range (split 60)", "SUSTAIN button momentary"],
)

LAUNCHKEY_MINI_MK4 = Profile(
    # User guide v3 p.15: default range C2-C4 with C3 = middle C (48-72 in C3=60 terms).
    name="Novation Launchkey Mini MK4", port_match="Launchkey Mini",
    controls={"pitch": "pitchbend", "mod": "cc:1", "pedal": "cc:64"},
    approve_control="pedal", split_note=60,
    unverified=["mod CC1", "knob CCs in Custom 1"],
)

MPK_MINI_MK3 = Profile(
    # Quick start: joystick X = pitch bend, Y = modulation. Knob CCs 70-77 from a third party.
    name="Akai MPK mini mk3", port_match="MPK mini",
    controls={"joy_x": "pitchbend", "joy_y": "cc:1", "k1": "cc:70", "k2": "cc:71", "pedal": "cc:64"},
    approve_control="pedal", split_note=60,
    unverified=["knob CCs 70-77", "port name", "default range"],
)

GENERIC = Profile(
    name="Generic MIDI keyboard",
    port_match="",
    controls={"pitchbend": "pitchbend", "mod_wheel": "cc:1", "sustain": "cc:64"},
    approve_control="sustain",
)

BUILTIN = {"microlab_mk3": MICROLAB_MK3, "nanokey2": NANOKEY2, "launchkey_mini_mk4": LAUNCHKEY_MINI_MK4,
           "mpk_mini_mk3": MPK_MINI_MK3, "generic": GENERIC}


def get_profile(name_or_path: str) -> Profile:
    if name_or_path in BUILTIN:
        return BUILTIN[name_or_path]
    return Profile.load(name_or_path)
