"""Main application: window wiring, event pump, protocol routing."""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
import tkinter as tk

import customtkinter as ctk

from . import protocol, userdirs
from .constants import (
    CHAT_PARTIAL_REVEAL_S,
    COL_WARN,
    EVENT_PUMP_INTERVAL_MS,
    KIND_CHAT,
    KIND_FILE_BLOCK,
)
from .net.control import ControlClient, ControlError
from .net.kiss import KissClient
from .services.monitor import Monitor
from .services.transfers import FileSender, IncomingFiles
from .state import (
    RX,
    TX,
    TX_CANCELLED,
    TX_FAILED,
    TX_SENT,
    AppState,
    PacketRow,
)
from .ui import theme
from .ui.channel import ChannelTab
from .ui.chat import ChatTab
from .ui.files import FilesTab
from .ui.header import Header
from .ui.modebar import MODE_FIELDS, changed_mode_fields, mode_differs
from .ui.packets import PacketsTab
from .ui.settings import SettingsTab
from . import __version__

log = logging.getLogger("m73gui.app")

class App:
    def __init__(
        self,
        root: ctk.CTk,
        control: tuple[str, int],
        kiss: tuple[str, int],
    ) -> None:
        self.root = root
        self.control_ep = control
        self.kiss_ep = kiss
        self.stop_event = threading.Event()
        self.events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.state = AppState()
        self.gui_settings = userdirs.load_settings()

        # network
        self.control = ControlClient(*control, self.events, self.stop_event)
        self.kiss = KissClient(*kiss, self.events, self.stop_event)
        self.monitor = Monitor(self.control, self.events, self.stop_event)
        self.outgoing_ids: set[int] = set()

        # transfers
        self.sender = FileSender(
            emit=lambda kind, **d: self.events.put((kind, d)),
            send_frame=self._send_payload,
            get_status=self._sender_status,
            get_config=self._sender_config,
            outgoing_ids=self.outgoing_ids,
        )
        self.incoming = IncomingFiles(
            downloads_dir=userdirs.received_dir,
            emit=lambda kind, **d: self.events.put((kind, d)),
            outgoing_ids=self.outgoing_ids,
            accept_self=lambda: bool(self.files_tab.accept_self.get()),
        )

        # rx metadata <-> payload correlation
        self._pending_meta: collections.deque[PacketRow] = collections.deque(maxlen=64)
        self._seen_seqs: set[int] = set()
        self._seq_order: collections.deque[int] = collections.deque(maxlen=4096)

        # chat
        self._chat_clock = time.time
        self.chat_asm = protocol.ChatReassembler(
            CHAT_PARTIAL_REVEAL_S, now_fn=self._chat_clock
        )

        self._build_ui()

        # tab mode rows carry the user's intent, not live modem state
        self._mode_adopted = False
        stored = self.gui_settings
        if isinstance(stored.get("chat_mode"), dict):
            self.chat_tab.mode_row.set_from_config(stored["chat_mode"])
        if isinstance(stored.get("files_mode"), dict):
            self.files_tab.mode_row.set_from_config(stored["files_mode"])

        self.control.start()
        self.kiss.start()
        self.monitor.start()
        self.root.after(EVENT_PUMP_INTERVAL_MS, self._pump)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        root = self.root
        root.title(f"m73gui {__version__} — modem73 station")
        root.geometry("1280x820")
        root.minsize(1024, 680)
        root.configure(fg_color=theme.BG)

        self.header = Header(root, self)
        self.header.pack(fill="x")
        self.header.set_endpoints(self.control_ep, self.kiss_ep)

        self.tabs = ctk.CTkTabview(root, fg_color=theme.BG)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=(2, 2))
        for cls, title, attr in [
            (ChatTab, "Chat", "chat_tab"),
            (PacketsTab, "Packets", "packets_tab"),
            (FilesTab, "Files", "files_tab"),
            (SettingsTab, "Settings", "settings_tab"),
            (ChannelTab, "Channel", "channel_tab"),
        ]:
            tab = cls(self.tabs.add(title), self)
            tab.pack(fill="both", expand=True)
            setattr(self, attr, tab)

        footer = ctk.CTkFrame(root, fg_color="#20242d", corner_radius=0, height=30)
        footer.pack(fill="x")
        self.footer_msg = ctk.CTkLabel(
            footer, text="starting…", font=theme.FONTS["small"],
            text_color=theme.FG_DIM, anchor="w",
        )
        self.footer_msg.pack(side="left", padx=10)
        self.footer_ver = ctk.CTkLabel(
            footer,
            text=f"control {self.control_ep[0]}:{self.control_ep[1]}  ·  "
            f"kiss {self.kiss_ep[0]}:{self.kiss_ep[1]}  ·  m73gui {__version__}",
            font=theme.FONTS["mono_small"],
            text_color=theme.FG_DIM,
        )
        self.footer_ver.pack(side="right", padx=10)

        self.chat_tab.set_connected(False)
        self.settings_tab.set_enabled(False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def notify(self, message: str, level: str = "info") -> None:
        color = {"error": "#f85149", "warn": COL_WARN}.get(level, theme.FG_DIM)
        stamp = time.strftime("%H:%M:%S")
        self.footer_msg.configure(text=f"[{stamp}] {message}", text_color=color)
        if level == "error":
            log.warning("notify: %s", message)

    # -------------------------------------------------------- app API for UI

    def persist_gui(self, **updates) -> None:
        self.gui_settings.update(updates)
        userdirs.save_settings(self.gui_settings)

    def apply_mode_then(self, fields: dict, then) -> None:
        """Push pending mode fields to the modem when they differ, then run
        then(cfg) on the Tk thread. Callers must not do this while a file
        transfer runs: the payload size would change under the stream."""

        def work():
            current = self.state.config or self.control.get_config()
            changed = changed_mode_fields(fields, current)
            if changed:
                self.control.set_config(changed)
                return self.control.get_config()
            return current

        def done(cfg, error):
            if error:
                self.notify(f"mode change failed: {error}", level="error")
                return
            if cfg and cfg is not self.state.config:
                self._on_config(cfg)
            then(cfg or self.state.config)

        self.run_async(work, done)

    def send_chat(self, text: str) -> None:
        callsign = self.state.config.get("callsign")
        if not callsign:
            self.notify("callsign not loaded yet (control offline?)", level="error")
            return
        fields = self.chat_tab.mode_row.fields()
        if self.sender.busy():
            # applying mid-broadcast would resize frames under the stream
            if mode_differs(fields, self.state.config):
                self.notify("mode change deferred: file transfer in progress",
                            level="warn")
            self._send_chat_frames(text)
            return
        self.apply_mode_then(fields, lambda cfg: self._send_chat_frames(text))

    def _send_chat_frames(self, text: str) -> None:
        callsign = self.state.config.get("callsign")
        ps = self.state.config.get("payload_size") or 512
        tid = protocol.new_transfer_id()
        try:
            frames = protocol.make_chat_frames(callsign, text, tid, ps)
        except protocol.ProtocolError as exc:
            self.notify(f"cannot send chat: {exc}", level="error")
            return
        self.outgoing_ids.add(tid)
        sent = sum(1 for frame in frames if self._send_payload(frame))
        stamp = time.strftime("%H:%M:%S")
        self.chat_tab.add_line(f"[{stamp}] {callsign}: {text}")
        self.chat_tab.set_part_info(
            f"broadcast · {len(text)} chars · {len(frames)} part(s)"
        )
        if sent < len(frames):
            self.notify(f"KISS down: only {sent}/{len(frames)} parts queued",
                        level="error")
        else:
            self.notify(f"queued {len(frames)} chat part(s) for broadcast")

    def send_file(self, path: str, fec: int = 2,
                  mode_fields: dict | None = None) -> str | None:
        if not self.kiss.connected:
            return "KISS link is down; cannot transfer files"
        if not self.control.connected:
            return "control link is down; pacing and settings are unavailable"
        if not mode_fields:
            self._start_file(path, fec)
            return None
        # start only after the pending mode landed: the sender reads the new
        # payload size from get_config when it packs
        self.apply_mode_then(
            mode_fields, lambda cfg: self._start_file(path, fec)
        )
        return None

    def _start_file(self, path: str, fec: int) -> None:
        err = self.sender.start(path, fec=fec)
        if err:
            self.notify(err, level="error")

    def cancel_file(self) -> None:
        self.sender.cancel()

    def apply_settings(self, fields: dict) -> None:
        if self.sender.busy():
            self.notify("cannot change modem settings during a transfer",
                        level="warn")
            return

        def work() -> str:
            try:
                self.control.set_config(fields)
            except ControlError as exc:
                raise exc
            return f"applied {len(fields)} field(s)"

        self.run_async(work, self._on_settings_applied)

    def _on_settings_applied(self, value, error) -> None:
        if error:
            self.settings_tab.on_apply_result(False, f"apply failed: {error}")
        else:
            self.settings_tab.on_apply_result(True, str(value))
            self.files_tab.on_live_config()

    def refresh_settings(self) -> None:
        def work():
            self.settings_tab.load(self.control.get_config())

        self.run_async(
            work,
            lambda v, e: self.notify(
                "settings refreshed" if not e else f"refresh failed: {e}",
                "info" if not e else "error",
            ),
        )

    def refresh_all(self) -> None:
        if not self.control.connected:
            self.notify("control link down — nothing to refresh", level="warn")
            return
        self.notify("refreshing config, status, and packet history…")

        def work():
            self.monitor.startup()

        self.run_async(work, None)

    # ------------------------------------------------------ async helper

    def run_async(self, fn, callback=None) -> None:
        """Run fn() in a worker thread; callback(value, error) runs on Tk."""

        def worker() -> None:
            try:
                value = fn()
                self.events.put(("async_result", {"cb": callback, "value": value,
                                                  "error": None}))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("async_result", {"cb": callback, "value": None,
                                                  "error": exc}))

        threading.Thread(target=worker, name="m73gui-async", daemon=True).start()

    # -------------------------------------------------- sender adapters

    def _send_payload(self, payload: bytes) -> bool:
        if not self.kiss.send(payload):
            return False
        self.state.tx_frames_queued += 1
        self.events.put(("tx_queued", {"payload": payload, "time": time.time()}))
        return True

    def _sender_status(self) -> dict | None:
        try:
            return self.control.get_status()
        except ControlError:
            return None

    def _sender_config(self) -> dict | None:
        try:
            return self.control.get_config()
        except ControlError:
            return None

    # ------------------------------------------------------------ event pump

    def _pump(self) -> None:
        try:
            for _ in range(256):
                try:
                    kind, data = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._dispatch(kind, data)
                except Exception:  # keep the pump alive
                    log.exception("event dispatch failed for %s", kind)
            self._sweep_periodic()
        finally:
            if not self.stop_event.is_set():
                self.root.after(EVENT_PUMP_INTERVAL_MS, self._pump)

    _last_sweep = 0.0

    def _sweep_periodic(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < 2.0:
            return
        self._last_sweep = now
        for line in self.chat_asm.sweep():
            stamp = time.strftime("%H:%M:%S")
            self.chat_tab.add_line(f"[{stamp}] {line}")
        self.incoming.sweep()
        self._publish_stale_meta()

    def _dispatch(self, kind: str, data: dict) -> None:
        if kind == "ctrl_connected":
            self.state.control_connected = True
            self.monitor.on_connected()
            self.header.update(self.state)
            self.settings_tab.set_enabled(True)
            self.notify(f"control connected ({data.get('host')}:{data.get('port')})")
        elif kind == "ctrl_disconnected":
            self.state.control_connected = False
            self.state.status = {}
            self.header.update(self.state)
            self.settings_tab.set_enabled(False)
            self.sender.notify_link_down()
            self.notify("control connection lost; reconnecting…", level="warn")
        elif kind == "kiss_connected":
            self.state.kiss_connected = True
            self.header.update(self.state)
            self.chat_tab.set_connected(True)
            self.notify(f"KISS connected ({data.get('host')}:{data.get('port')})")
        elif kind == "kiss_disconnected":
            self.state.kiss_connected = False
            self.header.update(self.state)
            self.chat_tab.set_connected(False)
            self.sender.notify_link_down()
            self.notify("KISS connection lost; reconnecting…", level="warn")
        elif kind == "ctrl_event":
            self._on_ctrl_event(data["event"])
        elif kind == "kiss_rx":
            self._on_kiss_payload(data["data"], float(data["time"]))
        elif kind == "tx_queued":
            self._add_tx_row(data["payload"], float(data["time"]))
        elif kind == "status":
            self.state.status = data["status"]
            self.header.update(self.state)
            self.channel_tab.update(self.state)
        elif kind == "config":
            self._on_config(data["config"])
        elif kind == "history":
            self._on_history(data["frames"])
        elif kind == "transfer_tx":
            self._on_transfer_tx(data["snapshot"])
        elif kind == "transfer_rx":
            snap = data["snapshot"]
            self.state.incoming[snap.transfer_id] = snap
            self.files_tab.update_incoming(snap)
            if snap.status == "complete":
                self.notify(f"received {snap.name or '?'} → {snap.path}")
                bad = protocol.file_type_extension(snap.name or "")
                if bad:
                    self.notify(
                        f"{snap.name}: unusual file type {bad} — open only if "
                        "you trust the sender",
                        level="warn",
                    )
            elif snap.status == "failed":
                self.notify(f"incoming {snap.sender}: {snap.message}",
                            level="warn")
        elif kind == "async_result":
            cb = data.get("cb")
            if cb:
                cb(data["value"], data["error"])

    # ------------------------------------------------------- control events

    def _on_ctrl_event(self, event: dict) -> None:
        name = event.get("event")
        if name == "config_changed":
            cfg = event.get("config") or {}
            self._on_config(cfg)
        elif name == "rx_frame":
            seq = int(event.get("seq", 0) or 0)
            if self._note_seq(seq):
                self.monitor.last_seq = max(self.monitor.last_seq, seq)
                self._note_link_decode(event)
                row = PacketRow(
                    time=float(event.get("time") or time.time()),
                    direction=RX,
                    size=int(event.get("size", 0) or 0),
                    kind="raw",
                    summary="waiting for payload…",
                    callsign=str(event.get("callsign", "") or ""),
                    mode=str(event.get("mode", "") or ""),
                    snr=event.get("snr"),
                    meta=event,
                    seq=seq,
                )
                self._pending_meta.append(row)

    def _on_config(self, cfg: dict) -> None:
        self.state.config = cfg
        self.header.update(self.state)
        self.settings_tab.load(cfg)
        self.channel_tab.update(self.state)
        if not self._mode_adopted and cfg:
            self._adopt_mode_defaults(cfg)
        self.chat_tab.on_live_config()
        self.files_tab.on_live_config()

    def _adopt_mode_defaults(self, cfg: dict) -> None:
        """First live config: a tab with no stored intent adopts the modem's
        current mode, so the rows start from reality, not widget defaults."""
        self._mode_adopted = True
        live = {k: cfg[k] for k in MODE_FIELDS if cfg.get(k) is not None}
        if "modem_type" not in live:
            return
        for key, tab in (("chat_mode", self.chat_tab),
                         ("files_mode", self.files_tab)):
            if isinstance(self.gui_settings.get(key), dict):
                continue
            self.persist_gui(**{key: live})
            tab.mode_row.set_from_config(live)

    def _note_link_decode(self, frame: dict) -> None:
        """Feed per-decode SNR/BER into the channel tab's link charts."""
        ber_pct = frame.get("ber_pct")
        ber = None
        if ber_pct is not None and float(ber_pct) >= 0:
            ber = float(ber_pct) / 100.0
        self.channel_tab.note_decode(frame.get("snr"), ber)

    def _note_seq(self, seq: int) -> bool:
        """True when this seq is new; remember it (bounded)."""
        if seq in self._seen_seqs:
            return False
        self._seen_seqs.add(seq)
        self._seq_order.append(seq)  # maxlen deque evicts oldest
        if len(self._seen_seqs) > 5000:  # periodic prune
            self._seen_seqs = set(self._seq_order)
        return True

    def _on_history(self, frames: list[dict]) -> None:
        for f in frames:
            seq = int(f.get("seq", 0) or 0)
            if not self._note_seq(seq):
                continue
            self._note_link_decode(f)
            ts = float(f.get("time") or (time.time() - float(f.get("age_s", 0))))
            row = PacketRow(
                time=ts,
                direction=RX,
                size=int(f.get("size", 0) or 0),
                kind="raw",
                summary="history (no payload)",
                callsign=str(f.get("callsign", "") or ""),
                mode=str(f.get("mode", "") or ""),
                snr=f.get("snr"),
                meta=f,
                history=True,
                seq=seq,
            )
            self.packets_tab.add_row(row)

    # --------------------------------------------------------- payload path

    def _on_kiss_payload(self, payload: bytes, when: float) -> None:
        row = self._match_meta(len(payload))
        if row is None:
            row = PacketRow(time=when, direction=RX, size=len(payload),
                            summary="payload only (no metadata)")
        row.payload = payload
        row.kind, row.summary, feed = self._decode_payload(payload)
        self.packets_tab.add_row(row)
        if feed:
            self._feed_protocol(payload, row.callsign)

    def _match_meta(self, size: int) -> PacketRow | None:
        now = time.time()
        for i, meta in enumerate(self._pending_meta):
            if meta.size == size and now - meta.time < 5.0:
                del self._pending_meta[i]
                return meta
        return None

    def _publish_stale_meta(self) -> None:
        now = time.time()
        while self._pending_meta and now - self._pending_meta[0].time > 3.0:
            row = self._pending_meta.popleft()
            row.summary = "decoded frame (payload not captured)"
            self.packets_tab.add_row(row)

    def _decode_payload(self, payload: bytes) -> tuple[str, str, bool]:
        """Returns (kind, summary, feed_to_protocol)."""
        try:
            env = protocol.decode_envelope(payload)
        except protocol.ProtocolError as exc:
            return "malformed", str(exc), False
        if env is None:
            return "raw", f"{len(payload)} B raw payload", False
        try:
            if env.kind == KIND_CHAT:
                part = protocol.decode_chat(env)
                echo = env.transfer_id in self.outgoing_ids
                return (
                    "chat",
                    f"{part.from_call} part {part.index + 1}/{part.parts}: "
                    f"{part.text[:40]!r}" + (" (self)" if echo else ""),
                    not echo,
                )
            if env.kind == KIND_FILE_BLOCK:
                fec, total, _block = protocol.decode_file_block(env)
                echo = env.transfer_id in self.outgoing_ids
                return (
                    "file-block",
                    f"file block {env.seq + 1}/{total} · L{fec}"
                    + (" (self)" if echo else ""),
                    True,
                )
        except protocol.ProtocolError as exc:
            return "malformed", str(exc), False
        return "control", f"unknown M73G kind {env.kind:#02x}", False

    def _feed_protocol(self, payload: bytes, sender: str = "") -> None:
        env = protocol.decode_envelope(payload)
        if env is None:
            return
        if env.kind == KIND_CHAT:
            part = protocol.decode_chat(env)
            result = self.chat_asm.add(part)
            if result:
                text, _complete = result
                stamp = time.strftime("%H:%M:%S", time.localtime())
                self.chat_tab.add_line(f"[{stamp}] {text}")
        elif env.kind == KIND_FILE_BLOCK:
            fec, total, block = protocol.decode_file_block(env)
            self.incoming.handle_block(env, fec, total, block, sender=sender)

    # ------------------------------------------------------------- TX rows

    def _add_tx_row(self, payload: bytes, when: float) -> None:
        kind, summary, _ = self._decode_payload(payload)
        row = PacketRow(
            time=when,
            direction=TX,
            size=len(payload),
            kind=kind,
            summary=summary or "queued",
            callsign=self.state.config.get("callsign", ""),
            mode=self.state.mode_label(),
            meta={"direction": "tx", "mode_at_queue": self.state.mode_label()},
        )
        self.packets_tab.add_row(row)

    # ------------------------------------------------------------ transfers

    def _on_transfer_tx(self, snap) -> None:
        self.state.outgoing = snap
        self.files_tab.update_outgoing(snap)
        busy = snap.status not in (TX_SENT, TX_FAILED, TX_CANCELLED)
        self.settings_tab.set_enabled(not busy and self.state.control_connected)

    # -------------------------------------------------------------- shutdown

    def _on_close(self) -> None:
        self.stop_event.set()
        try:
            self.sender.cancel()
            self.sender.notify_link_down()
        except Exception:
            pass
        self.incoming.close_all()
        self.control.close()
        self.kiss.close()
        self.root.destroy()


def run(
    control: tuple[str, int],
    kiss: tuple[str, int],
    appearance: str = "dark",
    debug: bool = False,
) -> int:
    theme.setup(appearance)
    try:
        root = ctk.CTk()
    except tk.TclError as exc:
        print(
            f"error: cannot open display ({exc}). m73gui is a desktop GUI; "
            "run it on a machine with X11/Wayland access.",
        )
        return 2
    app = App(root, control, kiss)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app._on_close()
    return 0
