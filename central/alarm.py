"""
VIGILANT audible alarm.

Same behaviour as the original single-file system monitor:
  * plays alarm_loop.wav (looping) on Windows, at 35 % volume via a quieter temp copy,
  * supports MP3 data saved as .wav through Windows MCI,
  * starts ONCE when an alarm condition appears and stops when it clears
    (never re-triggered every poll).
Extras: a generated two-tone siren if alarm_loop.wav is missing, and a
terminal bell fallback on non-Windows systems.
All methods must be called from the Tk main thread.
"""
from __future__ import annotations

import logging
import math
import os
import struct
import tempfile
import wave

log = logging.getLogger("vigilant.alarm")


def _find_sound(candidates):
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def generate_alarm_wav(path: str, rate: int = 22050) -> str:
    frames = bytearray()

    def tone(freq, secs, amp=0.32):
        n = int(rate * secs)
        ramp = rate * 0.01
        for i in range(n):
            env = min(1.0, i / ramp, (n - i) / ramp)
            frames.extend(struct.pack("<h", int(32767 * amp * env * math.sin(2 * math.pi * freq * i / rate))))

    def silence(secs):
        frames.extend(b"\x00\x00" * int(rate * secs))

    for _ in range(2):
        tone(880, 0.22); silence(0.06); tone(660, 0.22); silence(0.06)
    silence(0.40)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def generate_professional_alarm(path: str, rate: int = 44100) -> str:
    """VIGILANT alert tone in the style of IEC 60601-1-8 high-priority alarms (patient / NOC monitors):
    a 3-pulse burst, a short gap, a 2-pulse burst, then a pause — one 2.6 s cycle that loops seamlessly.
    Each pulse is a soft-edged harmonic tone (no clicks), so it is urgent without being harsh."""
    c5, f5, a5 = 523.25, 698.46, 880.0
    pulses = [(0.00, c5), (0.22, a5), (0.44, f5),            # burst 1
              (0.86, a5), (1.08, f5)]                        # burst 2
    length, pulse, attack, release = 2.6, 0.17, 0.015, 0.05
    harmonics = ((1, 1.0), (2, 0.42), (3, 0.22), (4, 0.10), (5, 0.05))
    n_total = int(rate * length)
    samples = [0.0] * n_total
    for start, freq in pulses:
        first = int(start * rate)
        n = int((pulse + release) * rate)
        for i in range(n):
            t = i / rate
            if t < attack:                                    # raised-cosine attack
                env = 0.5 - 0.5 * math.cos(math.pi * t / attack)
            elif t < pulse:
                env = 1.0
            else:                                             # smooth exponential release
                env = math.exp(-(t - pulse) / (release / 4))
            env *= 1.0 - 0.15 * min(t / pulse, 1.0)           # slight decay = less fatiguing
            v = sum(a * math.sin(2 * math.pi * freq * k * t) for k, a in harmonics)
            if first + i < n_total:
                samples[first + i] += env * v
    peak = max(abs(s) for s in samples) or 1.0
    scale = 0.85 * 32767 / peak
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(s * scale)) for s in samples))
    return path


def _is_pcm_wav(path) -> bool:
    try:
        with wave.open(path, "rb") as w:
            return w.getcomptype() == "NONE"
    except (wave.Error, EOFError, OSError):
        return False


def make_quiet_copy(source_path: str, volume: float = 0.35) -> str:
    """Quieter PCM copy (original monitor behaviour); the user's file is never modified."""
    try:
        with wave.open(source_path, "rb") as src:
            params = src.getparams()
            frames = src.readframes(src.getnframes())
        width = params.sampwidth
        if width == 1:
            quieter = bytes(max(0, min(255, round(128 + (s - 128) * volume))) for s in frames)
        elif width in (2, 4):
            code = "h" if width == 2 else "i"
            count = len(frames) // width
            values = struct.unpack("<" + code * count, frames[:count * width])
            lo, hi = -(2 ** (width * 8 - 1)), 2 ** (width * 8 - 1) - 1
            quieter = struct.pack("<" + code * count, *(max(lo, min(hi, round(v * volume))) for v in values))
        else:
            return source_path
        target = os.path.join(tempfile.gettempdir(), "vigilant_alarm.wav") #vigilant_vm_alarm_quiet.wav
        with wave.open(target, "wb") as out:
            out.setparams(params)
            out.writeframes(quieter)
        return target
    except Exception as exc:
        log.warning("Could not prepare attenuated alarm WAV; using original: %s", exc)
        return source_path


class AlarmPlayer:
    def __init__(self, root, candidates, volume: float = 0.35):
        self.root = root
        self.source = _find_sound(candidates)
        self.generated = False
        if not self.source:
            try:
                self.source = generate_alarm_wav(os.path.join(tempfile.gettempdir(), "vigilant_alarm.wav"))
                self.generated = True
            except Exception as exc:
                log.warning("Could not generate alarm tone: %s", exc)
                self.source = None
        self.is_wav = bool(self.source) and _is_pcm_wav(self.source)
        self.play_path = (make_quiet_copy(self.source, volume) if self.is_wav and not self.generated
                          else self.source)
        self.playing = False       # real alarm loop active
        self.testing = False       # test sound active
        self.last_error = None
        self._mci_open = False
        self._bell_job = None
        self._test_job = None

    def describe(self) -> str:
        if not self.source:
            return "System bell (no sound file available)"
        if self.generated:
            return "Built-in two-tone siren (put alarm_loop.wav beside monitor.py to use your own sound)"
        return f"{os.path.basename(self.source)}" + ("" if os.name == "nt" else " — system bell on non-Windows")

    # ----------------------------------------------------------- public API
    def start_loop(self):
        if self.testing:            # keep the sound running, it is now a real alarm
            self._cancel_test()
            self.testing = False
            self.playing = True
            return
        if self.playing:
            return
        self._start_sound()
        self.playing = True

    def stop(self):
        self._cancel_test()
        self._stop_sound()
        self.playing = False
        self.testing = False

    def play_test(self, seconds: float = 3.0) -> bool:
        if self.playing or self.testing:
            return False
        self._start_sound()
        self.testing = True
        self._test_job = self.root.after(int(seconds * 1000), self._end_test)
        return True

    # ----------------------------------------------------------- internals
    def _end_test(self):
        self._test_job = None
        if self.testing:
            self.testing = False
            if not self.playing:
                self._stop_sound()

    def _cancel_test(self):
        if self._test_job is not None:
            try:
                self.root.after_cancel(self._test_job)
            except Exception:
                pass
            self._test_job = None

    def _start_sound(self):
        if os.name == "nt" and self.source:
            try:
                self._play_windows()
                return
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Alarm playback failed, using bell: %s", exc)
        self._start_bell()

    def _play_windows(self):
        import winsound
        if self.is_wav:
            winsound.PlaySound(self.play_path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
            return
        import ctypes   # MP3 data inside a .wav file -> Windows MCI (as in the original app)
        send = ctypes.WinDLL("winmm").mciSendStringW
        send.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
        send.restype = ctypes.c_uint
        if self._mci_open:
            send("close vigilant_vm_alarm", None, 0, None)
            self._mci_open = False
        path = self.source.replace('"', "")
        if send(f'open "{path}" type mpegvideo alias vigilant_vm_alarm', None, 0, None):
            raise OSError("Windows could not open the alarm audio (MCI).")
        self._mci_open = True
        if send("play vigilant_vm_alarm repeat", None, 0, None):
            send("close vigilant_vm_alarm", None, 0, None)
            self._mci_open = False
            raise OSError("Windows could not play the alarm audio (MCI).")

    def _start_bell(self):
        def ring():
            try:
                self.root.bell()
            except Exception:
                pass
            self._bell_job = self.root.after(900, ring)
        if self._bell_job is None:
            ring()

    def _stop_sound(self):
        if self._bell_job is not None:
            try:
                self.root.after_cancel(self._bell_job)
            except Exception:
                pass
            self._bell_job = None
        if os.name == "nt":
            try:
                import winsound
                winsound.PlaySound(None, 0)
                if self._mci_open:
                    import ctypes
                    winmm = ctypes.WinDLL("winmm")
                    winmm.mciSendStringW("stop vigilant_vm_alarm", None, 0, None)
                    winmm.mciSendStringW("close vigilant_vm_alarm", None, 0, None)
                    self._mci_open = False
            except Exception:
                pass
