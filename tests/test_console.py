import threading
import time

import pytest

from midi_agent_console.bindings import Binding, BindingManager
from midi_agent_console.console import Console
from midi_agent_console.events import decode, decode_packed, note_name
from midi_agent_console.hardware import SimulatedDevice, validate
from midi_agent_console.profiles import MICROLAB_MK3


def later(console, *events, delay=0.05):
    def run():
        time.sleep(delay)
        for e in events:
            console.feed(e)
    threading.Thread(target=run, daemon=True).start()


def test_decode_basic():
    assert decode(0x90, 60, 100).kind == "note_on"
    assert decode(0x90, 60, 0).kind == "note_off"  # running-status style note off
    assert decode(0xB3, 1, 64).to_dict()["channel"] == 4
    assert decode(0xE0, 0, 64).value == 0  # centre
    assert decode(0xE0, 0x7F, 0x7F).value == 8191
    assert decode(0xE0, 0, 0).value == -8192
    assert decode_packed(0x90 | 60 << 8 | 100 << 16).note == 60
    assert note_name(60) == "C4" and note_name(61) == "C#4"


def test_snapshot_tracks_strips_and_held_notes():
    c = Console(MICROLAB_MK3)
    assert c.snapshot()["controls"]["mod_strip"]["value"] is None
    c.feed(decode(0xB0, 1, 127))
    c.feed(decode(0xE0, 0, 0))
    c.feed(decode(0x90, 64, 90))
    s = c.snapshot()
    assert s["controls"]["mod_strip"]["normalized"] == 1.0
    assert s["controls"]["pitch_strip"]["value"] == -8192
    assert s["held_notes"] == [{"note": 64, "name": "E4", "velocity": 90}]
    c.feed(decode(0x80, 64, 0))
    assert c.snapshot()["held_notes"] == []


def test_choice_uses_white_keys_any_octave_and_ignores_past_events():
    c = Console(MICROLAB_MK3)
    c.feed(decode(0x90, 48, 80))  # C pressed BEFORE the question: must not count
    later(c, decode(0x90, 49, 80), decode(0x90, 76, 70))  # C# ignored, then E5 -> option 3
    r = c.wait_choice(3, timeout=2)
    assert r == {"choice": 3, "key": "E5", "velocity": 70}


def test_choice_ignores_keys_beyond_option_count():
    c = Console(MICROLAB_MK3)
    later(c, decode(0x90, 67, 80), decode(0x90, 62, 80))  # G (=5) out of range, then D (=2)
    assert c.wait_choice(2, timeout=2)["choice"] == 2


def test_confirm_split_and_hold():
    c = Console(MICROLAB_MK3)
    later(c, decode(0x90, 55, 80))
    assert c.wait_confirm(2)["answer"] == "no"
    later(c, decode(0x90, 72, 80))
    assert c.wait_confirm(2)["answer"] == "yes"
    later(c, decode(0xB0, 64, 127))
    assert c.wait_confirm(2) == {"answer": "yes", "via": "hold", "velocity": None}


def test_wait_times_out():
    t0 = time.monotonic()
    assert Console(MICROLAB_MK3).wait_any_note(0.2) is None
    assert time.monotonic() - t0 >= 0.19


def test_validate_rejects_bad_commands():
    assert validate("servo 0 90") == "SERVO 0 90"
    for bad in ["", "RM -rf", "SERVO 0 181", "SERVO 0", "LED 0 1 2 300", "SERVO 0 abc"]:
        with pytest.raises(ValueError):
            validate(bad)


def test_binding_drives_simulated_servo():
    c, dev = Console(MICROLAB_MK3), SimulatedDevice()
    mgr = BindingManager(c, dev)
    mgr.add(Binding("mod_strip", "SERVO 0 {v}", 0, 180, min_interval=0))
    c.feed(decode(0xB0, 1, 127))
    assert dev.servos[0] == 180
    c.feed(decode(0xB0, 1, 0))
    assert dev.servos[0] == 0
    mgr.add(Binding("pitch_strip", "LED 0 {v} 0 0", 0, 255, min_interval=0))
    c.feed(decode(0xE0, 0, 64))  # centre -> about half
    assert dev.leds[0][0] in (127, 128)
    with pytest.raises(ValueError):
        mgr.add(Binding("mod_strip", "SERVO 0 {v}", 0, 999))
