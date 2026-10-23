"""MCP server: a MIDI keyboard as an agent's physical console, plus hardware output.

Run:  python -m midi_agent_console serve [--port MicroLab] [--device COM5]
"""
from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .bindings import Binding, BindingManager
from .console import AnyNote, Choice, Confirm, Console
from .hardware import Device

INSTRUCTIONS = """\
The user has a MIDI keyboard on their desk and answers you by touch. Always say the
question in chat as well, because the keyboard has no screen.
- ask_confirm: left half of the keyboard (below middle C) = no, right half = yes.
  Pass dangerous=true for anything irreversible or physical (moving motors, deleting,
  pushing, spending): then yes needs a 1.5 s long-press, and a tap does nothing.
- ask_choice: list 1-7 numbered options; white keys C D E F G A B answer 1-7 in any octave.
- read_controls: touch strips / wheels / pedal as continuous inputs.
- device_send drives the attached board; bind_control lets a strip drive it live.
- If the human slams 3+ keys on the left half, that is an EMERGENCY STOP: bindings are
  dropped, the board gets ESTOP, and every wait returns answer=estop. Stop what you are
  doing and ask in chat. Only the human can clear it via reset_emergency_stop.
"""

RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def build_server(console: Console, device: Device, port_name: str,
                 turn: asyncio.Lock | None = None) -> MCPServer:
    """`turn` serializes every wait for a key (MCP tools and permission hooks share it),
    so one key press can never answer two questions."""
    mcp = MCPServer(name="midi-agent-console", instructions=INSTRUCTIONS, version=__version__)
    binder = BindingManager(console, device)
    turn = turn or asyncio.Lock()

    async def progress_for(ctx: Context):
        async def report(elapsed: float, total: float) -> None:
            await ctx.report_progress(elapsed, total, "waiting for a key press on the MIDI keyboard")
        return report

    async def answer(ctx: Context, matcher, timeout_s: float, while_estopped: bool = False):
        async with turn:
            return await console.await_answer(matcher, min(max(timeout_s, 1), 600), await progress_for(ctx),
                                              while_estopped=while_estopped)

    @mcp.tool(title="Console status", annotations=RO)
    def console_status() -> dict[str, Any]:
        """Which MIDI port, controller profile and hardware device are connected, and whether
        the emergency stop is latched."""
        return {"midi_port": port_name, "profile": console.profile.name, "device": device.name,
                "controls": console.profile.controls, "bindings": binder.describe(),
                "binding_errors": binder.errors[-5:], "estopped": console.estopped}

    @mcp.tool(title="Read controls", annotations=RO)
    def read_controls() -> dict[str, Any]:
        """Current positions of the continuous controls (touch strips, wheels, pedal) and held keys.
        CC values are 0-127 (normalized 0..1); pitchbend is -8192..8191 (normalized -1..1).
        value is null until the control has been moved once."""
        return console.snapshot()

    @mcp.tool(title="Recent MIDI events", annotations=RO)
    def recent_events(n: int = 20) -> list[dict[str, Any]]:
        """The last n MIDI events from the keyboard, oldest first."""
        return console.recent(max(1, min(n, 200)))

    @mcp.tool(title="Ask yes/no on the keyboard")
    async def ask_confirm(question: str, ctx: Context, dangerous: bool = False, timeout_s: float = 60) -> dict[str, Any]:
        """Wait for the human to answer yes/no on the keyboard. Say the question in chat too.
        No = a key below middle C. Yes = a key at/above middle C (or Hold);
        with dangerous=true, yes needs a 1.5 s long-press. Returns {answer: yes|no|estop|timeout, ...}."""
        r = await answer(ctx, Confirm(console.profile, dangerous), timeout_s)
        return {**(r or {"answer": "timeout"}), "question": question, "dangerous": dangerous}

    @mcp.tool(title="Ask the human to choose on the keyboard")
    async def ask_choice(question: str, options: list[str], ctx: Context, timeout_s: float = 60) -> dict[str, Any]:
        """Wait for the human to pick one of 1-7 options with a white key (C=1 ... B=7, any octave).
        List the numbered options in chat too. Returns {choice, option, key} or {choice: null}."""
        if not 1 <= len(options) <= 7:
            raise ValueError("give 1-7 options")
        r = await answer(ctx, Choice(len(options)), timeout_s)
        if not r or r.get("answer") == "estop":
            return {"choice": None, "question": question, "reason": (r or {}).get("answer", "timeout")}
        return {**r, "option": options[r["choice"] - 1], "question": question}

    @mcp.tool(title="Wait for any key")
    async def wait_for_note(ctx: Context, timeout_s: float = 30) -> dict[str, Any]:
        """Wait for any key press; returns the note event, or {timeout: true}."""
        r = await answer(ctx, AnyNote(), timeout_s)
        return r or {"timeout": True}

    @mcp.tool(title="Send a hardware command")
    def device_send(command: str) -> str:
        """Send one command to the hardware: STATE | PING | STOP | ESTOP | SERVO i deg | LED i r g b |
        DRIVE left right ms (ranges: device_describe). Motion is refused while the emergency
        stop is latched. RESET is reserved for the human (reset_emergency_stop)."""
        op = command.strip().split(" ")[0].upper()
        if op == "RESET":
            return "refused: only the human can reset, via reset_emergency_stop"
        if console.estopped and op not in ("PING", "STATE", "STOP", "ESTOP", "DESCRIBE", "HB"):
            return "refused: emergency stop is latched; ask the human to reset it"
        try:
            return device.send(command)
        except ValueError as exc:  # validation: tell the agent what was wrong instead of a bare error
            return f"rejected: {exc}"

    @mcp.tool(title="Bind a control to hardware", annotations=ToolAnnotations(idempotent_hint=True))
    def bind_control(control: str, template: str, out_min: int | None = None, out_max: int | None = None) -> str:
        """Make a physical control drive a device command live, e.g. control='mod_strip',
        template='SERVO 0 {v}'. The range defaults to that servo's limits from device_describe.
        Replaces any binding on that control."""
        if console.estopped:
            return "refused: emergency stop is latched"
        lo, hi = binder.default_range(template)
        try:
            return binder.add(Binding(control, template, lo if out_min is None else out_min,
                                      hi if out_max is None else out_max))
        except ValueError as exc:
            return f"rejected: {exc}"

    @mcp.tool(title="Describe the hardware", annotations=RO)
    def device_describe() -> dict[str, Any]:
        """The board's own capability document: servos (min/max/safe/max_dps), leds, drive
        limits, heartbeat timeout. Use these ranges; anything outside is refused."""
        return {"device": device.name, "caps": device.caps}

    @mcp.tool(title="Remove a binding", annotations=ToolAnnotations(idempotent_hint=True))
    def unbind_control(control: str) -> bool:
        """Remove a live binding. Returns whether one existed."""
        return binder.remove(control)

    @mcp.tool(title="Emergency stop", annotations=ToolAnnotations(idempotent_hint=True))
    def emergency_stop() -> str:
        """Stop everything now: drop all bindings, send ESTOP to the board, fail pending waits."""
        console.trigger_estop()
        return "emergency stop latched"

    @mcp.tool(title="Reset emergency stop (human must long-press)")
    async def reset_emergency_stop(ctx: Context, timeout_s: float = 60) -> dict[str, Any]:
        """Ask the human to clear the emergency stop: they must long-press a key on the right half.
        Tell them in chat what happened and what you will do next before calling this."""
        if not console.estopped:
            return {"reset": False, "reason": "not stopped"}
        r = await answer(ctx, Confirm(console.profile, dangerous=True), timeout_s, while_estopped=True)
        if r and r.get("answer") == "yes":
            console.clear_estop()
            return {"reset": True, "device": binder.device_reset()}
        return {"reset": False, "answer": (r or {}).get("answer", "timeout")}

    return mcp
