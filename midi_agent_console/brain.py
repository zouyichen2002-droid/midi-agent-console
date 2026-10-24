"""An "external brain" demo: a language model plans a short robot task, the human
approves on the MIDI keyboard, the board executes, the result is observed.

    plan  -> the model writes a numbered plan (no tools); the human approves it once
    act   -> one tool call per model turn; every motion needs its own keyboard yes
             (DRIVE / SERVO: dangerous = long-press); the first no / e-stop / timeout
             sends STOP and ends the run
    observe -> after each action the bridge reads STATE itself and returns it

The model never sees device_send or the asking tools: it gets a few typed tools whose
ranges are tighter than the device's own limits, and the bridge enforces them again.
Budgets cap actions, model calls and wall time; the run always ends with STOP.

With a journal (journal.py) every message and step is in SQLite before the next thing
happens; `resume_task` continues a crashed run without replaying anything in flight.
Spans (tracing.py) follow the OpenTelemetry GenAI names: invoke_agent > chat /
execute_tool > hitl.approval / device.command / device.observe.

The model is anything OpenAI-compatible (llama.cpp server with --jinja, Ollama's /v1,
DeepSeek). Pass `chat` to inject a scripted stand-in for tests.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .journal import Journal, LeaseLost
from .tracing import Tracer

Chat = Callable[[list[dict], list[dict]], Awaitable[dict]]

SYSTEM = """You control a small two-wheel robot car with one servo arm and one LED.
First reply with a short numbered plan in plain text and NO tool call.
After the human approves, carry it out with exactly ONE tool call per reply.
Each motion is approved by the human on a MIDI keyboard; if a call comes back
"declined", stop and explain. Call finish when done. Keep it to {max_actions} actions or fewer."""


@dataclass
class Limits:
    pwm: int = 120
    ms: int = 1000
    servos: dict[int, tuple[int, int]] = field(default_factory=dict)
    max_actions: int = 6
    max_model_calls: int = 12
    max_seconds: float = 120
    max_bad_calls: int = 2


def limits_from_caps(caps: dict[str, Any], pwm: int = 120, ms: int = 1000) -> Limits:
    drive = caps.get("drive") or {}
    return Limits(pwm=min(pwm, int(drive.get("max", 0))), ms=min(ms, int(drive.get("max_ms", 0))),
                  servos={s["i"]: (s["min"], s["max"]) for s in caps.get("servos", [])})


def tool_schemas(lim: Limits) -> list[dict]:
    def fn(name, desc, props, required):
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
            "type": "object", "properties": props, "required": required, "additionalProperties": False}}}

    servo_ids = sorted(lim.servos)
    lo = min((r[0] for r in lim.servos.values()), default=0)
    hi = max((r[1] for r in lim.servos.values()), default=0)
    tools = [
        fn("stop", "Stop the motors now.", {}, []),
        fn("get_state", "Read the car's current state.", {}, []),
        fn("led", "Set the LED colour.", {c: {"type": "integer", "minimum": 0, "maximum": 255} for c in "rgb"},
           ["r", "g", "b"]),
        fn("finish", "End the task with a one-line summary.", {"summary": {"type": "string"}}, ["summary"]),
    ]
    if lim.pwm > 0:
        pwm = {"type": "integer", "minimum": -lim.pwm, "maximum": lim.pwm}
        tools.insert(0, fn("drive", f"Drive both wheels for ms milliseconds, then stop by itself. "
                                    f"Positive = forward. left/right in -{lim.pwm}..{lim.pwm}.",
                           {"left": pwm, "right": pwm, "ms": {"type": "integer", "minimum": 1, "maximum": lim.ms}},
                           ["left", "right", "ms"]))
    if servo_ids:
        tools.insert(1, fn("servo", f"Move servo i to an angle in degrees ({lo}..{hi}).",
                           {"i": {"type": "integer", "enum": servo_ids},
                            "deg": {"type": "integer", "minimum": lo, "maximum": hi}}, ["i", "deg"]))
    return tools


def openai_chat(base_url: str, model: str, api_key: str = "", timeout: float = 30,
                extra: dict | None = None) -> Chat:
    """OpenAI-compatible /chat/completions client using only the standard library."""
    url = base_url.rstrip("/") + "/chat/completions"

    def call(messages, tools):
        body = {"model": model, "messages": messages, "tools": tools, "temperature": 0.2,
                "parallel_tool_calls": False, **(extra or {})}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["choices"][0]["message"]

    async def chat(messages, tools):
        return await asyncio.to_thread(call, messages, tools)

    return chat


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    v = json.loads(raw)
    if not isinstance(v, dict):
        raise ValueError("arguments must be a JSON object")
    return v


def _check(name: str, a: dict, lim: Limits) -> str:
    """-> device command; raises ValueError when outside the bridge's limits."""
    def num(k, lo, hi):
        v = a.get(k)
        if isinstance(v, float) and v.is_integer():  # models often write 100.0
            v = int(v)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"{k}={v!r} must be an integer in {lo}..{hi}")
        return v

    if name == "drive":
        if lim.pwm <= 0:
            raise ValueError("this device has no motors")
        return f"DRIVE {num('left', -lim.pwm, lim.pwm)} {num('right', -lim.pwm, lim.pwm)} {num('ms', 1, lim.ms)}"
    if name == "servo":
        i = a.get("i")
        if isinstance(i, bool) or not isinstance(i, int) or i not in lim.servos:
            raise ValueError(f"no servo {i!r}; have {sorted(lim.servos)}")
        lo, hi = lim.servos[i]
        return f"SERVO {i} {num('deg', lo, hi)}"
    if name == "led":
        return f"LED 0 {num('r', 0, 255)} {num('g', 0, 255)} {num('b', 0, 255)}"
    raise ValueError(f"unknown tool {name!r}")


class _Run:
    """One brain run. With a journal every message and every step is written to SQLite
    before the next thing happens, so a crash leaves a record a resume can trust."""

    def __init__(self, client, chat: Chat, lim: Limits | None, log, journal: Journal | None, tracer: Tracer):
        self.client, self.chat, self.lim, self.log = client, chat, lim, log
        self.journal, self.tracer = journal, tracer
        self.run_id: str | None = None
        self.device_id, self.fence = "device", 0
        self.messages: list[dict] = []
        self.n = 0  # motion steps proposed so far (journal step numbers)
        self.last_audit: str | None = None
        self.lease_lost = False

    async def keep_lease(self) -> None:
        """Renew the device lease while the run is alive (a long wait for a key must not let
        another run take the robot); losing it blocks every further motion."""
        while True:
            await asyncio.sleep(self.journal.lease_s / 3)
            try:
                self.journal.renew(self.device_id, self.fence)
            except LeaseLost:
                self.lease_lost = True
                return

    async def tool(self, name, args=None):
        r = await self.client.call_tool(name, args or {})
        return r.structured_content if r.structured_content is not None else {}

    async def device(self, cmd):
        return (await self.tool("device_send", {"command": cmd})).get("result", "")

    async def setup(self) -> None:
        d = await self.tool("device_describe")
        self.device_id = str(d.get("device") or "device")
        if self.lim is None:
            self.lim = limits_from_caps(d["caps"])

    def push(self, msg: dict) -> None:
        self.messages.append(msg)
        if self.journal and self.run_id:
            self.journal.add_event(self.run_id, "msg", msg)

    def jstep(self, state: str, **fields) -> None:
        if self.journal and self.run_id:
            self.journal.step(self.run_id, self.n, state, **fields)

    async def ask(self, question: str, dangerous: bool) -> str:
        ref = f"{self.run_id}#{self.n}" if self.run_id else None
        with self.tracer.span("hitl.approval", **{"mac.approval.dangerous": dangerous}) as sp:
            t0 = time.monotonic()
            r = await self.tool("ask_confirm", {"question": question, "dangerous": dangerous, "timeout_s": 60,
                                                **({"ref": ref} if ref else {})})
            answer = r.get("answer")
            sp.set(**{"mac.approval.decision": answer, "mac.approval.latency_ms": int((time.monotonic() - t0) * 1000),
                      "mac.audit.hash": r.get("audit_hash"), "mac.ref": ref})
            if answer == "estop":
                sp.event("mac.estop")
                sp.error("estop", "emergency stop on the keyboard")
        self.last_audit = r.get("audit_hash")
        return answer

    async def loop(self, out: dict[str, Any]) -> None:
        lim = self.lim
        tools = tool_schemas(lim)
        valid = [t["function"]["name"] for t in tools]
        t0, calls, bad = time.monotonic(), 0, 0
        while True:
            if calls >= lim.max_model_calls or time.monotonic() - t0 > lim.max_seconds:
                out["ended"] = "budget"
                return
            with self.tracer.span("chat", "client", **{"gen_ai.operation.name": "chat"}) as sp:
                msg = await self.chat(self.messages, tools)
                sp.set(**{"mac.tool_calls": len(msg.get("tool_calls") or [])})
            calls += 1
            tcs = msg.get("tool_calls") or []

            if out["plan"] is None:  # ---- plan phase
                if tcs:
                    self.push({"role": "user", "content": "First write the numbered plan in text, no tool call."})
                    continue
                self.push({"role": "assistant", "content": msg.get("content") or ""})
                out["plan"] = (msg.get("content") or "").strip()
                self.log(f"[brain] plan:\n{out['plan']}")
                with self.tracer.span("plan brain", **{"gen_ai.operation.name": "plan"}):
                    answer = await self.ask(f"Approve this plan?\n{out['plan']}", False)
                if self.journal and self.run_id:
                    self.journal.set_run(self.run_id, plan=out["plan"], status="running" if answer == "yes" else "ended")
                if answer != "yes":
                    out["ended"] = f"plan {answer}"
                    return
                self.push({"role": "user", "content": "Approved. Go ahead, one tool call at a time."})
                continue

            self.push({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": tcs[:1]} if tcs else {})})
            if not tcs:  # ---- act phase
                out["ended"] = "model stopped"
                return
            tc = tcs[0]
            name = tc.get("function", {}).get("name", "")
            with self.tracer.span(f"execute_tool {name}", **{"gen_ai.operation.name": "execute_tool",
                                                             "gen_ai.tool.name": name,
                                                             "gen_ai.tool.call.id": tc.get("id")}) as sp:
                reply = await self.act(tc, name, valid, out)
                sp.set(**{"mac.reply": reply[:200]})
                if reply.startswith("error:"):
                    sp.error("tool_error", reply[:200])
            if reply.startswith("error:"):
                bad += 1
                if bad >= lim.max_bad_calls:
                    out["ended"] = "bad tool calls"
                    return
            else:
                bad = 0
            self.push({"role": "tool", "tool_call_id": tc.get("id", ""), "content": reply})
            if name == "finish" and not reply.startswith("error:"):
                out["ended"] = "finished"
                out["summary"] = _parse_args(tc["function"].get("arguments")).get("summary", "")
                return
            if reply.startswith(("declined", "stopped", "lease lost")):
                out["ended"] = reply.split(":")[0]
                return
            if len(out["actions"]) >= lim.max_actions:
                out["ended"] = "action budget"
                return

    async def act(self, tc, name, valid, out) -> str:
        lim = self.lim
        if name not in valid:
            return f"error: unknown tool {name!r}; valid: {valid}"
        try:
            args = _parse_args(tc.get("function", {}).get("arguments"))
        except ValueError as exc:
            return f"error: bad JSON arguments ({exc})"
        if name == "finish":
            return "ok"
        if name == "get_state":
            return await self.device("STATE")
        if name == "stop":
            return await self.device("STOP")
        try:
            cmd = _check(name, args, lim)
        except ValueError as exc:
            return f"error: {exc}"
        dangerous = name in ("drive", "servo")
        self.n += 1
        self.jstep("proposed", tool=name, cmd=cmd)
        question = f"Step {len(out['actions']) + 1}: {cmd}"
        self.log(f"[brain] approve? {question}" + ("  (long-press)" if dangerous else ""))  # say it BEFORE asking
        answer = await self.ask(question, dangerous)
        self.jstep("approved" if answer == "yes" else "declined",
                   answer=json.dumps({"answer": answer, "audit_hash": self.last_audit}))
        if answer != "yes":
            self.log(f"[brain] {cmd}: {answer}")
            if answer == "estop":
                return "stopped: emergency stop on the keyboard; the human must reset it"
            return f"declined: the human answered {answer}; do not retry"
        if self.journal and self.run_id:
            try:
                if self.lease_lost:
                    raise LeaseLost(f"lease on {self.device_id} was taken over")
                self.journal.renew(self.device_id, self.fence)
            except LeaseLost as exc:
                self.jstep("void")
                return f"lease lost: {exc}"
        self.jstep("sent")  # committed BEFORE the command leaves: a crash after this is "unknown"
        with self.tracer.span(f"device.command {name}", "client", **{"mac.device.command": cmd}) as sp:
            reply = await self.device(cmd)
            sp.set(**{"mac.device.reply": reply})
        self.jstep("acked", reply=reply)
        if name == "drive":
            await asyncio.sleep(args["ms"] / 1000 + 0.05)
        with self.tracer.span("device.observe"):
            state = await self.device("STATE")
        self.jstep("observed", observed=state)
        out["actions"].append({"cmd": cmd, "reply": reply, "state": state})
        self.log(f"[brain] {cmd} -> {reply} | {state}")
        return f"{reply}; {state}"

    async def run(self, out: dict[str, Any]) -> dict[str, Any]:
        with self.tracer.span("invoke_agent brain", **{"gen_ai.operation.name": "invoke_agent",
                                                       "gen_ai.agent.name": "brain",
                                                       "gen_ai.conversation.id": self.run_id}) as sp:
            out["trace_id"] = sp.trace_id
            if self.journal and self.run_id:
                self.journal.add_event(self.run_id, "trace", sp.trace_id)
            keeper = asyncio.create_task(self.keep_lease()) if self.journal and self.run_id else None
            try:
                await self.loop(out)
            finally:
                if keeper:
                    keeper.cancel()
                out["final_stop"] = await self.device("STOP")
                if self.journal and self.run_id:
                    self.journal.set_run(self.run_id, status="ended", ended=out["ended"])
                    self.journal.release(self.device_id, self.fence)
            sp.set(**{"mac.ended": out["ended"], "mac.actions": len(out["actions"])})
        self.log(f"[brain] ended: {out['ended']}")
        return out


async def run_task(task: str, client, chat: Chat, lim: Limits | None = None,
                   log: Callable[[str], None] = print, journal: Journal | None = None,
                   tracer: Tracer | None = None) -> dict[str, Any]:
    """client: an open mcp.Client connected to the midi-agent-console server.
    journal: record the run durably (see journal.py) so it can be resumed after a crash."""
    r = _Run(client, chat, lim, log, journal, tracer or Tracer())
    await r.setup()
    out: dict[str, Any] = {"task": task, "plan": None, "actions": [], "ended": None}
    if journal:
        r.run_id = out["run_id"] = journal.new_run(task, r.device_id)
        r.fence = journal.acquire(r.device_id, r.run_id)
    r.push({"role": "system", "content": SYSTEM.format(max_actions=r.lim.max_actions)})
    r.push({"role": "user", "content": task})
    return await r.run(out)


def resume_note(hanging: list[dict], state: str) -> str:
    lines = ["The previous run of this task was interrupted (the controller process died)."]
    for s in hanging:
        if s["state"] == "unknown":
            lines.append(f"Step {s['n']} `{s['cmd']}` was SENT but its outcome is UNKNOWN: it may or may not "
                         f"have happened.")
        else:
            lines.append(f"Step {s['n']} `{s['cmd']}` was never sent; its approval is void.")
    lines.append(f"The car has been stopped. Current state: {state}")
    lines.append("Nothing from before is approved any more. Write a NEW short numbered plan from the current "
                 "state (text only, no tool call); the human will approve it again.")
    return "\n".join(lines)


async def resume_task(run_id: str, client, chat: Chat, journal: Journal, lim: Limits | None = None,
                      log: Callable[[str], None] = print, tracer: Tracer | None = None) -> dict[str, Any]:
    """Continue an interrupted run. Never replays: in-flight steps become unknown, the device
    is stopped and read, and the model must write a new plan that the human approves again."""
    run = journal.run(run_id)
    if run is None:
        raise LookupError(f"no run {run_id!r} in {journal.path}")
    if run["status"] == "ended":
        raise ValueError(f"run {run_id} already ended ({run['ended']}); start a new one")
    r = _Run(client, chat, lim, log, journal, tracer or Tracer())
    await r.setup()
    r.run_id = run_id
    r.fence = journal.acquire(r.device_id, run_id)  # refuses while another live run holds the device
    hanging = journal.recover(run_id)
    r.n = max((s["n"] for s in journal.steps(run_id)), default=0)
    await r.device("STOP")
    state = await r.device("STATE")
    r.messages = journal.events(run_id, "msg")
    last = r.messages[-1] if r.messages else {}
    if last.get("role") == "assistant" and last.get("tool_calls"):  # died mid-call: close it for the API
        r.push({"role": "tool", "tool_call_id": last["tool_calls"][0].get("id", ""),
                "content": "unknown: the controller crashed while this call was running"})
    r.push({"role": "user", "content": resume_note(hanging, state)})
    journal.add_event(run_id, "resume", {"hanging": hanging, "state": state, "fence": r.fence})
    log(f"[brain] resuming {run_id}: " + (", ".join(f"step {s['n']} {s['state']}" for s in hanging) or "nothing in flight"))
    out: dict[str, Any] = {"task": run["task"], "plan": None, "actions": [], "ended": None, "run_id": run_id,
                           "resumed": hanging}
    return await r.run(out)
