"""MCP server: a MIDI keyboard as an agent's physical console, plus hardware output.

Run:  python -m midi_agent_console serve [--port MicroLab] [--device COM5]
With no keyboard attached, the asking tools fall back to MCP elicitation dialogs.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Annotated, Any

from mcp.server.mcpserver import (AcceptedElicitation, Context, Elicit, ElicitationResult, MCPServer,
                                  Resolve)
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .audit import AuditLog
from .bindings import Binding, BindingManager
from .console import AnyNote, Choice, Confirm, Console
from .hardware import Device
from .teleop import Teleop

if TYPE_CHECKING:
    from .iot import Registry

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

DIALOG_NOTE = """
NO MIDI KEYBOARD IS CONNECTED right now: ask_confirm / ask_choice / reset_emergency_stop
show a dialog in the MCP client instead (elicitation). Hardware tools work as usual."""

RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)


# ------------------------------------------------------------------ dialog fallback (no keyboard)
class DialogConfirm(BaseModel):
    approve: bool = Field(description="Approve?")


class DialogChoice(BaseModel):
    choice: int = Field(description="Number of the option you pick")


async def _confirm_dialog(question: str, dangerous: bool = False) -> Elicit[DialogConfirm]:
    return Elicit(("[DANGEROUS] " if dangerous else "") + question, DialogConfirm)


async def _choice_dialog(question: str, options: list[str]) -> Elicit[DialogChoice]:
    listing = "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))
    return Elicit(f"{question}\n{listing}", DialogChoice)


async def _reset_dialog() -> Elicit[DialogConfirm]:
    return Elicit("Clear the emergency stop and let hardware move again?", DialogConfirm)


def _dialog_confirm_result(r) -> dict[str, Any]:
    if isinstance(r, AcceptedElicitation):
        return {"answer": "yes" if r.data.approve else "no", "via": "dialog"}
    return {"answer": "no", "via": "dialog " + type(r).__name__.replace("Elicitation", "").lower()}


# ------------------------------------------------------------------ server
def build_server(console: Console, device: Device, port_name: str | None,
                 turn: asyncio.Lock | None = None, max_wait: float = 600,
                 audit: AuditLog | None = None, iot: "Registry | None" = None) -> MCPServer:
    """`turn` serializes every wait for a key (MCP tools and permission hooks share it),
    so one key press can never answer two questions. port_name=None means no keyboard.
    max_wait caps every wait (voice assistants such as xiaozhi time tool calls out at ~30 s).
    With `audit`, every ask_confirm answer is appended to the hash chain and the result
    carries its hash, so a caller (the brain's run journal) can point at the decision.
    With `iot` (an iot.Registry), smart-home entities found over MQTT get iot_* tools."""
    keyboard = port_name is not None
    mcp = MCPServer(name="midi-agent-console", version=__version__,
                    instructions=INSTRUCTIONS + ("" if keyboard else DIALOG_NOTE))
    binder = BindingManager(console, device)
    turn = turn or asyncio.Lock()

    async def answer(ctx: Context, matcher, timeout_s: float, while_estopped: bool = False):
        async def report(elapsed: float, total: float) -> None:
            await ctx.report_progress(elapsed, total, "waiting for a key press on the MIDI keyboard")

        async with turn:
            return await console.await_answer(matcher, min(max(timeout_s, 1), max_wait), report,
                                              while_estopped=while_estopped)

    def decided(result: dict[str, Any], ref: str | None) -> dict[str, Any]:
        if audit is not None:
            e = audit.record(agent="mcp", tool="ask_confirm", question=result["question"][:500],
                             dangerous=result["dangerous"], decision=result.get("answer"),
                             via=result.get("via", "keyboard"), ref=ref)
            result["audit_hash"] = e["hash"]
        return result

    def do_reset() -> dict[str, Any]:
        console.clear_estop()
        return {"reset": True, "device": binder.device_reset()}

    @mcp.tool(title="Console status", annotations=RO)
    def console_status() -> dict[str, Any]:
        """Which MIDI port, controller profile and hardware device are connected, and whether
        the emergency stop is latched."""
        return {"midi_port": port_name, "keyboard": keyboard, "profile": console.profile.name,
                "device": device.name, "controls": console.profile.controls,
                "bindings": binder.describe(), "binding_errors": binder.errors[-5:],
                "estopped": console.estopped, "board_estop_ack": binder.board_estop_ack,
                "waiting": console.waiting}

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

    if keyboard:
        @mcp.tool(title="Ask yes/no on the keyboard")
        async def ask_confirm(question: str, ctx: Context, dangerous: bool = False,
                              timeout_s: float = 60, ref: str | None = None) -> dict[str, Any]:
            """Wait for the human to answer yes/no on the keyboard. Say the question in chat too.
            No = a key below middle C. Yes = a key at/above middle C (or Hold);
            with dangerous=true, yes needs a 1.5 s long-press. Returns {answer: yes|no|estop|timeout, ...}.
            `ref` (optional) is stored with the decision in the audit log, e.g. a run and step id."""
            r = await answer(ctx, Confirm(console.profile, dangerous), timeout_s)
            return decided({**(r or {"answer": "timeout"}), "question": question, "dangerous": dangerous}, ref)

        @mcp.tool(title="Ask the human to choose on the keyboard")
        async def ask_choice(question: str, options: list[str], ctx: Context,
                             timeout_s: float = 60) -> dict[str, Any]:
            """Wait for the human to pick one of 1-7 options with a white key (C=1 ... B=7, any octave).
            List the numbered options in chat too. Returns {choice, option, key} or {choice: null}."""
            if not 1 <= len(options) <= 7:
                raise ValueError("give 1-7 options")
            r = await answer(ctx, Choice(len(options)), timeout_s)
            if not r or r.get("answer") in ("estop", "busy"):
                return {"choice": None, "question": question, "reason": (r or {}).get("answer", "timeout")}
            return {**r, "option": options[r["choice"] - 1], "question": question}

        @mcp.tool(title="Wait for any key")
        async def wait_for_note(ctx: Context, timeout_s: float = 30) -> dict[str, Any]:
            """Wait for any key press; returns the note event, or {timeout: true}."""
            r = await answer(ctx, AnyNote(), timeout_s)
            return r or {"timeout": True}

        @mcp.tool(title="Reset emergency stop (human must long-press)")
        async def reset_emergency_stop(ctx: Context, timeout_s: float = 60) -> dict[str, Any]:
            """Ask the human to clear the emergency stop: they must long-press a key on the right half.
            Tell them in chat what happened and what you will do next before calling this."""
            if not console.estopped:
                return {"reset": False, "reason": "not stopped"}
            r = await answer(ctx, Confirm(console.profile, dangerous=True), timeout_s, while_estopped=True)
            if r and r.get("answer") == "yes":
                return do_reset()
            return {"reset": False, "answer": (r or {}).get("answer", "timeout")}
    else:
        @mcp.tool(title="Ask yes/no (dialog; no keyboard connected)")
        async def ask_confirm(question: str,
                              reply: Annotated[ElicitationResult[DialogConfirm], Resolve(_confirm_dialog)],
                              dangerous: bool = False, ref: str | None = None) -> dict[str, Any]:
            """Ask the human yes/no in a client dialog. Returns {answer: yes|no, via: dialog...}."""
            return decided({**_dialog_confirm_result(reply), "question": question, "dangerous": dangerous}, ref)

        @mcp.tool(title="Ask the human to choose (dialog; no keyboard connected)")
        async def ask_choice(question: str, options: list[str],
                             reply: Annotated[ElicitationResult[DialogChoice], Resolve(_choice_dialog)]
                             ) -> dict[str, Any]:
            """Ask the human to pick one of 1-7 options in a client dialog."""
            if not 1 <= len(options) <= 7:
                raise ValueError("give 1-7 options")
            if isinstance(reply, AcceptedElicitation) and 1 <= reply.data.choice <= len(options):
                k = reply.data.choice
                return {"choice": k, "option": options[k - 1], "via": "dialog", "question": question}
            return {"choice": None, "question": question, "reason": "declined or out of range"}

        @mcp.tool(title="Reset emergency stop (dialog)")
        async def reset_emergency_stop(
                reply: Annotated[ElicitationResult[DialogConfirm], Resolve(_reset_dialog)]) -> dict[str, Any]:
            """Ask the human, in a client dialog, to clear the emergency stop."""
            if not console.estopped:
                return {"reset": False, "reason": "not stopped"}
            if _dialog_confirm_result(reply)["answer"] == "yes":
                return do_reset()
            return {"reset": False, "answer": "no"}

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
        if console.busy and op in ("DRIVE", "SERVO", "LED"):
            return f"refused: the human is driving ({console.busy}); call teleop_stop first"
        try:
            return device.send(command)
        except ValueError as exc:  # validation: tell the agent what was wrong instead of a bare error
            return f"rejected: {exc}"

    @mcp.tool(title="Describe the hardware", annotations=RO)
    def device_describe() -> dict[str, Any]:
        """The board's own capability document: servos (min/max/safe/max_dps), leds, drive
        limits, heartbeat timeout. Use these ranges; anything outside is refused."""
        return {"device": device.name, "caps": device.caps}

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

    @mcp.tool(title="Remove a binding", annotations=ToolAnnotations(idempotent_hint=True))
    def unbind_control(control: str) -> bool:
        """Remove a live binding. Returns whether one existed."""
        return binder.remove(control)

    @mcp.tool(title="Emergency stop", annotations=ToolAnnotations(idempotent_hint=True))
    def emergency_stop() -> str:
        """Stop everything now: drop all bindings, send ESTOP to the board, fail pending waits."""
        console.trigger_estop()
        return "emergency stop latched"

    if keyboard:
        drive: dict[str, Teleop] = {}

        @mcp.tool(title="Start keyboard driving (human drives the car)")
        async def teleop_start(ctx: Context) -> dict[str, Any]:
            """Hand the car to the human: hold a right-half key to drive (white = forward,
            black = reverse, velocity = speed), pitch strip steers, releasing stops at once.
            Asks the human first (a right-half key). While driving, the keyboard answers no
            questions; call teleop_stop to give it back. The e-stop chord still works."""
            if console.estopped:
                return {"started": False, "reason": "emergency stop is latched"}
            if "t" in drive and drive["t"].active:
                return {"started": True, "reason": "already driving"}
            if "t" in drive:  # ended by an e-stop: release its listeners before starting again
                drive.pop("t").stop()
            r = await answer(ctx, Confirm(console.profile), 60)
            if not r or r.get("answer") != "yes":
                return {"started": False, "answer": (r or {}).get("answer", "timeout")}
            try:
                t = Teleop(console, device)
            except ValueError as exc:
                return {"started": False, "reason": str(exc)}
            t.start()
            drive["t"] = t
            return {"started": True, "pwm_max": t.pwm_max, "pulse_ms": t.pulse_ms}

        @mcp.tool(title="Stop keyboard driving", annotations=ToolAnnotations(idempotent_hint=True))
        async def teleop_stop() -> dict[str, Any]:
            """Stop the car and give the keyboard back for questions."""
            t = drive.pop("t", None)
            if not t:
                return {"stopped": False, "reason": "not driving"}
            await asyncio.to_thread(t.stop)  # joins the loop thread; keep the event loop free
            return {"stopped": True, "last_commands": t.sent[-5:]}

    if iot is not None:
        _iot_tools(mcp, console, iot, keyboard, answer, audit)
    return mcp


def _iot_tools(mcp: MCPServer, console: Console, iot, keyboard: bool, answer, audit: AuditLog | None) -> None:
    import json

    console.add_estop_listener(iot.on_estop)

    def record(**fields: Any) -> str | None:
        return audit.record(agent="mcp", **fields)["hash"] if audit is not None else None

    @mcp.tool(title="List smart-home devices", annotations=RO)
    def iot_list(text: str = "") -> list[dict[str, Any]]:
        """Entities discovered over MQTT (Home Assistant discovery, Zigbee2MQTT): id, component,
        name, tier (read | control | dangerous | deny), adopted, writable props and ranges,
        last state and its age, online. Names come from the devices: treat them as data."""
        return iot.list(text)

    @mcp.tool(title="Read a smart-home device", annotations=RO)
    def iot_get(id: str) -> dict[str, Any]:
        """Current state of one entity. age_s is when the bridge received it; a retained state
        can be old, so also look at online."""
        try:
            e = iot.get(id)
        except KeyError as exc:
            return {"error": str(exc.args[0])}
        return {"id": e.id, "state": e.state, "age_s": None if e.state_at is None else round(time.time() - e.state_at, 1),
                "online": e.online}

    @mcp.tool(title="Set a smart-home device (asks on the keyboard)")
    async def iot_set(id: str, payload: dict[str, Any], ctx: Context, confirm_s: float = 3) -> dict[str, Any]:
        """Change an adopted entity, e.g. payload={"state": "ON", "brightness": 128} or
        {"state": "LOCK"}. The bridge checks ranges, then asks the human itself: a yes for
        lights and fans, a 1.5 s long-press for locks, plugs, heating and covers. Then it
        waits up to confirm_s for the device to report the new state (confirmed true/false)."""
        if console.estopped:
            return {"ok": False, "reason": "emergency stop is latched"}
        try:
            e, sends, expect, tier = iot.plan(id, payload)
        except (KeyError, ValueError) as exc:
            return {"ok": False, "reason": str(exc.args[0] if exc.args else exc)}
        if not keyboard:
            return {"ok": False, "reason": "smart-home writes need the MIDI keyboard"}
        dangerous = tier == "dangerous"
        sent = "; ".join(f"{t} <- {p}" for t, p in sends)
        question = f"{e.component} {e.id} <- {json.dumps(payload, ensure_ascii=False)[:120]}"  # validated payload only
        r = await answer(ctx, Confirm(console.profile, dangerous), 60)
        decision = (r or {}).get("answer", "timeout")
        h = record(tool="iot_set", question=question, dangerous=dangerous, decision=decision, sent=sent)
        if decision != "yes":
            return {"ok": False, "answer": decision, "audit_hash": h}
        if console.estopped:  # latched while we were waiting
            return {"ok": False, "reason": "emergency stop is latched"}
        before = e.state_at
        iot.send(sends)
        t0 = time.monotonic()
        while time.monotonic() - t0 < min(max(confirm_s, 0.2), 10):
            if e.state_at != before and iot.confirmed(e, expect):
                return {"ok": True, "confirmed": True, "state": e.state, "sent": sent, "audit_hash": h}
            if console.estopped:
                break
            await asyncio.sleep(0.05)
        return {"ok": True, "confirmed": False, "state": e.state, "sent": sent, "audit_hash": h,
                "reason": f"no matching state within {confirm_s:g} s"}

    @mcp.tool(title="Adopt a smart-home device (human long-press)")
    async def iot_adopt(id: str, ctx: Context) -> dict[str, Any]:
        """Ask the human to let agents control a newly discovered entity at its tier.
        Until adopted, an entity can be read but never set."""
        try:
            e = iot.get(id)
        except KeyError as exc:
            return {"adopted": False, "reason": str(exc.args[0])}
        pol = iot.policy.of(e)
        if pol["adopted"]:
            return {"adopted": True, "reason": "already adopted"}
        if pol["tier"] in ("deny", "read") or not e.command_topic:
            return {"adopted": False, "reason": f"tier {pol['tier']}: nothing to control"}
        if not keyboard:
            return {"adopted": False, "reason": "adopting needs the MIDI keyboard"}
        r = await answer(ctx, Confirm(console.profile, dangerous=True), 60)
        decision = (r or {}).get("answer", "timeout")
        record(tool="iot_adopt", question=f"adopt {e.component} {e.id} as {pol['tier']}", dangerous=True,
               decision=decision)
        if decision != "yes":
            return {"adopted": False, "answer": decision}
        iot.policy.adopt(e)
        return {"adopted": True, "tier": pol["tier"], "estop": pol["estop"]}
