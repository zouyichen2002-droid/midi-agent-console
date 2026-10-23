"""The external-brain loop with a scripted stand-in model and keys injected on a timer."""
import json
import threading
import time

import pytest
from mcp import Client

from midi_agent_console.brain import limits_from_caps, run_task, tool_schemas
from midi_agent_console.console import Console
from midi_agent_console.events import decode
from midi_agent_console.hardware import SimulatedDevice
from midi_agent_console.profiles import MICROLAB_MK3
from midi_agent_console.server import build_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


def call(name, **args):
    return {"content": None, "tool_calls": [{"id": f"c-{name}", "type": "function",
                                             "function": {"name": name, "arguments": json.dumps(args)}}]}


def text(s):
    return {"content": s}


class Scripted:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    async def chat(self, messages, tools):
        self.seen.append([m.get("role") for m in messages])
        return self.replies.pop(0) if self.replies else text("done")


def keys(c, *steps, delay=0.3):
    """steps: ('tap', note) | ('hold', note, seconds) | ('wait', seconds), played in order."""
    def run():
        time.sleep(delay)
        for s in steps:
            if s[0] == "wait":
                time.sleep(s[1])
                continue
            note = s[1]
            c.feed(decode(0x90, note, 90))
            time.sleep(s[2] if s[0] == "hold" else 0.05)
            c.feed(decode(0x80, note, 0))
    threading.Thread(target=run, daemon=True).start()


def rig():
    c, dev = Console(MICROLAB_MK3, arm_delay=0), SimulatedDevice()
    return c, dev, build_server(c, dev, "test")


def test_tool_schemas_are_tighter_than_the_device():
    lim = limits_from_caps(SimulatedDevice().caps)
    assert (lim.pwm, lim.ms) == (120, 1000)
    names = [t["function"]["name"] for t in tool_schemas(lim)]
    assert names == ["drive", "servo", "stop", "get_state", "led", "finish"]
    assert "device_send" not in names and "reset_emergency_stop" not in names


@pytest.mark.anyio
async def test_plan_then_each_motion_approved_then_finish():
    c, dev, mcp = rig()
    llm = Scripted(text("1. drive forward\n2. wave the arm"), call("drive", left=100, right=100, ms=200),
                   call("servo", i=0, deg=150.0), call("finish", summary="drove and waved"))
    keys(c, ("tap", 72), ("wait", 0.3), ("hold", 74, 1.7), ("wait", 0.5), ("hold", 76, 1.7))
    async with Client(mcp, raise_exceptions=True) as cl:
        out = await run_task("drive a bit and wave", cl, llm.chat, log=lambda s: None)
    assert out["ended"] == "finished" and out["summary"] == "drove and waved"
    assert [a["cmd"] for a in out["actions"]] == ["DRIVE 100 100 200", "SERVO 0 150"]
    assert "st=RUN" in out["actions"][0]["state"] and dev.servos[0] == 150
    assert dev.log[-1] == "STOP"  # always ends with a stop


@pytest.mark.anyio
async def test_first_no_stops_the_run():
    c, dev, mcp = rig()
    llm = Scripted(text("1. drive"), call("drive", left=100, right=100, ms=200),
                   call("drive", left=100, right=100, ms=200))
    keys(c, ("tap", 72), ("wait", 0.3), ("tap", 50))  # approve the plan, then say no to step 1
    async with Client(mcp, raise_exceptions=True) as cl:
        out = await run_task("drive", cl, llm.chat, log=lambda s: None)
    assert out["ended"] == "declined" and out["actions"] == []
    assert not [x for x in dev.log if x.startswith("DRIVE")]


@pytest.mark.anyio
async def test_plan_rejected_means_nothing_moves():
    c, dev, mcp = rig()
    llm = Scripted(text("1. drive into the wall"))
    keys(c, ("tap", 50))
    async with Client(mcp, raise_exceptions=True) as cl:
        out = await run_task("crash", cl, llm.chat, log=lambda s: None)
    assert out["ended"] == "plan no" and not [x for x in dev.log if x.startswith(("DRIVE", "SERVO"))]


@pytest.mark.anyio
async def test_bad_calls_are_not_executed_and_end_the_run():
    c, dev, mcp = rig()
    llm = Scripted(text("1. go"), call("device_send", command="RESET"),
                   {"tool_calls": [{"id": "x", "function": {"name": "drive", "arguments": "{oops"}}]})
    keys(c, ("tap", 72))
    async with Client(mcp, raise_exceptions=True) as cl:
        out = await run_task("go", cl, llm.chat, log=lambda s: None)
    assert out["ended"] == "bad tool calls" and out["actions"] == []
    assert "RESET" not in dev.log


@pytest.mark.anyio
async def test_out_of_range_is_refused_before_asking():
    c, dev, mcp = rig()
    llm = Scripted(text("1. go fast"), call("drive", left=180, right=180, ms=5000), call("finish", summary="x"))
    keys(c, ("tap", 72))
    async with Client(mcp, raise_exceptions=True) as cl:
        out = await run_task("go fast", cl, llm.chat, log=lambda s: None)
    assert out["ended"] == "finished" and out["actions"] == []
    assert not [x for x in dev.log if x.startswith("DRIVE")]


def test_openai_chat_request_and_response_shape():
    import asyncio
    import http.server

    from midi_agent_console.brain import openai_chat

    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got["path"] = self.path
            got["auth"] = self.headers.get("Authorization")
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "stop", "arguments": "{}"}}]}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        chat = openai_chat(f"http://127.0.0.1:{srv.server_address[1]}/v1", "m", "k",
                           extra={"chat_template_kwargs": {"enable_thinking": False}})
        msg = asyncio.run(chat([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "stop"}}]))
    finally:
        srv.shutdown()
    assert got["path"] == "/v1/chat/completions" and got["auth"] == "Bearer k"
    assert got["body"]["parallel_tool_calls"] is False and got["body"]["tools"][0]["function"]["name"] == "stop"
    assert got["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert msg["tool_calls"][0]["function"]["name"] == "stop"