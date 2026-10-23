"""Append-only, hash-chained record of every decision made on the keyboard.

Each line is JSON with `prev` (the previous line's hash) and `hash` =
sha256(prev + canonical JSON of the entry), so a deleted or edited line breaks
the chain from that point on. `verify()` checks a whole file."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _digest(prev: str, entry: dict[str, Any]) -> str:
    body = json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._prev = GENESIS
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._prev = json.loads(line).get("hash", self._prev)

    def record(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            entry = {"ts": round(time.time(), 3), **{k: v for k, v in fields.items() if v is not None}}
            entry["prev"] = self._prev
            entry["hash"] = _digest(self._prev, {k: v for k, v in entry.items() if k != "hash"})
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._prev = entry["hash"]
            return entry


def verify(path: Path) -> tuple[bool, int, str]:
    """-> (ok, lines checked, message)."""
    prev, n = GENESIS, 0
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        e = json.loads(line)
        if e.get("prev") != prev:
            return False, n, f"line {n}: chain broken (prev does not match)"
        if _digest(prev, {k: v for k, v in e.items() if k != "hash"}) != e.get("hash"):
            return False, n, f"line {n}: content was changed"
        prev = e["hash"]
    return True, n, "ok"
