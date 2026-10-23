"""Claude Code hook bridge: answer permission prompts from the MIDI keyboard.

Claude Code POSTs hook payloads to the daemon (one process owns the MIDI port),
the daemon waits for a key, and replies in the hook's JSON format.

PermissionRequest
    normal    : key at/above middle C or Hold -> allow; key below -> deny
    dangerous : right-half long-press (1.5 s) -> allow; a tap does nothing; left key -> deny
    timeout   : no opinion (204) -> Claude Code shows its normal terminal prompt
    e-stop    : deny + interrupt
PreToolUse
    e-stop latched -> {"continue": false} + deny, so even auto-allowed tools stop
    AskUserQuestion (opt-in) -> white keys C..B answer options 1..7, one question at a time
Requests are served one at a time, oldest first; each new one re-arms the guard
(keys still held from the last answer never count).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .console import Choice, Confirm, Console
from .risk import classify, summarize

DEFAULT_PORT = 47811
TOKEN_ENV = "MIDI_AGENT_CONSOLE_TOKEN"


def state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/state")
    d = Path(base) / "midi-agent-console"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_or_create_token() -> str:
    f = state_dir() / "token"
    if f.exists():
        return f.read_text(encoding="ascii").strip()
    tok = secrets.token_hex(32)
    f.write_text(tok, encoding="ascii")
    return tok


class HookBridge:
    def __init__(self, console: Console, timeout: float = 110, answer_questions: bool = False,
                 turn: asyncio.Lock | None = None):
        self.console = console
        self.timeout = timeout
        self.answer_questions = answer_questions
        self._turn = turn or asyncio.Lock()  # shared with the MCP tools; wakes waiters FIFO
        self.served = 0

    async def handle(self, p: dict[str, Any]) -> dict[str, Any] | None:
        event = p.get("hook_event_name")
        tool, tin = p.get("tool_name", ""), p.get("tool_input") or {}
        if event == "PreToolUse":
            if self.console.estopped:
                return {"continue": False, "stopReason": "MIDI emergency stop is latched",
                        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": "MIDI emergency stop is latched"}}
            if tool == "AskUserQuestion" and self.answer_questions:
                return await self._questions(tin)
            return None
        if event != "PermissionRequest":
            return None
        if self.console.estopped:
            return _perm_deny("MIDI emergency stop is latched; the human must reset it", interrupt=True)

        tier, why = classify(tool, tin)
        async with self._turn:
            line = summarize(tool, tin)
            tag = f"  !! DANGEROUS ({why}): long-press right half to allow" if tier == "dangerous" else ""
            print(f"[hook] {p.get('cwd', '')}  {line}{tag}", file=sys.stderr, flush=True)
            r = await self.console.await_answer(Confirm(self.console.profile, tier == "dangerous"), self.timeout)
            self.served += 1
        if r is None:
            print("[hook]   -> no key pressed, falling back to the terminal prompt", file=sys.stderr)
            return None
        print(f"[hook]   -> {r['answer']} via {r.get('via')}", file=sys.stderr, flush=True)
        if r["answer"] == "yes":
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                           "decision": {"behavior": "allow"}}}
        if r["answer"] == "estop":
            return _perm_deny("Emergency stop on the MIDI keyboard", interrupt=True)
        return _perm_deny(f"Denied on the MIDI keyboard ({r.get('via')})")

    async def _questions(self, tin: dict[str, Any]) -> dict[str, Any] | None:
        qs = tin.get("questions") or []
        if not qs or any(q.get("multiSelect") or not 1 <= len(q.get("options") or []) <= 7 for q in qs):
            return None  # leave multi-select / long lists to the terminal
        answers = {}
        async with self._turn:
            for q in qs:
                labels = [o.get("label", "") for o in q["options"]]
                print(f"[hook] question: {q.get('question')}", file=sys.stderr)
                for k, lab in enumerate(labels, 1):
                    print(f"[hook]   {'CDEFGAB'[k - 1]} = {k}. {lab}", file=sys.stderr)
                r = await self.console.await_answer(Choice(len(labels)), self.timeout)
                if not r or r.get("answer") == "estop":
                    return None
                answers[q["question"]] = labels[r["choice"] - 1]
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "updatedInput": {"questions": qs, "answers": answers}}}


def _perm_deny(message: str, interrupt: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {"behavior": "deny", "message": message}
    if interrupt:
        d["interrupt"] = True
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}


# ------------------------------------------------------------------ HTTP endpoint
def make_route(bridge: HookBridge, token: str, port: int):
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def hook(request: Request) -> Response:
        if request.headers.get("host") not in allowed_hosts:  # DNS rebinding
            return Response(status_code=403)
        if request.headers.get("origin"):  # browsers send Origin; Claude Code does not
            return Response(status_code=403)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return Response(status_code=415)
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
            return Response(status_code=401)
        try:
            payload = json.loads(await request.body())
        except ValueError:
            return Response(status_code=400)
        out = await bridge.handle(payload)
        return Response(status_code=204) if out is None else JSONResponse(out)

    return hook


# ------------------------------------------------------------------ settings.json install
def hook_entries(port: int, timeout: int, questions: bool) -> dict[str, list]:
    def http(t: int) -> dict:
        return {"type": "http", "url": f"http://127.0.0.1:{port}/hook", "timeout": t,
                "headers": {"Authorization": f"Bearer ${TOKEN_ENV}"}, "allowedEnvVars": [TOKEN_ENV]}

    entries = {"PermissionRequest": [{"matcher": "*", "hooks": [http(timeout)]}],
               "PreToolUse": [{"matcher": "*", "hooks": [http(5)]}]}
    if questions:
        entries["PreToolUse"].append({"matcher": "AskUserQuestion", "hooks": [http(timeout)]})
    return entries


def _ours(group: dict, port: int) -> bool:
    return any(h.get("url", "").startswith(f"http://127.0.0.1:{port}/hook") for h in group.get("hooks", []))


def install(settings: Path, port: int = DEFAULT_PORT, timeout: int = 130, questions: bool = False) -> str:
    """Add our hooks to a Claude Code settings file (backup first, idempotent)."""
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    if settings.exists():
        backup = settings.with_suffix(settings.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(settings, backup)
    hooks = data.setdefault("hooks", {})
    for event, groups in hook_entries(port, timeout, questions).items():
        kept = [g for g in hooks.get(event, []) if not _ours(g, port)]
        hooks[event] = kept + groups
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(settings)


def uninstall(settings: Path, port: int = DEFAULT_PORT) -> str:
    data = json.loads(settings.read_text(encoding="utf-8"))
    shutil.copy2(settings, settings.with_suffix(settings.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    hooks = data.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not _ours(g, port)]
        if not hooks[event]:
            del hooks[event]
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(settings)
