from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest

import rsfec

from m73gui import protocol
from m73gui.constants import (
    FILE_MAX_BLOCKS,
    FILE_MAX_BYTES,
    KIND_FILE_BLOCK,
)
from m73gui.services.transfers import FileSender, IncomingFiles
from m73gui.state import (
    INCOMING_ACTIVE,
    INCOMING_COMPLETE,
    INCOMING_FAILED,
    TX_CANCELLED,
    TX_FAILED,
    TX_SENT,
)

BASE_CONFIG = {
    "callsign": "VK2GUI",
    "modem_type": 0,
    "modulation": "QPSK",
    "code_rate": "1/2",
    "frame_size": "normal",
    "mfsk_mode": 0,
    "robust_mode": 0,
    "payload_size": 512,
    "postamble": False,
}

BLOCK = protocol.block_size_for(BASE_CONFIG["payload_size"])  # 491


class FakeStation:
    """Fake modem73: config, status, KISS sink."""

    def __init__(self, config=None, status=None):
        self.config = dict(config or BASE_CONFIG)
        self.status = status if status is not None else {
            "tx_queue": 0,
            "channel_state": "idle",
        }
        self.sent: list[bytes] = []
        self.link_up = True
        self._lock = threading.Lock()

    def send_frame(self, payload: bytes) -> bool:
        if not self.link_up:
            return False
        with self._lock:
            self.sent.append(payload)
        return True

    def get_status(self):
        return dict(self.status) if self.link_up else None

    def get_config(self):
        if not self.link_up:
            return None
        return dict(self.config)

    def decoded(self):
        out = []
        for payload in self.sent:
            try:
                env = protocol.decode_envelope(payload)
            except protocol.ProtocolError:
                env = None
            out.append((payload, env))
        return out

    def by_kind(self, kind: int):
        return [env for _, env in self.decoded() if env and env.kind == kind]


def fast_sleep(abort: threading.Event):
    def sleep(seconds: float) -> bool:
        # spin briefly so time-based deadlines still advance
        end = time.monotonic() + min(seconds, 0.01)
        while time.monotonic() < end:
            time.sleep(0.001)
        return abort.is_set()

    return sleep


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class SenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.events: list[tuple[str, dict]] = []
        self.outgoing_ids: set[int] = set()

        def emit(kind, **data):
            self.events.append((kind, data))

        self.emit = emit

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_file(self, size, seed=b"f", name="payload.bin"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write((seed * (size + 1))[:size])
        return path

    def make_sender(self, station: FakeStation):
        abort = threading.Event()
        sender = FileSender(
            emit=self.emit,
            send_frame=station.send_frame,
            get_status=station.get_status,
            get_config=station.get_config,
            outgoing_ids=self.outgoing_ids,
            sleep=fast_sleep(abort),
        )
        sender._test_abort = abort
        return sender

    def run_sender(self, sender, path, fec=2):
        error = sender.start(path, fec=fec)
        self.assertIsNone(error)
        self.assertTrue(
            wait_until(lambda: sender.snapshot.status in
                       (TX_SENT, TX_FAILED, TX_CANCELLED) or not sender.busy()),
            "sender did not finish",
        )
        sender._thread.join(timeout=2)

    def collect_blocks(self, station):
        """(transfer_id, fec, total, {seq: block}) from sent frames."""
        blocks = {}
        tids = set()
        fecs = set()
        totals = set()
        for env in station.by_kind(KIND_FILE_BLOCK):
            fec, total, block = protocol.decode_file_block(env)
            tids.add(env.transfer_id)
            fecs.add(fec)
            totals.add(total)
            self.assertIsNone(blocks.get(env.seq))  # each seq sent once
            blocks[env.seq] = block
        self.assertEqual(len(tids), 1)
        self.assertEqual(len(fecs), 1)
        self.assertEqual(len(totals), 1)
        return tids.pop(), fecs.pop(), totals.pop(), blocks

    def test_normal_transfer_frames(self):
        station = FakeStation()
        path = self.make_file(1000)
        sender = self.make_sender(station)
        self.run_sender(sender, path)
        self.assertEqual(sender.snapshot.status, TX_SENT, sender.snapshot.message)

        tid, fec, total, blocks = self.collect_blocks(station)
        self.assertEqual(fec, 2)
        self.assertEqual(total, sender.snapshot.total_blocks)
        self.assertEqual(sorted(blocks), list(range(total)))
        # no start/end frames anymore, only blocks
        self.assertEqual(len(station.sent), total)

        # the block stream IS the container: unpack recovers the file
        stream = b"".join(blocks[i] for i in range(total))
        unpacked = rsfec.unpack(stream, block_size=BLOCK, fec=fec)
        self.assertEqual(unpacked.name, "payload.bin")
        with open(path, "rb") as fh:
            self.assertEqual(unpacked.data, fh.read())
        self.assertEqual(sender.snapshot.sent_blocks, total)
        self.assertEqual(sender.snapshot.sent_bytes, 1000)
        self.assertIn(tid, self.outgoing_ids)

    def test_fec_levels(self):
        for fec, min_extra in ((1, 1), (2, 1), (3, 1), (4, 1)):
            station = FakeStation()
            path = self.make_file(3000, seed=b"x")
            sender = self.make_sender(station)
            self.run_sender(sender, path, fec=fec)
            self.assertEqual(sender.snapshot.status, TX_SENT, sender.snapshot.message)
            _tid, fec_out, total, blocks = self.collect_blocks(station)
            self.assertEqual(fec_out, fec)
            self.assertGreater(total, 3000 // BLOCK + 1)  # parity included
            stream = b"".join(blocks[i] for i in range(total))
            unpacked = rsfec.unpack(stream, block_size=BLOCK, fec=fec)
            self.assertEqual(len(unpacked.data), 3000)

    def test_rejects_empty_file(self):
        station = FakeStation()
        sender = self.make_sender(station)
        error = sender.start(self.make_file(0))
        self.assertIsNotNone(error)
        self.assertIn("empty", error)
        self.assertEqual(len(station.sent), 0)

    def test_rejects_oversized_file(self):
        path = os.path.join(self.tmp, "big.bin")
        with open(path, "wb") as fh:
            fh.truncate(FILE_MAX_BYTES + 1)  # sparse file
        sender = self.make_sender(station := FakeStation())
        error = sender.start(path)
        self.assertIsNotNone(error)
        self.assertIn("too large", error)
        self.assertEqual(len(station.sent), 0)

    def test_rejects_bad_fec(self):
        sender = self.make_sender(FakeStation())
        self.assertIsNotNone(sender.start(self.make_file(10), fec=7))

    def test_kiss_loss_aborts(self):
        station = FakeStation()
        path = self.make_file(BLOCK * 40)

        orig = station.send_frame

        def flaky(payload):
            if len(station.sent) > 5:
                station.link_up = False
            return orig(payload)

        station.send_frame = flaky
        sender = self.make_sender(station)
        self.run_sender(sender, path)
        self.assertEqual(sender.snapshot.status, TX_FAILED)

    def test_cancel(self):
        station = FakeStation()
        path = self.make_file(BLOCK * 200)
        sender = self.make_sender(station)
        sender.start(path)
        wait_until(lambda: len(station.by_kind(KIND_FILE_BLOCK)) > 0)
        sender.cancel()
        self.assertTrue(wait_until(lambda: sender.snapshot.status == TX_CANCELLED))

    def test_mode_change_makes_frames_too_large(self):
        station = FakeStation()
        path = self.make_file(100)
        orig = station.get_config
        calls = {"n": 0}

        def shrinking():
            calls["n"] += 1
            cfg = orig()
            if calls["n"] >= 2:  # after initial sizing: drop to a small MTU
                cfg["payload_size"] = 100
            return cfg

        station.get_config = shrinking
        sender = self.make_sender(station)
        self.run_sender(sender, path)
        self.assertEqual(sender.snapshot.status, TX_FAILED)
        self.assertIn("payload", sender.snapshot.message)
        # no blocks were sent
        self.assertEqual(len(station.by_kind(KIND_FILE_BLOCK)), 0)


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.events: list[tuple[str, dict]] = []
        self.outgoing_ids: set[int] = set()

        def emit(kind, **data):
            self.events.append((kind, data))

        self.files = IncomingFiles(
            downloads_dir=os.path.join(self.tmp, "dl"),
            emit=emit,
            outgoing_ids=self.outgoing_ids,
            accept_self=lambda: False,
            idle_timeout=0.3,
        )

    def tearDown(self):
        self.files.close_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def snapshots(self):
        return [d["snapshot"] for k, d in self.events if k == "transfer_rx"]

    def get(self, sender, tid):
        return self.files.streams.get((sender, tid))

    def wait_status(self, sender, tid, status, timeout=8.0):
        # the app pump normally drives sweep(); emulate it here so idle
        # streams reach their repair attempt
        def probe():
            self.files.sweep()
            stream = self.get(sender, tid)
            return stream is not None and stream.status == status

        ok = wait_until(probe, timeout=timeout)
        self.assertTrue(ok, f"stream {sender}/{tid} never reached {status}")

    def pack(self, data, name="file.bin", fec=2):
        return rsfec.pack_bytes(data, name=name, block_size=BLOCK, fec=fec)

    def broadcast(self, data, name="file.bin", drop=frozenset(),
                  tid=None, duplicates=(), sender="VK9RX", fec=2):
        tid = tid or protocol.new_transfer_id()
        packed = self.pack(data, name=name, fec=fec)
        total = packed.total_blocks
        for i in range(total):
            if i in drop:
                continue
            block = packed.stream[i * BLOCK : (i + 1) * BLOCK]
            env = protocol.decode_envelope(
                protocol.encode_file_block(tid, i, fec, total, block)
            )
            self.files.handle_block(env, fec, total, block, sender=sender)
            if i in duplicates:
                self.files.handle_block(env, fec, total, block, sender=sender)
        return tid, packed

    def test_complete_transfer(self):
        data = os.urandom(2000)
        tid, packed = self.broadcast(data)
        self.wait_status("VK9RX", tid, INCOMING_COMPLETE)
        stream = self.get("VK9RX", tid)
        self.assertEqual(len(stream.received), packed.total_blocks)
        self.assertFalse(stream.path.endswith(".part"))
        with open(stream.path, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertEqual(os.path.dirname(stream.path), os.path.join(self.tmp, "dl"))

    def test_dropped_blocks_repaired_after_idle(self):
        # data+header spans 5 data blocks -> L2 gives 1 parity per group
        data = os.urandom(2000)
        tid, packed = self.broadcast(data, drop={1})
        self.assertEqual(len(self.get("VK9RX", tid).received), packed.total_blocks - 1)
        # sweep sees the idle stream and repairs from parity
        self.wait_status("VK9RX", tid, INCOMING_COMPLETE, timeout=8.0)
        stream = self.get("VK9RX", tid)
        with open(stream.path, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertIn("repairing", " ".join(
            s.message for s in self.snapshots()
        ))

    def test_over_budget_marks_failed(self):
        data = os.urandom(2000)
        # two blocks of a group with parity 1 is beyond the budget
        tid, _packed = self.broadcast(data, drop={0, 1})
        self.wait_status("VK9RX", tid, INCOMING_FAILED, timeout=8.0)
        snaps = self.snapshots()
        self.assertTrue(any(s.status == INCOMING_FAILED for s in snaps))
        self.assertTrue(any("unrecoverable" in s.message for s in snaps))

    def test_late_blocks_revive_failed_stream(self):
        data = os.urandom(2000)
        tid, packed = self.broadcast(data, drop={0, 1})
        self.wait_status("VK9RX", tid, INCOMING_FAILED, timeout=8.0)
        # resend the whole thing; the second pass completes it cleanly
        self.broadcast(data, tid=tid)
        self.wait_status("VK9RX", tid, INCOMING_COMPLETE, timeout=8.0)
        stream = self.get("VK9RX", tid)
        with open(stream.path, "rb") as fh:
            self.assertEqual(fh.read(), data)

    def test_duplicates_ignored(self):
        data = os.urandom(1500)
        tid, packed = self.broadcast(data, duplicates={0, 2})
        self.wait_status("VK9RX", tid, INCOMING_COMPLETE)
        stream = self.get("VK9RX", tid)
        self.assertEqual(len(stream.received), packed.total_blocks)

    def test_seq_beyond_total_and_mismatch_ignored(self):
        data = os.urandom(1500)
        tid, packed = self.broadcast(data)
        self.wait_status("VK9RX", tid, INCOMING_COMPLETE)
        before = len(self.get("VK9RX", tid).received)
        # out-of-range seq on a fresh-looking stream with same key is ignored
        # (completed streams keep no writable slack)
        env = protocol.decode_envelope(
            protocol.encode_file_block(tid, packed.total_blocks + 5, 2,
                                       packed.total_blocks, b"\x00" * BLOCK)
        )
        self.files.handle_block(
            env, 2, packed.total_blocks, b"\x00" * BLOCK, sender="VK9RX"
        )
        # same tid but a different block size is a mismatch -> ignored
        env = protocol.decode_envelope(
            protocol.encode_file_block(tid, 0, 2, packed.total_blocks, b"\x00" * 64)
        )
        self.files.handle_block(
            env, 2, packed.total_blocks, b"\x00" * 64, sender="VK9RX"
        )
        self.assertEqual(len(self.get("VK9RX", tid).received), before)

    def test_name_revealed_from_block_zero(self):
        data = os.urandom(2000)
        tid = protocol.new_transfer_id()
        packed = self.pack(data, name="peek.txt")
        total = packed.total_blocks
        block = packed.stream[0:BLOCK]
        env = protocol.decode_envelope(
            protocol.encode_file_block(tid, 0, 2, total, block)
        )
        self.files.handle_block(env, 2, total, block, sender="VK9RX")
        stream = self.get("VK9RX", tid)
        self.assertEqual(stream.name, "peek.txt")
        self.assertEqual(stream.file_size, 2000)
        self.assertEqual(stream.total, total)

    def test_snapshot_progress_from_first_packet(self):
        data = os.urandom(2000)
        planned = self.pack(data)
        tid, packed = self.broadcast(
            data, drop=set(range(1, planned.total_blocks))
        )
        snaps = [s for s in self.snapshots() if s.transfer_id == tid]
        first = snaps[0]
        self.assertEqual(first.total_blocks, packed.total_blocks)
        self.assertEqual(first.received_blocks, 1)
        self.assertEqual(first.status, INCOMING_ACTIVE)

    def test_self_echo_suppressed_and_toggle(self):
        data = os.urandom(100)
        tid = protocol.new_transfer_id()
        self.outgoing_ids.add(tid)
        self.broadcast(data, tid=tid)
        self.assertIsNone(self.get("VK9RX", tid))
        # with loopback accept on, a fresh broadcast is received
        self.files.accept_self = lambda: True
        tid2, _ = self.broadcast(data, tid=protocol.new_transfer_id())
        self.wait_status("VK9RX", tid2, INCOMING_COMPLETE)

    def test_two_senders_same_tid_isolated(self):
        data1 = os.urandom(400)
        data2 = os.urandom(500)
        tid = protocol.new_transfer_id()
        self.broadcast(data1, tid=tid, sender="AAA")
        self.broadcast(data2, tid=tid, sender="BBB")
        self.wait_status("AAA", tid, INCOMING_COMPLETE)
        self.wait_status("BBB", tid, INCOMING_COMPLETE)
        with open(self.get("AAA", tid).path, "rb") as fh:
            self.assertEqual(fh.read(), data1)
        with open(self.get("BBB", tid).path, "rb") as fh:
            self.assertEqual(fh.read(), data2)

    def test_path_traversal_name_sanitized(self):
        data = os.urandom(50)
        tid, _ = self.broadcast(data, name="../../evil.txt")
        self.wait_status("VK9RX", tid, INCOMING_COMPLETE)
        stream = self.get("VK9RX", tid)
        self.assertEqual(os.path.basename(stream.path), "evil.txt")
        self.assertTrue(stream.path.startswith(os.path.join(self.tmp, "dl")))

    def test_name_collision_gets_suffix(self):
        data = os.urandom(50)
        tid1, _ = self.broadcast(data, name="dup.txt")
        self.wait_status("VK9RX", tid1, INCOMING_COMPLETE)
        tid2, _ = self.broadcast(b"second!", name="dup.txt")
        self.wait_status("VK9RX", tid2, INCOMING_COMPLETE)
        p1 = self.get("VK9RX", tid1).path
        p2 = self.get("VK9RX", tid2).path
        self.assertNotEqual(p1, p2)
        with open(p2, "rb") as fh:
            self.assertEqual(fh.read(), b"second!")

    def test_absurd_total_rejected(self):
        tid = protocol.new_transfer_id()
        env = protocol.decode_envelope(
            protocol.encode_file_block(tid, 0, 2, FILE_MAX_BLOCKS + 1, b"x" * 64)
        )
        self.files.handle_block(
            env, 2, FILE_MAX_BLOCKS + 1, b"x" * 64, sender="VK9RX"
        )
        self.assertEqual(len(self.files.streams), 0)


if __name__ == "__main__":
    unittest.main()
