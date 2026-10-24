"""Durable journal for brain runs (stdlib sqlite3, WAL, synchronous=FULL).

Robot steps are not idempotent (driving twice moves twice), and durable-execution
frameworks only promise at-least-once, so the rules here are explicit:

  * a step's state is written BEFORE the device command goes out ("sent") and after the
    reply ("acked") and the observation ("observed");
  * after a crash, a step left in sent / acked is UNKNOWN and is never replayed; an
    approval whose command was not yet sent is void (approvals do not survive a crash);
  * resuming means: take the device lease, STOP, read STATE, tell the model what is
    unknown, and ask the human again before any motion (IEC 60204-1: resetting a stop
    may allow a restart, never cause one).

A lease per device (holder + fence + expiry) keeps two runs from driving the same robot.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, task TEXT, device TEXT, status TEXT,
  plan TEXT, ended TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS events(run_id TEXT, seq INTEGER, kind TEXT, body TEXT, at REAL,
  PRIMARY KEY(run_id, seq));
CREATE TABLE IF NOT EXISTS steps(run_id TEXT, n INTEGER, tool TEXT, cmd TEXT,
  state TEXT CHECK(state IN ('proposed','approved','declined','sent','acked','observed','unknown','void')),
  answer TEXT, reply TEXT, observed TEXT, updated_at REAL, PRIMARY KEY(run_id, n));
CREATE TABLE IF NOT EXISTS leases(device TEXT PRIMARY KEY, holder TEXT, run_id TEXT,
  fence INTEGER NOT NULL DEFAULT 0, expires_at REAL);
"""


class LeaseLost(RuntimeError):
    pass


class Journal:
    def __init__(self, path: str | Path, holder: str | None = None, lease_s: float = 5.0):
        self.path = Path(path)
        self.holder = holder or f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_s = lease_s
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")  # survives power loss, not just a crash
        self.db.executescript(SCHEMA)

    def _tx(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(sql, args)
            self.db.execute("COMMIT")
            return cur
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ runs and events
    def new_run(self, task: str, device: str) -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        now = time.time()
        self._tx("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)", (run_id, task, device, "planning", None, None, now, now))
        return run_id

    def set_run(self, run_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._tx(f"UPDATE runs SET {cols}, updated_at=? WHERE run_id=?", (*fields.values(), time.time(), run_id))

    def run(self, run_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if r is None:
            return None
        return dict(zip([c[0] for c in self.db.execute("SELECT * FROM runs LIMIT 0").description], r))

    def runs(self, limit: int = 20) -> list[dict]:
        cur = self.db.execute("SELECT run_id, task, status, ended, updated_at FROM runs ORDER BY created_at DESC LIMIT ?",
                              (limit,))
        return [dict(zip(("run_id", "task", "status", "ended", "updated_at"), r)) for r in cur]

    def add_event(self, run_id: str, kind: str, body: Any) -> None:
        seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        self._tx("INSERT INTO events VALUES(?,?,?,?,?)", (run_id, seq, kind, json.dumps(body, ensure_ascii=False),
                                                         time.time()))

    def events(self, run_id: str, kind: str | None = None) -> list[Any]:
        q = "SELECT kind, body FROM events WHERE run_id=?" + (" AND kind=?" if kind else "") + " ORDER BY seq"
        return [json.loads(b) for _k, b in self.db.execute(q, (run_id, kind) if kind else (run_id,))]

    # ------------------------------------------------------------ steps
    def step(self, run_id: str, n: int, state: str, **fields: Any) -> None:
        now = time.time()
        if self.db.execute("SELECT 1 FROM steps WHERE run_id=? AND n=?", (run_id, n)).fetchone() is None:
            self._tx("INSERT INTO steps(run_id, n, tool, cmd, state, updated_at) VALUES(?,?,?,?,?,?)",
                     (run_id, n, fields.pop("tool", None), fields.pop("cmd", None), state, now))
        else:
            fields["state"] = state
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self._tx(f"UPDATE steps SET {cols}, updated_at=? WHERE run_id=? AND n=?",
                     (*fields.values(), now, run_id, n))

    def steps(self, run_id: str) -> list[dict]:
        cur = self.db.execute("SELECT n, tool, cmd, state, answer, reply, observed FROM steps WHERE run_id=? ORDER BY n",
                              (run_id,))
        return [dict(zip(("n", "tool", "cmd", "state", "answer", "reply", "observed"), r)) for r in cur]

    def recover(self, run_id: str) -> list[dict]:
        """After a crash: in-flight motions become unknown, unsent approvals void."""
        self._tx("UPDATE steps SET state='unknown', updated_at=? WHERE run_id=? AND state IN ('sent','acked')",
                 (time.time(), run_id))
        self._tx("UPDATE steps SET state='void', updated_at=? WHERE run_id=? AND state IN ('proposed','approved')",
                 (time.time(), run_id))
        return [s for s in self.steps(run_id) if s["state"] in ("unknown", "void")]

    # ------------------------------------------------------------ device lease
    def acquire(self, device: str, run_id: str) -> int:
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT holder, fence, expires_at FROM leases WHERE device=?", (device,)).fetchone()
            if row and row[0] != self.holder and row[2] > now:
                raise LeaseLost(f"{device} is held by run {row[0]} for another {row[2] - now:.1f} s")
            fence = (row[1] if row else 0) + 1
            self.db.execute("INSERT INTO leases VALUES(?,?,?,?,?) ON CONFLICT(device) DO UPDATE SET "
                            "holder=excluded.holder, run_id=excluded.run_id, fence=excluded.fence, "
                            "expires_at=excluded.expires_at", (device, self.holder, run_id, fence, now + self.lease_s))
            self.db.execute("COMMIT")
            return fence
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def renew(self, device: str, fence: int) -> None:
        cur = self._tx("UPDATE leases SET expires_at=? WHERE device=? AND holder=? AND fence=?",
                       (time.time() + self.lease_s, device, self.holder, fence))
        if cur.rowcount != 1:
            raise LeaseLost(f"lease on {device} was taken over")

    def release(self, device: str, fence: int) -> None:
        self._tx("UPDATE leases SET expires_at=0 WHERE device=? AND holder=? AND fence=?", (device, self.holder, fence))

    def close(self) -> None:
        self.db.close()
