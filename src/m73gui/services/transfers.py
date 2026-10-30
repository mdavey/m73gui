"""Outgoing file sender and incoming reassembler for rsfec block streams.

A transfer is a flat stream of rsfec blocks: block i goes out as a
FILE_BLOCK packet with envelope seq == i, and every packet repeats the FEC
level and packet total. There are no start/end frames; a receiver bootstraps
from any single packet and repairs whole lost blocks with RS erasure decoding
once the stream idles out (or completes it immediately when nothing was lost).

All logic is transport-agnostic: the sender takes callables for frame sending,
status/config access, sleeping, and clock, so the full sequence can be unit
tested with fakes.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Callable

import rsfec
from rsfec.geometry import total_blocks_for
from rsfec.header import header_length, parse_header

from .. import protocol
from ..constants import (
    FILE_MAX_BLOCKS,
    FILE_MAX_BYTES,
    INCOMING_IDLE_TIMEOUT_S,
    PACING_HIGH_WATER,
    PACING_LOW_WATER,
    PACING_POLL_S,
    TX_DRAIN_TIMEOUT_S,
)
from ..state import (
    INCOMING_ACTIVE,
    INCOMING_COMPLETE,
    INCOMING_FAILED,
    INCOMING_RECOVERING,
    TX_CANCELLED,
    TX_FAILED,
    TX_PENDING,
    TX_SENDING,
    TX_SENT,
    IncomingSnapshot,
    OutgoingSnapshot,
)

log = logging.getLogger("m73gui.transfers")

DEFAULT_FEC = 2
SMALL_BLOCK_WARN = 128  # below this the 21 B header dominates airtime


def blocks_needed(name: str, file_size: int, block_size: int, fec: int) -> int:
    """Packed block count (data + parity) a file of *file_size* bytes needs."""
    data_blocks = max(
        1, math.ceil((header_length(name) + file_size) / block_size)
    )
    return total_blocks_for(fec, data_blocks)


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

    def start(self, path: str, fec: int = DEFAULT_FEC) -> str | None:
        """Begin transfer; returns an error string or None."""
        if self.busy():
            return "a transfer is already running"
        if fec not in (1, 2, 3, 4):
            return f"invalid FEC level {fec!r}"
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return f"cannot read file: {exc}"
        if size < 1:
            return "file is empty"
        if size > FILE_MAX_BYTES:
            return (
                f"file too large ({protocol.human_bytes(size)}); "
                f"maximum is {protocol.human_bytes(FILE_MAX_BYTES)}"
            )
        snap = OutgoingSnapshot(
            filename=os.path.basename(path),
            file_size=size,
            fec=fec,
            status=TX_PENDING,
            message="Packing…",
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

    def _run(self, path: str) -> None:
        snap = self.snapshot
        assert snap is not None

        cfg = self.get_config()
        if cfg is None:
            self._fail("control link required to compute block size")
            return
        payload_size = int(cfg.get("payload_size", 0))
        try:
            block_size = protocol.block_size_for(payload_size)
        except protocol.ProtocolError as exc:
            self._fail(str(exc))
            return

        try:
            packed = rsfec.pack(
                path, block_size=block_size, fec=snap.fec
            )
        except (OSError, ValueError, rsfec.RsfecError) as exc:
            self._fail(f"packing failed: {exc}")
            return
        total = packed.total_blocks
        if total > FILE_MAX_BLOCKS:
            self._fail(
                f"file needs {total} blocks (max {FILE_MAX_BLOCKS})"
            )
            return
        snap.block_size = block_size
        snap.total_blocks = total
        transfer_id = protocol.new_transfer_id()
        self.outgoing_ids.add(transfer_id)
        self._publish(
            status=TX_SENDING,
            message=(
                f"Broadcasting {total} blocks of {block_size} B "
                f"(FEC L{snap.fec}, +{packed.parity_blocks} parity)"
            ),
        )
        if block_size < SMALL_BLOCK_WARN:
            self.emit(
                "notify",
                level="warn",
                message=(
                    f"small {block_size} B blocks: framing overhead dominates "
                    "airtime on this mode"
                ),
            )
        if total > 200:
            self.emit(
                "notify",
                level="warn",
                message=(
                    f"large transfer: {protocol.human_bytes(snap.file_size)} "
                    f"will take a while ({total} blocks incl. parity)"
                ),
            )

        if self._stopped():
            self._finish(TX_CANCELLED, "cancelled")
            return

        # Re-verify config before streaming (mode may have changed externally).
        cfg = self.get_config()
        if cfg is None:
            self._fail("control link lost before transfer")
            return
        if int(cfg.get("payload_size", 0)) < block_size + 21:
            self._fail(
                "modem mode changed; current payload no longer fits the "
                "planned blocks"
            )
            return

        # paced block loop
        depth: int | None = None
        fallback_delay = max(0.3, payload_size / 400.0)
        for index in range(total):
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
            block = packed.stream[index * block_size : (index + 1) * block_size]
            frame = protocol.encode_file_block(
                transfer_id, index, snap.fec, total, block
            )
            if not self.send_frame(frame):
                self._fail("KISS link lost mid-transfer")
                return
            snap.sent_blocks = index + 1
            queued_data = (index + 1) * block_size - header_length(snap.filename)
            snap.sent_bytes = min(snap.file_size, max(0, queued_data))
            if index % 8 == 0 or index == total - 1:
                self._publish()
            depth = None if depth is None else depth + 1
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
                message="TX queue did not drain before timeout",
            )
        self._finish(TX_SENT, "Broadcast sent (unconfirmed)")


# ---------------------------------------------------------------------------
# Incoming reassembler


class _Stream:
    """One (sender, transfer_id) file stream buffered entirely in RAM."""

    def __init__(
        self, transfer_id: int, sender: str, fec: int, total: int, block_size: int
    ) -> None:
        self.transfer_id = transfer_id
        self.sender = sender
        self.fec = fec
        self.total = total
        self.block_size = block_size
        self.buf = bytearray(total * block_size)
        self.received: set[int] = set()
        self.status = INCOMING_ACTIVE
        self.message = "receiving"
        self.path = ""
        self.name = ""
        self.file_size = 0
        self.name_known = False
        self.attempted = False
        self.retried_full = False
        self.last_block_time = time.monotonic()

    @property
    def finalized(self) -> bool:
        return self.status in (INCOMING_COMPLETE, INCOMING_FAILED)

    def snapshot(self) -> IncomingSnapshot:
        return IncomingSnapshot(
            transfer_id=self.transfer_id,
            sender=self.sender,
            name=self.name,
            file_size=self.file_size,
            block_size=self.block_size,
            total_blocks=self.total,
            received_blocks=len(self.received),
            status=self.status,
            path=self.path,
            message=self.message,
        )


class IncomingFiles:
    """Receives broadcast block streams. handle_block runs on the app (Tk)
    thread; RS repair/verification runs in a short-lived worker."""

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
        self.streams: dict[tuple[str, int], _Stream] = {}
        self.max_records = 64

    def _evict_old(self) -> None:
        """Keep the record list bounded; prefer dropping finished transfers."""
        while len(self.streams) > self.max_records:
            victim = next(
                (k for k, s in self.streams.items() if s.finalized), None
            )
            if victim is None:
                victim = next(iter(self.streams))
            self.streams.pop(victim)

    # -- routing --------------------------------------------------------------

    def handle_block(
        self, env: protocol.Envelope, fec: int, total: int, block: bytes,
        sender: str = "",
    ) -> None:
        tid = env.transfer_id
        if tid in self.outgoing_ids and not self.accept_self():
            return  # our own echo
        key = (sender, tid)
        stream = self.streams.get(key)
        if stream is None:
            if not 1 <= total <= FILE_MAX_BLOCKS:
                log.info("rejecting stream %s: total_packets %s", tid, total)
                return
            stream = _Stream(tid, sender, fec, total, len(block))
            self.streams[key] = stream
            self._evict_old()
        elif fec != stream.fec or total != stream.total or len(block) != stream.block_size:
            log.info(
                "mismatched file_block (tid=%s seq=%s): ignoring", tid, env.seq
            )
            return

        seq = env.seq
        if seq >= stream.total or seq in stream.received:
            return
        b = stream.block_size
        stream.buf[seq * b : (seq + 1) * b] = block
        stream.received.add(seq)
        stream.last_block_time = time.monotonic()
        if stream.status == INCOMING_FAILED:
            # late blocks revive a failed stream for another sweep attempt
            stream.status = INCOMING_ACTIVE
            stream.attempted = False
            stream.retried_full = False
            stream.message = "resumed"
        self._peek(stream)
        self._update(stream)
        if len(stream.received) == stream.total and not stream.attempted:
            self._attempt(stream, set())

    def sweep(self) -> None:
        """Attempt RS repair on streams that idled out (periodic pump call)."""
        now = time.monotonic()
        for stream in list(self.streams.values()):
            if stream.status != INCOMING_ACTIVE or stream.attempted:
                continue
            if now - stream.last_block_time > self.idle_timeout:
                missing = set(range(stream.total)) - stream.received
                self._attempt(stream, missing)

    def close_all(self) -> None:
        self.streams.clear()

    # -- repair / finalize ------------------------------------------------------

    def _attempt(self, stream: _Stream, missing: set[int]) -> None:
        stream.attempted = True
        stream.status = INCOMING_RECOVERING
        stream.message = (
            "verifying…" if not missing else f"repairing {len(missing)} missing blocks…"
        )
        self._update(stream)
        buf = bytes(stream.buf)  # snapshot; handle_block may keep mutating

        def worker() -> None:
            try:
                unpacked = rsfec.unpack(
                    buf,
                    block_size=stream.block_size,
                    fec=stream.fec,
                    missing=missing or None,
                )
            except (rsfec.RsfecError, ValueError) as exc:
                self._attempt_failed(stream, exc)
                return
            name = protocol.sanitize_filename(unpacked.name)
            path = self._unique_path(name)
            try:
                with open(path, "wb") as fh:
                    fh.write(unpacked.data)
            except OSError as exc:
                stream.status = INCOMING_FAILED
                stream.message = f"write error: {exc}"
                self.emit("transfer_rx", snapshot=stream.snapshot())
                return
            stream.status = INCOMING_COMPLETE
            stream.message = "sha1 verified"
            stream.path = path
            stream.name = name
            stream.file_size = unpacked.size
            self.emit("transfer_rx", snapshot=stream.snapshot())

        threading.Thread(target=worker, name="m73gui-repair", daemon=True).start()

    def _attempt_failed(self, stream: _Stream, exc: Exception) -> None:
        if len(stream.received) >= stream.total and not stream.retried_full:
            # blocks arrived after the buffer snapshot; one retry with the
            # now-complete stream before giving up
            stream.retried_full = True
            stream.attempted = False
            self._attempt(stream, set())
            return
        stream.status = INCOMING_FAILED
        stream.message = f"unrecoverable: {exc}"[:160]
        self.emit("transfer_rx", snapshot=stream.snapshot())

    # -- helpers ---------------------------------------------------------------

    def _peek(self, stream: _Stream) -> None:
        """Reveal name/size as soon as the container header is fully buffered."""
        if stream.name_known:
            return
        n = 0
        while n in stream.received and n < stream.total:
            n += 1
        if n == 0:
            return
        try:
            hdr = parse_header(stream.buf[: n * stream.block_size])
        except rsfec.RsfecError:
            return
        if hdr.fec != stream.fec or hdr.block_size != stream.block_size:
            return  # coincidental bytes; header not really intact
        stream.name = protocol.sanitize_filename(hdr.name)
        stream.file_size = hdr.file_size
        stream.name_known = True

    def _unique_path(self, name: str) -> str:
        """Atomically reserve a non-existing downloads path (workers and
        concurrent streams may race on the same filename)."""
        os.makedirs(self.downloads_dir, exist_ok=True)
        counter = 0
        while True:
            if counter == 0:
                path = os.path.join(self.downloads_dir, name)
            else:
                stem, dot, ext = name.rpartition(".")
                if not dot:
                    stem, ext = name, ""
                path = os.path.join(
                    self.downloads_dir, f"{stem}-{counter}{dot}{ext}"
                )
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                counter += 1
                continue
            os.close(fd)
            return path

    def _update(self, stream: _Stream) -> None:
        self.emit("transfer_rx", snapshot=stream.snapshot())
