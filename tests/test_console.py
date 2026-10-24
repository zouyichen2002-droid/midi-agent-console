import threading
import time

import pytest

from midi_agent_console.bindings import Binding, BindingManager
from midi_agent_console.console import Console
from midi_agent_console.events import decode, decode_packed, note_name
from midi_agent_console.hardware import SimulatedDevice, validate
from midi_agent_console.profiles import MICROLAB_MK3

ON, OFF, CC, PB = 0x90, 0x80, 0xB0, 0xE0


def until(pred, timeout=1.0):
    """Wait for something another thread does (binding I/O, ESTOP, teleop) to show up."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def console(**kw):
    kw.setdefault("arm_delay", 0)
    return Console(MICROLAB_MK3, **kw)


def later(c, *msgs, delay=0.05, gap=0.0):
    """Feed (status, d1, d2) messages from another thread; events are stamped when sent."""
    def run():
        time.sleep(delay)
        for m in msgs:
            if isinstance(m, (int, float)):
                time.sleep(m)  # a number in the list = pause
                continue
            c.feed(decode(*m))
            time.sleep(gap)
    threading.Thread(target=run, daemon=True).start()


def test_decode_basic():
    assert decode(ON, 60, 100).kind == "note_on"
    assert decode(ON, 60, 0).kind == "note_off"  # running-status style note off
    assert decode(0xB3, 1, 64).to_dict()["channel"] == 4
    assert decode(PB, 0, 64).value == 0  # centre
    assert decode(PB, 0x7F, 0x7F).value == 8191
    assert decode(PB, 0, 0).value == -8192
    assert decode_packed(0x90 | 60 << 8 | 100 << 16).note == 60
    assert note_name(60) == "C4" and note_name(61) == "C#4"


def test_snapshot_tracks_strips_and_held_notes():
    c = console()
    assert c.snapshot()["controls"]["mod_strip"]["value"] is None
    c.feed(decode(CC, 1, 127))
    c.feed(decode(PB, 0, 0))
    c.feed(decode(ON, 64, 90))
    s = c.snapshot()
    assert s["controls"]["mod_strip"]["normalized"] == 1.0
    assert s["controls"]["pitch_strip"]["value"] == -8192
    assert "hold" not in s["controls"]  # Hold latches on the MicroLab: not an approve control
    assert s["held_notes"] == [{"note": 64, "name": "E4", "velocity": 90}]
    c.feed(decode(OFF, 64, 0))
    assert c.snapshot()["held_notes"] == []


def test_choice_white_keys_any_octave():
    c = console()
    later(c, (ON, 49, 80), (ON, 76, 70))  # C# ignored, then E5 -> option 3
    assert c.wait_choice(3, timeout=2) == {"choice": 3, "key": "E5", "velocity": 70}


def test_choice_ignores_keys_beyond_option_count():
    c = console()
    later(c, (ON, 67, 80), (ON, 62, 80))  # G (=5) out of range, then D (=2)
    assert c.wait_choice(2, timeout=2)["choice"] == 2


def test_events_before_the_question_never_count():
    c = console()
    c.feed(decode(ON, 48, 80))  # C held down BEFORE the question
    later(c, (OFF, 48, 0), (ON, 50, 80))  # releasing it is not an answer; D is
    assert c.wait_choice(3, timeout=2)["choice"] == 2


def test_arm_delay_swallows_early_presses():
    c = console(arm_delay=0.3)
    later(c, (ON, 72, 80), (OFF, 72, 0), 0.4, (ON, 55, 80), delay=0.05)  # early yes ignored, later no counts
    assert c.wait_confirm(2)["answer"] == "no"


def test_confirm_split_and_hold():
    c = console()
    later(c, (ON, 55, 80))
    assert c.wait_confirm(2)["answer"] == "no"
    later(c, (ON, 72, 80))
    assert c.wait_confirm(2)["answer"] == "yes"
    later(c, (CC, 64, 127))
    assert c.wait_confirm(2) == {"answer": "yes", "via": "pedal"}


def test_dangerous_needs_long_press_and_tap_does_nothing():
    c = console()
    later(c, (ON, 72, 80), 0.2, (OFF, 72, 0), 0.1, (ON, 74, 80))  # tap C5, then hold D5
    t0 = time.monotonic()
    r = c.wait_confirm(5, dangerous=True)
    assert r["answer"] == "yes" and r["via"] == "long-press D5"
    assert time.monotonic() - t0 >= 1.5
    later(c, (CC, 64, 127), (ON, 50, 80))  # Hold does not approve dangerous; a left key says no
    assert c.wait_confirm(2, dangerous=True)["answer"] == "no"


def test_estop_chord_ends_waits_latches_and_stops_bindings():
    c, dev = console(), SimulatedDevice()
    mgr = BindingManager(c, dev)
    mgr.add(Binding("mod_strip", "SERVO 0 {v}", 10, 170, min_interval=0))
    later(c, (ON, 48, 100), (ON, 50, 100), (ON, 52, 100))  # three keys on the left half
    assert c.wait_choice(7, timeout=2) == {"answer": "estop"}
    assert c.estopped and mgr.bindings == {} and until(lambda: dev.estopped)
    assert until(lambda: mgr.board_estop_ack is True)
    assert c.wait_confirm(1) == {"answer": "estop", "reason": "emergency stop is latched; reset it first"}
    assert dev.send("SERVO 0 90") == "ERR ESTOP"
    c.clear_estop()
    assert mgr.device_reset() == "OK" and dev.send("SERVO 0 90") == "OK"


def test_wait_times_out():
    t0 = time.monotonic()
    assert console().wait_any_note(0.2) is None
    assert time.monotonic() - t0 >= 0.19


def test_validate_rejects_bad_commands():
    assert validate("servo 0 90") == "SERVO 0 90"
    for bad in ["", "RM -rf", "SERVO 0 171", "SERVO 0 5", "SERVO 9 90", "SERVO 0", "LED 0 1 2 300",
                "SERVO 0 abc", "SERVO 0 +90", "SERVO 0 9_0", "SERVO 0 ９０", "DRIVE 0 0 0", "DRIVE 300 0 10",
                "DRIVE 0 0 5000", "STATE 1"]:
        with pytest.raises(ValueError):
            validate(bad)


def test_binding_drives_simulated_servo():
    c, dev = console(), SimulatedDevice()
    mgr = BindingManager(c, dev)
    mgr.add(Binding("mod_strip", "SERVO 0 {v}", 10, 170, min_interval=0))
    c.feed(decode(CC, 1, 127))
    assert until(lambda: dev.servos[0] == 170)
    c.feed(decode(CC, 1, 0))
    assert until(lambda: dev.servos[0] == 10)
    mgr.add(Binding("pitch_strip", "LED 0 {v} 0 0", 0, 255, min_interval=0))
    c.feed(decode(PB, 0, 64))  # centre -> about half
    assert until(lambda: dev.leds[0][0] in (127, 128))
    with pytest.raises(ValueError):
        mgr.add(Binding("mod_strip", "SERVO 0 {v}", 0, 999))
    with pytest.raises(ValueError):
        mgr.add(Binding("mod_strip", "DRIVE {v} {v} 2000", 0, 100))  # motors only via teleop
    c.busy = "teleop"
    c.feed(decode(CC, 1, 127))
    time.sleep(0.1)
    assert dev.servos[0] == 10  # bindings pause while teleop has the keyboard
    c.busy = None
    assert mgr.default_range("SERVO 1 {v}") == (10, 170)


def test_simulator_drive_stops_by_itself():
    dev = SimulatedDevice()
    assert dev.send("DRIVE 150 150 100") == "OK"
    assert "drv=150,150," in dev.send("STATE")
    time.sleep(0.15)
    assert "drv=0,0,0" in dev.send("STATE")
    assert dev.send("DESCRIBE").startswith("OK {")


def test_dangerous_slam_is_panic_and_long_press_ticks():
    c = console()
    later(c, (ON, 72, 127))  # slammed on the yes side
    assert c.wait_confirm(2, dangerous=True)["answer"] == "no"

    class Fb:
        def __init__(self): self.played = []
        def play(self, m): self.played.append(m)

    fb = Fb()
    c = console(feedback=fb)
    later(c, (ON, 74, 90))
    assert c.wait_confirm(4, dangerous=True)["answer"] == "yes"
    assert fb.played[0].startswith("danger") and fb.played[-1] == "yes"  # rotating danger cue
    assert fb.played.count("hold") >= 3 and "yes" not in fb.played[:-1]  # yes only on commit

def test_teleop_drives_ramps_steers_and_stops_on_release():
    from midi_agent_console.teleop import Teleop

    c, dev = console(), SimulatedDevice()
    BindingManager(c, dev)
    t = Teleop(c, dev, period=0.02)
    t.start()
    assert c.wait_confirm(1) == {"answer": "busy", "reason": "keyboard is in teleop mode"}
    c.feed(decode(ON, 72, 127))  # C5 white = forward, full velocity
    time.sleep(0.2)
    drives = [s.split(" -> ")[0] for s in t.sent if s.startswith("DRIVE")]
    firsts = [int(d.split()[1]) for d in drives]
    assert firsts[0] == 40 and max(firsts) == 180 and firsts == sorted(firsts)  # ramped up, capped
    c.feed(decode(PB, 0x7F, 0x7F))  # steer hard right
    time.sleep(0.15)
    left, right = map(int, t.sent[-1].split()[1:3])
    assert left > right
    c.feed(decode(OFF, 72, 0))
    assert until(lambda: t.sent[-1].startswith("STOP"))
    n = len(t.sent)
    time.sleep(0.1)
    assert len(t.sent) == n  # nothing at all is sent after the release STOP
    c.feed(decode(PB, 0, 64))
    c.feed(decode(ON, 73, 90))  # C#5 black = reverse
    time.sleep(0.15)
    assert int(t.sent[-1].split()[1]) < 0
    c.feed(decode(ON, 48, 100)); c.feed(decode(ON, 50, 100)); c.feed(decode(ON, 52, 100))  # e-stop chord
    time.sleep(0.15)
    assert not t.active and dev.estopped and c.busy is None
    t.stop()


def test_teleop_refuses_device_without_motors():
    from midi_agent_console.hardware import DEFAULT_CAPS
    from midi_agent_console.teleop import Teleop

    caps = {**DEFAULT_CAPS, "drive": None}
    with pytest.raises(ValueError):
        Teleop(console(), SimulatedDevice(caps))

def test_session_cues_are_stable_and_spread():
    from midi_agent_console.feedback import MOTIFS, session_cue

    assert session_cue("abc") == session_cue("abc")
    cues = {session_cue(f"session-{i}") for i in range(40)}
    assert len(cues) >= 4 and all(c in MOTIFS for c in cues)

def test_tone_rendering_and_mixer():
    import array

    from midi_agent_console.feedback import MOTIFS
    from midi_agent_console.tones import SR, StreamBackend, render, to_wav

    clip = render(MOTIFS["yes"])
    assert len(clip) == int(0.18 * SR) and clip[0] == 0.0 and max(map(abs, clip)) <= 0.2  # faded, quiet
    assert to_wav(clip)[:4] == b"RIFF"
    b = StreamBackend({"a": array.array("f", [0.5] * 10), "b": array.array("f", [0.75] * 10)}, open_stream=False)
    b.play("a"); b.play("b"); b.play("missing")
    out = bytearray(4 * 2 * 8)
    b._cb(out, 8, None, None)  # 8 stereo frames = 16 samples
    mixed = array.array("f", bytes(out))
    assert mixed[0] == 1.0 and mixed[15] == 1.0  # 0.5 + 0.75 clipped to 1.0
    b._cb(out, 8, None, None)
    tail = array.array("f", bytes(out))
    assert tail[3] == 1.0 and tail[4] == 0.0  # clips ran out (20 samples each); then silence
    assert b._voices == []

def test_sim_kinematics_straight_and_spin():
    import math

    dev = SimulatedDevice()
    dev.send("DRIVE 180 180 500")
    time.sleep(0.6)
    p = dev.pose()
    assert abs(p["x"] - 0.15) < 0.02 and abs(p["y"]) < 0.005 and p["drive"] == [0, 0]  # 0.3 m/s * 0.5 s
    dev.send("DRIVE -90 90 300")  # spin left in place
    time.sleep(0.35)
    p2 = dev.pose()
    expect = math.degrees((2 * 0.15 / 0.12) * 0.3)  # w = (vr - vl) / track
    assert abs(p2["deg"] - expect) < 3 and abs(p2["x"] - p["x"]) < 0.01
    assert "pose=" in dev.send("STATE") and len(p2["trail"]) > 5


def test_sim_routes_serve_state_with_host_check():
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from midi_agent_console.simview import make_routes

    dev = SimulatedDevice()
    page, state = make_routes(dev, 47811)
    app = Starlette(routes=[Route("/sim", page), Route("/sim/state", state)])
    with TestClient(app, base_url="http://127.0.0.1:47811") as cl:
        assert "<canvas" in cl.get("/sim", headers={"host": "127.0.0.1:47811"}).text
        assert cl.get("/sim/state", headers={"host": "127.0.0.1:47811"}).json()["x"] == 0
        assert cl.get("/sim/state", headers={"host": "evil.example:47811"}).status_code == 403