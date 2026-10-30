from __future__ import annotations

import hashlib
import os
import queue
import shutil
import tempfile
import threading
import time
import unittest

from m73gui import protocol
from m73gui.constants import (
    FLAG_ROBUST_ANNOUNCED,
    KIND_FILE_CHUNK,
    KIND_FILE_END,
    KIND_FILE_START,
)
from m73gui.services.transfers import (
    FileSender,
    IncomingFiles,
    announced_settings_to_fields,
    target_settings,
)
from m73gui.state import (
    INCOMING_COMPLETE,
    INCOMING_CORRUPT,
    INCOMING_INCOMPLETE,
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


class FakeStation:
    """Fake modem73: config, status, KISS sink."""

    def __init__(self, config=None, status=None):
        self.config = dict(config or BASE_CONFIG)
        self.status = status if status is not None else {
            "tx_queue": 0,
            "channel_state": "idle",
        }
        self.sent: list[bytes] = []
        self.set_calls: list[dict] = []
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

    def set_config(self, fields: dict) -> bool:
        self.set_calls.append(dict(fields))
        self.config.update(fields)
        return True

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

    def make_file(self, size, seed=b"f"):
        path = os.path.join(self.tmp, "payload.bin")
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
            set_config=station.set_config,
            outgoing_ids=self.outgoing_ids,
            sleep=fast_sleep(abort),
        )
        sender._test_abort = abort
        return sender

    def last_tx_status(self):
        for kind, data in reversed(self.events):
            if kind == "transfer_tx":
                return data["snapshot"]
        return None

    def run_sender(self, sender, path, robust=False):
        sender.start(path, robust)
        self.assertTrue(
            wait_until(lambda: sender.snapshot.status in
                       (TX_SENT, TX_FAILED, TX_CANCELLED) or not sender.busy()),
            "sender did not finish",
        )
        sender._thread.join(timeout=2)

    def test_normal_transfer_frames(self):
        station = FakeStation()
        path = self.make_file(1000)
        sha = hashlib.sha256(open(path, "rb").read()).digest()
        sender = self.make_sender(station)
        self.run_sender(sender, path)
        self.assertEqual(sender.snapshot.status, TX_SENT, sender.snapshot.message)

        starts = station.by_kind(KIND_FILE_START)
        ends = station.by_kind(KIND_FILE_END)
        chunks = station.by_kind(KIND_FILE_CHUNK)

        chunk_size = protocol.chunk_size_for(BASE_CONFIG["payload_size"])  # 492
        total = 3  # ceil(1000/492)
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(ends), 2)
        self.assertEqual(len(chunks), total)
        tid = starts[0].transfer_id
        self.assertEqual(len({c.transfer_id for c in chunks + starts + ends}), 1)
        fs = protocol.decode_file_start(starts[0])
        self.assertEqual(fs.file_size, 1000)
        self.assertEqual(fs.chunk_size, chunk_size)
        self.assertEqual(fs.total_chunks, total)
        self.assertEqual(fs.sha256, sha)
        self.assertEqual(fs.flags & FLAG_ROBUST_ANNOUNCED, 0)
        self.assertEqual(fs.modem_type, 0)
        self.assertEqual(fs.modulation, 1)  # QPSK

        # chunk payloads reassemble the original file
        with open(path, "rb") as fh:
            expected = fh.read()
        data = bytearray()
        for env in sorted(chunks, key=lambda e: e.seq):
            chunk, ok = protocol.decode_file_chunk(env)
            self.assertTrue(ok)
            data += chunk
        self.assertEqual(bytes(data), expected)
        total_out, sha_out = protocol.decode_file_end(ends[0])
        self.assertEqual(total_out, total)
        self.assertEqual(sha_out, sha)
        self.assertEqual(sender.snapshot.sent_chunks, total)
        self.assertIn(tid, self.outgoing_ids)

    def test_empty_file(self):
        station = FakeStation()
        path = self.make_file(0)
        sender = self.make_sender(station)
        self.run_sender(sender, path)
        self.assertEqual(sender.snapshot.status, TX_SENT)
        self.assertEqual(len(station.by_kind(KIND_FILE_CHUNK)), 0)
        self.assertEqual(len(station.by_kind(KIND_FILE_START)), 2)
        self.assertEqual(len(station.by_kind(KIND_FILE_END)), 2)

    def test_kiss_loss_aborts(self):
        station = FakeStation()
        path = self.make_file(492 * 40)

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
        path = self.make_file(492 * 200)
        sender = self.make_sender(station)
        sender.start(path, False)
        wait_until(lambda: len(station.by_kind(KIND_FILE_CHUNK)) > 0)
        sender.cancel()
        self.assertTrue(wait_until(lambda: sender.snapshot.status == TX_CANCELLED))

    def test_robust_announce_sequence(self):
        station = FakeStation()
        path = self.make_file(1500)
        sender = self.make_sender(station)
        self.run_sender(sender, path, robust=True)
        self.assertEqual(sender.snapshot.status, TX_SENT, sender.snapshot.message)

        # switch to ROBUST then restore original modem fields
        self.assertEqual(station.set_calls[0], {"modem_type": 2, "robust_mode": 5})
        restore = station.set_calls[-1]
        self.assertEqual(restore["modem_type"], 0)
        self.assertEqual(restore["modulation"], "QPSK")
        self.assertEqual(station.config["modem_type"], 0)  # restored

        starts = station.by_kind(KIND_FILE_START)
        self.assertEqual(len(starts), 3)  # announced: 3x, not 2x more
        self.assertTrue(all(s.flags & FLAG_ROBUST_ANNOUNCED for s in map(
            protocol.decode_file_start, starts)))
        # target settings describe the post-announcement (original) mode
        fs = protocol.decode_file_start(starts[0])
        self.assertEqual(fs.modem_type, 0)
        # announcement precedes chunks in wire order
        kinds = [env.kind for _, env in station.decoded() if env]
        self.assertEqual(kinds[:3], [KIND_FILE_START] * 3)
        self.assertLess(kinds.index(KIND_FILE_CHUNK), kinds.index(KIND_FILE_END))

    def test_announce_switch_unconfirmed_fails_and_restores(self):
        station = FakeStation()
        # set_config records but does not apply -> verification fails
        station.set_config = lambda fields: (station.set_calls.append(dict(fields)), True)[1]
        path = self.make_file(100)
        sender = self.make_sender(station)
        self.run_sender(sender, path, robust=True)
        self.assertEqual(sender.snapshot.status, TX_FAILED)
        # restore attempts after the failed switch (retry loop up to 3)
        self.assertGreaterEqual(len(station.set_calls), 2)
        self.assertEqual(station.set_calls[-1]["modem_type"], 0)

    def test_announce_skipped_when_busy(self):
        station = FakeStation(status={"tx_queue": 3, "channel_state": "tx"})
        path = self.make_file(100)
        sender = self.make_sender(station)
        sender.quiet_timeout = 0.2
        sender.drain_timeout = 0.3
        self.run_sender(sender, path, robust=True)
        # skipped announcement but normal send proceeds
        self.assertEqual(sender.snapshot.status, TX_SENT, sender.snapshot.message)
        self.assertEqual(len(station.set_calls), 0)
        self.assertEqual(len(station.by_kind(KIND_FILE_START)), 2)

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
        # no chunks were sent
        self.assertEqual(len(station.by_kind(KIND_FILE_CHUNK)), 0)


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.events: list[tuple[str, dict]] = []
        self.outgoing_ids: set[int] = set()
        self.applied: list[tuple[dict, str]] = []

        def emit(kind, **data):
            self.events.append((kind, data))

        self.files = IncomingFiles(
            downloads_dir=os.path.join(self.tmp, "dl"),
            emit=emit,
            outgoing_ids=self.outgoing_ids,
            accept_self=lambda: False,
            apply_announced=lambda: True,
            apply_settings=lambda f, label: self.applied.append((f, label)),
            idle_timeout=0.3,
        )

    def tearDown(self):
        self.files.close_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def snapshots(self):
        return [d["snapshot"] for k, d in self.events if k == "transfer_rx"]

    def wait_status(self, tid, status, timeout=5.0):
        ok = wait_until(
            lambda: self.files.transfers.get(tid)
            and self.files.transfers[tid].status == status
        )
        self.assertTrue(ok, f"transfer {tid} never reached {status}")

    def broadcast(self, data: bytes, name="file.bin", drop: set[int] = frozenset(),
                  tid: int | None = None, duplicates=(), corrupt=(), start_times=2):
        payload_size = 512
        chunk_size = protocol.chunk_size_for(payload_size)
        tid = tid or protocol.new_transfer_id()
        sha = hashlib.sha256(data).digest()
        total = (len(data) + chunk_size - 1) // chunk_size if data else 0
        settings = target_settings(BASE_CONFIG)
        start = protocol.encode_file_start(
            tid, len(data), chunk_size, total, name, sha, settings, flags=0
        )
        for _ in range(start_times):
            self.files.handle_start(
                protocol.decode_envelope(start), protocol.decode_file_start(
                    protocol.decode_envelope(start)), sender="VK9RX"
            )
        for i in range(total):
            chunk = data[i * chunk_size : (i + 1) * chunk_size]
            if i in corrupt:
                env = protocol.decode_envelope(
                    protocol.encode_envelope(
                        KIND_FILE_CHUNK, tid, i, b"\xff\xff\xff\xff" + chunk
                    )
                )
            elif i in drop:
                continue
            else:
                env = protocol.decode_envelope(
                    protocol.encode_file_chunk(tid, i, chunk)
                )
            self.files.handle_chunk(env)
            if i in duplicates:
                self.files.handle_chunk(env)
        end = protocol.decode_envelope(protocol.encode_file_end(tid, total, sha))
        self.files.handle_end(end)
        return tid, sha

    def test_complete_transfer(self):
        data = os.urandom(2000)
        tid, sha = self.broadcast(data)
        self.wait_status(tid, INCOMING_COMPLETE)
        t = self.files.transfers[tid]
        self.assertEqual(t.received, 5)  # ceil(2000/492)
        final = t.path
        self.assertFalse(final.endswith(".part"))
        self.assertEqual(open(final, "rb").read(), data)
        self.assertEqual(os.path.dirname(final), os.path.join(self.tmp, "dl"))

    def test_dropped_chunks_incomplete(self):
        data = os.urandom(2000)
        tid, _ = self.broadcast(data, drop={1})
        self.wait_status(tid, INCOMING_INCOMPLETE)
        t = self.files.transfers[tid]
        self.assertEqual(t.received, 4)
        self.assertTrue(t.path.endswith(".part"))
        # FILE_END with missing chunks marks incomplete immediately (no sweep)
        snaps = self.snapshots()
        self.assertTrue(any(s.status == INCOMING_INCOMPLETE for s in snaps))

    def test_timeout_marks_incomplete(self):
        data = os.urandom(1500)
        chunk_size = protocol.chunk_size_for(512)
        tid = protocol.new_transfer_id()
        sha = hashlib.sha256(data).digest()
        start = protocol.encode_file_start(
            tid, len(data), chunk_size, 4, "t.bin", sha,
            target_settings(BASE_CONFIG), flags=0,
        )
        self.files.handle_start(
            protocol.decode_envelope(start),
            protocol.decode_file_start(protocol.decode_envelope(start)),
        )
        self.files.handle_chunk(
            protocol.decode_envelope(
                protocol.encode_file_chunk(tid, 0, data[:chunk_size])
            )
        )
        # no FILE_END; sweep after idle timeout
        time.sleep(0.35)
        self.files.sweep()
        self.assertEqual(self.files.transfers[tid].status, INCOMING_INCOMPLETE)
        self.assertTrue(self.files.transfers[tid].path.endswith(".part"))

    def test_duplicates_and_corrupt_chunks(self):
        data = os.urandom(492 * 3)
        tid, _ = self.broadcast(data, duplicates={0}, corrupt={1})
        self.wait_status(tid, INCOMING_INCOMPLETE)
        t = self.files.transfers[tid]
        self.assertEqual(t.dupes, 1)
        self.assertEqual(t.corrupt, 1)
        self.assertEqual(t.received, 2)

    def test_corrupt_hash_marks_corrupt(self):
        data = os.urandom(500)
        tid, _ = self.broadcast(data)
        self.wait_status(tid, INCOMING_COMPLETE)
        # second transfer: same tid can't be reused; craft mismatch by editing
        # file mid-stream is complex; instead assert verified-complete works and
        # hash mismatch path via a direct _finalize on tampered .part
        self.files.transfers.clear()
        payload_size = 512
        chunk_size = protocol.chunk_size_for(payload_size)
        tid2 = protocol.new_transfer_id()
        small = b"tamper-me"
        start = protocol.encode_file_start(
            tid2, len(small), chunk_size, 1, "tamper.bin", b"\x00" * 32,
            target_settings(BASE_CONFIG), flags=0,
        )
        self.files.handle_start(
            protocol.decode_envelope(start),
            protocol.decode_file_start(protocol.decode_envelope(start)),
        )
        self.files.handle_chunk(
            protocol.decode_envelope(protocol.encode_file_chunk(tid2, 0, small))
        )
        self.wait_status(tid2, INCOMING_CORRUPT)
        self.assertIn("sha256 mismatch", self.files.transfers[tid2].message)

    def test_empty_file_via_end(self):
        tid, _ = self.broadcast(b"")
        self.wait_status(tid, INCOMING_COMPLETE)
        t = self.files.transfers[tid]
        self.assertEqual(os.path.getsize(t.path), 0)

    def test_self_echo_suppressed_and_toggle(self):
        data = os.urandom(100)
        tid = protocol.new_transfer_id()
        self.outgoing_ids.add(tid)
        self.broadcast(data, tid=tid)
        self.assertNotIn(tid, self.files.transfers)
        # with loopback accept on, a fresh broadcast is received
        self.files.accept_self = lambda: True
        self.files.transfers.clear()
        tid2, _ = self.broadcast(data, tid=protocol.new_transfer_id())
        self.wait_status(tid2, INCOMING_COMPLETE)

    def test_path_traversal_name_sanitized(self):
        data = os.urandom(50)
        tid, _ = self.broadcast(data, name="../../evil.txt")
        self.wait_status(tid, INCOMING_COMPLETE)
        t = self.files.transfers[tid]
        self.assertEqual(os.path.basename(t.path), "evil.txt")
        self.assertTrue(t.path.startswith(os.path.join(self.tmp, "dl")))

    def test_name_collision_gets_suffix(self):
        data = os.urandom(50)
        tid1, _ = self.broadcast(data, name="dup.txt")
        self.wait_status(tid1, INCOMING_COMPLETE)
        tid2, _ = self.broadcast(b"second!", name="dup.txt")
        self.wait_status(tid2, INCOMING_COMPLETE)
        t1 = self.files.transfers[tid1]
        t2 = self.files.transfers[tid2]
        self.assertNotEqual(t1.path, t2.path)
        self.assertEqual(open(t2.path, "rb").read(), b"second!")

    def test_validation_rejects(self):
        settings = target_settings(BASE_CONFIG)
        chunk_size = protocol.chunk_size_for(512)

        def start_with(**over):
            fields = dict(
                file_size=10, chunk_size=chunk_size, total_chunks=1,
                name="ok.bin", sha256=b"\x01" * 32, settings=settings, flags=0,
            )
            fields.update(over)
            raw = protocol.encode_file_start(protocol.new_transfer_id(), **fields)
            return protocol.decode_envelope(raw)

        before = len(self.files.transfers)
        # too big
        self.files.handle_start(start_with(file_size=200 * 1024 * 1024, total_chunks=1),
                                protocol.decode_file_start(start_with(file_size=200 * 1024 * 1024, total_chunks=1)))
        # chunk count mismatch
        s = start_with(total_chunks=5)
        self.files.handle_start(s, protocol.decode_file_start(s))
        # bad modem_type index
        s = start_with(settings={**settings, "modem_type": 9})
        self.files.handle_start(s, protocol.decode_file_start(s))
        self.assertEqual(len(self.files.transfers), before)

    def test_announced_settings_applied_only_when_flagged_and_enabled(self):
        data = os.urandom(100)
        tid, _ = self.broadcast(data)  # flags=0
        self.assertEqual(self.applied, [])
        # flagged start with apply on
        settings = target_settings(BASE_CONFIG)
        raw = protocol.encode_file_start(
            4242, len(data), protocol.chunk_size_for(512), 1, "x.bin",
            hashlib.sha256(data).digest(),
            {**settings, "modem_type": 2, "robust_mode": 5},
            flags=FLAG_ROBUST_ANNOUNCED,
        )
        env = protocol.decode_envelope(raw)
        self.files.handle_start(env, protocol.decode_file_start(env))
        self.assertEqual(len(self.applied), 1)
        fields, label = self.applied[0]
        self.assertEqual(fields, {"modem_type": 2, "robust_mode": 5})
        self.assertIn("RDM-1200S", label)
        # toggle off -> no further applies
        self.applied.clear()
        self.files.apply_announced = lambda: False
        self.files.transfers.pop(4242, None)
        env = protocol.decode_envelope(raw)
        self.files.handle_start(env, protocol.decode_file_start(env))
        self.assertEqual(self.applied, [])


class AnnounceFieldsTest(unittest.TestCase):
    def test_translation_ranges(self):
        settings = target_settings(BASE_CONFIG)
        raw = protocol.encode_file_start(
            1, 0, 100, 0, "a", b"\x00" * 32,
            {"modem_type": 1, "mfsk_mode": 2}, flags=0,
        )
        fs = protocol.decode_file_start(protocol.decode_envelope(raw))
        self.assertEqual(announced_settings_to_fields(fs), {"modem_type": 1, "mfsk_mode": 2})


if __name__ == "__main__":
    unittest.main()
