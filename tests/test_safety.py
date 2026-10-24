"""Regression tests for the v0.4 safety review (each one reproduced a real failure)."""
import asyncio
import time

import pytest

from midi_agent_console.bindings import Binding, BindingManager
from midi_agent_console.console import Console, Confirm
from midi_agent_console.events import decode
from midi_agent_console.hardware import SimulatedDevice
from midi_agent_console.hooks import HookBridge
from midi_agent_console.profiles import MICROLAB_MK3


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SlowDevice(SimulatedDevice):
    """Commands reach the board but replies are lost: each SERVO waits out a 0.5 s timeout."""
    def __init__(self):
        super().__init__()
        self.sent_at: dict[str, float] = {}

    def send(self, command):
        if command.startswith("SERVO"):
            time.sleep(0.5)
        r = super().send(command)
        self.sent_at.setdefault(command.split()[0], time.monotonic())
        return r


def test_estop_is_not_queued_behind_slow_device_io():
    c, dev = Console(MICROLAB_MK3, arm_delay=0), SlowDevice()
    mgr = BindingManager(c, dev)
    mgr.add(Binding("mod_strip", "SERVO 0 {v}", 10, 170, min_interval=0))
    for v in range(0, 127, 3):  # a second of strip movement: ~40 events
        c.feed(decode(0xB0, 1, v))
    t0 = time.monotonic()
    for n in (48, 50, 52):  # slam the e-stop chord
        c.feed(decode(0x90, n, 110))
    assert time.monotonic() - t0 < 0.05  # the MIDI thread did not block on the device
    deadline = time.monotonic() + 2
    while "ESTOP" not in dev.sent_at and time.monotonic() < deadline:
        time.sleep(0.005)
    # at most one in-flight SERVO (0.5 s) can be ahead of it; before the fix this was ~24 s
    assert dev.sent_at["ESTOP"] - t0 < 0.7
    mgr.close()


class FlakyEstop(SimulatedDevice):
    def __init__(self, fail_first=2):
        super().__init__()
        self.fail_left = fail_first
        self.estop_calls = 0

    def send(self, command):
        if command == "ESTOP":
            self.estop_calls += 1
            if self.fail_left:
                self.fail_left -= 1
                return "ERR RATE"
        return super().send(command)


def until(pred, timeout=2.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_lost_estop_is_retried_and_repeated_requests_resend():
    c, dev = Console(MICROLAB_MK3, arm_delay=0), FlakyEstop(fail_first=2)
    mgr = BindingManager(c, dev)
    c.trigger_estop()
    assert until(lambda: mgr.board_estop_ack is True) and dev.estopped and dev.estop_calls == 3
    dev.estopped = False  # pretend the board rebooted / lost it
    c.trigger_estop()  # already latched on this side: must still reach the board
    assert until(lambda: dev.estopped) and dev.estop_calls == 4
    mgr.close()


def test_slam_while_latched_cancels_a_pending_reset():
    c = Console(MICROLAB_MK3, arm_delay=0)
    c.trigger_estop()

    async def scenario():
        async def slam():
            await asyncio.sleep(0.2)
            c.feed(decode(0x90, 72, 90))  # starts a reset long-press...
            await asyncio.sleep(0.3)
            for n in (48, 50, 52):  # ...then panics
                c.feed(decode(0x90, n, 110))
        asyncio.get_running_loop().create_task(slam())
        return await c.await_answer(Confirm(c.profile, dangerous=True), 3, while_estopped=True)

    assert asyncio.run(scenario()) == {"answer": "estop"}


@pytest.mark.anyio
async def test_agent_can_still_ask_for_reset_while_latched():
    c = Console(MICROLAB_MK3, arm_delay=0)
    b = HookBridge(c, timeout=1)
    c.trigger_estop()
    pre = lambda tool: {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}
    assert (await b.handle(pre("Bash")))["continue"] is False
    assert await b.handle(pre("mcp__midi-console__reset_emergency_stop")) is None
    assert await b.handle(pre("mcp__midi-console__console_status")) is None


def test_out_of_order_virtual_tap_cannot_become_a_hold():
    from midi_agent_console.virtual import VirtualKeys

    c = Console(MICROLAB_MK3, arm_delay=0)
    vk = VirtualKeys(c, 47811, key="k")
    # the page sends press (seq 1) then release (seq 2); the network delivers them swapped
    assert vk.accept({"seq": 2, "status": 0x80, "d1": 72, "d2": 0}) == 204
    assert vk.accept({"seq": 1, "status": 0x90, "d1": 72, "d2": 100}) == 409  # stale press dropped
    assert c.held_notes() == set()
    vk.close()


def test_virtual_key_held_by_a_dead_page_is_released():
    from midi_agent_console.teleop import Teleop
    from midi_agent_console.virtual import VirtualKeys

    c, dev = Console(MICROLAB_MK3, arm_delay=0), SimulatedDevice()
    BindingManager(c, dev)
    t = Teleop(c, dev)
    t.start()
    vk = VirtualKeys(c, 47811, key="k")
    vk.accept({"seq": 1, "status": 0x90, "d1": 72, "d2": 100})  # page dies with the key down
    assert until(lambda: any(s.startswith("DRIVE") for s in t.sent))
    assert until(lambda: t.sent[-1].startswith("STOP"), timeout=2.5)  # watchdog released it -> STOP
    n = len(t.sent)
    time.sleep(0.4)
    assert len(t.sent) == n
    t.stop()
    vk.close()

def test_page_reload_takes_over_and_releases_the_old_pages_keys():
    from midi_agent_console.virtual import VirtualKeys

    c = Console(MICROLAB_MK3, arm_delay=0)
    vk = VirtualKeys(c, 47811, key="k")
    assert vk.accept({"sid": "page-1", "seq": 1, "status": 0x90, "d1": 72, "d2": 100}) == 204
    assert vk.accept({"sid": "page-1", "seq": 99, "alive": True}) == 204
    assert c.held_notes() == {72}
    # reload: a new session starts again at seq 1 and must be accepted
    assert vk.accept({"sid": "page-2", "seq": 1, "status": 0x90, "d1": 64, "d2": 100}) == 204
    assert c.held_notes() == {64}  # the old page's C5 was released on takeover
    assert vk.accept({"sid": "page-1", "seq": 100, "status": 0x80, "d1": 72, "d2": 0}) == 409  # old page is void
    vk.close()