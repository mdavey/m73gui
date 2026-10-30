"""Outgoing file sender and incoming reassembler.

All logic is transport-agnostic: the sender takes callables for frame sending,
status/config access, sleeping, and clock, so the full sequence can be unit
tested with fakes.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import queue
import threading
import time
from typing import Callable

from .. import protocol
from ..constants import (
    CODE_RATES,
    FILE_MAX_BYTES,
    FILE_MAX_CHUNKS,
    FLAG_POSTAMBLE,
    FRAME_SIZE_NAMES,
    INCOMING_IDLE_TIMEOUT_S,
    MFSK_MODE_NAMES,
    OFDM_MODULATIONS,
    PACING_HIGH_WATER,
    PACING_LOW_WATER,
    PACING_POLL_S,
    ROBUST_MODE_NAMES,
    TX_DRAIN_TIMEOUT_S,
)
from ..state import (
    TX,
    INCOMING_ACTIVE,
    INCOMING_COMPLETE,
    INCOMING_CORRUPT,
    INCOMING_INCOMPLETE,
    TX_CANCELLED,
    TX_FAILED,
    TX_PENDING,
    TX_SENDING,
    TX_SENT,
    IncomingSnapshot,
    OutgoingSnapshot,
)

log = logging.getLogger("m73gui.transfers")

MODEM_SETTING_FIELDS = (
    "modem_type",
    "modulation",
    "code_rate",
    "frame_size",
    "mfsk_mode",
    "robust_mode",
)


def target_settings(config: dict) -> dict:
    return {k: config[k] for k in MODEM_SETTING_FIELDS if k in config}


def settings_in_range(fs: protocol.FileStart) -> str | None:
    """Error string when FILE_START modem field indexes are out of range."""
    if fs.modem_type not in (0, 1, 2):
        return "invalid modem_type"
    if fs.modem_type == 0:
        if (
            fs.modulation >= len(OFDM_MODULATIONS)
            or fs.code_rate >= len(CODE_RATES)
            or fs.frame_size >= len(FRAME_SIZE_NAMES)
        ):
            return "invalid OFDM settings"
    elif fs.modem_type == 1:
        if fs.mfsk_mode >= len(MFSK_MODE_NAMES):
            return "invalid MFSK mode"
    elif fs.robust_mode >= len(ROBUST_MODE_NAMES):
        return "invalid ROBUST mode"
    return None


# ---------------------------------------------------------------------------
# Outgoing file sender


class FileSender:
    """Sends one outgoing broadcast transfer at a time in a worker thread."""

    def __init__(
        self,
        emit: Callable[..., None],
        send_frame: Callable[[bytes], bool],
        get_status: Callable[[], dict | None],
        get_config: Callable[[], dict | None],
        outgoing_ids: set[int],
        sleep: Callable[[float], bool] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.emit = emit
        self.send_frame = send_frame
        self.get_status = get_status
        self.get_config = get_config
        self.outgoing_ids = outgoing_ids
        self.sleep = sleep  # returns True if aborted during sleep
        self.monotonic = monotonic
        self.drain_timeout = TX_DRAIN_TIMEOUT_S
        self.snapshot: OutgoingSnapshot | None = None
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._abort = threading.Event()

    # -- public -------------------------------------------------------------

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, path: str) -> str | None:
        """Begin transfer; returns an error string or None."""
        if self.busy():
            return "a transfer is already running"
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return f"cannot read file: {exc}"
        snap = OutgoingSnapshot(
            filename=os.path.basename(path),
            file_size=size,
            chunk_size=0,
            total_chunks=0,
            status=TX_PENDING,
            message="Hashing…",
        )
        self.snapshot = snap
        self._cancel.clear()
        self._abort.clear()
        self._thread = threading.Thread(
            target=self._guarded_run, args=(path,), daemon=True
        )
        self._thread.start()
        return None

    def cancel(self) -> None:
        self._cancel.set()

    def notify_link_down(self) -> None:
        """KISS or control dropped: abort the active transfer."""
        if self.busy():
            self._abort.set()

    # -- internals ------------------------------------------------------------

    def _guarded_run(self, path: str) -> None:
        try:
            self._run(path)
        except Exception:  # never let a worker kill the app
            log.exception("file sender crashed")
            self._finish(TX_FAILED, "internal error, see log")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    def _stopped(self) -> bool:
        return self._cancel.is_set() or self._abort.is_set()

    def _wait(self, seconds: float) -> bool:
        """Sleep; returns True if the transfer should stop."""
        step = min(seconds, 0.05)
        elapsed = 0.0
        while elapsed < seconds:
            if self._stopped():
                return True
            self.sleep(min(step, seconds - elapsed))
            elapsed += step
        return self._stopped()

    def _publish(self, status: str | None = None, message: str | None = None) -> None:
        snap = self.snapshot
        assert snap is not None
        if status is not None:
            snap.status = status
        if message is not None:
            snap.message = message
        self.emit("transfer_tx", snapshot=snap)

    def _finish(self, status: str, message: str) -> None:
        if self.snapshot is not None:
            self.snapshot.status = status
            self.snapshot.message = message
            self.emit("transfer_tx", snapshot=self.snapshot)

    def _fail(self, message: str) -> None:
        log.info("transfer failed: %s", message)
        self._finish(TX_FAILED, message)

    def _queue_depth(self) -> int | None:
        status = self.get_status()
        if not status:
            return None
        value = status.get("tx_queue")
        return None if value is None else int(value)

    def _wait_drain(self, timeout: float) -> bool:
        """Wait until tx_queue == 0 and channel not transmitting."""
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            if self._stopped():
                return False
            status = self.get_status() or {}
            depth = status.get("tx_queue")
            state = status.get("channel_state")
            if (depth is None or int(depth) == 0) and state != "tx":
                return True
            if self._wait(PACING_POLL_S):
                return False
        return False

    def _send_start(self, frame: bytes, times: int) -> bool:
        for _ in range(times):
            if not self.send_frame(frame):
                self._fail("KISS link unavailable while queueing file start")
                return False
        return True

    def _run(self, path: str) -> None:
        snap = self.snapshot
        assert snap is not None

        sha = hashlib.sha256()
        with open(path, "rb") as fh:
            while block := fh.read(1 << 20):
                sha.update(block)
                if self._stopped():
                    self._finish(TX_CANCELLED, "cancelled")
                    return
        sha256 = sha.digest()

        cfg = self.get_config()
        if cfg is None:
            self._fail("control link required to compute chunk size")
            return
        payload_size = int(cfg.get("payload_size", 0))
        try:
            chunk_size = protocol.chunk_size_for(payload_size)
        except protocol.ProtocolError as exc:
            self._fail(str(exc))
            return
        total = (
            math.ceil(snap.file_size / chunk_size) if snap.file_size else 0
        )
        snap.chunk_size = chunk_size
        snap.total_chunks = total
        transfer_id = protocol.new_transfer_id()
        self.outgoing_ids.add(transfer_id)
        settings = target_settings(cfg)
        self._publish(
            status=TX_SENDING,
            message=f"Broadcasting {total} chunks of {chunk_size} B",
        )
        if total > 200:
            self.emit(
                "notify",
                level="warn",
                message=f"large transfer: {protocol.human_bytes(snap.file_size)} will take a while",
            )

        if self._stopped():
            self._finish(TX_CANCELLED, "cancelled")
            return

        # Re-verify config before chunking (mode may have changed externally).
        cfg = self.get_config()
        if cfg is None:
            self._fail("control link lost before transfer")
            return
        if int(cfg.get("payload_size", 0)) < chunk_size + 20:
            self._fail(
                "modem mode changed; current payload no longer fits the planned chunks"
            )
            return

        flags = FLAG_POSTAMBLE if cfg.get("postamble") else 0
        start = protocol.encode_file_start(
            transfer_id,
            snap.file_size,
            chunk_size,
            total,
            snap.filename,
            sha256,
            settings,
            flags=flags,
        )
        if not self._send_start(start, 2):
            return

        # paced chunk loop
        depth: int | None = None
        fallback_delay = max(0.3, payload_size / 400.0)
        with open(path, "rb") as fh:
            index = 0
            while index < total:
                if self._stopped():
                    self._finish(TX_CANCELLED, "cancelled")
                    return
                if depth is not None and depth >= PACING_HIGH_WATER:
                    while depth is not None and depth > PACING_LOW_WATER:
                        if self._stopped():
                            self._finish(TX_CANCELLED, "cancelled")
                            return
                        self._wait(PACING_POLL_S)
                        depth = self._queue_depth()
                        if depth is None and self.get_status() is None:
                            self._fail("control link lost during transfer")
                            return
                fh.seek(index * chunk_size)
                data = fh.read(chunk_size)
                frame = protocol.encode_file_chunk(transfer_id, index, data)
                if not self.send_frame(frame):
                    self._fail("KISS link lost mid-transfer")
                    return
                index += 1
                depth = None if depth is None else depth + 1
                snap.sent_chunks = index
                snap.sent_bytes = min(snap.file_size, index * chunk_size)
                if index % 8 == 0 or index == total:
                    self._publish()
                if depth is None:
                    if self._wait(fallback_delay):
                        continue  # loop re-checks cancelled/aborted
                    depth = self._queue_depth()

        if self._stopped():
            self._finish(TX_CANCELLED, "cancelled")
            return

        self._publish(message="Waiting for TX drain…")
        if not self._wait_drain(self.drain_timeout):
            if self._stopped():
                self._finish(TX_CANCELLED, "cancelled")
                return
            self.emit(
                "notify",
                level="warn",
                message="TX queue did not drain; sending file end anyway",
            )
        end = protocol.encode_file_end(transfer_id, total, sha256)
        if not self._send_start(end, 2):
            return
        self._wait_drain(self.drain_timeout)
        self._finish(TX_SENT, "Broadcast sent (unconfirmed)")


# ---------------------------------------------------------------------------
# Incoming reassembler


class _Incoming:
    def __init__(
        self,
        transfer_id: int,
        start: protocol.FileStart,
        sender: str,
        path: str,
    ) -> None:
        self.transfer_id = transfer_id
        self.start = start
        self.sender = sender
        self.path = path
        self.file = open(path, "wb")
        self.bitmap = bytearray(math.ceil(start.total_chunks / 8)) if start.total_chunks else bytearray()
        self.received = 0
        self.corrupt = 0
        self.dupes = 0
        self.got_end = False
        self.status = INCOMING_ACTIVE
        self.last_chunk_time = time.monotonic()
        self.finalized = False
        self.message = ""

    def has(self, index: int) -> bool:
        return bool(self.bitmap[index // 8] & (1 << (index % 8)))

    def set(self, index: int) -> None:
        self.bitmap[index // 8] |= 1 << (index % 8)

    def snapshot(self) -> IncomingSnapshot:
        total = self.start.total_chunks
        return IncomingSnapshot(
            transfer_id=self.transfer_id,
            sender=self.sender,
            name=os.path.basename(self.path).removesuffix(".part"),
            file_size=self.start.file_size,
            chunk_size=self.start.chunk_size,
            total_chunks=total,
            received_chunks=self.received,
            missing_chunks=max(0, total - self.received),
            status=self.status,
            path=self.path,
            message=self.message,
        )

    def close(self) -> None:
        try:
            self.file.close()
        except OSError:
            pass


class IncomingFiles:
    """Receives broadcast transfers. Called from the app (Tk) thread only;
    hashing/finalize runs in a short-lived worker."""

    def __init__(
        self,
        downloads_dir: str,
        emit: Callable[..., None],
        outgoing_ids: set[int],
        accept_self: Callable[[], bool],
        idle_timeout: float = INCOMING_IDLE_TIMEOUT_S,
    ) -> None:
        self.downloads_dir = downloads_dir
        self.emit = emit
        self.outgoing_ids = outgoing_ids
        self.accept_self = accept_self
        self.idle_timeout = idle_timeout
        self.transfers: dict[int, _Incoming] = {}
        self.max_records = 64

    def _evict_old(self) -> None:
        """Keep the record list bounded; prefer dropping finished transfers."""
        while len(self.transfers) > self.max_records:
            victim = next(
                (tid for tid, t in self.transfers.items() if t.finalized), None
            )
            if victim is None:
                victim = next(iter(self.transfers))
            self.transfers.pop(victim)

    # -- routing --------------------------------------------------------------

    def handle_start(
        self, env: protocol.Envelope, fs: protocol.FileStart, sender: str = ""
    ) -> None:
        error = self._validate(fs)
        if error:
            log.info("rejecting file_start (%s): %s", env.transfer_id, error)
            return
        if fs.transfer_id in self.outgoing_ids and not self.accept_self():
            return  # our own echo
        if fs.transfer_id in self.transfers:
            return  # duplicate start ignored

        name = protocol.sanitize_filename(fs.name)
        path = self._unique_path(name + ".part")
        try:
            transfer = _Incoming(fs.transfer_id, fs, sender, path)
        except OSError as exc:
            self.emit("notify", level="error", message=f"cannot create {name}: {exc}")
            return
        self.transfers[fs.transfer_id] = transfer
        self._evict_old()
        transfer.message = f"receiving {name}"
        self._update(transfer)

    @staticmethod
    def _validate(fs: protocol.FileStart) -> str | None:
        if not 0 <= fs.file_size <= FILE_MAX_BYTES:
            return f"file size {fs.file_size} out of range"
        if fs.chunk_size < 1:
            return "chunk_size < 1"
        expected = math.ceil(fs.file_size / fs.chunk_size) if fs.file_size else 0
        if fs.total_chunks != expected:
            return f"total_chunks {fs.total_chunks} != expected {expected}"
        if fs.total_chunks > FILE_MAX_CHUNKS:
            return "too many chunks"
        return settings_in_range(fs)
        return None

    def handle_chunk(self, env: protocol.Envelope) -> None:
        transfer = self.transfers.get(env.transfer_id)
        if transfer is None or transfer.finalized:
            return
        index = env.seq
        if index >= transfer.start.total_chunks:
            return
        data, ok = protocol.decode_file_chunk(env)
        if not ok:
            transfer.corrupt += 1
            return
        expected_len = min(
            transfer.start.chunk_size,
            transfer.start.file_size - index * transfer.start.chunk_size,
        )
        if len(data) != expected_len:
            transfer.corrupt += 1
            return
        if transfer.has(index):
            transfer.dupes += 1
            return
        try:
            transfer.file.seek(index * transfer.start.chunk_size)
            transfer.file.write(data)
        except OSError as exc:
            transfer.status = INCOMING_INCOMPLETE
            transfer.message = f"write error: {exc}"
            transfer.close()
            transfer.finalized = True
            self._update(transfer)
            return
        transfer.set(index)
        transfer.received += 1
        transfer.last_chunk_time = time.monotonic()
        if transfer.status == INCOMING_INCOMPLETE and not transfer.finalized:
            transfer.status = INCOMING_ACTIVE  # late chunks revive a timed-out offer
            transfer.message = "resumed"
        if transfer.received % 16 == 0 or transfer.received == transfer.start.total_chunks:
            self._update(transfer)
        if transfer.received == transfer.start.total_chunks:
            self._finalize(transfer)

    def handle_end(self, env: protocol.Envelope) -> None:
        transfer = self.transfers.get(env.transfer_id)
        if transfer is None or transfer.finalized:
            return
        transfer.got_end = True
        if transfer.received < transfer.start.total_chunks:
            transfer.status = INCOMING_INCOMPLETE
            transfer.message = (
                f"{transfer.start.total_chunks - transfer.received} chunks missing"
            )
            self._update(transfer)
        else:
            self._finalize(transfer)  # empty files complete on FILE_END

    def sweep(self) -> None:
        """Mark idle transfers incomplete (called periodically from the pump)."""
        now = time.monotonic()
        for transfer in self.transfers.values():
            if transfer.finalized or transfer.status != INCOMING_ACTIVE:
                continue
            if now - transfer.last_chunk_time > self.idle_timeout:
                if transfer.status == INCOMING_ACTIVE:
                    transfer.status = INCOMING_INCOMPLETE  # keep .part openable
                    transfer.message = "timed out (idle)"
                    self._update(transfer)

    def close_all(self) -> None:
        for transfer in self.transfers.values():
            if not transfer.finalized:
                transfer.close()
                transfer.finalized = True

    # -- finalize ----------------------------------------------------------------

    def _finalize(self, transfer: _Incoming) -> None:
        transfer.close()
        transfer.finalized = True

        def worker() -> None:
            digest = hashlib.sha256()
            try:
                with open(transfer.path, "rb") as fh:
                    while block := fh.read(1 << 20):
                        digest.update(block)
            except OSError:
                transfer.status = INCOMING_CORRUPT
                transfer.message = "cannot re-read .part file"
                self.emit("transfer_rx", snapshot=transfer.snapshot())
                return
            if digest.digest() == transfer.start.sha256:
                final = self._unique_final_name(
                    os.path.splitext(transfer.path)[0]
                )
                try:
                    os.replace(transfer.path, final)
                    transfer.status = INCOMING_COMPLETE
                    transfer.message = "sha256 verified"
                    transfer.path = final
                except OSError as exc:
                    transfer.status = INCOMING_CORRUPT
                    transfer.message = f"rename failed: {exc}"
            else:
                transfer.status = INCOMING_CORRUPT
                transfer.message = "sha256 mismatch"
            self.emit("transfer_rx", snapshot=transfer.snapshot())

        threading.Thread(target=worker, name="m73gui-hash", daemon=True).start()

    # -- helpers ---------------------------------------------------------------

    def _unique_path(self, name: str) -> str:
        os.makedirs(self.downloads_dir, exist_ok=True)
        path = os.path.join(self.downloads_dir, name)
        counter = 1
        while os.path.exists(path):
            stem, dot, ext = name.rpartition(".")
            if not dot:
                stem, ext = name, ""
            path = os.path.join(self.downloads_dir, f"{stem}-{counter}{dot}{ext}")
            counter += 1
        return path

    def _unique_final_name(self, base: str) -> str:
        path = base
        counter = 1
        while os.path.exists(path):
            path = f"{base}-{counter}"
            counter += 1
        return path

    def _update(self, transfer: _Incoming) -> None:
        self.emit("transfer_rx", snapshot=transfer.snapshot())
