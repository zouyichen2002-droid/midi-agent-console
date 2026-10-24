import json

from midi_agent_console.events import decode
from midi_agent_console.profiles import Profile
from midi_agent_console.wizard import run_wizard, save_profile


def scripted(*steps):
    """One list of (status, d1, d2) per wizard step, returned in order."""
    queue = [[decode(*m) for m in step] for step in steps]
    prompts = []

    def collect(prompt, seconds):
        prompts.append(prompt)
        return queue.pop(0) if queue else []

    return collect, prompts


def test_wizard_microlab_like_device(tmp_path):
    collect, prompts = scripted(
        [(0x90, 48, 80), (0x80, 48, 0)],                                    # lowest key C3
        [(0x90, 72, 80), (0x80, 72, 0)],                                    # highest key C5
        [(0xE0, 0, 64), (0xE0, 0, 80), (0xE0, 0, 100), (0xE0, 0, 64)],      # pitch strip
        [(0xB0, 1, 10), (0xB0, 1, 60), (0xB0, 1, 120), (0xB0, 1, 0)],       # mod strip CC1
        [(0xB0, 64, 127), (0xB0, 64, 0)],                                   # Hold pressed twice: latches
        [(0xB0, 64, 127), (0xB0, 64, 0), (0xB0, 64, 127), (0xB0, 64, 0)],   # pedal twice: momentary
    )
    r = run_wizard(collect, "MicroLab mk3", "MicroLab")
    p = r["profile"]
    assert p["split_note"] == 60 and r["report"]["steps"]["range_names"] == ["C3", "C5", "C4"]
    assert p["controls"]["pitch_strip"] == "pitchbend" and p["controls"]["strip_2"] == "cc:1"
    assert r["report"]["steps"]["hold"]["kind"] == "latching"
    assert p["approve_control"] == "pedal" and p["controls"]["pedal"] == "cc:64"
    assert any("latching" in u for u in p["unverified"])
    assert len(prompts) == 6
    path = save_profile(r, tmp_path / "microlab.json")
    loaded = Profile.load(path)  # round-trips into the real Profile type
    assert loaded.approve_control == "pedal" and loaded.split_note == 60
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "MicroLab mk3"


def test_wizard_with_nothing_but_keys_falls_back_safely():
    collect, _ = scripted([(0x90, 53, 80)], [(0x90, 84, 80)], [], [], [], [])
    p = run_wizard(collect, "x", "x")["profile"]
    assert p["approve_control"] is None and p["controls"] == {}
    assert p["split_note"] == 68  # midpoint of F3..C6, no C close enough
    assert any("no momentary approve" in u for u in p["unverified"])
