"""Durable runs: kill the brain process at each point of a motion step, then resume.

The rules under test: a step is journaled as "sent" before the command leaves; after a
crash nothing is replayed (sent/acked -> unknown, approved-but-unsent -> void); resume
stops the device, tells the model what is unknown, and needs a new yes before any motion."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import Client

from midi_agent_console.audit import AuditLog, verify
from midi_agent_console.brain import resume_task, run_task
from midi_agent_console.console import Console
from midi_agent_console.evals import ScriptedHuman
from midi_agent_console.hardware import SimulatedDevice
from midi_agent_console.journal import Journal, LeaseLost
from midi_agent_console.profiles import MICROLAB_MK3
from midi_agent_console.server import build_server
from midi_agent_console.tracing import Tracer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def call(name, **args):
    return {"content": None, "tool_calls": [{"id": f"c-{name}", "type": "function",
                                             "function": {"name": name, "arguments": json.dumps(args)}}]}


class Scripted:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    async def chat(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else {"content": "done"}


def crash(tmp_path, at):
    jpath, logpath = tmp_path / "runs.db", tmp_path / "device.log"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    p = subprocess.run([sys.executable, str(ROOT / "tests" / "crash_child.py"), str(jpath), str(logpath), at],
                       env=env, capture_output=True, text=True, timeout=60)
    sent = logpath.read_text(encoding="utf-8").split() if logpath.exists() else []
    return p, jpath, [c for c in sent if c == "DRIVE"]


@pytest.mark.parametrize("at, drives, state", [
    ("after_approved", 0, "void"),     # approved, never sent: the approval dies with the process
    ("after_sent", 0, "unknown"),      # journaled as sent, died before the bytes left: still unknown
    ("after_device", 1, "unknown"),    # the board got it, no reply recorded
    ("after_acked", 1, "unknown"),     # reply recorded, observation not
])
def test_crash_points_are_never_replayed(tmp_path, at, drives, state):
    p, jpath, sent = crash(tmp_path, at)
    assert p.returncode == 137, p.stderr[-2000:]
    assert len(sent) == drives
    j = Journal(jpath)
    (run,) = j.runs()
    assert run["status"] == "running" and run["ended"] is None
    assert j.steps(run["run_id"])[0]["state"] in ("approved", "sent", "acked")
    hanging = j.recover(run["run_id"])
    assert [(s["n"], s["state"], s["cmd"]) for s in hanging] == [(1, state, "DRIVE 120 120 1000")]


@pytest.mark.anyio
async def test_resume_stops_explains_and_needs_a_new_yes(tmp_path):
    p, jpath, sent = crash(tmp_path, "after_device")
    assert p.returncode == 137 and len(sent) == 1
    time.sleep(0.6)  # the dead run's lease (0.5 s) runs out
    j = Journal(jpath)
    run_id = j.runs()[0]["run_id"]

    # 1) the human refuses the new plan: no motion at all
    c, dev = Console(MICROLAB_MK3, arm_delay=0), SimulatedDevice()
    human = ScriptedHuman(c, policy=lambda kind: False, think_s=0.05)
    llm = Scripted({"content": "1. check where I am"}, call("drive", left=100, right=100, ms=200))
    async with Client(build_server(c, dev, "t"), raise_exceptions=True) as cl:
        out = await resume_task(run_id, cl, llm.chat, j, log=lambda s: None)
    human.close()
    assert out["ended"] == "plan no"
    assert not [x for x in dev.log if x.startswith(("DRIVE", "SERVO"))]
    assert dev.log[:2] == ["STOP", "STATE"]  # stopped and read before the model is asked anything
    note = llm.seen[0][-1]["content"]
    assert "UNKNOWN" in note and "DRIVE 120 120 1000" in note and "NEW" in note
    tool_msgs = [m for m in llm.seen[0] if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[-1]["content"].startswith("unknown:")  # the dangling call is closed
    assert j.run(run_id)["status"] == "ended"
    with pytest.raises(ValueError):  # an ended run cannot be resumed again (checked before any I/O)
        await resume_task(run_id, None, llm.chat, j, log=lambda s: None)


@pytest.mark.anyio
async def test_resume_then_approved_motion_is_a_new_step(tmp_path):
    p, jpath, _ = crash(tmp_path, "after_acked")
    time.sleep(0.6)
    j = Journal(jpath)
    run_id = j.runs()[0]["run_id"]
    c, dev = Console(MICROLAB_MK3, arm_delay=0), SimulatedDevice()
    human = ScriptedHuman(c, think_s=0.05)
    llm = Scripted({"content": "1. drive a little"}, call("drive", left=100, right=100, ms=200),
                   call("finish", summary="ok"))
    async with Client(build_server(c, dev, "t"), raise_exceptions=True) as cl:
        out = await resume_task(run_id, cl, llm.chat, j, log=lambda s: None)
    human.close()
    assert out["ended"] == "finished" and [a["cmd"] for a in out["actions"]] == ["DRIVE 100 100 200"]
    assert [(s["n"], s["state"]) for s in j.steps(run_id)] == [(1, "unknown"), (2, "observed")]
    assert human.asked == 2  # new plan + the motion: nothing carried over


def test_lease_blocks_a_second_live_run(tmp_path):
    a, b = Journal(tmp_path / "r.db", holder="a", lease_s=5), Journal(tmp_path / "r.db", holder="b", lease_s=5)
    fa = a.acquire("car", "run-a")
    with pytest.raises(LeaseLost):
        b.acquire("car", "run-b")
    a.release("car", fa)
    fb = b.acquire("car", "run-b")
    assert fb == fa + 1
    with pytest.raises(LeaseLost):  # the old holder's fence is stale now
        a.renew("car", fa)


@pytest.mark.anyio
async def test_keyboard_decisions_are_in_the_audit_chain_and_steps_point_at_them(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    j = Journal(tmp_path / "runs.db")
    c, dev = Console(MICROLAB_MK3, arm_delay=0), SimulatedDevice()
    human = ScriptedHuman(c, think_s=0.05)
    tracer = Tracer(tmp_path / "spans.jsonl")
    llm = Scripted({"content": "1. drive"}, call("drive", left=100, right=100, ms=200), call("finish", summary="ok"))
    async with Client(build_server(c, dev, "t", audit=audit), raise_exceptions=True) as cl:
        out = await run_task("drive", cl, llm.chat, log=lambda s: None, journal=j, tracer=tracer)
    human.close()
    assert out["ended"] == "finished"
    lines = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(e["decision"], e["dangerous"], e["ref"]) for e in lines] == [
        ("yes", False, f"{out['run_id']}#0"), ("yes", True, f"{out['run_id']}#1")]
    assert verify(tmp_path / "audit.jsonl")[0]
    step = j.steps(out["run_id"])[0]
    assert json.loads(step["answer"])["audit_hash"] == lines[1]["hash"]

    spans = {s.name: s for s in tracer.finished}
    root = spans["invoke_agent brain"]
    assert {s.trace_id for s in tracer.finished} == {root.trace_id} == {out["trace_id"]}
    assert spans["execute_tool drive"].parent_id == root.span_id
    for child in ("hitl.approval", "device.command drive", "device.observe"):
        assert any(s.name == child and s.parent_id == spans["execute_tool drive"].span_id for s in tracer.finished)
    approval = [s for s in tracer.finished if s.name == "hitl.approval"][-1]
    assert approval.attrs["mac.approval.decision"] == "yes" and approval.attrs["mac.audit.hash"] == lines[1]["hash"]
    first = json.loads((tmp_path / "spans.jsonl").read_text(encoding="utf-8").splitlines()[0])
    span = first["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16 and span["endTimeUnixNano"] >= span["startTimeUnixNano"]


def test_traceparent_joins_the_callers_trace(monkeypatch):
    monkeypatch.setenv("TRACEPARENT", "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01")
    t = Tracer()
    with t.span("outer"):
        with t.span("inner"):
            pass
    inner, outer = t.finished
    assert outer.trace_id == "ab" * 16 and outer.parent_id == "cd" * 8 and inner.parent_id == outer.span_id
