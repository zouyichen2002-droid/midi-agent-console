"""MIDI input backends.

Windows: talks to winmm directly through ctypes, so it works on any Python
version without compiling anything (python-rtmidi has no wheels past cp312).
Elsewhere: falls back to mido if installed (rtmidi2 / python-rtmidi underneath).

The winmm callback runs on a driver thread where calling other multimedia
functions can deadlock, so it only enqueues; a dispatcher thread decodes and
runs the user callback (which may play feedback sounds or write to serial).
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Callable, Protocol

from .events import MidiEvent, decode, decode_packed

EventCallback = Callable[[MidiEvent], None]


class MidiInput(Protocol):
    name: str

    def close(self) -> None: ...


class _Dispatcher:
    """Runs callbacks off the driver thread, in arrival order."""

    def __init__(self, callback: EventCallback, name: str):
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._callback = callback
        self._thread = threading.Thread(target=self._run, name=f"midi-dispatch-{name}", daemon=True)
        self._thread.start()

    def put(self, ev: MidiEvent | None) -> None:
        self._q.put(ev)

    def _run(self) -> None:
        while True:
            ev = self._q.get()
            if ev is None:
                return
            try:
                self._callback(ev)
            except Exception as exc:
                print(f"[midi] callback error: {exc!r}", file=sys.stderr)

    def stop(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=1)


# ---------------------------------------------------------------- winmm (Windows)
if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    _winmm = ctypes.WinDLL("winmm")

    class _MIDIINCAPSW(ctypes.Structure):
        _fields_ = [("wMid", wt.WORD), ("wPid", wt.WORD), ("vDriverVersion", wt.UINT),
                    ("szPname", wt.WCHAR * 32), ("dwSupport", wt.DWORD)]

    _MidiInProc = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.UINT, ctypes.c_size_t,
                                     ctypes.c_size_t, ctypes.c_size_t)
    _MIM_DATA = 0x3C3
    _CALLBACK_FUNCTION = 0x30000

    _winmm.midiInMessage.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_size_t, ctypes.c_size_t]
    _winmm.midiInMessage.restype = wt.UINT
    _DRV_QUERYDEVICEINTERFACE, _DRV_QUERYDEVICEINTERFACESIZE = 0x80C, 0x80D

    def _interface_path(dev_id: int) -> str:
        r"""The driver's device interface path for input `dev_id`, lower-cased, without opening
        it. Real USB ports look like \\?\usb#vid_1c75&pid_020c&mi_01#...; virtual drivers such
        as loopMIDI report \\?\root#media#...; empty if the driver does not say."""
        size = wt.ULONG(0)
        if _winmm.midiInMessage(ctypes.c_void_p(dev_id), _DRV_QUERYDEVICEINTERFACESIZE,
                                ctypes.addressof(size), 0) or size.value == 0:
            return ""
        buf = ctypes.create_unicode_buffer(size.value // 2 + 1)
        if _winmm.midiInMessage(ctypes.c_void_p(dev_id), _DRV_QUERYDEVICEINTERFACE,
                                ctypes.addressof(buf), size.value):
            return ""
        return buf.value.lower()

    def _winmm_inputs() -> list[str]:
        names = []
        for i in range(_winmm.midiInGetNumDevs()):
            caps = _MIDIINCAPSW()
            _winmm.midiInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
            names.append(caps.szPname)
        return names

    class WinMMInput:
        def __init__(self, index: int, name: str, callback: EventCallback):
            self.name = name
            self._handle = wt.HANDLE()
            self._dispatch = _Dispatcher(callback, name)
            put = self._dispatch.put

            def proc(_h, wmsg, _inst, p1, _p2):
                if wmsg == _MIM_DATA:  # driver thread: decode + enqueue only
                    put(decode_packed(p1, time.monotonic()))

            self._proc = _MidiInProc(proc)  # keep a reference, or winmm calls freed memory
            rc = _winmm.midiInOpen(ctypes.byref(self._handle), index, self._proc, 0,
                                   _CALLBACK_FUNCTION)
            if rc:
                self._dispatch.stop()
                raise OSError(f"midiInOpen({name!r}) failed, MMRESULT={rc}. Another program may hold "
                              "the port; on Windows 11 with Windows MIDI Services ports are multi-client.")
            _winmm.midiInStart(self._handle)

        def close(self) -> None:
            if self._handle:
                _winmm.midiInStop(self._handle)
                _winmm.midiInReset(self._handle)
                _winmm.midiInClose(self._handle)
                self._handle = wt.HANDLE()
                self._dispatch.stop()


# ---------------------------------------------------------------- mido (fallback)
class MidoInput:
    def __init__(self, name: str, callback: EventCallback):
        import mido

        self.name = name
        self._dispatch = _Dispatcher(callback, name)

        def on_msg(msg):
            data = msg.bytes()
            if len(data) <= 3:
                self._dispatch.put(decode(*(data + [0, 0])[:3]))

        self._port = mido.open_input(name, callback=on_msg)

    def close(self) -> None:
        self._port.close()
        self._dispatch.stop()


# ---------------------------------------------------------------- public API
def list_inputs() -> list[str]:
    if sys.platform == "win32":
        return _winmm_inputs()
    import mido

    return mido.get_input_names()


def describe_inputs() -> list[dict]:
    """[{index, name, interface, usb}]; interface paths are Windows-only ('' elsewhere)."""
    out = []
    for i, name in enumerate(list_inputs()):
        path = _interface_path(i) if sys.platform == "win32" else ""
        out.append({"index": i, "name": name, "interface": path, "usb": path.startswith("\\\\?\\usb#")})
    return out


class PortRejected(LookupError):
    pass


def choose_input(match: str, usb_id: str | None, inputs: list[dict]) -> dict:
    """Pick the port to listen to. With usb_id (e.g. "vid_1c75&pid_020c") only a real USB
    port of that device is accepted: a virtual port that merely carries the right name
    (loopMIDI, app-to-app endpoints) could otherwise inject approvals."""
    named = [p for p in inputs if match.lower() in p["name"].lower()]
    if not usb_id:
        if not named:
            raise LookupError(f"no MIDI input matching {match!r}; available: {[p['name'] for p in inputs]}")
        return named[0]
    want = usb_id.lower()
    real = [p for p in inputs if p["usb"] and want in p["interface"]]
    if len(real) == 1:
        return real[0]
    listing = "; ".join(f"[{p['index']}] {p['name']!r} -> {p['interface'] or '(no interface path)'}"
                        for p in inputs) or "none"
    if len(real) > 1:
        raise PortRejected(f"{len(real)} USB ports match {usb_id}; unplug the extra ones. Ports: {listing}")
    fakes = [p["name"] for p in named]
    hint = f" Ports named like {match!r} but NOT that USB device (refused): {fakes}." if fakes else ""
    raise PortRejected(f"no USB MIDI input with {usb_id} (is the keyboard plugged in?).{hint} "
                       f"Ports: {listing}. Use --any-port to accept a port by its name only.")


def open_input(match: str, callback: EventCallback, usb_id: str | None = None) -> MidiInput:
    """Open the input chosen by choose_input (name substring, plus USB identity if given)."""
    if sys.platform != "win32":
        if usb_id:
            raise PortRejected("USB identity checks need the Windows backend; use --any-port here")
        names = list_inputs()
        hits = [n for n in names if match.lower() in n.lower()]
        if not hits:
            raise LookupError(f"no MIDI input matching {match!r}; available: {names}")
        return MidoInput(hits[0], callback)
    p = choose_input(match, usb_id, describe_inputs())
    inp = WinMMInput(p["index"], p["name"], callback)
    inp.interface = p["interface"]
    return inp
