"""The eval harness itself, with scripted models (a good one and a sloppy one)."""
import asyncio
import json

from midi_agent_console.brain import Limits
from midi_agent_console.evals import TASKS, pass_hat_k, run_one, run_suite, violations


def call(name, **args):
    return {"content": None, "tool_calls": [{"id": f"c-{name}", "type": "function",
                                             "function": {"name": name, "arguments": json.dumps(args)}}]}


def scripted(*replies):
    replies = list(replies)

    async def chat(messages, tools):
        return replies.pop(0) if replies else {"content": "done"}
    return chat


FORWARD = TASKS[0]


def test_a_good_plan_passes_the_forward_task():
    chat = scripted({"content": "1. drive forward 30 cm"}, call("drive", left=120, right=120, ms=1000),
                    call("drive", left=120, right=120, ms=500), call("finish", summary="drove 30 cm"))
    r = asyncio.run(run_one(FORWARD, chat))
    assert r.success and r.ended == "finished" and r.actions == 2
    assert r.asked == 3 and r.granted == 3  # plan + two long-pressed motions
    assert 0.25 < r.final["x"] < 0.35


def test_a_sloppy_plan_fails_and_a_refusing_human_stops_it():
    short = scripted({"content": "1. drive"}, call("drive", left=60, right=60, ms=300), call("finish", summary="x"))
    assert not asyncio.run(run_one(FORWARD, short)).success
    chat = scripted({"content": "1. drive"}, call("drive", left=120, right=120, ms=1000))
    r = asyncio.run(run_one(FORWARD, chat, policy=lambda kind: kind != "confirm-dangerous"))
    assert r.ended == "declined" and r.actions == 0 and r.final["x"] == 0


def test_suite_report_shape():
    async def chat(messages, tools):
        return {"content": "1. nothing"} if len(messages) <= 2 else call("finish", summary="nothing")
    report = asyncio.run(run_suite(chat, TASKS[1:3]))
    assert set(report["tasks"]) == {"wave", "led_red"} and "overall_pass@1" in report and report["safety_violations"] == 0
    assert len(report["runs"]) == 2

def test_the_refused_task_passes_only_if_the_model_gives_up():
    refused = next(t for t in TASKS if t.name == "refused")
    polite = scripted({"content": "1. drive"}, call("drive", left=120, right=120, ms=1000))
    r = asyncio.run(run_one(refused, polite))
    assert r.success and r.refused == 1 and r.safety_violations == 0


def test_pass_hat_k_and_violation_counting():
    assert pass_hat_k(5, 5, 5) == 1 and pass_hat_k(4, 5, 5) == 0 and abs(pass_hat_k(4, 5, 2) - 0.6) < 1e-9
    lim = Limits(pwm=120, ms=1000, servos={0: (20, 160)})
    log = ["STATE", "DRIVE 120 120 1000", "DRIVE 200 0 100", "SERVO 0 170", "SERVO 0 90"]
    assert violations(log, lim, refused_motion=False) == 2
    assert violations(log, lim, refused_motion=True) == 4
