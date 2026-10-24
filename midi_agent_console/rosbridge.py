"""Drive a ROS 2 robot (turtlesim, TurtleBot3, ros2_control diff-drive, Gazebo) through
rosbridge_server's websocket JSON protocol — no ROS install on this machine.

    --device ws://robot:9090#tb3-jazzy      (profiles: turtlesim, tb3-humble, tb3-jazzy)

DRIVE l r ms becomes a Twist on /cmd_vel (differential mix), published at once and then
re-sent at 20 Hz until ms runs out, followed by three zero Twists. STOP / ESTOP publish
zero immediately; ESTOP latches until RESET. STATE comes from /odom or turtlesim's pose.

Safety note: we cannot rely on the robot to stop by itself. turtlesim zeroes after 1 s
and ros2_control's diff_drive_controller after cmd_vel_timeout (0.5 s), but a real
TurtleBot3 (OpenCR firmware) and Gazebo's DiffDrive keep the last velocity. So every
DRIVE is bounded (<= max_ms) and ends with zeros sent from here; if the websocket dies
mid-drive, a robot without its own timeout keeps going — test that before trusting it.
Needs the optional dependency `websocket-client` (pip install -e ".[ros]").
"""
from __future__ import annotations

import itertools
import json
import math
import threading
import time
from typing import Any

from .hardware import DEFAULT_CAPS, DeviceError, validate

PROFILES: dict[str, dict[str, Any]] = {
    # max_lin m/s, max_ang rad/s, track m, cmd_vel type, state topic
    "turtlesim": dict(cmd="/turtle1/cmd_vel", stamped=False, max_lin=2.0, max_ang=2.0, track=1.0,
                      state=("/turtle1/pose", "turtlesim/msg/Pose")),
    "tb3-humble": dict(cmd="/cmd_vel", stamped=False, max_lin=0.22, max_ang=2.84, track=0.160,
                       state=("/odom", "nav_msgs/msg/Odometry")),
    "tb3-jazzy": dict(cmd="/cmd_vel", stamped=True, max_lin=0.22, max_ang=2.84, track=0.160,
                      state=("/odom", "nav_msgs/msg/Odometry")),
}


def _yaw(q: dict) -> float:
    return math.atan2(2 * (q["w"] * q["z"] + q["x"] * q["y"]), 1 - 2 * (q["y"] ** 2 + q["z"] ** 2))


class RosbridgeDevice:
    def __init__(self, url: str = "ws://127.0.0.1:9090", profile: str = "turtlesim",
                 rate_hz: float = 20.0, connect_timeout: float = 3.0, **over: Any):
        import websocket  # websocket-client, optional dependency

        if profile not in PROFILES:
            raise ValueError(f"unknown ROS profile {profile!r}; have {sorted(PROFILES)}")
        self._wsmod = websocket
        p = {**PROFILES[profile], **over}
        self.p, self.name = p, f"rosbridge:{url}#{profile}"
        self.caps = json.loads(json.dumps(DEFAULT_CAPS))
        self.caps.update(fw="rosbridge", servos=[], leds=[], max_rate_hz=int(rate_hz))
        self.type = "geometry_msgs/msg/TwistStamped" if p["stamped"] else "geometry_msgs/msg/Twist"
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[str, list] = {}
        self.odom: dict[str, float] = {}
        self.odom_t = 0.0
        self.errors: list[str] = []
        self.connected = True
        self.target, self.deadline, self.estopped, self._zeros = (0.0, 0.0), 0.0, False, 0
        self._stop = threading.Event()
        self.ws = websocket.create_connection(url, timeout=connect_timeout, enable_multithread=True)
        self.ws.settimeout(0.5)
        self._tx({"op": "advertise", "topic": p["cmd"], "type": self.type})
        topic, typ = p["state"]
        self._tx({"op": "subscribe", "topic": topic, "type": typ, "throttle_rate": 100, "queue_length": 1})
        threading.Thread(target=self._reader, daemon=True, name="rb-reader").start()
        threading.Thread(target=self._pump, args=(1.0 / rate_hz,), daemon=True, name="rb-pump").start()

    # ------------------------------------------------------------ wire
    def _tx(self, msg: dict) -> None:
        try:
            self.ws.send(json.dumps(msg, separators=(",", ":")))
        except Exception as exc:
            self.connected = False
            raise DeviceError(f"rosbridge send failed: {exc}") from exc

    def _twist(self, v: float, w: float) -> dict:
        t = {"linear": {"x": v, "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": w}}
        if not self.p["stamped"]:
            return t
        # stamp 0: diff_drive_controller replaces a zero stamp with its own now() -> no clock sync needed
        return {"header": {"stamp": {"sec": 0, "nanosec": 0}, "frame_id": ""}, "twist": t}

    def _publish(self, v: float, w: float) -> None:
        self._tx({"op": "publish", "topic": self.p["cmd"], "msg": self._twist(v, w)})

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self.ws.recv()
            except self._wsmod.WebSocketTimeoutException:
                continue
            except Exception as exc:
                self.connected = False
                self.errors.append(f"link lost: {exc}")
                return
            if not raw:  # close frame
                self.connected = False
                return
            try:
                m = json.loads(raw)
                op = m.get("op")
                if op == "publish" and m.get("topic") == self.p["state"][0]:
                    self._on_state(m["msg"])
                elif op == "service_response" and m.get("id") in self._pending:
                    slot = self._pending.pop(m["id"])
                    slot[1] = m
                    slot[0].set()
                elif op == "status":
                    self.errors.append(f"{m.get('level')}: {m.get('msg')}")
            except (ValueError, KeyError, TypeError) as exc:  # a malformed message must not kill the reader
                self.errors.append(f"bad message: {exc!r}")
            del self.errors[:-20]

    def _on_state(self, msg: dict) -> None:
        if "pose" in msg and "twist" in msg:  # nav_msgs/Odometry
            pos, q = msg["pose"]["pose"]["position"], msg["pose"]["pose"]["orientation"]
            tw = msg["twist"]["twist"]
            odom = dict(x=pos["x"], y=pos["y"], yaw=_yaw(q), v=tw["linear"]["x"], w=tw["angular"]["z"])
        else:  # turtlesim/Pose
            odom = dict(x=msg["x"], y=msg["y"], yaw=msg["theta"],
                        v=msg["linear_velocity"], w=msg["angular_velocity"])
        with self._lock:
            self.odom, self.odom_t = odom, time.monotonic()

    def _pump(self, period: float) -> None:
        nxt = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                now = time.monotonic()
                if not self.estopped and now < self.deadline:
                    cmd = self.target
                elif self._zeros > 0:  # a few trailing zeros after every drive / stop
                    self._zeros, cmd = self._zeros - 1, (0.0, 0.0)
                else:
                    cmd = None
                if cmd is not None:
                    try:
                        self._publish(*cmd)
                    except DeviceError:
                        pass
            nxt += period
            self._stop.wait(max(0.0, nxt - time.monotonic()))

    # ------------------------------------------------------------ mapping
    def drive_to_twist(self, left: int, right: int) -> tuple[float, float]:
        m = self.caps["drive"]["max"]
        vl, vr = left / m * self.p["max_lin"], right / m * self.p["max_lin"]
        v = (vl + vr) / 2
        w = max(-self.p["max_ang"], min(self.p["max_ang"], (vr - vl) / self.p["track"]))
        return round(v, 4), round(w, 4)

    # ------------------------------------------------------------ Device protocol
    def send(self, command: str) -> str:
        cmd = validate(command, self.caps)  # SERVO / LED fail here: caps lists none
        op, *a = cmd.split()
        with self._lock:
            if op in ("STOP", "ESTOP"):
                self.deadline, self._zeros = 0.0, 3
                self.estopped = self.estopped or op == "ESTOP"
                self._publish(0.0, 0.0)  # now, not on the next tick
                return "OK"
            if op == "RESET":
                self.estopped = False
                return "OK"
            if op == "HB":
                return f"OK HB st={self._st()}"
            if op == "PING":
                return "OK PONG"
            if op == "HELLO":
                return f"OK HELLO proto=2 fw=rosbridge hb_ms={self.caps['hb_timeout_ms']}"
            if op == "DESCRIBE":
                return "OK " + json.dumps(self.caps, separators=(",", ":"))
            if op == "STATE":
                return "OK STATE " + self._state_line()
            if self.estopped:
                return "ERR ESTOP"
            left, right, ms = map(int, a)  # DRIVE (validate allows nothing else here)
            self.target = self.drive_to_twist(left, right)
            self.deadline, self._zeros = time.monotonic() + ms / 1000, 3
            self._publish(*self.target)
            return "OK"

    def call_service(self, service: str, srv_type: str, args: dict | None = None, timeout: float = 3.0) -> dict:
        rid = f"svc{next(self._ids)}"
        slot = [threading.Event(), None]
        self._pending[rid] = slot
        self._tx({"op": "call_service", "id": rid, "service": service, "type": srv_type, "args": args or {}})
        if not slot[0].wait(timeout):
            self._pending.pop(rid, None)
            raise DeviceError(f"no service_response for {service} within {timeout}s")
        return slot[1]

    def _st(self) -> str:
        return "ESTOP" if self.estopped else "RUN" if self.connected else "LINK_LOST"

    def _state_line(self) -> str:
        ms_left = max(0, int((self.deadline - time.monotonic()) * 1000))
        v, w = self.target if ms_left else (0.0, 0.0)
        o = self.odom
        age = int((time.monotonic() - self.odom_t) * 1000) if self.odom_t else -1
        pose = (f"pose={o['x']:.3f},{o['y']:.3f},{math.degrees(o['yaw']):.1f} vel={o['v']:.3f},{o['w']:.3f}"
                if o else "pose=none")
        return f"st={self._st()} drv_cmd={v},{w},{ms_left} {pose} pose_age_ms={age}"

    def close(self) -> None:
        try:
            with self._lock:
                self.deadline = 0.0
                self._publish(0.0, 0.0)
            self._tx({"op": "unadvertise", "topic": self.p["cmd"]})
            self._tx({"op": "unsubscribe", "topic": self.p["state"][0]})
        except DeviceError:
            pass
        self._stop.set()
        try:
            self.ws.close()
        except Exception:
            pass
