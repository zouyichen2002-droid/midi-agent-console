"""MIDI input backends.

Windows: talks to winmm directly through ctypes, so it works on any Python
version without compiling anything (python-rtmidi has no wheels past cp312).
Elsewhere: falls back to mido + python-rtmidi if installed.
"""
from __future__ import annotations

import sys
from typing import Callable, Protocol

from .events import MidiEvent, decode, decode_packed

EventCallback = Callable[[MidiEvent], None]


class MidiInput(Protocol):
    name: str

    def close(self) -> None: ...


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

            def proc(_h, wmsg, _inst, p1, _p2):
                if wmsg == _MIM_DATA:
                    try:
                        callback(decode_packed(p1))
                    except Exception as exc:  # never let an exception escape into winmm
                        print(f"[midi] callback error: {exc}", file=sys.stderr)

            self._proc = _MidiInProc(proc)  # keep a reference, or winmm calls freed memory
            rc = _winmm.midiInOpen(ctypes.byref(self._handle), index, self._proc, 0,
                                   _CALLBACK_FUNCTION)
            if rc:
                raise OSError(f"midiInOpen({name!r}) failed, MMRESULT={rc} "
                              "(another program may hold the port exclusively)")
            _winmm.midiInStart(self._handle)

        def close(self) -> None:
            if self._handle:
                _winmm.midiInStop(self._handle)
                _winmm.midiInClose(self._handle)
                self._handle = wt.HANDLE()


# ---------------------------------------------------------------- mido (fallback)
class MidoInput:
    def __init__(self, name: str, callback: EventCallback):
        import mido

        self.name = name

        def on_msg(msg):
            data = msg.bytes()
            if len(data) <= 3:
                callback(decode(*(data + [0, 0])[:3]))

        self._port = mido.open_input(name, callback=on_msg)

    def close(self) -> None:
        self._port.close()


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
