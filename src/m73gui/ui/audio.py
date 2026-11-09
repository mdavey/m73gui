"""Audio tab: live Opus broadcast (OFDM modes only) and receive sessions."""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk
from opuslink import plan_label

from .. import phytime
from ..services.audio_tx import audio_plan
from ..state import TX_FAILED, TX_PENDING, TX_SENDING
from . import theme
from .modebar import ModeRow
from .theme import BG, FG, FG_DIM, FONTS
from .widgets import Card

GOOD_COLOR = "#3fb950"
BAD_COLOR = "#f85149"


class AudioTab(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color=BG)
        self.app = app
        self._rows: dict[tuple[str, int], str] = {}

        outer = ctk.CTkFrame(self, fg_color=BG)
        outer.pack(fill="both", expand=True, padx=6, pady=6)

        # ---- tx settings ----
        mode_card = Card(outer, "Tx Settings")
        mode_card.pack(fill="x", pady=(0, 4))
        self.mode_row = ModeRow(mode_card, on_change=self._mode_changed,
                                label="", modem_types=("OFDM",))
        self.mode_row.pack(fill="x", padx=12, pady=(8, 2))
        self.estimate_label = ctk.CTkLabel(
            mode_card, text="—", font=FONTS["small"], text_color=FG_DIM,
            anchor="w", justify="left", wraplength=980,
        )
        self.estimate_label.pack(fill="x", padx=12, pady=(0, 8))

        # ---- broadcast ----
        bcard = Card(outer, "Live broadcast")
        bcard.pack(fill="x", pady=4)
        brow = ctk.CTkFrame(bcard, fg_color="transparent")
        brow.pack(fill="x", padx=12, pady=(6, 4))
        self.start_btn = ctk.CTkButton(brow, text="Start broadcast", width=140,
                                       command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(brow, text="Stop", width=90,
                                      fg_color="#6e3b3b",
                                      command=self._stop)
        self.stop_btn.pack(side="left", padx=6)
        self.stop_btn.configure(state="disabled")
        self.tx_status = ctk.CTkLabel(bcard, text="Idle.", font=FONTS["small"],
                                      text_color=FG_DIM, anchor="w")
        self.tx_status.pack(fill="x", padx=12, pady=(0, 8))

        # ---- incoming ----
        in_card = Card(outer, "Incoming audio")
        in_card.pack(fill="both", expand=True, pady=4)
        irow = ctk.CTkFrame(in_card, fg_color="transparent")
        irow.pack(fill="x", padx=12, pady=(6, 0))
        self.speaker_on = tk.BooleanVar(
            value=bool(self.app.gui_settings.get("audio_speaker", True))
        )
        switch = ctk.CTkSwitch(
            irow, text="Play audio out loud", variable=self.speaker_on,
            command=self._persist_speaker,
        )
        switch.pack(side="left")
        self.rx_hint = ctk.CTkLabel(
            irow, text="the most recent talker plays; buffer absorbs packet bursts",
            font=FONTS["small"], text_color=FG_DIM,
        )
        self.rx_hint.pack(side="left", padx=10)

        from tkinter import ttk

        tree = ttk.Treeview(
            in_card,
            columns=("sender", "mode", "packets", "gaps", "plc", "buffer",
                     "state"),
            show="headings",
            height=7,
        )
        theme.style_treeview(tree)
        for key, label, width in [
            ("sender", "Sender", 110),
            ("mode", "Opus mode", 210),
            ("packets", "Packets", 80),
            ("gaps", "Lost pk", 70),
            ("plc", "PLC", 60),
            ("buffer", "Buffer", 90),
            ("state", "State", 90),
        ]:
            tree.heading(key, text=label)
            tree.column(key, width=width)
        tree.pack(fill="both", expand=True, padx=12, pady=6)
        self.tree = tree

        self.refresh_estimate()

    # -- estimate -------------------------------------------------------------

    def broadcast_config(self) -> dict:
        return {**self.app.state.config, **self.mode_row.fields()}

    def _mode_changed(self) -> None:
        self.app.persist_gui(audio_mode=self.mode_row.fields())
        self.refresh_estimate()

    def on_live_config(self) -> None:
        self.refresh_estimate()

    def refresh_estimate(self, *_args) -> None:
        # Judge the PENDING selection: payload_size in the live config is
        # still the applied mode's MTU, so mirror the Files tab and size the
        # row's mode via phytime.payload_bytes_for(broadcast_config()).
        cfg = self.broadcast_config()
        payload = phytime.payload_bytes_for(cfg)
        airtime = phytime.frame_seconds(cfg)
        plan = audio_plan(cfg, payload_bytes=payload)
        if plan is not None:
            text = (
                f"{plan_label(plan)} · mode data rate "
                f"{payload * 8 / airtime / 1000:.1f} kbps · "
                f"{plan.packet_audio_s:.1f} s of audio per packet · est. "
                f"latency ~{plan.latency_est_s:.0f} s"
            )
            self.estimate_label.configure(text=text, text_color=GOOD_COLOR)
            if not self._busy_tx():
                self.start_btn.configure(state="normal")
            return
        if payload <= 0:
            text = "Modem mode invalid — no payload capacity."
        else:
            kbps = payload * 8 / airtime / 1000 if airtime > 0 else 0.0
            text = (
                f"Not enough data rate: mode data rate is only "
                f"{kbps:.1f} kbps and opus audio needs at least 6 kbps "
                "once packet overheads are paid. Pick a faster OFDM mode "
                "(wide OFDM recommended)."
            )
        self.estimate_label.configure(text=text, text_color=BAD_COLOR)
        if not self._busy_tx():
            self.start_btn.configure(state="disabled")

    # -- broadcast actions --------------------------------------------------------

    def _busy_tx(self) -> bool:
        snap = self.app.state.audio
        return snap is not None and snap.status in (TX_PENDING, TX_SENDING)

    def _start(self) -> None:
        err = self.app.start_audio(self.mode_row.fields())
        if err:
            self.app.notify(err, level="error")

    def _stop(self) -> None:
        self.app.stop_audio()

    # -- snapshots ------------------------------------------------------------

    def update_tx(self, snap) -> None:
        running = snap.status == TX_SENDING
        if running:
            text = (
                f"{snap.tier} · {snap.bitrate // 1000} kbps · sent "
                f"{snap.packets_sent} packets ({snap.audio_seconds():.0f} s "
                f"of audio)"
            )
            if snap.frames_dropped:
                text += f" · dropped {snap.frames_dropped} frames (link busy)"
            color = FG
        else:
            text = f"{snap.status}: {snap.message}"
            color = (
                BAD_COLOR if snap.status == TX_FAILED
                else FG_DIM
            )
        self.tx_status.configure(text=text, text_color=color)
        self.start_btn.configure(state="disabled" if self._busy_tx() else "normal")
        self.stop_btn.configure(state="normal" if self._busy_tx() else "disabled")

    def update_rx(self, snap) -> None:
        key = (snap.callsign, snap.session)
        iid = self._rows.get(key)
        mode = (
            f"{snap.tier} · {snap.sample_rate // 1000} kHz · "
            f"{'stereo' if snap.channels == 2 else 'mono'} · "
            f"{snap.bitrate // 1000} kbps"
        )
        values = (
            snap.callsign or "?",
            mode,
            snap.packets,
            snap.gap_packets,
            snap.plc_frames,
            f"{snap.buffer_ms} ms",
            "playing" if snap.playing else "buffering",
        )
        if iid is None:
            iid = self.tree.insert("", "end", values=values)
            self._rows[key] = iid
        else:
            self.tree.item(iid, values=values)

    def prune_dead(self, live_keys) -> None:
        live = set(live_keys)
        for key, iid in list(self._rows.items()):
            if key not in live:
                try:
                    self.tree.delete(iid)
                except tk.TclError:
                    pass
                del self._rows[key]

    # -- settings -------------------------------------------------------------

    def _persist_speaker(self) -> None:
        self.app.set_audio_speaker(bool(self.speaker_on.get()))
