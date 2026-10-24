"""Smart-home devices over MQTT, behind the keyboard.

Discovery comes from the two formats almost everything already speaks:
  * Home Assistant MQTT discovery (`homeassistant/<component>/[<node>/]<object>/config`
    and the per-device `homeassistant/device/<id>/config`); Tasmota, ESPHome and
    Zigbee2MQTT can all publish it;
  * Zigbee2MQTT's own `zigbee2mqtt/bridge/devices` (exposes with access bits and ranges).

Every entity has a tier, from its component unless the policy file says otherwise:
  read       sensors: the agent may read, never write
  control    lights, fans: a yes on the keyboard
  dangerous  locks, plugs, heating, covers, valves: a 1.5 s long-press
A device the human has not adopted (iot_adopt, a long-press) can be read but never set.
The approval text is built from the registry (id, component, payload), never from the
agent's words; names from discovery are external data and get cut and cleaned.

After a set the registry waits for the state topic to show the new value and says
`confirmed: false` (with the real state) if it does not. The emergency stop freezes all
writes and applies each entity's `estop` action: hold (default: lights, locks - an
automatic unlock or lock-in is its own hazard), off (heating), stop (covers). Do not put
"off" on a plug that might feed a fridge or the router. MQTT is best effort, so anything
that heats or moves should also switch itself off on the device (Tasmota PulseTime,
an ESPHome on-turn-on delay).

The broker should give the agent's account write access to the command topics only
(a mosquitto ACL), or it could forge discovery messages.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

Handler = Callable[[str, bytes, bool], None]  # topic, payload, retained

TIER_BY_COMPONENT = {"sensor": "read", "binary_sensor": "read", "light": "control", "fan": "control",
                     "switch": "dangerous", "lock": "dangerous", "climate": "dangerous",
                     "water_heater": "dangerous", "cover": "dangerous", "valve": "dangerous",
                     "alarm_control_panel": "dangerous"}
ESTOP_BY_COMPONENT = {"climate": "off", "water_heater": "off", "cover": "stop"}  # everything else: hold
TIERS = ("deny", "read", "control", "dangerous")

ABBR = {"~": "~", "stat_t": "state_topic", "cmd_t": "command_topic", "uniq_id": "unique_id",
        "obj_id": "object_id", "avty_t": "availability_topic", "avty": "availability", "dev": "device",
        "o": "origin", "dev_cla": "device_class", "val_tpl": "value_template", "unit_of_meas": "unit_of_measurement",
        "bri_cmd_t": "brightness_command_topic", "bri_stat_t": "brightness_state_topic", "bri_scl": "brightness_scale",
        "pl_on": "payload_on", "pl_off": "payload_off", "stat_on": "state_on", "stat_off": "state_off",
        "pl_lock": "payload_lock", "pl_unlk": "payload_unlock", "stat_locked": "state_locked",
        "stat_unlocked": "state_unlocked", "pl_open": "payload_open", "pl_cls": "payload_close",
        "pl_stop": "payload_stop", "pl_avail": "payload_available", "pl_not_avail": "payload_not_available",
        "t": "topic", "p": "platform", "cmps": "components"}


class Transport(Protocol):
    def subscribe(self, topic: str, handler: Handler) -> None: ...
    def publish(self, topic: str, payload: str, retain: bool = False) -> None: ...


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class MemoryBroker:
    """In-process broker for tests and demos: wildcards, retained messages, sync delivery."""

    def __init__(self):
        self.subs: list[tuple[str, Handler]] = []
        self.retained: dict[str, bytes] = {}
        self.published: list[tuple[str, str]] = []
        self._lock = threading.RLock()

    def subscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            self.subs.append((topic, handler))
            hits = [(t, p) for t, p in self.retained.items() if topic_matches(topic, t)]
        for t, p in hits:
            handler(t, p, True)

    def publish(self, topic: str, payload: str | bytes, retain: bool = False) -> None:
        data = payload.encode() if isinstance(payload, str) else payload
        with self._lock:
            self.published.append((topic, data.decode("utf-8", "replace")))
            if retain:
                if data:
                    self.retained[topic] = data
                else:
                    self.retained.pop(topic, None)
            subs = [h for pat, h in self.subs if topic_matches(pat, topic)]
        for h in subs:
            h(topic, data, False)


class PahoTransport:
    """paho-mqtt 2.x on its own network thread (pip install midi-agent-console[iot])."""

    def __init__(self, host: str, port: int = 1883, username: str | None = None, password: str | None = None,
                 client_id: str = "midi-agent-console"):
        import paho.mqtt.client as mqtt

        self.subs: list[tuple[str, Handler]] = []
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self.c.username_pw_set(username, password)
        self.c.on_connect = lambda c, u, flags, rc, props: [c.subscribe(t, qos=1) for t, _ in self.subs]
        self.c.on_message = self._on_message
        self.c.connect(host, port, keepalive=30)
        self.c.loop_start()

    def _on_message(self, c, u, msg) -> None:
        for pat, h in list(self.subs):
            if topic_matches(pat, msg.topic):
                h(msg.topic, msg.payload, bool(msg.retain))

    def subscribe(self, topic: str, handler: Handler) -> None:
        self.subs.append((topic, handler))
        self.c.subscribe(topic, qos=1)

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.c.publish(topic, payload, qos=1, retain=retain)

    def close(self) -> None:
        self.c.loop_stop()
        self.c.disconnect()


def clean_name(s: Any, n: int = 60) -> str:
    """Discovery names are written by whoever can publish to the broker: keep them short and inert."""
    s = re.sub(r"[\x00-\x1f\x7f`<>{}\[\]]", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


def _expand(cfg: dict[str, Any]) -> dict[str, Any]:
    out = {ABBR.get(k, k): v for k, v in cfg.items()}
    base = out.pop("~", None)
    if base:
        for k, v in out.items():
            if isinstance(v, str) and k.endswith("topic"):
                out[k] = (base + v[1:]) if v.startswith("~") else (v[:-1] + base) if v.endswith("~") else v
    return out


def _template(tpl: str | None, raw: str) -> Any:
    """Only `{{ value_json.x }}` / `{{ value_json['x'] }}`; Jinja is never evaluated."""
    if not tpl:
        return raw
    m = re.fullmatch(r"\{\{\s*value_json(?:\.(\w+)|\[['\"](\w+)['\"]\])\s*\}\}", tpl.strip())
    if not m:
        return raw
    try:
        return json.loads(raw).get(m.group(1) or m.group(2))
    except (ValueError, AttributeError):
        return raw


@dataclass
class Entity:
    id: str
    source: str  # "ha" | "z2m"
    component: str
    name: str
    device_class: str | None = None
    unit: str | None = None
    state_topic: str | None = None
    command_topic: str | None = None
    json_cmd: bool = False
    value_template: str | None = None
    avail_topic: str | None = None
    avail_on: str = "online"
    payloads: dict[str, str] = field(default_factory=dict)  # verb -> payload (ON/OFF/LOCK/...)
    states: dict[str, str] = field(default_factory=dict)  # verb -> state value that confirms it
    numbers: dict[str, tuple[float, float]] = field(default_factory=dict)  # writable numeric props
    enums: dict[str, list[str]] = field(default_factory=dict)  # writable enum props (z2m)
    # live
    state: Any = None
    state_at: float | None = None
    online: bool | None = None

    def describe(self, tier: str, adopted: bool, estop: str) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if v not in (None, {}, [], "") and k not in (
            "state_topic", "command_topic", "value_template", "avail_topic", "avail_on", "json_cmd", "state_at")}
        if "payloads" in d:
            d["payloads"] = {k: v for k, v in d["payloads"].items() if not k.startswith("_")}
        d.update(tier=tier, adopted=adopted, estop=estop, writable=bool(self.command_topic),
                 age_s=None if self.state_at is None else round(time.time() - self.state_at, 1))
        return d


def ha_entity(component: str, object_id: str, cfg: dict[str, Any]) -> Entity:
    c = _expand(cfg)
    av = c.get("availability")
    avail_topic = c.get("availability_topic") or (av[0].get("topic") if isinstance(av, list) and av else None)
    e = Entity(id=f"ha:{clean_name(c.get('unique_id') or object_id, 80)}", source="ha", component=component,
               name=clean_name(c.get("name") or object_id), device_class=c.get("device_class"),
               unit=c.get("unit_of_measurement"), state_topic=c.get("state_topic"),
               command_topic=c.get("command_topic"), json_cmd=c.get("schema") == "json",
               value_template=c.get("value_template"), avail_topic=avail_topic,
               avail_on=str(c.get("payload_available", "online")))
    if component in ("light", "switch", "fan"):
        on, off = str(c.get("payload_on", "ON")), str(c.get("payload_off", "OFF"))
        e.payloads = {"ON": on, "OFF": off}
        e.states = {"ON": str(c.get("state_on", "ON" if e.json_cmd else on)),
                    "OFF": str(c.get("state_off", "OFF" if e.json_cmd else off))}
        if component == "light" and (c.get("brightness") or c.get("brightness_command_topic")):
            e.numbers["brightness"] = (0, float(c.get("brightness_scale", 255)))
            e.payloads["_brightness_topic"] = c.get("brightness_command_topic") or ""
    elif component == "lock":
        e.payloads = {"LOCK": str(c.get("payload_lock", "LOCK")), "UNLOCK": str(c.get("payload_unlock", "UNLOCK"))}
        e.states = {"LOCK": str(c.get("state_locked", "LOCKED")), "UNLOCK": str(c.get("state_unlocked", "UNLOCKED"))}
    elif component == "cover":
        e.payloads = {"OPEN": str(c.get("payload_open", "OPEN")), "CLOSE": str(c.get("payload_close", "CLOSE")),
                      "STOP": str(c.get("payload_stop", "STOP"))}
        e.states = {"OPEN": "open", "CLOSE": "closed"}
    else:
        e.command_topic = None  # climate, valve, ...: readable here, not writable yet
    return e


def z2m_entities(devices: list[dict[str, Any]], base: str = "zigbee2mqtt") -> list[Entity]:
    out = []
    for d in devices if isinstance(devices, list) else []:
        if not isinstance(d, dict) or d.get("type") == "Coordinator" or not d.get("definition"):
            continue
        fn = str(d.get("friendly_name") or d.get("ieee_address") or "")
        flat, kinds = [], []
        for x in d["definition"].get("exposes") or []:
            if x.get("features"):
                kinds.append(x.get("type"))
                flat.extend(x["features"])
            else:
                flat.append(x)
        component = next((k for k in kinds if k in TIER_BY_COMPONENT), None)
        writable = [x for x in flat if int(x.get("access", 0)) & 2 and x.get("property")]
        component = component or ("switch" if writable else "sensor")
        e = Entity(id=f"z2m:{clean_name(fn, 80)}", source="z2m", component=component, name=clean_name(fn),
                   state_topic=f"{base}/{fn}", command_topic=f"{base}/{fn}/set" if writable else None,
                   json_cmd=True, avail_topic=f"{base}/{fn}/availability")
        for x in writable:
            prop, kind = x["property"], x.get("type")
            if kind == "binary":
                on, off = x.get("value_on", "ON"), x.get("value_off", "OFF")
                if prop == "state":
                    e.payloads.update({"ON": str(on), "OFF": str(off)} if component != "lock" else
                                      {"LOCK": str(on), "UNLOCK": str(off)})
                    e.states.update({k: v for k, v in e.payloads.items()})
                else:
                    e.enums[prop] = [str(on), str(off)]
            elif kind == "numeric":
                e.numbers[prop] = (float(x.get("value_min", 0)), float(x.get("value_max", 255)))
            elif kind == "enum" and x.get("values"):
                e.enums[prop] = [str(v) for v in x["values"]]
        out.append(e)
    return out


class Policy:
    """JSON file the human owns: {"entities": {"<id>": {"tier": ..., "estop": ..., "adopted": true}}}."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.data: dict[str, Any] = {"entities": {}}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.setdefault("entities", {})

    def of(self, e: Entity) -> dict[str, Any]:
        p = self.data["entities"].get(e.id, {})
        tier = p.get("tier") or TIER_BY_COMPONENT.get(e.component, "deny")
        return {"tier": tier if tier in TIERS else "deny", "adopted": bool(p.get("adopted")),
                "estop": p.get("estop") or ESTOP_BY_COMPONENT.get(e.component, "hold")}

    def adopt(self, e: Entity) -> None:
        self.data["entities"].setdefault(e.id, {})["adopted"] = True
        if self.path:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


class Registry:
    def __init__(self, transport: Transport, policy: Policy | None = None, prefix: str = "homeassistant",
                 z2m_base: str = "zigbee2mqtt"):
        self.t, self.policy = transport, policy or Policy()
        self.prefix, self.z2m_base = prefix, z2m_base
        self.entities: dict[str, Entity] = {}
        self._by_topic: dict[str, list[str]] = {}
        self._from_cfg: dict[str, list[str]] = {}  # discovery topic -> entity ids it created
        self._lock = threading.RLock()
        for pat in (f"{prefix}/+/+/config", f"{prefix}/+/+/+/config"):
            self.t.subscribe(pat, self._on_ha)
        self.t.subscribe(f"{z2m_base}/bridge/devices", self._on_z2m)

    # ------------------------------------------------------------ discovery
    def _on_ha(self, topic: str, payload: bytes, retained: bool) -> None:
        parts = topic.split("/")
        component, object_id = parts[1], parts[-2]
        if not payload:  # empty retained config = entity removed
            with self._lock:
                for k in self._from_cfg.pop(topic, []):
                    self.entities.pop(k, None)
            return
        try:
            cfg = json.loads(payload)
        except ValueError:
            return
        if not isinstance(cfg, dict):
            return
        if component == "device":  # one message, many components
            shared = {k: v for k, v in cfg.items() if k not in ("cmps", "components", "dev", "device", "o", "origin")}
            es = [ha_entity(sub.get("p") or sub.get("platform"), oid, {**shared, **sub})
                  for oid, sub in (cfg.get("cmps") or cfg.get("components") or {}).items()
                  if isinstance(sub, dict) and (sub.get("p") or sub.get("platform"))]
        else:
            es = [ha_entity(component, object_id, cfg)]
        with self._lock:
            self._from_cfg[topic] = [e.id for e in es]
        for e in es:
            self._add(e)

    def _on_z2m(self, topic: str, payload: bytes, retained: bool) -> None:
        try:
            devices = json.loads(payload or b"[]")
        except ValueError:
            return
        for e in z2m_entities(devices, self.z2m_base):
            self._add(e)

    def _add(self, e: Entity) -> None:
        with self._lock:
            old = self.entities.get(e.id)
            if old:
                e.state, e.state_at, e.online = old.state, old.state_at, old.online
            self.entities[e.id] = e
            for topic in (e.state_topic, e.avail_topic):
                if topic and e.id not in self._by_topic.get(topic, []):
                    first = topic not in self._by_topic
                    self._by_topic.setdefault(topic, []).append(e.id)
                    if first:
                        self.t.subscribe(topic, self._on_state)

    def _on_state(self, topic: str, payload: bytes, retained: bool) -> None:
        raw = payload.decode("utf-8", "replace")
        with self._lock:
            for eid in self._by_topic.get(topic, []):
                e = self.entities.get(eid)
                if e is None:
                    continue
                if topic == e.avail_topic and topic != e.state_topic:
                    try:
                        v = json.loads(raw)
                        raw_state = v.get("state") if isinstance(v, dict) else raw
                    except ValueError:
                        raw_state = raw
                    e.online = str(raw_state) == e.avail_on
                    continue
                e.state = _template(e.value_template, raw)
                if e.json_cmd and isinstance(e.state, str):
                    try:
                        e.state = json.loads(e.state)
                    except ValueError:
                        pass
                e.state_at = time.time()  # when WE got it: a retained state can be old

    # ------------------------------------------------------------ reading
    def get(self, eid: str) -> Entity:
        with self._lock:
            if eid not in self.entities:
                raise KeyError(f"no entity {eid!r}; see iot_list")
            return self.entities[eid]

    def list(self, text: str = "") -> list[dict[str, Any]]:
        with self._lock:
            es = sorted(self.entities.values(), key=lambda e: e.id)
        t = text.lower()
        return [e.describe(**self.policy.of(e)) for e in es
                if not t or t in e.id.lower() or t in e.name.lower() or t in e.component]

    # ------------------------------------------------------------ writing
    def plan(self, eid: str, payload: dict[str, Any]) -> tuple[Entity, list[tuple[str, str]], dict[str, Any], str]:
        """Validate a request -> (entity, [(topic, payload)], expected state, tier). Raises ValueError."""
        e = self.get(eid)
        pol = self.policy.of(e)
        if pol["tier"] in ("deny", "read") or not e.command_topic:
            raise ValueError(f"{eid} is not writable (tier={pol['tier']})")
        if not pol["adopted"]:
            raise ValueError(f"{eid} is not adopted yet: ask the human to adopt it (iot_adopt) first")
        if e.online is False:
            raise ValueError(f"{eid} is offline")
        if not isinstance(payload, dict) or not payload:
            raise ValueError('payload must be an object such as {"state": "ON"}')
        msg: dict[str, Any] = {}
        expect: dict[str, Any] = {}
        sends: list[tuple[str, str]] = []
        for k, v in payload.items():
            if k == "state":
                verb = str(v).upper()
                if verb not in e.payloads or verb.startswith("_"):
                    raise ValueError(f"state must be one of {[p for p in e.payloads if not p.startswith('_')]}")
                msg["state"] = e.payloads[verb]
                if verb in e.states:
                    expect["state"] = e.states[verb]
            elif k in e.numbers:
                lo, hi = e.numbers[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                    raise ValueError(f"{k} must be a number in {lo:g}..{hi:g}")
                msg[k] = int(v) if float(v).is_integer() else v
                expect[k] = msg[k]
            elif k in e.enums:
                if str(v) not in e.enums[k]:
                    raise ValueError(f"{k} must be one of {e.enums[k]}")
                msg[k] = str(v)
                expect[k] = msg[k]
            else:
                raise ValueError(f"{eid} has no writable {k!r}; has state{', ' if e.numbers or e.enums else ''}"
                                 f"{', '.join([*e.numbers, *e.enums])}")
        if e.json_cmd:
            sends.append((e.command_topic, json.dumps(msg)))
        else:
            if "state" in msg:
                sends.append((e.command_topic, msg["state"]))
            if "brightness" in msg:
                bt = e.payloads.get("_brightness_topic")
                if not bt:
                    raise ValueError(f"{eid} takes brightness only with the state in JSON")
                sends.append((bt, str(msg["brightness"])))
            expect = {"state": expect["state"]} if "state" in expect else {}
        return e, sends, expect, pol["tier"]

    def send(self, sends: list[tuple[str, str]]) -> None:
        for topic, p in sends:
            self.t.publish(topic, p)

    def confirmed(self, e: Entity, expect: dict[str, Any]) -> bool:
        s = e.state
        if not expect:
            return False
        if isinstance(s, dict):
            return all(str(s.get(k)) == str(v) for k, v in expect.items())
        return set(expect) == {"state"} and str(s) == str(expect["state"])

    def on_estop(self) -> None:
        """Runs on the MIDI thread; publish never blocks (paho queues it)."""
        with self._lock:
            es = list(self.entities.values())
        for e in es:
            pol = self.policy.of(e)
            if not pol["adopted"] or not e.command_topic:
                continue
            verb = {"off": "OFF", "stop": "STOP"}.get(pol["estop"])
            if verb and verb in e.payloads:
                p = json.dumps({"state": e.payloads[verb]}) if e.json_cmd else e.payloads[verb]
                try:
                    self.t.publish(e.command_topic, p)
                except Exception:
                    pass
