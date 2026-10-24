"""CLI: ports | monitor | learn | serve"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .backends import list_inputs, open_input
from .console import Console
from .feedback import Feedback
from .hardware import open_device
from .profiles import get_profile


def _open(args, console: Console):
    match = args.port or console.profile.port_match
    usb_id = None if args.any_port else console.profile.usb_id
    inp = open_input(match, console.feed, usb_id=usb_id)
    where = getattr(inp, "interface", "") or "(no interface path)"
    print(f"[midi] listening on {inp.name!r} (profile: {console.profile.name})", file=sys.stderr)
    print(f"[midi]   device: {where}" + ("   identity NOT checked (--any-port)" if args.any_port else
                                       f"   identity checked: {usb_id}" if usb_id else ""), file=sys.stderr)
    return inp


def cmd_ports(_args) -> None:
    from .backends import describe_inputs

    for p in describe_inputs():
        kind = "USB device" if p["usb"] else "virtual / unknown"
        print(f"{p['index']}: {p['name']}  [{kind}]  {p['interface'] or '(no interface path)'}")


def cmd_monitor(args) -> None:
    console = Console(get_profile(args.profile))
    console.add_listener(lambda e: print(json.dumps(e.to_dict(), ensure_ascii=False), flush=True))
    inp = _open(args, console)
    try:
        time.sleep(args.seconds) if args.seconds else _forever()
    finally:
        inp.close()


def cmd_learn(args) -> None:
    """Touch one control at a time; prints what it sends, so profiles can be filled in from fact."""
    console = Console(get_profile(args.profile))
    inp = _open(args, console)
    if args.wizard:
        from .wizard import console_collector, run_wizard, save_profile

        try:
            result = run_wizard(console_collector(console, lambda s: print(s, file=sys.stderr, flush=True)),
                                name=inp.name, port_match=inp.name)
            import re

            m = re.search(r"vid_[0-9a-f]{4}&pid_[0-9a-f]{4}", getattr(inp, "interface", "") or "")
            result["profile"]["usb_id"] = m.group(0) if m else None
            if not m:
                result["profile"]["unverified"].append("no USB identity: port matched by name only")
        finally:
            inp.close()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.out:
            print(f"profile written to {save_profile(result, args.out)}  (use --profile {args.out})",
                  file=sys.stderr)
        return
    seen: dict[str, int] = {}

    def on(e):
        key = {"cc": f"cc:{e.control}", "pitchbend": "pitchbend"}.get(e.kind, e.kind)
        if key not in seen:
            print(f"new control: {key:12s} ch{e.channel}  first value {e.value if e.value is not None else e.note}",
                  flush=True)
        seen[key] = seen.get(key, 0) + 1

    console.add_listener(on)
    print("Move each strip, press Hold / octave / Chord buttons, a few keys. Ctrl+C to finish.",
          file=sys.stderr)
    try:
        time.sleep(args.seconds) if args.seconds else _forever()
    except KeyboardInterrupt:
        pass
    finally:
        inp.close()
        print(json.dumps({"seen": seen}, indent=2))


def cmd_serve(args) -> None:
    from .server import build_server

    feedback = Feedback(enabled=not args.quiet, backend=args.feedback)
    console = Console(get_profile(args.profile), feedback=feedback)
    try:
        inp = _open(args, console)
    except LookupError as exc:  # no keyboard: still useful, the asking tools become dialogs
        print(f"[midi] {exc}\n[midi] no keyboard: ask_confirm / ask_choice will use client dialogs",
              file=sys.stderr)
        inp = None
    device = open_device(args.device)
    print(f"[hw] device: {device.name}", file=sys.stderr)
    try:
        build_server(console, device, inp.name if inp else None, max_wait=args.max_wait).run("stdio")
    finally:
        if inp:
            inp.close()
        device.close()
        feedback.close()


def cmd_daemon(args) -> None:
    """One process owns the MIDI port and serves both MCP (streamable HTTP, /mcp) and the
    Claude Code permission hooks (/hook). A second daemon fails to bind the port and exits."""
    import asyncio

    import uvicorn

    from .audit import AuditLog
    from .hooks import BearerGuard, HookBridge, load_or_create_token, make_route, state_dir
    from .server import build_server
    from .virtual import VirtualKeys

    feedback = Feedback(enabled=not args.quiet, backend=args.feedback)
    console = Console(get_profile(args.profile), feedback=feedback)
    try:
        inp = _open(args, console)
    except LookupError as exc:
        print(f"[midi] {exc}\n[midi] no keyboard: use the virtual keys page below", file=sys.stderr)
        inp = None
    device = open_device(args.device)
    token = load_or_create_token()
    turn = asyncio.Lock()
    mcp = build_server(console, device, inp.name if inp else "virtual", turn=turn)
    audit = AuditLog(state_dir() / "audit.jsonl")
    bridge = HookBridge(console, timeout=args.hook_timeout, answer_questions=args.answer_questions, turn=turn,
                        force_ask_dangerous=not args.no_force_ask, audit=audit)
    mcp.custom_route("/hook", methods=["POST"])(make_route(bridge, token, args.http_port))
    vk = None
    if args.virtual_keys or inp is None:  # never alongside a real keyboard unless asked for
        vk = VirtualKeys(console, args.http_port)
        keys, virtual = vk.routes()
        mcp.custom_route("/keys", methods=["GET"])(keys)
        mcp.custom_route("/virtual", methods=["POST"])(virtual)
    from .simview import make_routes as make_sim_routes

    sim_page, sim_state = make_sim_routes(device, args.http_port)
    mcp.custom_route("/sim", methods=["GET"])(sim_page)
    mcp.custom_route("/sim/state", methods=["GET"])(sim_state)
    base = f"http://127.0.0.1:{args.http_port}"
    print(f"[daemon] MCP  {base}/mcp", file=sys.stderr)
    print(f"[daemon] hook {base}/hook   device: {device.name}", file=sys.stderr)
    if vk:
        print(f"[daemon] keys {vk.url}", file=sys.stderr)
        print("[daemon] !! virtual keys are ON: any program that can read this console or runs as you can "
              "press them. Fine for trying things; use a physical keyboard for real approvals.", file=sys.stderr)
    if hasattr(device, "pose"):
        print(f"[daemon] sim  {base}/sim    (watch the simulated car)", file=sys.stderr)
    print(f"[daemon] audit {audit.path}", file=sys.stderr)
    app = BearerGuard(mcp.streamable_http_app(host="127.0.0.1"), token, "/mcp")
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.http_port, log_level="warning")
    finally:
        if inp:
            inp.close()
        device.close()
        feedback.close()


def cmd_token(_args) -> None:
    from .hooks import TOKEN_ENV, load_or_create_token, state_dir

    tok = load_or_create_token()
    print(tok)
    print(f"# stored in {state_dir() / 'token'}; Claude Code reads it from the env var {TOKEN_ENV}:",
          file=sys.stderr)
    print(f"#   setx {TOKEN_ENV} <token>     (then restart Claude Code)", file=sys.stderr)


def cmd_hooks(args) -> None:
    from pathlib import Path

    from .hooks import install, uninstall

    path = Path(args.settings).expanduser()
    if args.action == "install":
        print("hooks added to", install(path, args.http_port, args.timeout, args.answer_questions))
    else:
        print("hooks removed from", uninstall(path, args.http_port))
    print("a timestamped .bak copy of the previous file sits next to it", file=sys.stderr)


def cmd_hook_relay(args) -> None:
    from .hooks import relay

    out = relay(sys.stdin.read(), args.http_port, args.dialect)
    if out:
        sys.stdout.write(out)
    sys.exit(0)  # always 0: a relay failure must never block the agent's own prompt


def cmd_teleop(args) -> None:
    """Drive a car from the keyboard with no agent at all (demo / testing)."""
    from .teleop import Teleop

    feedback = Feedback(enabled=not args.quiet, backend=args.feedback)
    console = Console(get_profile(args.profile), feedback=feedback)
    inp = _open(args, console)
    device = open_device(args.device)
    from .bindings import BindingManager

    BindingManager(console, device)  # so the e-stop chord sends ESTOP to the board
    t = Teleop(console, device, pwm_min=args.pwm_min)
    t.start()
    print(f"[teleop] {device.name}: hold right-half keys (white fwd, black rev), pitch strip steers; "
          "slam 3 left keys = e-stop; Ctrl+C quits", file=sys.stderr)
    try:
        while t.active:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        t.stop()
        inp.close()
        device.close()
        feedback.close()


def cmd_brain(args) -> None:
    """A model plans, the keyboard approves every motion, the device executes."""
    import asyncio
    import os
    from pathlib import Path

    from mcp import Client

    from .brain import openai_chat, run_task
    from .server import build_server

    key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    if args.api_key_file:
        key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
    extra = {"chat_template_kwargs": {"enable_thinking": False}} if args.no_thinking else None
    chat = openai_chat(args.base_url, args.model, key, extra=extra)
    feedback = Feedback(enabled=not args.quiet, backend=args.feedback)
    console = Console(get_profile(args.profile), feedback=feedback)
    inp = _open(args, console)
    device = open_device(args.device)
    mcp = build_server(console, device, inp.name)

    async def main():
        async with Client(mcp, raise_exceptions=True) as cl:
            out = await run_task(args.task, cl, chat, log=lambda s: print(s, file=sys.stderr, flush=True))
        print(json.dumps(out, ensure_ascii=False, indent=2))

    try:
        asyncio.run(main())
    finally:
        inp.close()
        device.close()
        feedback.close()


def cmd_audit(args) -> None:
    from pathlib import Path

    from .audit import verify
    from .hooks import state_dir

    path = Path(args.file) if args.file else state_dir() / "audit.jsonl"
    if not path.exists():
        print(f"no audit log at {path}")
        return
    ok, n, msg = verify(path)
    print(f"{path}: {n} entries, {msg}")
    sys.exit(0 if ok else 1)


def _forever():
    while True:
        time.sleep(3600)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="midi_agent_console")
    p.add_argument("--profile", default="microlab_mk3", help="microlab_mk3 | generic | path/to/profile.json")
    p.add_argument("--port", help="substring of the MIDI input name (default: from profile)")
    p.add_argument("--any-port", action="store_true",
                   help="accept a port by name only, skipping the USB identity check (loopMIDI tests)")
    p.add_argument("--feedback", choices=["auto", "tones", "midi"], default="auto",
                   help="tones: low-latency WASAPI/PlaySound (~80 ms); midi: GS wavetable synth (~280 ms)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="list MIDI inputs").set_defaults(fn=cmd_ports)
    for name, fn in (("monitor", cmd_monitor), ("learn", cmd_learn)):
        s = sub.add_parser(name)
        s.add_argument("--seconds", type=float, default=0)
        if name == "learn":
            s.add_argument("--wizard", action="store_true", help="guided: one control at a time, writes a profile")
            s.add_argument("--out", help="profile JSON to write (with --wizard)")
        s.set_defaults(fn=fn)
    s = sub.add_parser("serve", help="run the MCP server on stdio")
    s.add_argument("--device", default="sim", help="sim | COM5 | COM5@115200 | /dev/ttyUSB0")
    s.add_argument("--quiet", action="store_true", help="no audible feedback on the GS wavetable synth")
    s.add_argument("--max-wait", type=float, default=600,
                   help="cap on any wait for a key, seconds (use 10 behind a voice assistant)")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("daemon", help="own the MIDI port; serve MCP over HTTP and Claude Code hooks")
    s.add_argument("--device", default="sim")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--http-port", type=int, default=47811)
    s.add_argument("--hook-timeout", type=float, default=110,
                   help="seconds to wait for a key before falling back to the terminal prompt")
    s.add_argument("--answer-questions", action="store_true",
                   help="also answer AskUserQuestion with white keys (experimental)")
    s.add_argument("--virtual-keys", action="store_true",
                   help="serve the browser keyboard even when a physical keyboard is connected")
    s.add_argument("--no-force-ask", action="store_true",
                   help="do not force a prompt for dangerous calls in Claude Code auto mode")
    s.set_defaults(fn=cmd_daemon)
    s = sub.add_parser("teleop", help="drive a car from the keyboard, no agent")
    s.add_argument("--device", default="sim")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--pwm-min", type=int, default=90, help="lowest PWM that still turns your motors")
    s.set_defaults(fn=cmd_teleop)
    s = sub.add_parser("brain", help="a model plans a robot task; the keyboard approves each motion")
    s.add_argument("--task", required=True)
    s.add_argument("--base-url", default="http://127.0.0.1:8081/v1", help="OpenAI-compatible endpoint")
    s.add_argument("--model", default="local")
    s.add_argument("--api-key-file")
    s.add_argument("--api-key-env", default="", help="read the key from this environment variable")
    s.add_argument("--no-thinking", action="store_true", help="send enable_thinking=false (Qwen3 on llama.cpp)")
    s.add_argument("--device", default="sim")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_brain)
    s = sub.add_parser("audit", help="verify the hash chain of the decision log")
    s.add_argument("--file", default=None)
    s.set_defaults(fn=cmd_audit)
    sub.add_parser("token", help="print (and create) the hook token").set_defaults(fn=cmd_token)
    s = sub.add_parser("hook-relay", help="command hook: stdin JSON -> daemon /hook -> stdout (Codex)")
    s.add_argument("--dialect", choices=["claude", "codex", "dsh"], default="claude")
    s.add_argument("--http-port", type=int, default=47811)
    s.set_defaults(fn=cmd_hook_relay)
    s = sub.add_parser("hooks", help="add/remove our hooks in a Claude Code settings.json")
    s.add_argument("action", choices=["install", "uninstall"])
    s.add_argument("--settings", default="~/.claude/settings.json")
    s.add_argument("--http-port", type=int, default=47811)
    s.add_argument("--timeout", type=int, default=130)
    s.add_argument("--answer-questions", action="store_true")
    s.set_defaults(fn=cmd_hooks)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        pass
    except LookupError as exc:  # no / refused MIDI port: a message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
