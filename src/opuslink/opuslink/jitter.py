"""Playback jitter buffer (device-agnostic, unit-testable).

Arrival is bursty — one fat packet every ``period_s`` seconds — while
playback consumes PCM continuously. The buffer therefore:

- stays in PRIME state, emitting silence, until ``prime_bytes`` of audio
  have accumulated (roughly one packet's worth);
- plays from there on; if it ever runs dry it returns a shortfall (the
  caller fills with PLC/silence) and re-primes for the next burst.
  Re-priming is also what absorbs TX/RX clock drift on long streams;
- caps at ``max_bytes`` (~2 packets) and drops the oldest audio on
  overrun, bounding latency instead of queueing it forever.
"""

from __future__ import annotations


class JitterBuffer:
    def __init__(self, *, prime_bytes: int, max_bytes: int) -> None:
        if prime_bytes < 1 or max_bytes < prime_bytes:
            raise ValueError("need max_bytes >= prime_bytes >= 1")
        self.prime_bytes = prime_bytes
        self.max_bytes = max_bytes
        self._buf = bytearray()
        self._primed = False
        self.fed_bytes = 0
        self.dropped_bytes = 0
        self.underruns = 0

    @property
    def primed(self) -> bool:
        return self._primed

    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._buf.extend(pcm)
        self.fed_bytes += len(pcm)
        excess = len(self._buf) - self.max_bytes
        if excess > 0:
            del self._buf[:excess]
            self.dropped_bytes += excess

    def pull(self, n_bytes: int) -> tuple[bytes, int]:
        """Return (audio, short_by). ``audio`` is always exactly n_bytes
        long (zeros where the buffer came up short); ``short_by`` is how
        much the caller may want to replace with PLC."""
        if n_bytes <= 0:
            return b"", 0
        if not self._primed:
            if len(self._buf) >= self.prime_bytes:
                self._primed = True
            else:
                return bytes(n_bytes), n_bytes
        take = min(len(self._buf), n_bytes)
        chunk = bytes(self._buf[:take])
        del self._buf[:take]
        short = n_bytes - take
        if short:
            # Ran dry mid-playback: report the shortfall (caller papers over
            # it with PLC/silence) and re-prime for the next burst.
            self.underruns += 1
            self._primed = False
        return chunk + bytes(short), short
