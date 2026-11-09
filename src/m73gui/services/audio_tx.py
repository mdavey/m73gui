"""Outgoing live-audio broadcast: mic -> CBR opus -> full-size data packets.

Pacing is self-limiting — the mic produces audio in real time and a packet
only goes out once ``frames_per_packet`` frames have accumulated, which is
roughly one packet per modem frame period. The local frame buffer is bounded
(one packet + AUDIO_BUFFER_EXTRA_S of slack): when the channel falls behind,
the oldest audio is dropped rather than queueing latency forever. The payload
budget is re-read every packet so a remote mode change shrinks packets instead
of silently truncating them at the modem.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable

import opuslink

from .. import phytime, protocol
from ..constants import (
    AUDIO_BUFFER_EXTRA_S,
    PACING_HIGH_WATER,
    PACING_POLL_S,
)
from ..state import (
    AudioTxSnapshot,
    TX_CANCELLED,
    TX_FAILED,
    TX_PENDING,
    TX_SENDING,
)
from . import audio_io

log = logging.getLogger(__name__)


def audio_plan(cfg: dict, payload_bytes: int | None = None) -> opuslink.AudioPlan | None:
    """Planner view of a (merged: live config + pending mode fields) config:
    which Opus tier would this modem mode stream at, or None if it cannot.

    ``payload_bytes`` overrides the payload capacity: for a PENDING mode not
    yet applied, ``cfg["payload_size"]`` is still the old mode's MTU, so the
    UI must pass ``phytime.payload_bytes_for(cfg)`` (the Files-tab pattern)
    to judge the selection itself. Only trust ``payload_size`` for a config
    that reflects the live, applied mode.
    """
    callsign = str(cfg.get("callsign", "") or "")
    call_len = min(
        len(callsign.encode("ascii", "ignore")), 16
    )
    if payload_bytes is None:
        try:
            payload_bytes = int(cfg.get("payload_size", 0) or 0)
        except (TypeError, ValueError):
            return None
    if payload_bytes <= 0:
        return None
    budget = protocol.audio_budget_for(payload_bytes, call_len)
    return opuslink.plan_for(budget, phytime.frame_seconds(cfg))


class AudioBroadcaster:
    """Streams one live audio broadcast at a time in a worker thread."""

    def __init__(
        self,
        emit: Callable[..., None],
        send_frame: Callable[[bytes], bool],
        get_status: Callable[[], dict | None],
        get_config: Callable[[], dict | None],
        outgoing_ids: set[int],
        capture_factory: Callable | None = None,  # (rate, ch, on_pcm) -> captor
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.emit = emit
        self.send_frame = send_frame
        self.get_status = get_status
        self.get_config = get_config
        self.outgoing_ids = outgoing_ids
        self.capture_factory = capture_factory or audio_io.PcmInput
        self.sleep = sleep
        self.monotonic = monotonic
        self.snapshot: AudioTxSnapshot | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frames: collections.deque[bytes] = collections.deque()
        self._wake = threading.Condition()
        self._pcm = bytearray()
        self._plan: opuslink.AudioPlan | None = None
        self._encoder = None
        self._callsign = ""

    # -- public -------------------------------------------------------------

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, cfg: dict, callsign: str) -> str | None:
        """Begin broadcasting on the (just-applied) modem config; returns an
        error string or None."""
        if self.busy():
            return "audio broadcast is already running"
        self._callsign = callsign.encode("ascii", "ignore").decode()[:16]
        payload_size = int(cfg.get("payload_size", 0) or 0)
        airtime = phytime.frame_seconds(cfg)
        budget = protocol.audio_budget_for(payload_size, len(self._callsign))
        plan = opuslink.plan_for(budget, airtime)
        if plan is None:
            return (
                "not enough data rate for audio: mode carries about "
                f"{budget * 8 / airtime / 1000:.1f} kbps, audio needs at "
                f"least {opuslink.MIN_BITRATE // 1000} kbps"
            )
        snap = AudioTxSnapshot(
            session=protocol.new_transfer_id(),
            tier=plan.tier.name,
            bitrate=plan.bitrate,
            sample_rate=plan.sample_rate,
            channels=plan.channels,
            duration_ms=plan.duration_ms,
            frames_per_packet=plan.frames_per_packet,
            period_s=plan.period_s,
            started_at=self.monotonic(),
            status=TX_PENDING,
            message=f"starting {opuslink.plan_label(plan)}…",
        )
        self._plan = plan
        self._encoder = opuslink.make_encoder(plan.tier, plan.duration_ms)
        self.snapshot = snap
        self.outgoing_ids.add(snap.session)
        self._stop.clear()
        with self._wake:
            self._frames.clear()
        self._pcm.clear()
        self._thread = threading.Thread(
            target=self._guarded_run, args=(snap,), daemon=True,
            name="m73gui-audio-tx",
        )
        self._thread.start()
        return None

    def stop(self) -> None:
        """Ask the broadcast to stop (no-op when idle)."""
        self._stop.set()
        with self._wake:
            self._wake.notify_all()

    def close(self, timeout: float = 1.5) -> None:
        self.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def notify_link_down(self) -> None:
        self._stop.set()

    # -- worker ---------------------------------------------------------------

    def _guarded_run(self, snap: AudioTxSnapshot) -> None:
        try:
            self._run(snap)
        except Exception:  # never let a worker kill the app
            log.exception("audio broadcaster crashed")
            self._finish(snap, TX_FAILED, "internal error, see log")
        finally:
            self.outgoing_ids.discard(snap.session)

    def _run(self, snap: AudioTxSnapshot) -> None:
        plan = self._plan
        assert plan is not None and self._encoder is not None
        try:
            captor = self.capture_factory(
                plan.sample_rate, plan.channels, self._on_pcm
            )
            captor.start()
        except Exception as exc:  # RuntimeError when PortAudio is missing
            self._finish(snap, TX_FAILED, f"audio capture failed: {exc}")
            return
        self._publish(
            snap, TX_SENDING,
            f"broadcasting {opuslink.plan_label(plan)} · "
            f"{plan.frames_per_packet} frames/packet · period "
            f"{plan.period_s:.1f} s",
        )
        seq = 0
        try:
            while True:
                if self._stop.is_set():
                    break
                n = self._packet_frames(snap, plan)
                if n is None:
                    return  # budget evaporated; already failed
                if not self._wait_frames(n):
                    break
                self._trim_frames(snap)
                if self._queue_busy():
                    continue
                batch = self._take_frames(n)
                if batch is None:
                    continue
                payload = protocol.encode_audio(
                    snap.session, seq, plan.tier, plan.duration_ms,
                    self._callsign, batch,
                )
                if not self.send_frame(payload):
                    self._finish(snap, TX_FAILED, "KISS link lost")
                    return
                snap.packets_sent += 1
                seq += 1
                self._publish(snap)
        finally:
            captor.stop()
        if snap.status not in (TX_FAILED,):
            self._finish(
                snap, TX_CANCELLED,
                f"stopped after {snap.packets_sent} packets "
                f"({snap.audio_seconds():.0f} s of audio)",
            )

    # -- capture side (runs on the audio callback thread) ----------------------

    def _on_pcm(self, chunk: bytes) -> None:
        plan = self._plan
        frame_pcm = plan.pcm_bytes_per_frame
        self._pcm.extend(chunk)
        while len(self._pcm) >= frame_pcm:
            block = bytes(self._pcm[:frame_pcm])
            del self._pcm[:frame_pcm]
            try:
                frame = opuslink.encode_frame(
                    self._encoder, block, plan.frame_samples
                )
            except Exception:  # noqa: BLE001 - one bad block must not kill capture
                log.debug("opus encode failed", exc_info=True)
                continue
            with self._wake:
                self._frames.append(frame)
                self._wake.notify()

    # -- sender loop helpers ----------------------------------------------------

    def _wait_frames(self, need: int) -> bool:
        """Block until ``need`` frames are queued; False when stopping."""
        with self._wake:
            while len(self._frames) < need:
                if self._stop.is_set():
                    return False
                self._wake.wait(0.25)
            return not self._stop.is_set()

    def _packet_frames(self, snap: AudioTxSnapshot, plan) -> int | None:
        """Frames for the next packet, adapting to the live payload_size."""
        cfg = self.get_config()
        if not cfg:
            return plan.frames_per_packet
        try:
            payload_size = int(cfg.get("payload_size", 0) or 0)
        except (TypeError, ValueError):
            return plan.frames_per_packet
        if payload_size <= 0:
            return plan.frames_per_packet
        budget = protocol.audio_budget_for(payload_size, len(self._callsign))
        n = budget // plan.frame_bytes
        if n < 1:
            self._finish(
                snap, TX_FAILED,
                f"modem payload_size dropped to {payload_size} B: no room "
                "for audio frames",
            )
            return None
        return min(n, plan.frames_per_packet)

    def _queue_busy(self) -> bool:
        try:
            status = self.get_status()
        except Exception:  # noqa: BLE001
            return False
        if not status:
            return False
        try:
            depth = int(status.get("tx_queue", 0))
        except (TypeError, ValueError):
            return False
        if depth < PACING_HIGH_WATER:
            return False
        self.sleep(PACING_POLL_S)
        return True

    def _take_frames(self, n: int) -> list[bytes] | None:
        with self._wake:
            if len(self._frames) < n:
                return None
            return [self._frames.popleft() for _ in range(n)]

    def _trim_frames(self, snap: AudioTxSnapshot) -> None:
        """Bound the local buffer: one packet plus a little slack."""
        per_frame_s = self._plan.duration_ms / 1000
        extra = max(1, int(round(AUDIO_BUFFER_EXTRA_S / per_frame_s)))
        limit = self._plan.frames_per_packet + extra
        with self._wake:
            excess = len(self._frames) - limit
            if excess > 0:
                for _ in range(excess):
                    self._frames.popleft()
                snap.frames_dropped += excess

    # -- publishing -------------------------------------------------------------

    def _publish(
        self, snap: AudioTxSnapshot, status: str | None = None,
        message: str | None = None,
    ) -> None:
        if status is not None:
            snap.status = status
        if message is not None:
            snap.message = message
        self.emit("audio_tx", snap=snap)

    def _finish(self, snap: AudioTxSnapshot, status: str, message: str) -> None:
        if snap.status in (TX_FAILED, TX_CANCELLED):
            return
        self._stop.set()
        self._publish(snap, status, message)
