"""Low-latency confirmation tones, no compiler needed.

Measured on the dev machine (process loopback capture, same tap for every method):
an always-open WASAPI stream started sound ~80 ms after play(); the Microsoft GS
Wavetable Synth took ~277 ms. Above ~100 ms people notice that feedback lags the key.

Primary : one persistent WASAPI shared-mode stream (optional dependency `sounddevice`,
          no numpy), fed by a callback mixer. play() is a SimpleQueue.put: never blocks.
Fallback: stdlib only, Windows: ctypes winmm.PlaySoundW(SND_MEMORY|SND_ASYNC) on cached
          WAV buffers (CPython's winsound refuses SND_MEMORY|SND_ASYNC). A new sound cuts
          off the previous one.
"""
from __future__ import annotations

import array
import ctypes
import io
import math
import queue
import sys
import wave

SR = 48000
FADE_S = 0.005


def render(steps, gain: float = 0.2) -> array.array:
    """(notes, velocity, seconds) steps -> mono float32 samples, 5 ms fades (no clicks)."""
    out = array.array("f")
    nf = int(FADE_S * SR)
    for notes, vel, dur in steps:
        n = int(dur * SR)
        amp = gain * (vel / 127) / len(notes)
        ws = [2 * math.pi * 440 * 2 ** ((m - 69) / 12) / SR for m in notes]
        for i in range(n):
            env = min(1.0, i / nf, (n - 1 - i) / nf)
            out.append(amp * env * sum(math.sin(w * i) for w in ws))
    return out


class StreamBackend:
    name = "wasapi-stream"

    def __init__(self, clips: dict[str, array.array], open_stream: bool = True):
        self._clips = {k: array.array("f", [s for s in v for _ in (0, 1)]) for k, v in clips.items()}  # stereo
        self._new: queue.SimpleQueue = queue.SimpleQueue()
        self._voices: list[list] = []  # [clip, pos]; touched only by the audio thread
        self._stream = None
        self.latency = None
        if open_stream:
            import sounddevice as sd  # optional dependency

            apis = [h["name"] for h in sd.query_hostapis()]
            dev = (sd.query_hostapis(apis.index("Windows WASAPI"))["default_output_device"]
                   if "Windows WASAPI" in apis else None)
            self._stream = sd.RawOutputStream(samplerate=SR, channels=2, dtype="float32", device=dev,
                                              latency="low", callback=self._cb)
            self._stream.start()
            self.latency = self._stream.latency

    def _cb(self, outdata, frames, time_info, status):
        while True:
            try:
                self._voices.append([self._new.get_nowait(), 0])
            except queue.Empty:
                break
        n = frames * 2
        if not self._voices:
            outdata[:] = bytes(n * 4)
            return
        mix = array.array("f", bytes(n * 4))
        mixed = len(self._voices)
        for v in self._voices:
            clip, pos = v
            seg = clip[pos:pos + n]
            if mixed == 1:
                mix[:len(seg)] = seg
            else:
                for i, s in enumerate(seg):
                    mix[i] += s
            v[1] = pos + n
        self._voices = [v for v in self._voices if v[1] < len(v[0])]
        if mixed > 1:  # count voices mixed in THIS buffer, before dropping the finished ones
            for i, s in enumerate(mix):
                mix[i] = 1.0 if s > 1.0 else -1.0 if s < -1.0 else s
        outdata[:] = mix.tobytes()

    def play(self, name: str) -> None:
        clip = self._clips.get(name)
        if clip is not None:
            self._new.put(clip)

    def close(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class PlaySoundBackend:
    name = "playsound"
    SND_ASYNC, SND_NODEFAULT, SND_MEMORY = 0x0001, 0x0002, 0x0004

    def __init__(self, clips: dict[str, array.array]):
        self._ps = ctypes.WinDLL("winmm").PlaySoundW
        self._ps.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        self._bufs = {k: ctypes.create_string_buffer(w, len(w)) for k, w in
                      ((k, to_wav(x)) for k, x in clips.items())}  # kept alive: read asynchronously
        self.latency = None

    def play(self, name: str) -> None:
        buf = self._bufs.get(name)
        if buf is not None:
            self._ps(ctypes.addressof(buf), None, self.SND_MEMORY | self.SND_ASYNC | self.SND_NODEFAULT)

    def close(self) -> None:
        self._ps(None, None, 0)


def to_wav(samples: array.array) -> bytes:
    pcm = array.array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in samples))
    if sys.byteorder != "little":
        pcm.byteswap()
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return b.getvalue()


def open_tones(motifs: dict, gain: float = 0.2):
    """Best available low-latency backend, or raise if none (caller falls back to MIDI)."""
    clips = {k: render(v, gain) for k, v in motifs.items()}
    try:
        return StreamBackend(clips)
    except Exception as exc:
        print(f"[tones] no WASAPI stream ({exc!r}); trying PlaySound", file=sys.stderr)
    if sys.platform == "win32":
        return PlaySoundBackend(clips)
    raise RuntimeError("no low-latency audio backend on this platform")
