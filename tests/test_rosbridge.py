"""RosbridgeDevice against a fake rosbridge server (records every op, answers like turtlesim)."""
import asyncio
import json
import threading
import time

import pytest

websockets = pytest.importorskip("websockets")
pytest.importorskip("websocket")

from midi_agent_console.hardware import DeviceError, open_device  # noqa: E402


class FakeRosbridge:
    def __init__(self):
        self.ops: list[dict] = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def handler(ws):
            async for raw in ws:
                m = json.loads(raw)
                self.ops.append(m)
                if m["op"] == "subscribe":
                    await ws.send(json.dumps({"op": "publish", "topic": m["topic"], "msg": {
                        "x": 5.5, "y": 5.5, "theta": 0.5, "linear_velocity": 0.0, "angular_velocity": 0.0}}))
                    await ws.send("{not json")  # a malformed frame must not kill the reader
                elif m["op"] == "call_service":
                    await ws.send(json.dumps({"op": "service_response", "id": m["id"], "service": m["service"],
                                              "values": {}, "result": True}))

        async def main():
            self.server = await websockets.serve(handler, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            self.ready.set()
            await asyncio.Future()

        self.loop.run_until_complete(main())

    def publishes(self):
        return [o["msg"] for o in self.ops if o["op"] == "publish"]

    def wait(self, pred, timeout=3.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return True
            time.sleep(0.01)
        return False


@pytest.fixture
def fake():
    return FakeRosbridge()


def test_drive_publishes_bounded_twists_then_zeros(fake):
    dev = open_device(f"ws://127.0.0.1:{fake.port}#turtlesim")
    try:
        assert fake.wait(lambda: [o["op"] for o in fake.ops][:2] == ["advertise", "subscribe"])
        assert fake.wait(lambda: dev.odom.get("x") == 5.5)
        assert dev.send("DRIVE 180 180 500") == "OK"
        assert fake.wait(lambda: sum(1 for m in fake.publishes() if m["linear"]["x"] == 0) >= 3, 2)
        moving = [m for m in fake.publishes() if m["linear"]["x"] > 0]
        assert 8 <= len(moving) <= 13 and moving[0]["linear"]["x"] == 2.0  # 20 Hz for 0.5 s, full speed
        n = len(fake.publishes())
        time.sleep(0.3)
        assert len(fake.publishes()) == n  # nothing more once the zeros are out
        assert "pose=5.500,5.500,28.6" in dev.send("STATE") and "st=RUN" in dev.send("STATE")
        assert dev.errors and "bad message" in dev.errors[0]
    finally:
        dev.close()
    assert fake.wait(lambda: fake.ops[-1]["op"] == "unsubscribe")


def test_stop_is_immediate_and_estop_latches(fake):
    dev = open_device(f"ws://127.0.0.1:{fake.port}#tb3-jazzy")
    try:
        dev.send("DRIVE 100 -100 2000")
        assert fake.wait(lambda: len(fake.publishes()) >= 2)
        first = fake.publishes()[0]
        assert "header" in first and first["twist"]["angular"]["z"] < 0  # TwistStamped, turning right
        t0 = time.monotonic()
        assert dev.send("ESTOP") == "OK"
        assert fake.wait(lambda: fake.publishes()[-1]["twist"]["linear"]["x"] == 0, 0.2)
        assert time.monotonic() - t0 < 0.2
        assert dev.send("DRIVE 100 100 100") == "ERR ESTOP"
        with pytest.raises(ValueError):
            dev.send("SERVO 0 90")  # no servos on a ROS base
        assert dev.send("RESET") == "OK" and dev.send("DRIVE 50 50 100") == "OK"
        assert dev.call_service("/reset", "std_srvs/srv/Empty")["result"] is True
    finally:
        dev.close()


def test_unknown_profile_and_dead_server():
    with pytest.raises(ValueError):
        open_device("ws://127.0.0.1:1#nope")
    with pytest.raises(Exception):
        open_device("ws://127.0.0.1:1#turtlesim")  # connection refused


def test_twist_mapping():
    from midi_agent_console.rosbridge import PROFILES, RosbridgeDevice

    d = RosbridgeDevice.__new__(RosbridgeDevice)
    d.p, d.caps = PROFILES["tb3-humble"], {"drive": {"max": 180}}
    assert d.drive_to_twist(180, 180) == (0.22, 0.0)
    v, w = d.drive_to_twist(180, -180)
    assert v == 0 and w == -2.75  # (-0.22 - 0.22) / 0.16
    assert d.drive_to_twist(-180, 180)[1] == 2.75
