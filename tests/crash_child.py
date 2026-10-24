"""Run one journaled brain task and die hard (os._exit) at a chosen point.

    python tests/crash_child.py JOURNAL DEVICE_LOG CRASH_AT

CRASH_AT: after_approved | after_sent | after_device | after_acked | never
Every command that reaches the device is appended to DEVICE_LOG first, so the parent
can count exactly what went out."""
import asyncio
import json
import os
import sys

from mcp import Client

from midi_agent_console.brain import run_task
from midi_agent_console.console import Console
from midi_agent_console.evals import ScriptedHuman
from midi_agent_console.hardware import SimulatedDevice
from midi_agent_console.journal import Journal
from midi_agent_console.profiles import MICROLAB_MK3
from midi_agent_console.server import build_server


def call(name, **args):
    return {"content": None, "tool_calls": [{"id": f"c-{name}", "type": "function",
                                             "function": {"name": name, "arguments": json.dumps(args)}}]}


async def main(jpath, logpath, crash_at):
    replies = [{"content": "1. drive forward 30 cm"}, call("drive", left=120, right=120, ms=1000),
               call("finish", summary="done")]

    async def chat(messages, tools):
        return replies.pop(0) if replies else {"content": "done"}

    journal = Journal(jpath, lease_s=0.5)
    step = journal.step

    def crashing_step(run_id, n, state, **fields):
        step(run_id, n, state, **fields)
        if state == crash_at.removeprefix("after_"):
            os._exit(137)

    journal.step = crashing_step
    console, dev = Console(MICROLAB_MK3, arm_delay=0), SimulatedDevice()
    send = dev.send

    def logged_send(cmd):
        with open(logpath, "a", encoding="utf-8") as f:
            f.write(cmd + "\n")
        r = send(cmd)
        if crash_at == "after_device" and cmd.startswith("DRIVE"):
            os._exit(137)
        return r

    dev.send = logged_send
    human = ScriptedHuman(console, think_s=0.05)
    async with Client(build_server(console, dev, "crash"), raise_exceptions=True) as cl:
        await run_task("drive forward", cl, chat, log=lambda s: None, journal=journal)
    human.close()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:4]))
