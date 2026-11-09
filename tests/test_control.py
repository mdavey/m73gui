from __future__ import annotations

import json
import queue
import socket
import struct
import threading
import time
import unittest

from m73gui.net.control import ControlClient, ControlError, _RecvBuf, encode_frame


class RecvBufTest(unittest.TestCase):
    def test_partial_reads(self):
        frame = encode_frame({"a": 1}) + encode_frame({"b": 2})
        buf = _RecvBuf()
        msgs = []
        for i in range(len(frame)):
            buf.feed(frame[i : i + 1])
            while (m := buf.next_message()) is not None:
                msgs.append(m)
        self.assertEqual(msgs, [{"a": 1}, {"b": 2}])

    def test_multiple_per_read(self):
        buf = _RecvBuf()
        buf.feed(encode_frame({"ok": True}) * 3)
        self.assertEqual(buf.next_message(), {"ok": True})
        self.assertEqual(buf.next_message(), {"ok": True})
        self.assertEqual(buf.next_message(), {"ok": True})
        self.assertIsNone(buf.next_message())

    def test_huge_length_rejected(self):
        buf = _RecvBuf()
        buf.feed(struct.pack(">I", 10**9))
        with self.assertRaises(ControlError):
            buf.next_message()


class FakeServer:
    """Accepts one connection; replies to requests per a handler; can push events."""

    def __init__(self, handler=None, respond=True):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.handler = handler or (lambda req: {"ok": True, "echo": req.get("cmd")})
        self.respond = respond
        self.requests: list[dict] = []
        self.conn: socket.socket | None = None
        self.buf = bytearray()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _send(self, obj: dict) -> None:
        data = encode_frame(obj)
        self.conn.sendall(data)

    def push_event(self, obj: dict) -> None:
        self._send({"event": obj["event"], **{k: v for k, v in obj.items() if k != "event"}})

    def _run(self):
        try:
            self.conn, _ = self.sock.accept()
        except OSError:
            return
        while not self.stop.is_set():
            try:
                data = self.conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            self.buf.extend(data)
            while len(self.buf) >= 4:
                (length,) = struct.unpack_from(">I", self.buf, 0)
                if len(self.buf) < 4 + length:
                    break
                req = json.loads(self.buf[4 : 4 + length])
                del self.buf[: 4 + length]
                self.requests.append(req)
                if self.respond:
                    self._send(self.handler(req))

    def close(self):
        self.stop.set()
        for s in (self.conn, self.sock):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.stop = threading.Event()
        self.client: ControlClient | None = None

    def tearDown(self):
        self.stop.set()
        if self.client:
            self.client.close()

    def make_client(self, server: FakeServer, timeout: float = 2.0) -> ControlClient:
        self.client = ControlClient(
            "127.0.0.1", server.port, self.events, self.stop, timeout=timeout
        )
        self.client.start()
        end = time.monotonic() + 5
        while not self.client.connected and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(self.client.connected, "client did not connect")
        return self.client

    def test_request_response(self):
        server = FakeServer()
        client = self.make_client(server)
        resp = client.request("get_status")
        self.assertEqual(resp["ok"], True)
        self.assertEqual(resp["echo"], "get_status")
        server.close()

    def test_helper_roundtrip(self):
        server = FakeServer(
            handler=lambda req: {"ok": True, "callsign": "VK2X", "payload_size": 510}
        )
        client = self.make_client(server)
        cfg = client.get_config()
        self.assertEqual(cfg["callsign"], "VK2X")
        server.close()

    def test_events_dispatched(self):
        server = FakeServer()
        client = self.make_client(server)
        server.push_event({"event": "rx_frame", "seq": 5, "size": 42})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            kind, data = self.events.get(timeout=5)
            if kind == "ctrl_event":
                break
        else:
            self.fail("no ctrl_event")
        self.assertEqual(data["event"]["seq"], 5)
        server.close()

    def test_ok_false_raises(self):
        server = FakeServer(handler=lambda req: {"ok": False, "error": "nope"})
        client = self.make_client(server)
        with self.assertRaises(ControlError) as cm:
            client.set_config({"callsign": "x"})
        self.assertIn("nope", str(cm.exception))
        server.close()

    def test_set_config_partial_apply_surfaces_rejected(self):
        server = FakeServer(handler=lambda req: {
            "ok": True, "rejected": ["blocks (1-8, wide family)"],
        })
        client = self.make_client(server)
        resp = client.set_config({"blocks": 9})
        self.assertEqual(resp["rejected"], ["blocks (1-8, wide family)"])
        server.close()

    def test_set_config_all_rejected_names_fields(self):
        server = FakeServer(handler=lambda req: {
            "ok": False,
            "rejected": ["blocks (1-8, wide family)",
                         "bandwidth (OFDM family change requires restart)"],
        })
        client = self.make_client(server)
        with self.assertRaises(ControlError) as cm:
            client.set_config({"blocks": 9, "bandwidth": 2})
        self.assertIn("bandwidth (OFDM family change requires restart)",
                      str(cm.exception))
        server.close()

    def test_timeout_drops_connection(self):
        server = FakeServer(respond=False)
        client = self.make_client(server, timeout=0.5)
        start = time.monotonic()
        with self.assertRaises(ControlError):
            client.request("get_status")
        self.assertLess(time.monotonic() - start, 2.0)
        # socket was closed: server sees EOF and stops serving
        time.sleep(0.2)
        self.assertFalse(client.connected)
        server.close()

    def test_request_when_disconnected(self):
        server = FakeServer()
        client = self.make_client(server)
        server.close()
        end = time.monotonic() + 5
        while client.connected and time.monotonic() < end:
            time.sleep(0.01)
        with self.assertRaises(ControlError):
            client.request("get_status")

    def test_requests_serialized_fifo(self):
        def slow(req):
            time.sleep(0.05)
            return {"ok": True, "id": req.get("id")}

        server = FakeServer(handler=slow)
        client = self.make_client(server)
        results = []
        threads = [
            threading.Thread(target=lambda i=i: results.append(client.request("x", id=i)))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(results), 4)
        self.assertEqual(sorted(r["id"] for r in results), [0, 1, 2, 3])
        server.close()


if __name__ == "__main__":
    unittest.main()
