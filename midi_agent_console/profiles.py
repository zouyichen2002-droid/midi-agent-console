"""Controller profiles: which physical control means what.

Values for MicroLab mk3 are the usual defaults for this class of keyboard and
still need confirming on the real device with `python -m midi_agent_console learn`.
Edit or add a profile here, or pass a JSON file with --profile.
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
    # a control that means "yes / approve" when it goes >= 64 (e.g. Hold sending CC64)
    approve_control: str | None = None
    # notes >= split_note answer "yes", notes below answer "no"
    split_note: int = 60

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


MICROLAB_MK3 = Profile(
    name="Arturia MicroLab mk3",
    port_match="MicroLab",
    controls={"pitch_strip": "pitchbend", "mod_strip": "cc:1", "hold": "cc:64"},
    approve_control="hold",
    split_note=60,
)

GENERIC = Profile(
    name="Generic MIDI keyboard",
    port_match="",
    controls={"pitchbend": "pitchbend", "mod_wheel": "cc:1", "sustain": "cc:64"},
    approve_control="sustain",
)

BUILTIN = {"microlab_mk3": MICROLAB_MK3, "generic": GENERIC}


def get_profile(name_or_path: str) -> Profile:
    if name_or_path in BUILTIN:
        return BUILTIN[name_or_path]
    return Profile.load(name_or_path)
