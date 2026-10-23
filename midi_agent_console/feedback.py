"""Audible feedback, because most cheap keyboards (MicroLab mk3 included) have no
host-controllable lights. Plays short motifs on a MIDI output — by default the
"Microsoft GS Wavetable Synth" that ships with Windows.

Sounds are played from a worker thread: never from the MIDI input callback.
"""
from __future__ import annotations

import queue
import sys
import threading
import time

# (note, velocity, seconds) steps; chords are lists of notes
MOTIFS: dict[str, list[tuple[list[int], int, float]]] = {
    "ask": [([72], 70, 0.07), ([76], 70, 0.07), ([79], 80, 0.12)],     # rising arpeggio: a question
    "yes": [([72, 76], 90, 0.18)],                                      # major third
    "no": [([72, 73], 90, 0.18)],                                       # minor second
    "hold": [([84], 50, 0.05)],                                         # tick while long-press arms
    "timeout": [([48], 80, 0.30)],                                      # low note
    "estop": [([36, 37, 38, 39], 120, 0.45)],                           # low cluster
    "idle": [([96], 40, 0.04)],                                         # key pressed, nothing pending
}


class Feedback:
    def __init__(self, enabled: bool = True, port_match: str = "GS Wavetable", program: int = 11):
        self.enabled = enabled
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._out = None
        if enabled:
            try:
                self._out = _open_output(port_match)
                if program is not None and self._out:
                    self._out.short(0xC0 | 15, program, 0)  # ch16, 11 = vibraphone (GM)
            except Exception as exc:
                print(f"[feedback] disabled: {exc}", file=sys.stderr)
                self._out = None
        threading.Thread(target=self._run, name="feedback", daemon=True).start()

    def play(self, motif: str) -> None:
        if self._out:
            self._q.put(motif)

    def _run(self) -> None:
        while True:
            motif = self._q.get()
            for notes, vel, dur in MOTIFS.get(motif, []):
                for n in notes:
                    self._out.short(0x9F, n, vel)
                time.sleep(dur)
                for n in notes:
                    self._out.short(0x8F, n, 0)  # always release, or the synth keeps ringing

    def close(self) -> None:
        if self._out:
            self._out.close()
            self._out = None


def _open_output(match: str):
    if sys.platform != "win32":
        import mido

        names = [n for n in mido.get_output_names() if match.lower() in n.lower()]
        if not names:
            raise LookupError(f"no MIDI output matching {match!r}")
        port = mido.open_output(names[0])

        class _M:
            def short(self, st, d1, d2):
                port.send(mido.Message.from_bytes([st, d1, d2] if st & 0xF0 != 0xC0 else [st, d1]))

            def close(self):
                port.close()

        return _M()

    import ctypes
    import ctypes.wintypes as wt

    winmm = ctypes.WinDLL("winmm")

    class CAPS(ctypes.Structure):
        _fields_ = [("wMid", wt.WORD), ("wPid", wt.WORD), ("vDriverVersion", wt.UINT),
                    ("szPname", wt.WCHAR * 32), ("wTechnology", wt.WORD), ("wVoices", wt.WORD),
                    ("wNotes", wt.WORD), ("wChannelMask", wt.WORD), ("dwSupport", wt.DWORD)]

    for i in range(winmm.midiOutGetNumDevs()):
        c = CAPS()
        winmm.midiOutGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c))
        if match.lower() in c.szPname.lower():
            h = wt.HANDLE()
            if winmm.midiOutOpen(ctypes.byref(h), i, 0, 0, 0):
                raise OSError(f"midiOutOpen({c.szPname!r}) failed")

            class _W:
                def short(self, st, d1, d2):
                    winmm.midiOutShortMsg(h, st | d1 << 8 | d2 << 16)

                def close(self):
                    winmm.midiOutReset(h)
                    winmm.midiOutClose(h)

            return _W()
    raise LookupError(f"no MIDI output matching {match!r}")
