"""End-to-end pipeline over real sockets: KISS server push -> parser ->
protocol decode -> incoming reassembler, plus control startup sequence."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest

from m73gui import protocol
from m73gui.constants import (
    KIND_FILE_CHUNK,
    KIND_FILE_END,
    KIND_FILE_START,
)
from m73gui.net.control import ControlClient
from m73gui.net.kiss import KissClient, wrap_frame
from m73gui.services.monitor import Monitor
from m73gui.services.transfers import IncomingFiles, target_settings
from m73gui.state import INCOMING_COMPLETE

CONFIG = {
    "ok": True,
    "callsign": "VK2E2E",
    "modem_type": 0,
    "modulation": "QPSK",
    "code_rate": "1/2",
    "frame_size": 1,
    "mfsk_mode": 0,
    "robust_mode": 0,
    "payload_size": 512,
    "postamble": False,
}


def accept_one(server: socket.socket) -> socket.socket:
    server.settimeout(5)
    conn, _ = server.accept()
    conn.settimeout(5)
    return conn


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.stop = threading.Event()
        self.events: "queue.Queue[tuple[str, dict]]" = queue.Queue()

        self.control_srv = socket.socket()
        self.control_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.control_srv.bind(("127.0.0.1", 0))
        self.control_srv.listen(1)

        self.kiss_srv = socket.socket()
        self.kiss_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.kiss_srv.bind(("127.0.0.1", 0))
        self.kiss_srv.listen(1)

        self.control = ControlClient(
            "127.0.0.1", self.control_srv.getsockname()[1], self.events, self.stop,
            timeout=3,
        )
        self.kiss = KissClient(
            "127.0.0.1", self.kiss_srv.getsockname()[1], self.events, self.stop
        )

    def tearDown(self):
        self.stop.set()
        self.control.close()
        self.kiss.close()
        self.control_srv.close()
        self.kiss_srv.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------

    def test_control_startup_and_history_sequence(self):
        def serve():
            conn = accept_one(self.control_srv)
            buf = bytearray()
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buf.extend(data)
                while len(buf) >= 4:
                    (ln,) = struct.unpack_from(">I", buf, 0)
                    if len(buf) < 4 + ln:
                        break
                    req = json.loads(buf[4:4 + ln])
                    del buf[:4 + ln]
                    if req["cmd"] == "get_config":
                        resp = dict(CONFIG)
                    elif req["cmd"] == "get_status":
                        resp = {"ok": True, "channel_state": "idle", "tx_queue": 0}
                    elif req["cmd"] == "rx_frame_history":
                        self.assertEqual(req["limit"], 100)
                        resp = {"ok": True, "frames": [
                            {"seq": 2, "time": time.time(), "snr": 5.0, "size": 10,
                             "mode": "RDM-600", "callsign": "VK9H"},
                        ]}
                    else:
                        resp = {"ok": False, "error": "?"}
                    body = json.dumps(resp).encode()
                    conn.sendall(struct.pack(">I", len(body)) + body)

        threading.Thread(target=serve, daemon=True).start()
        monitor = Monitor(self.control, self.events, self.stop, interval=5)
        self.control.start()
        end_wait = time.monotonic() + 5
        while not self.control.connected and time.monotonic() < end_wait:
            time.sleep(0.01)
        self.assertTrue(self.control.connected, "control client did not connect")
        monitor.startup()

        kinds = {}
        end = time.monotonic() + 5
        while time.monotonic() < end and not {"config", "status", "history"} <= kinds.keys():
            try:
                kind, data = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            kinds[kind] = data
        self.assertIn("config", kinds)
        self.assertIn("status", kinds)
        self.assertIn("history", kinds)
        self.assertEqual(kinds["config"]["config"]["callsign"], "VK2E2E")
        self.assertEqual(kinds["history"]["frames"][0]["seq"], 2)
        self.assertEqual(monitor.last_seq, 2)

    def test_kiss_broadcast_file_received(self):
        data = os.urandom(1200)  # 492*2 + 216 -> 3 chunks
        sha = hashlib.sha256(data).digest()
        chunk_size = protocol.chunk_size_for(CONFIG["payload_size"])
        total = (len(data) + chunk_size - 1) // chunk_size
        tid = protocol.new_transfer_id()

        applied: list[tuple[dict, str]] = []
        files = IncomingFiles(
            downloads_dir=os.path.join(self.tmp, "dl"),
            emit=lambda kind, **d: self.events.put((kind, d)),
            outgoing_ids=set(),
            accept_self=lambda: False,
            apply_announced=lambda: False,
            apply_settings=lambda f, label: applied.append((f, label)),
        )

        def serve():
            conn = accept_one(self.kiss_srv)
            start = protocol.encode_file_start(
                tid, len(data), chunk_size, total, "e2e.bin", sha,
                target_settings(CONFIG), flags=0,
            )
            conn.sendall(wrap_frame(start))
            for i in range(total):
                chunk = data[i * chunk_size : (i + 1) * chunk_size]
                conn.sendall(wrap_frame(protocol.encode_file_chunk(tid, i, chunk)))
                time.sleep(0.02)
            conn.sendall(wrap_frame(protocol.encode_file_end(tid, total, sha)))

        threading.Thread(target=serve, daemon=True).start()
        self.kiss.start()

        end = time.monotonic() + 10
        routed = False
        while time.monotonic() < end:
            try:
                kind, payload = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "kiss_rx":
                env = protocol.decode_envelope(payload["data"])
                if env is None:
                    continue
                if env.kind == KIND_FILE_START:
                    files.handle_start(
                        env, protocol.decode_file_start(env), sender="VK9E2E"
                    )
                elif env.kind == KIND_FILE_CHUNK:
                    files.handle_chunk(env)
                elif env.kind == KIND_FILE_END:
                    files.handle_end(env)
            elif kind == "transfer_rx":
                snap = payload["snapshot"]
                if snap.status == INCOMING_COMPLETE:
                    routed = True
                    with open(snap.path, "rb") as fh:
                        self.assertEqual(fh.read(), data)
                    self.assertTrue(snap.path.startswith(
                        os.path.join(self.tmp, "dl")))
                    break
        self.assertTrue(routed, "incoming transfer never completed")
        self.assertEqual(applied, [])


if __name__ == "__main__":
    unittest.main()
