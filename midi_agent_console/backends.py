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


def open_input(match: str, callback: EventCallback) -> MidiInput:
    """Open the first input whose name contains `match` (case-insensitive)."""
    names = list_inputs()
    hits = [i for i, n in enumerate(names) if match.lower() in n.lower()]
    if not hits:
        raise LookupError(f"no MIDI input matching {match!r}; available: {names}")
    i = hits[0]
    if sys.platform == "win32":
        return WinMMInput(i, names[i], callback)
    return MidoInput(names[i], callback)
