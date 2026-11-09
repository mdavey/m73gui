"""Incoming live-audio broadcasts: unpack data packets into per-sender
jitter buffers and play the most recent talker.

Packets arrive in bursts (one per modem frame period, seconds apart) while
playback consumes PCM continuously, so every session owns an
``opuslink.JitterBuffer`` that primes on a full packet before playing and
re-primes whenever it runs dry. Gaps between packet sequence numbers are
concealed by a couple of opus PLC frames and then silence.

All session mutation happens on the Tk thread (from _feed_protocol); the
playback callback only touches the active session's jitter buffer under the
receiver lock — and NEVER calls out to app-provided callbacks while holding
it (a tk.BooleanVar.get() from the audio thread deadlocks against a Tk
thread waiting for the same lock; Tcl marshals to the main loop).
Decoding ~400 frames of a wide-mode packet is tens of milliseconds —
acceptable there, same as file-block bookkeeping.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import opuslink

from ..constants import (
    AUDIO_BUFFER_EXTRA_S,
    AUDIO_PLC_MAX_FRAMES,
    AUDIO_SESSION_IDLE_FLOOR_S,
)
from ..state import AudioSessionSnapshot
from . import audio_io

log = logging.getLogger(__name__)

MAX_AUDIO_SESSIONS = 8


class _Session:
    """One received broadcast: decoder + jitter buffer + counters."""

    def __init__(self, body, session_id: int, now: float) -> None:
        tier = body.tier
        self.callsign = body.callsign
        self.session_id = session_id
        self.tier = tier
        self.duration_ms = body.duration_ms
        self.frame_samples = tier.sample_rate * body.duration_ms // 1000
        self.pcm_frame_bytes = self.frame_samples * tier.channels * 2
        self.decoder = opuslink.make_decoder(tier)
        # one packet's PCM (a packet carries ~one frame period of audio)
        # plus a fixed slack; beyond that the oldest audio is dropped
        extra = int(tier.sample_rate * AUDIO_BUFFER_EXTRA_S) * tier.channels * 2
        content = body.frame_count * self.pcm_frame_bytes
        self.jitter = opuslink.JitterBuffer(
            prime_bytes=content, max_bytes=content + extra
        )
        self.last_seq: int | None = None
        self.packets = 0
        self.stale = 0
        self.gap_packets = 0
        self.plc_frames = 0
        self.bad_frames = 0
        self.plc_used = 0  # PLC frames used in the current dry episode
        self.plc_pad = bytearray()
        self.last_seen = now
        self.period_s = 0.0  # EWMA inter-arrival estimate

    def matches(self, body) -> bool:
        same = (
            self.tier.sample_rate, self.tier.channels, self.tier.bitrate,
            self.duration_ms,
        )
        return same == (
            body.tier.sample_rate, body.tier.channels, body.tier.bitrate,
            body.duration_ms,
        )

    def accept(self, seq: int, body, now: float) -> bool:
        """Decode a packet's frames into the jitter buffer; False if stale."""
        if self.last_seq is None:
            self.last_seq = seq
        elif seq <= self.last_seq:
            self.stale += 1
            return False
        else:
            if seq > self.last_seq + 1:
                self.gap_packets += seq - self.last_seq - 1
            self.last_seq = seq
        for frame in body.frames:
            try:
                pcm = opuslink.decode_frame(
                    self.decoder, frame, self.frame_samples
                )
            except Exception:  # noqa: BLE001 - garbage frame: PLC will cover
                self.bad_frames += 1
                continue
            self.jitter.feed(pcm)
        self.packets += 1
        if self.period_s <= 0:
            self.period_s = max(now - self.last_seen, 0.0)
        elif now > self.last_seen:
            inst = now - self.last_seen
            self.period_s = 0.7 * self.period_s + 0.3 * inst
        self.last_seen = now
        return True

    def snapshot(self, playing: bool) -> AudioSessionSnapshot:
        ms = self.jitter.buffered() * 1000.0 / (self.tier.sample_rate *
                                                self.tier.channels * 2)
        return AudioSessionSnapshot(
            callsign=self.callsign,
            session=self.session_id,
            tier=self.tier.name,
            bitrate=self.tier.bitrate,
            sample_rate=self.tier.sample_rate,
            channels=self.tier.channels,
            duration_ms=self.duration_ms,
            last_seen=self.last_seen,
            packets=self.packets,
            stale=self.stale,
            gap_packets=self.gap_packets,
            plc_frames=self.plc_frames,
            bad_frames=self.bad_frames,
            playing=playing,
            primed=self.jitter.primed,
            buffer_ms=int(ms),
        )


class AudioReceiver:
    """Decodes audio packets, plays the most recently heard session."""

    def __init__(
        self,
        emit: Callable[..., None],
        speaker_enabled: Callable[[], bool],
        playback_factory: Callable | None = None,  # (rate, ch, pull) -> player
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.emit = emit
        self.speaker_enabled = speaker_enabled
        self.playback_factory = playback_factory or audio_io.PcmOutput
        self.monotonic = monotonic
        self.sessions: dict[tuple[str, int], _Session] = {}
        self.active_key: tuple[str, int] | None = None
        self._lock = threading.Lock()
        self._device = None
        self._device_key: tuple[int, int] | None = None
        self._device_failed = False

    # -- Tk-thread ingestion ----------------------------------------------------

    def handle_packet(self, env, body) -> None:
        now = self.monotonic()
        key = (body.callsign, env.transfer_id)
        with self._lock:
            sess = self.sessions.get(key)
            if sess is None or not sess.matches(body):
                if sess is not None:
                    log.info(
                        "audio settings changed mid-session for %s, resetting",
                        body.callsign,
                    )
                sess = _Session(body, env.transfer_id, now)
                self.sessions[key] = sess
                self._evict_old()
            fresh = sess.accept(env.seq, body, now)
        self.active_key = key  # latest talker wins, stale packet or not
        snap = sess.snapshot(playing=True)
        if fresh:
            self._ensure_device(sess)
        self.emit("audio_rx", snap=snap)

    def _evict_old(self) -> None:
        while len(self.sessions) > MAX_AUDIO_SESSIONS:
            oldest = min(
                (k for k in self.sessions if k != self.active_key),
                key=lambda k: self.sessions[k].last_seen,
                default=None,
            )
            if oldest is None:
                return
            del self.sessions[oldest]

    # -- playback -----------------------------------------------------------------

    def pull(self, frames: int, channels: int | None = None) -> bytes:
        """Produce interleaved int16 PCM for the playback device."""
        # Evaluate the speaker toggle BEFORE taking the lock: it may be any
        # callable at all (a tk.BooleanVar.get() from the GUI thread would
        # block waiting for the Tk thread, which may be waiting on this very
        # lock — a guaranteed deadlock).
        speaker = self.speaker_enabled()
        key = self.active_key
        if key is None:
            ch = channels or 1
            return b"\x00" * (frames * ch * 2)
        with self._lock:
            sess = self.sessions.get(key)
            if sess is None:
                ch = channels or 1
                return b"\x00" * (frames * ch * 2)
            ch = sess.tier.channels
            nbytes = frames * ch * 2
            if not speaker:
                # drain so switching the speaker back on does not burst
                buffered = sess.jitter.buffered()
                if buffered:
                    sess.jitter.pull(buffered)
                return b"\x00" * nbytes
            was_primed = sess.jitter.primed
            data, short = sess.jitter.pull(nbytes)
            if short == 0:
                sess.plc_used = 0  # healthy pull
            elif was_primed:
                # Just ran dry mid-playback: paper over with PLC frames,
                # capped per episode; further waiting is plain silence.
                while (
                    len(sess.plc_pad) < short
                    and sess.plc_used < AUDIO_PLC_MAX_FRAMES
                ):
                    try:
                        sess.plc_pad += opuslink.decode_frame(
                            sess.decoder, None, sess.frame_samples
                        )
                    except Exception:  # noqa: BLE001
                        break
                    sess.plc_used += 1
                    sess.plc_frames += 1
                pad = bytes(sess.plc_pad[:short])
                del sess.plc_pad[: len(pad)]
                if pad:
                    data = data[: len(data) - len(pad)] + pad
            else:
                sess.plc_used = 0  # waiting silence between bursts
            return data

    def _ensure_device(self, sess: _Session) -> None:
        want = (sess.tier.sample_rate, sess.tier.channels)
        if self._device is not None and self._device_key == want:
            return
        self._stop_device()
        if self._device_failed:
            return
        rate, ch = want
        try:
            device = self.playback_factory(
                rate, ch, lambda frames: self.pull(frames, ch)
            )
            device.start()
        except Exception as exc:  # noqa: BLE001 - silent reception is fine
            log.info("audio playback unavailable: %s", exc)
            self._device_failed = True
            return
        self._device = device
        self._device_key = want

    def _stop_device(self) -> None:
        if self._device is not None:
            try:
                self._device.stop()
            except Exception:  # noqa: BLE001
                pass
        self._device = None
        self._device_key = None

    # -- housekeeping ---------------------------------------------------------------

    def sweep(self) -> None:
        now = self.monotonic()
        retired = False
        with self._lock:
            for key in list(self.sessions):
                sess = self.sessions[key]
                idle_limit = max(
                    AUDIO_SESSION_IDLE_FLOOR_S, 3 * sess.period_s
                )
                if now - sess.last_seen > idle_limit:
                    del self.sessions[key]
                    if self.active_key == key:
                        self.active_key = None
                    retired = True
                    log.debug("audio session retired: %s", key)
        if retired:
            if not self.sessions:
                self._stop_device()
            elif self.active_key is None and self.sessions:
                newest = max(
                    self.sessions, key=lambda k: self.sessions[k].last_seen
                )
                self.active_key = newest

    def close(self) -> None:
        self._stop_device()
        self.sessions.clear()
        self.active_key = None
