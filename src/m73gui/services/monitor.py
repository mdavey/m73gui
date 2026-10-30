"""Status polling + rx_frame_history catch-up.

Runs the startup/reconnect sequence (get_config, get_status,
rx_frame_history) and then polls get_status at a fixed interval. All work
happens in a worker thread; results are pushed onto the app event queue.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from ..constants import HISTORY_POLL_LIMIT, STATUS_POLL_INTERVAL_S
from ..net.control import ControlClient, ControlError

log = logging.getLogger("m73gui.monitor")


class Monitor:
    def __init__(
        self,
        control: ControlClient,
        event_queue: "queue.Queue[tuple[str, dict]]",
        stop_event: threading.Event,
        interval: float = STATUS_POLL_INTERVAL_S,
    ) -> None:
        self.control = control
        self.events = event_queue
        self.stop_event = stop_event
        self.interval = interval
        self.last_seq = 0
        self._fresh = False  # startup sequence needed after connect/reconnect
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def on_connected(self) -> None:
        """Called when a new control connection is established."""
        self._fresh = True

    def _emit(self, kind: str, **data) -> None:
        self.events.put((kind, data))

    def startup(self) -> None:
        """Fetch config, status, and history catch-up (worker thread only)."""
        cfg = self.control.get_config()
        self._emit("config", config=cfg)
        st = self.control.get_status()
        self._emit("status", status=st)
        self.catchup_history()

    def catchup_history(self) -> None:
        try:
            frames = self.control.rx_frame_history(
                limit=HISTORY_POLL_LIMIT, since_seq=self.last_seq
            )
        except ControlError as exc:
            log.info("history catch-up failed: %s", exc)
            return
        if frames:
            newest = max(int(f.get("seq", 0)) for f in frames)
            if newest > self.last_seq:
                self.last_seq = newest
                # oldest first for display
                frames = sorted(frames, key=lambda f: int(f.get("seq", 0)))
                self._emit("history", frames=frames)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            if not self.control.connected:
                self._fresh = False
                if self.stop_event.wait(0.25):
                    return
                continue
            try:
                if self._fresh:
                    self.startup()
                    self._fresh = False
                else:
                    st = self.control.get_status()
                    self._emit("status", status=st)
            except ControlError as exc:
                log.debug("status poll failed: %s", exc)
                self._fresh = True
            elapsed = time.monotonic() - started
            if self.stop_event.wait(max(0.05, self.interval - elapsed)):
                return
