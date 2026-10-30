"""KISS client for modem73's raw payload TCP port.

Mirrors modem73's KISS parser semantics: FEND-delimited frames, FESC
escaping (unknown escapes passed through), empty frames dropped, command byte
high nibble = port, low nibble = command. Only DATA (0x00) frames are used.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time

from ..constants import FEND, FESC, KISS_CMD_DATA, TFEND, TFESC

log = logging.getLogger("m73gui.kiss")

MAX_FRAME = 65536


class KissParser:
    """Incremental KISS parser tolerant of partial reads and multiple frames
    per read. feed() returns a list of (port, cmd, payload) tuples."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._in_frame = False
        self._escape = False

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        out: list[tuple[int, int, bytes]] = []
        for byte in data:
            if byte == FEND:
                if self._in_frame and len(self._buffer) > 0:
                    cmd_byte = self._buffer[0]
                    port = (cmd_byte >> 4) & 0x0F
                    cmd = cmd_byte & 0x0F
                    payload = bytes(self._buffer[1:])
                    out.append((port, cmd, payload))
                self._in_frame = True
                self._buffer.clear()
                self._escape = False
            elif self._in_frame:
                if len(self._buffer) >= MAX_FRAME:
                    self._in_frame = False
                    self._buffer.clear()
                    self._escape = False
                    continue
                if self._escape:
                    if byte == TFEND:
                        self._buffer.append(FEND)
                    elif byte == TFESC:
                        self._buffer.append(FESC)
                    else:
                        self._buffer.append(byte)
                    self._escape = False
                elif byte == FESC:
                    self._escape = True
                else:
                    self._buffer.append(byte)
        return out


def wrap_frame(payload: bytes) -> bytes:
    """Wrap a raw payload in a KISS DATA frame."""
    out = bytearray([FEND, KISS_CMD_DATA])
    for byte in payload:
        if byte == FEND:
            out += bytes([FESC, TFEND])
        elif byte == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(byte)
    out.append(FEND)
    return bytes(out)


class KissClient:
    """Background TCP client: reader thread pushes RX payloads onto the app
    event queue; send() enqueues onto a bounded writer queue."""

    def __init__(
        self,
        host: str,
        port: int,
        event_queue: "queue.Queue[tuple[str, dict]]",
        stop_event: threading.Event,
        max_queue: int = 256,
    ) -> None:
        self.host = host
        self.port = port
        self.events = event_queue
        self.stop_event = stop_event
        self._send_q: "queue.Queue[bytes]" = queue.Queue(maxsize=max_queue)
        self._connected = threading.Event()
        self._sock: socket.socket | None = None
        self._gen = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._reader: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="kiss-conn", daemon=True
        )
        self._thread.start()

    def _emit(self, kind: str, **data) -> None:
        self.events.put((kind, data))

    def _run(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            sock = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(None)
            except OSError as exc:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if self.stop_event.is_set():
                    return
                log.debug("kiss connect failed: %s", exc)
                if self.stop_event.wait(backoff):
                    return
                backoff = min(backoff * 2, 5.0)
                continue

            with self._lock:
                self._gen += 1
                gen = self._gen
                self._sock = sock
            self._connected.set()
            self._emit("kiss_connected", host=self.host, port=self.port, gen=gen)
            backoff = 1.0

            read_exit = threading.Event()
            reader = threading.Thread(
                target=self._read_loop,
                args=(sock, gen, read_exit),
                name="kiss-read",
                daemon=True,
            )
            self._reader = reader
            reader.start()
            self._write_loop(sock, gen, read_exit)
            reader.join(timeout=2)

            self._connected.clear()
            with self._lock:
                if self._gen == gen:
                    self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            if self.stop_event.is_set():
                return
            self._emit("kiss_disconnected", host=self.host, port=self.port, gen=gen)
            # drop queued sends from the broken connection
            self._drain_send_queue()
            if self.stop_event.wait(backoff):
                return

    def _drain_send_queue(self) -> None:
        while True:
            try:
                self._send_q.get_nowait()
            except queue.Empty:
                return

    def _read_loop(self, sock: socket.socket, gen: int, read_exit: threading.Event) -> None:
        parser = KissParser()
        try:
            while not self.stop_event.is_set():
                data = sock.recv(65536)
                if not data:
                    break
                now = time.time()
                for port, cmd, payload in parser.feed(data):
                    if cmd != KISS_CMD_DATA or not payload:
                        continue
                    log.debug("kiss rx %d bytes port=%d", len(payload), port)
                    self._emit("kiss_rx", data=payload, time=now, gen=gen)
        except OSError:
            pass
        finally:
            read_exit.set()
            with self._lock:
                if self._gen == gen and self._sock is not None:
                    try:
                        self._sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

    def _write_loop(self, sock: socket.socket, gen: int, read_exit: threading.Event) -> None:
        while not self.stop_event.is_set():
            if read_exit.is_set():
                return
            with self._lock:
                if self._gen != gen:
                    return
            try:
                payload = self._send_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                sock.sendall(wrap_frame(payload))
                log.debug("kiss tx %d bytes", len(payload))
            except OSError:
                log.debug("kiss write failed; connection dropped")
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    # -- public API ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def send(self, payload: bytes) -> bool:
        """Enqueue a raw payload for transmission. False if not connected or
        the writer queue is full."""
        if not self._connected.is_set():
            return False
        try:
            self._send_q.put_nowait(payload)
            return True
        except queue.Full:
            log.warning("kiss send queue full; dropping %d-byte frame", len(payload))
            return False

    def close(self) -> None:
        self.stop_event.set()
        with self._lock:
            sock = self._sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
