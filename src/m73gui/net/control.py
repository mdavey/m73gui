"""Length-prefixed JSON control client for modem73.

Wire format: 4-byte big-endian length + JSON object. Commands are serialized
behind a lock and matched FIFO with responses; `event` messages are dispatched
straight to the app event queue. A request timeout drops the socket and forces
a reconnect with a fresh get_config/get_status.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import struct
import threading
import time

log = logging.getLogger("m73gui.control")

MAX_JSON_LEN = 4 * 1024 * 1024


class ControlError(Exception):
    pass


def encode_frame(obj: dict) -> bytes:
    payload = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


class _RecvBuf:
    """Accumulates socket bytes and yields complete length-prefixed messages.

    feed() appends; next_message() returns a dict or None when incomplete.
    """

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> None:
        self.buf.extend(data)

    def next_message(self) -> dict | None:
        if len(self.buf) < 4:
            return None
        (length,) = struct.unpack_from(">I", self.buf, 0)
        if length > MAX_JSON_LEN:
            raise ControlError(f"control frame too large ({length})")
        if len(self.buf) < 4 + length:
            return None
        payload = bytes(self.buf[4 : 4 + length])
        del self.buf[: 4 + length]
        return json.loads(payload.decode("utf-8"))


class ControlClient:
    def __init__(
        self,
        host: str,
        port: int,
        event_queue: "queue.Queue[tuple[str, dict]]",
        stop_event: threading.Event,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.events = event_queue
        self.stop_event = stop_event
        self.timeout = timeout
        self._connected = threading.Event()
        self._response_q: "queue.Queue[dict]" = queue.Queue()
        self._request_lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._gen = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="ctrl-conn", daemon=True
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
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
            except OSError:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if self.stop_event.wait(backoff):
                    return
                backoff = min(backoff * 2, 5.0)
                continue

            with self._lock:
                self._gen += 1
                gen = self._gen
                self._sock = sock
            self._connected.set()
            self._emit("ctrl_connected", host=self.host, port=self.port, gen=gen)
            backoff = 1.0
            reason = "eof"
            try:
                self._read_loop(sock, gen)
            except (ControlError, OSError) as exc:
                reason = str(exc)
            finally:
                self._connected.clear()
                with self._lock:
                    if self._gen == gen:
                        self._sock = None
                    # wake any waiter, then future requests see "not connected"
                    while True:
                        try:
                            self._response_q.get_nowait()
                        except queue.Empty:
                            break
                    self._response_q.put({"__disconnected__": True})
                try:
                    sock.close()
                except OSError:
                    pass
                log.info("control disconnected (%s)", reason)
                if not self.stop_event.is_set():
                    self._emit(
                        "ctrl_disconnected", host=self.host, port=self.port, gen=gen
                    )
            if self.stop_event.wait(backoff):
                return

    def _read_loop(self, sock: socket.socket, gen: int) -> None:
        buf = _RecvBuf()
        while not self.stop_event.is_set():
            data = sock.recv(65536)
            if not data:
                return
            buf.feed(data)
            while True:
                try:
                    msg = buf.next_message()
                except (ControlError, ValueError) as exc:
                    log.warning("bad control frame: %s", exc)
                    return
                if msg is None:
                    break
                if isinstance(msg, dict) and "event" in msg:
                    self._emit("ctrl_event", event=msg)
                else:
                    self._response_q.put(msg)

    # -- requests ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def request(self, cmd: str, **fields) -> dict:
        """Serialized request/response. Raises ControlError on failure."""
        with self._request_lock:
            self._drain_responses()
            if not self._connected.is_set():
                raise ControlError("control not connected")
            sock = self._sock
            if sock is None:
                raise ControlError("control not connected")
            try:
                sock.sendall(encode_frame({"cmd": cmd, **fields}))
            except OSError as exc:
                self._drop_connection()
                raise ControlError(f"send failed: {exc}") from exc
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._drop_connection()
                    raise ControlError(f"timeout waiting for {cmd}")
                try:
                    msg = self._response_q.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    continue
                if msg.get("__disconnected__"):
                    raise ControlError("disconnected during request")
                return msg

    def _drain_responses(self) -> None:
        while True:
            try:
                self._response_q.get_nowait()
            except queue.Empty:
                return

    def _drop_connection(self) -> None:
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        self.stop_event.set()
        self._drop_connection()

    # -- command helpers -------------------------------------------------------

    def _request_ok(self, cmd: str, **fields) -> dict:
        resp = self.request(cmd, **fields)
        if not resp.get("ok", False):
            raise ControlError(resp.get("error", f"{cmd} failed"))
        return resp

    def get_config(self) -> dict:
        return self._request_ok("get_config")

    def get_status(self) -> dict:
        return self._request_ok("get_status")

    def set_config(self, fields: dict) -> dict:
        """Apply config fields. Raises ControlError when nothing was applied.

        The beta control port may partially apply a request: check the
        returned response's ``rejected`` list for ``field (reason)`` entries.
        """
        resp = self.request("set_config", **fields)
        if not resp.get("ok", False):
            rejected = ", ".join(resp.get("rejected") or [])
            raise ControlError(
                resp.get("error") or rejected or "set_config failed"
            )
        return resp

    def rx_frame_history(self, limit: int = 100, since_seq: int = 0) -> list[dict]:
        resp = self._request_ok("rx_frame_history", limit=limit, since_seq=since_seq)
        return resp.get("frames", [])
