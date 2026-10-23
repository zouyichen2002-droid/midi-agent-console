"""CLI: ports | monitor | learn | serve"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .backends import list_inputs, open_input
from .console import Console
from .hardware import open_device
from .profiles import get_profile


def _open(args, console: Console):
    match = args.port or console.profile.port_match
    inp = open_input(match, console.feed)
    print(f"[midi] listening on {inp.name!r} (profile: {console.profile.name})", file=sys.stderr)
    return inp


def cmd_ports(_args) -> None:
    for i, n in enumerate(list_inputs()):
        print(f"{i}: {n}")


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

    console = Console(get_profile(args.profile))
    inp = _open(args, console)
    device = open_device(args.device)
    print(f"[hw] device: {device.name}", file=sys.stderr)
    try:
        build_server(console, device, inp.name).run("stdio")
    finally:
        inp.close()
        device.close()


def _forever():
    while True:
        time.sleep(3600)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="midi_agent_console")
    p.add_argument("--profile", default="microlab_mk3", help="microlab_mk3 | generic | path/to/profile.json")
    p.add_argument("--port", help="substring of the MIDI input name (default: from profile)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="list MIDI inputs").set_defaults(fn=cmd_ports)
    for name, fn in (("monitor", cmd_monitor), ("learn", cmd_learn)):
        s = sub.add_parser(name)
        s.add_argument("--seconds", type=float, default=0)
        s.set_defaults(fn=fn)
    s = sub.add_parser("serve", help="run the MCP server on stdio")
    s.add_argument("--device", default="sim", help="sim | COM5 | COM5@115200 | /dev/ttyUSB0")
    s.set_defaults(fn=cmd_serve)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
