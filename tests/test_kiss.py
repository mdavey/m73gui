from __future__ import annotations

import queue
import socket
import threading
import unittest

from m73gui.constants import FEND, FESC, TFEND, TFESC
from m73gui.net.kiss import KissClient, KissParser, wrap_frame


def parse_all(chunks: list[bytes]) -> list[tuple[int, int, bytes]]:
    parser = KissParser()
    out = []
    for chunk in chunks:
        out.extend(parser.feed(chunk))
    return out


class ParserTest(unittest.TestCase):
    def test_roundtrip_with_escapes(self):
        payload = bytes([FEND, 0x41, FESC, 0x42, TFEND, TFESC])
        frames = parse_all([wrap_frame(payload)])
        self.assertEqual(frames, [(0, 0x00, payload)])

    def test_split_reads(self):
        wrapped = wrap_frame(b"hello world")
        mid = 7
        frames = parse_all([wrapped[:mid], wrapped[mid:]])
        self.assertEqual(frames, [(0, 0x00, b"hello world")])

    def test_bit_by_bit(self):
        wrapped = wrap_frame(b"a\xdb\xc0b")
        frames = parse_all([bytes([b]) for b in wrapped])
        self.assertEqual(frames, [(0, 0x00, b"a\xdb\xc0b")])

    def test_multiple_frames_per_read(self):
        data = wrap_frame(b"one") + wrap_frame(b"two") + wrap_frame(b"three")
        frames = parse_all([data])
        self.assertEqual([f[2] for f in frames], [b"one", b"two", b"three"])

    def test_empty_frames_dropped(self):
        frames = parse_all([bytes([FEND, FEND, FEND])])
        self.assertEqual(frames, [])

    def test_garbage_before_start_ignored(self):
        frames = parse_all([b"\x01\x02\x03" + wrap_frame(b"ok")])
        self.assertEqual([f[2] for f in frames], [b"ok"])

    def test_command_and_port_nibbles(self):
        frames = parse_all([bytes([FEND, 0x31, 0x05, FEND])])
        self.assertEqual(frames, [(3, 0x01, b"\x05")])

    def test_unknown_escape_passes_through(self):
        # modem73 passes unknown escape bytes through unchanged
        frames = parse_all([bytes([FEND, 0x00, FESC, 0x41, FEND])])
        self.assertEqual(frames, [(0, 0x00, b"A")])

    def test_max_frame_guard(self):
        parser = KissParser()
        out = parser.feed(bytes([FEND, 0x00]) + b"x" * 70000)
        self.assertEqual(out, [])
        # parser recovers on next FEND
        out = parser.feed(wrap_frame(b"after"))
        self.assertEqual([f[2] for f in out], [b"after"])


class WrapTest(unittest.TestCase):
    def test_frame_shape(self):
        wrapped = wrap_frame(b"A")
        self.assertEqual(wrapped, bytes([FEND, 0x00, 0x41, FEND]))

    def test_send_requires_connection(self):
        client = KissClient("127.0.0.1", 1, queue.Queue(), threading.Event())
        self.assertFalse(client.send(b"nope"))


class LiveClientTest(unittest.TestCase):
    """Exercises connect, reader, and writer threads against a fake server."""

    def setUp(self):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.stop = threading.Event()
        self.accepted: socket.socket | None = None

    def tearDown(self):
        self.stop.set()
        if self.accepted:
            try:
                self.accepted.close()
            except OSError:
                pass
        self.server.close()

    def wait_event(self, kind: str, timeout: float = 5.0):
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                k, data = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if k == kind:
                return data
            # buffer others for later waits
            self.events.put((k, data))
            time.sleep(0.01)
        raise AssertionError(f"event {kind!r} not received")

    def test_roundtrip_and_disconnect(self):
        client = KissClient("127.0.0.1", self.port, self.events, self.stop)
        client.start()
        self.accepted, _ = self.server.accept()
        self.wait_event("kiss_connected")

        client.send(b"hello kiss")
        raw = b""
        parser = KissParser()
        got = []
        while not got:
            raw += self.accepted.recv(4096)
            got = parser.feed(raw)
            raw = b""
        self.assertEqual(got[0][2], b"hello kiss")

        # server -> client payload
        self.accepted.sendall(wrap_frame(b"from modem"))
        rx = self.wait_event("kiss_rx")
        self.assertEqual(rx["data"], b"from modem")

        # server hangup -> disconnected event
        self.accepted.close()
        self.wait_event("kiss_disconnected")

    def test_send_accepted_when_connected(self):
        client = KissClient("127.0.0.1", self.port, self.events, self.stop)
        client.start()
        self.accepted, _ = self.server.accept()
        self.wait_event("kiss_connected")
        self.assertTrue(client.send(b"x"))


if __name__ == "__main__":
    unittest.main()
