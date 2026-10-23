"""MCP server: a MIDI keyboard as an agent's physical console, plus hardware output.

Run:  python -m midi_agent_console serve [--port MicroLab] [--device COM5]
"""
from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

from .bindings import Binding, BindingManager
from .console import Console
from .hardware import Device

INSTRUCTIONS = """\
The user has a MIDI keyboard on their desk and can answer you by touch.
- Before a risky or irreversible step, call ask_confirm and tell the user in chat
  what you are asking: right half of the keyboard (or the Hold button) = yes, left half = no.
- For a choice among 2-7 options, list them as 1-7 and call ask_choice:
  white keys C D E F G A B answer 1-7 in any octave.
- read_controls gives the current touch-strip / wheel positions as continuous inputs.
- device_send drives the attached board (LED / SERVO / PING / STATE);
  bind_control lets a strip drive a device command live without you in the loop.
"""


def build_server(console: Console, device: Device, port_name: str) -> MCPServer:
    mcp = MCPServer(name="midi-agent-console", instructions=INSTRUCTIONS)
    binder = BindingManager(console, device)

    @mcp.tool()
    def console_status() -> dict:
        """Which MIDI port, controller profile and hardware device are connected."""
        return {"midi_port": port_name, "profile": console.profile.name, "device": device.name,
                "controls": console.profile.controls, "bindings": binder.describe(),
                "binding_errors": binder.errors[-5:]}

    @mcp.tool()
    def read_controls() -> dict:
        """Current positions of the continuous controls (touch strips, wheels, pedal) and held keys.
        CC values are 0-127 (normalized 0..1); pitchbend is -8192..8191 (normalized -1..1).
        value is null until the control has been moved once."""
        return console.snapshot()

    @mcp.tool()
    def recent_events(n: int = 20) -> list[dict]:
        """The last n MIDI events from the keyboard, oldest first."""
        return console.recent(max(1, min(n, 200)))

    @mcp.tool()
    async def ask_confirm(question: str, timeout_s: float = 60) -> dict:
        """Wait for the human to answer yes/no on the keyboard.
        Say the question in chat too. Yes = key at/above middle C or Hold; No = key below middle C.
        Returns {answer: yes|no, via, velocity} or {answer: timeout}."""
        r = await asyncio.to_thread(console.wait_confirm, min(timeout_s, 600))
        return r or {"answer": "timeout", "question": question}

    @mcp.tool()
    async def ask_choice(question: str, options: list[str], timeout_s: float = 60) -> dict:
        """Wait for the human to pick one of 2-7 options with a white key (C=1 ... B=7, any octave).
        List the numbered options in chat too. Returns {choice, option, key, velocity} or {choice: null}."""
        if not 1 <= len(options) <= 7:
            raise ValueError("give 1-7 options")
        r = await asyncio.to_thread(console.wait_choice, len(options), min(timeout_s, 600))
        if not r:
            return {"choice": None, "question": question, "reason": "timeout"}
        return {**r, "option": options[r["choice"] - 1]}

    @mcp.tool()
    async def wait_for_note(timeout_s: float = 30) -> dict:
        """Wait for any key press; returns the note event, or {timeout: true}."""
        r = await asyncio.to_thread(console.wait_any_note, min(timeout_s, 600))
        return r or {"timeout": True}

    @mcp.tool()
    def device_send(command: str) -> str:
        """Send one command to the hardware: PING | STATE | LED i r g b | SERVO i deg."""
        try:
            return device.send(command)
        except ValueError as exc:  # validation: tell the agent what was wrong instead of a bare error
            return f"rejected: {exc}"

    @mcp.tool()
    def bind_control(control: str, template: str, out_min: int = 0, out_max: int = 180) -> str:
        """Make a physical control drive a device command live, e.g.
        control='mod_strip', template='SERVO 0 {v}', 0..180. Replaces any binding on that control."""
        return binder.add(Binding(control, template, out_min, out_max))

    @mcp.tool()
    def unbind_control(control: str) -> bool:
        """Remove a live binding. Returns whether one existed."""
        return binder.remove(control)

    return mcp
