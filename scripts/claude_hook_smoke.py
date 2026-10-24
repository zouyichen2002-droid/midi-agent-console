"""End-to-end smoke test with the REAL Claude Code CLI (needs `claude` logged in).

1. start a daemon without a keyboard (virtual keys on), e.g.
       python -m midi_agent_console --port none daemon --device sim --http-port 47831
2. note the key in the "/keys?k=..." line it prints
3. python scripts/claude_hook_smoke.py <virtual-key> allow     (then again with: deny)

It writes hooks into a temporary settings file (your ~/.claude/settings.json is not
touched), runs `claude -p` in a temporary folder asking it to run `echo`, waits until
the daemon reports a pending question, "presses" a right-half key (allow) or left-half
key (deny) through the virtual keyboard, and prints what Claude Code did.
Uses a tiny amount of your Claude usage (one short haiku session).
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from midi_agent_console.hooks import install, load_or_create_token

PORT = int(os.environ.get("MIDI_CONSOLE_PORT", "47831"))


def press(vkey: str, note: int, sid: str) -> None:
    for i, (st, vel) in enumerate(((0x90, 90), (0x80, 0)), 1):
        body = json.dumps({"sid": sid, "seq": i, "status": st, "d1": note, "d2": vel}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{PORT}/virtual", data=body,
            headers={"Content-Type": "application/json", "X-Virtual-Key": vkey}))


async def main(vkey: str, mode: str) -> int:
    tok = load_or_create_token()
    work = Path(tempfile.mkdtemp(prefix="mac-smoke-"))
    (work / "proj").mkdir()
    install(work / "settings.json", PORT, 70)
    prompt = "Use the Bash tool to run exactly this command: echo midi-hook-test-ok   Then reply with only its output."
    proc = subprocess.Popen(["claude", "-p", prompt, "--settings", str(work / "settings.json"), "--model", "haiku",
                             "--output-format", "json", "--max-turns", "4"], cwd=work / "proj",
                            env={**os.environ, "MIDI_AGENT_CONSOLE_TOKEN": tok},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {tok}"})
    pressed, t0 = None, time.monotonic()
    async with Client(streamable_http_client(f"http://127.0.0.1:{PORT}/mcp", http_client=http)) as cl:
        while proc.poll() is None and time.monotonic() - t0 < 150:
            st = (await cl.call_tool("console_status", {})).structured_content
            if st.get("waiting") and not pressed:
                await asyncio.sleep(0.5)
                note = 72 if mode == "allow" else 50
                press(vkey, note, f"smoke-{mode}-{int(t0)}")
                pressed = (st["waiting"], round(time.monotonic() - t0, 1))
            await asyncio.sleep(0.3)
    out, err = proc.communicate(timeout=60)
    print("question seen / pressed after s:", pressed)
    try:
        res = json.loads(out)
    except ValueError:
        print("claude output:", out[-800:], err[-400:])
        return 1
    text = str(res.get("result"))
    print("claude result:", text[:300])
    ok = pressed and (("midi-hook-test-ok" in text) if mode == "allow" else ("midi-hook-test-ok" not in text))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "allow")))
