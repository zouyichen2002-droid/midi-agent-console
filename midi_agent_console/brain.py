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
        if i not in lim.servos:
            raise ValueError(f"no servo {i!r}; have {sorted(lim.servos)}")
        lo, hi = lim.servos[i]
        return f"SERVO {i} {num('deg', lo, hi)}"
    if name == "led":
        return f"LED 0 {num('r', 0, 255)} {num('g', 0, 255)} {num('b', 0, 255)}"
    raise ValueError(f"unknown tool {name!r}")


async def run_task(task: str, client, chat: Chat, lim: Limits | None = None,
                   log: Callable[[str], None] = print) -> dict[str, Any]:
    """client: an open mcp.Client connected to the midi-agent-console server."""
    async def tool(name, args=None):
        r = await client.call_tool(name, args or {})
        return r.structured_content if r.structured_content is not None else {}

    async def device(cmd):
        return (await tool("device_send", {"command": cmd})).get("result", "")

    if lim is None:
        lim = limits_from_caps((await tool("device_describe"))["caps"])
    tools = tool_schemas(lim)
    valid = [t["function"]["name"] for t in tools]
    messages = [{"role": "system", "content": SYSTEM.format(max_actions=lim.max_actions)},
                {"role": "user", "content": task}]
    out: dict[str, Any] = {"task": task, "plan": None, "actions": [], "ended": None}
    t0, calls, bad = time.monotonic(), 0, 0

    async def ask(question, dangerous):
        r = await tool("ask_confirm", {"question": question, "dangerous": dangerous, "timeout_s": 60})
        return r.get("answer")

    try:
        while True:
            if calls >= lim.max_model_calls or time.monotonic() - t0 > lim.max_seconds:
                out["ended"] = "budget"
                break
            msg = await chat(messages, tools)
            calls += 1
            tcs = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             **({"tool_calls": tcs[:1]} if tcs else {})})

            if out["plan"] is None:  # ---- plan phase
                if tcs:
                    messages.pop()
                    messages.append({"role": "user", "content": "First write the numbered plan in text, no tool call."})
                    continue
                out["plan"] = (msg.get("content") or "").strip()
                log(f"[brain] plan:\n{out['plan']}")
                answer = await ask(f"Approve this plan?\n{out['plan']}", False)
                if answer != "yes":
                    out["ended"] = f"plan {answer}"
                    break
                messages.append({"role": "user", "content": "Approved. Go ahead, one tool call at a time."})
                continue

            if not tcs:  # ---- act phase
                out["ended"] = "model stopped"
                break
            tc = tcs[0]
            name = tc.get("function", {}).get("name", "")
            reply = await _act(tc, name, lim, valid, out, ask, device, log)
            if reply.startswith("error:"):
                bad += 1
                if bad >= lim.max_bad_calls:
                    out["ended"] = "bad tool calls"
                    break
            else:
                bad = 0
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": reply})
            if name == "finish" and not reply.startswith("error:"):
                out["ended"] = "finished"
                out["summary"] = _parse_args(tc["function"].get("arguments")).get("summary", "")
                break
            if reply.startswith(("declined", "stopped")):
                out["ended"] = reply.split(":")[0]
                break
            if len(out["actions"]) >= lim.max_actions:
                out["ended"] = "action budget"
                break
    finally:
        out["final_stop"] = await device("STOP")
    log(f"[brain] ended: {out['ended']}")
    return out


async def _act(tc, name, lim, valid, out, ask, device, log) -> str:
    if name not in valid:
        return f"error: unknown tool {name!r}; valid: {valid}"
    try:
        args = _parse_args(tc.get("function", {}).get("arguments"))
    except ValueError as exc:
        return f"error: bad JSON arguments ({exc})"
    if name == "finish":
        return "ok"
    if name == "get_state":
        return await device("STATE")
    if name == "stop":
        return await device("STOP")
    try:
        cmd = _check(name, args, lim)
    except ValueError as exc:
        return f"error: {exc}"
    dangerous = name in ("drive", "servo")
    answer = await ask(f"Step {len(out['actions']) + 1}: {cmd}", dangerous)
    if answer != "yes":
        log(f"[brain] {cmd}: {answer}")
        if answer == "estop":
            return "stopped: emergency stop on the keyboard; the human must reset it"
        return f"declined: the human answered {answer}; do not retry"
    reply = await device(cmd)
    if name == "drive":
        await asyncio.sleep(args["ms"] / 1000 + 0.05)
    state = await device("STATE")
    out["actions"].append({"cmd": cmd, "reply": reply, "state": state})
    log(f"[brain] {cmd} -> {reply} | {state}")
    return f"{reply}; {state}"
