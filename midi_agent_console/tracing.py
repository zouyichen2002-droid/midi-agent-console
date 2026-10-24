"""Tiny span tracer with no dependencies: one OTLP/JSON line per finished span.

Each line is an ExportTraceServiceRequest, so a file can be replayed into any OTLP/HTTP
collector (Jaeger v2, Phoenix, otel-tui) with `POST /v1/traces` line by line, or read
as plain JSON. Names follow the OpenTelemetry GenAI conventions where they exist
(invoke_agent / chat / execute_tool, gen_ai.*); the human-approval parts have no
standard yet, so they live under `mac.*` (approval decision, audit hash, key latency).

If TRACEPARENT is set (Claude Code passes it to the commands it runs), the first span
joins that trace.
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_current: contextvars.ContextVar["Span | None"] = contextvars.ContextVar("mac_span", default=None)
KINDS = {"internal": 1, "server": 2, "client": 3}


def _value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _attrs(d: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _value(v)} for k, v in d.items() if v is not None]


def _parent_from_env() -> tuple[str, str] | None:
    tp = os.environ.get("TRACEPARENT", "")
    parts = tp.split("-")
    if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
        try:
            int(parts[1], 16), int(parts[2], 16)
        except ValueError:
            return None
        return parts[1], parts[2]
    return None


class Span:
    def __init__(self, name: str, kind: str, trace_id: str, parent_id: str | None, attrs: dict[str, Any]):
        self.name, self.kind = name, kind
        self.trace_id, self.parent_id = trace_id, parent_id
        self.span_id = os.urandom(8).hex()
        self.attrs = dict(attrs)
        self.events: list[dict[str, Any]] = []
        self.status: dict[str, Any] = {}
        self.start = time.time_ns()
        self.end = 0

    def set(self, **attrs: Any) -> "Span":
        self.attrs.update(attrs)
        return self

    def event(self, name: str, **attrs: Any) -> None:
        self.events.append({"timeUnixNano": str(time.time_ns()), "name": name, "attributes": _attrs(attrs)})

    def error(self, kind: str, message: str = "") -> None:
        self.attrs["error.type"] = kind
        self.status = {"code": 2, "message": message}

    def otlp(self) -> dict[str, Any]:
        s = {"traceId": self.trace_id, "spanId": self.span_id, "name": self.name, "kind": KINDS[self.kind],
             "startTimeUnixNano": str(self.start), "endTimeUnixNano": str(self.end),
             "attributes": _attrs(self.attrs), "events": self.events}
        if self.parent_id:
            s["parentSpanId"] = self.parent_id
        if self.status:
            s["status"] = self.status
        return s


class Tracer:
    """path=None keeps spans in memory only (`finished`), which tests and evals read."""

    def __init__(self, path: str | Path | None = None, service: str = "midi-agent-console", keep: int = 2000):
        self.path = Path(path) if path else None
        self.service = service
        self.finished: list[Span] = []
        self.keep = keep
        self._lock = threading.Lock()

    @contextmanager
    def span(self, name: str, kind: str = "internal", **attrs: Any) -> Iterator[Span]:
        parent = _current.get()
        if parent is not None:
            trace_id, parent_id = parent.trace_id, parent.span_id
        else:
            trace_id, parent_id = _parent_from_env() or (os.urandom(16).hex(), None)
        sp = Span(name, kind, trace_id, parent_id, attrs)
        token = _current.set(sp)
        try:
            yield sp
        except BaseException as exc:
            if not sp.status:
                sp.error(type(exc).__name__, str(exc)[:200])
            raise
        finally:
            _current.reset(token)
            sp.end = time.time_ns()
            self._emit(sp)

    def _emit(self, sp: Span) -> None:
        with self._lock:
            self.finished.append(sp)
            del self.finished[:-self.keep]
            if self.path:
                line = {"resourceSpans": [{"resource": {"attributes": _attrs({"service.name": self.service})},
                                           "scopeSpans": [{"scope": {"name": "midi_agent_console"},
                                                           "spans": [sp.otlp()]}]}]}
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")


def current() -> Span | None:
    return _current.get()
