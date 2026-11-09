"""sounddevice glue for live audio.

Every sounddevice/numpy import happens lazily inside guarded functions so
headless test runs, machines without PortAudio, and the rest of the app keep
working with no audio stack at all. Pulse/PipeWire handles device selection
and resampling: we just open the default device at the plan's rate/channels.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def import_sounddevice():
    """The sounddevice module, or None if unavailable (no wheel or no
    PortAudio shared library — ImportError and OSError both occur)."""
    try:
        import sounddevice
    except (ImportError, OSError):
        return None
    return sounddevice


def available() -> bool:
    return import_sounddevice() is not None


class PcmInput:
    """Microphone capture pushing raw interleaved int16 chunks to on_pcm."""

    def __init__(self, sample_rate: int, channels: int, on_pcm) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.on_pcm = on_pcm
        self._stream = None

    def start(self) -> None:
        if self._stream is not None:
            return
        sd = import_sounddevice()
        if sd is None:
            raise RuntimeError("sounddevice/PortAudio not available")
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001 - best effort close
            log.debug("input close failed", exc_info=True)
        self._stream = None

    def _callback(self, indata, frames, time_info, status) -> None:
        self.on_pcm(indata.tobytes())


class PcmOutput:
    """Speakers pulling interleaved int16 PCM from pull(frames) -> bytes."""

    def __init__(self, sample_rate: int, channels: int, pull) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.pull = pull
        self._stream = None
        self._np = None

    def start(self) -> None:
        if self._stream is not None:
            return
        sd = import_sounddevice()
        if sd is None:
            raise RuntimeError("sounddevice/PortAudio not available")
        import numpy

        self._np = numpy
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001 - best effort close
            log.debug("output close failed", exc_info=True)
        self._stream = None

    def _callback(self, outdata, frames, time_info, status) -> None:
        data = self.pull(frames)
        arr = self._np.frombuffer(data, dtype="int16")
        expected = frames * self.channels
        if arr.size < expected:  # defensive: pad zeros, never crash callback
            arr = self._np.pad(arr, (0, expected - arr.size))
        outdata[:] = arr[:expected].reshape(-1, self.channels)
