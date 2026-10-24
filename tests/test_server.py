"""The MCP layer, through the SDK's in-memory client (no stdio, no MIDI hardware)."""
import asyncio
import threading
import time

import anyio
import pytest
from mcp import Client

from midi_agent_console.console import Console
from midi_agent_console.events import decode
from midi_agent_console.hardware import SimulatedDevice
from midi_agent_console.profiles import MICROLAB_MK3
from midi_agent_console.server import build_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def rig():
    c, dev = Console(MICROLAB_MK3, arm_delay=0), SimulatedDevice()
    return c, dev, build_server(c, dev, "test-port")


def press(c, *msgs, delay=0.2):
    def run():
        time.sleep(delay)
        for m in msgs:
            if isinstance(m, (int, float)):
                time.sleep(m)
            else:
                c.feed(decode(*m))
    threading.Thread(target=run, daemon=True).start()


@pytest.mark.anyio
async def test_tools_listed_with_annotations(rig):
    async with Client(rig[2], raise_exceptions=True) as cl:
        tools = {t.name: t for t in (await cl.list_tools()).tools}
    assert {"ask_confirm", "ask_choice", "emergency_stop", "reset_emergency_stop", "bind_control"} <= set(tools)
    assert tools["read_controls"].annotations.read_only_hint is True
    assert "ctx" not in tools["ask_confirm"].input_schema["properties"]


@pytest.mark.anyio
async def test_choice_and_progress(rig):
    c, _, mcp = rig
    seen = []

    async def on_progress(p, total, msg):
        seen.append((p, total))

    press(c, (0x90, 67, 100), delay=1.3)  # G -> option 5, after at least one progress tick
    async with Client(mcp, raise_exceptions=True) as cl:
        r = await cl.call_tool("ask_choice", {"question": "pick", "options": list("abcde"), "timeout_s": 5},
                               progress_callback=on_progress)
    assert r.structured_content["choice"] == 5 and r.structured_content["option"] == "e"
    assert seen and seen[0][1] == 5


@pytest.mark.anyio
async def test_cancelled_wait_cleans_up(rig):
    c, _, mcp = rig
    baseline = (len(c._listeners), len(c._estop_listeners))  # the binding manager listens for good
    async with Client(mcp, raise_exceptions=True) as cl:
        with pytest.raises(Exception):
            await cl.call_tool("wait_for_note", {"timeout_s": 30}, read_timeout_seconds=0.3)
        await asyncio.sleep(0.2)
    assert (len(c._listeners), len(c._estop_listeners)) == baseline  # no zombie waiter eats the next key


@pytest.mark.anyio
async def test_estop_blocks_device_until_human_long_press(rig):
    c, dev, mcp = rig
    async with Client(mcp, raise_exceptions=True) as cl:
        await cl.call_tool("bind_control", {"control": "mod_strip", "template": "SERVO 0 {v}"})
        c.feed(decode(0x90, 48, 100)); c.feed(decode(0x90, 50, 100)); c.feed(decode(0x90, 52, 100))
        st = (await cl.call_tool("console_status", {})).structured_content
        assert st["estopped"] and st["bindings"] == []
        for _ in range(50):  # the ESTOP reaches the board on its own sender thread
            if dev.estopped:
                break
            await anyio.sleep(0.02)
        assert dev.estopped
        r = await cl.call_tool("device_send", {"command": "SERVO 0 90"})
        assert "refused" in r.structured_content["result"]
        press(c, (0x80, 48, 0), (0x80, 50, 0), (0x80, 52, 0), (0x90, 72, 90), delay=0.2)  # then hold C5
        r = (await cl.call_tool("reset_emergency_stop", {"timeout_s": 5})).structured_content
        assert r["reset"] is True and not c.estopped and not dev.estopped


@pytest.mark.anyio
async def test_bad_device_command_is_explained(rig):
    async with Client(rig[2], raise_exceptions=True) as cl:
        r = await cl.call_tool("device_send", {"command": "REBOOT"})
    assert r.structured_content["result"].startswith("rejected: unknown command 'REBOOT'")


@pytest.mark.anyio
async def test_dialog_fallback_without_keyboard():
    from mcp.types import ElicitResult

    c, dev = Console(MICROLAB_MK3, arm_delay=0), SimulatedDevice()
    mcp = build_server(c, dev, None)  # no MIDI port
    asked = []

    async def on_elicit(ctx, params):
        asked.append(params.message)
        if "approve" in params.requested_schema["properties"]:
            return ElicitResult(action="accept", content={"approve": True})
        return ElicitResult(action="accept", content={"choice": 2})

    async with Client(mcp, raise_exceptions=True, elicitation_callback=on_elicit) as cl:
        tools = {t.name: t for t in (await cl.list_tools()).tools}
        assert "reply" not in tools["ask_confirm"].input_schema["properties"]
        assert "wait_for_note" not in tools
        r = (await cl.call_tool("ask_confirm", {"question": "move?", "dangerous": True})).structured_content
        assert r["answer"] == "yes" and r["via"] == "dialog"
        r = (await cl.call_tool("ask_choice", {"question": "which", "options": ["a", "b", "c"]})).structured_content
        assert r["choice"] == 2 and r["option"] == "b"
        await cl.call_tool("emergency_stop", {})
        assert (await cl.call_tool("reset_emergency_stop", {})).structured_content["reset"] is True
    assert asked[0].startswith("[DANGEROUS] move?") and "2. b" in asked[1]