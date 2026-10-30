"""Persistent status header."""

from __future__ import annotations

import customtkinter as ctk

from ..constants import COL_OFF, COL_IDLE
from .theme import FG, FG_DIM, FONTS, state_color
from .widgets import StatusChip


class Header(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="#20242d", corner_radius=0, height=64)
        self.app = app
        self.grid_columnconfigure(4, weight=1)
        self.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=(14, 0), pady=8)
        ctk.CTkLabel(left, text="M73", font=FONTS["title"], text_color="#7fd0ff").pack(
            side="left"
        )
        self.callsign = StatusChip(left, "?", COL_IDLE)
        self.callsign.pack(side="left", padx=(8, 0))
        self.mode = StatusChip(left, "—", "#9aa4b2")
        self.mode.pack(side="left", padx=(6, 0))
        self.payload = ctk.CTkLabel(
            left, text="payload —", font=FONTS["small"], text_color=FG_DIM
        )
        self.payload.pack(side="left", padx=(8, 0))

        conn = ctk.CTkFrame(self, fg_color="transparent")
        conn.grid(row=0, column=1, sticky="w", padx=12)
        self.ctrl_chip = StatusChip(conn, "control ●", COL_OFF)
        self.ctrl_chip.pack(side="left")
        self.kiss_chip = StatusChip(conn, "kiss ●", COL_OFF)
        self.kiss_chip.pack(side="left", padx=(6, 0))
        self.endpoints = ctk.CTkLabel(
            conn, text="", font=FONTS["mono_small"], text_color=FG_DIM
        )
        self.endpoints.pack(side="left", padx=(6, 0))

        chan = ctk.CTkFrame(self, fg_color="transparent")
        chan.grid(row=0, column=3, sticky="e", padx=8)
        self.channel = StatusChip(chan, "Idle", COL_IDLE)
        self.channel.pack(side="left")
        self.occupancy_bar = ctk.CTkProgressBar(chan, width=110, mode="determinate")
        self.occupancy_bar.set(0)
        self.occupancy_bar.pack(side="left", padx=(8, 0))
        self.occupancy_label = ctk.CTkLabel(
            chan, text="occ —", font=FONTS["small"], text_color=FG_DIM, width=52
        )
        self.occupancy_label.pack(side="left")
        self.snr = ctk.CTkLabel(chan, text="SNR —", font=FONTS["small"], text_color=FG_DIM)
        self.snr.pack(side="left", padx=(8, 0))
        self.ber = ctk.CTkLabel(chan, text="BER —", font=FONTS["small"], text_color=FG_DIM)
        self.ber.pack(side="left", padx=(4, 0))

        self.refresh = ctk.CTkButton(
            self,
            text="Refresh",
            width=86,
            command=lambda: app.refresh_all(),
        )
        self.refresh.grid(row=0, column=5, sticky="e", padx=(4, 14), pady=10)

    def set_endpoints(self, control: tuple[str, int], kiss: tuple[str, int]) -> None:
        self.endpoints.configure(
            text=f"{control[0]}:{control[1]} · {kiss[0]}:{kiss[1]}"
        )

    def update(self, state) -> None:
        self.callsign.set(state.callsign, COL_IDLE if state.control_connected else COL_OFF)
        self.mode.set(state.mode_label(), "#9aa4b2")
        ps = state.payload_size
        self.payload.configure(text=f"payload {ps} B" if ps else "payload —")

        self.ctrl_chip.set(
            "Control ●" if state.control_connected else "Control ○",
            "#3fb950" if state.control_connected else COL_OFF,
        )
        self.kiss_chip.set(
            "KISS ●" if state.kiss_connected else "KISS ○",
            "#3fb950" if state.kiss_connected else COL_OFF,
        )

        cstate = state.channel_state()
        self.channel.set(cstate.capitalize(), state_color(cstate))
        status = state.status
        if state.config.get("csma_enabled", False):
            pct = int(status.get("occupancy_pct", 0) or 0)
            self.occupancy_bar.set(min(1.0, max(0.0, pct / 100.0)))
            self.occupancy_bar.configure(
                progress_color="#d29922" if pct > 75 else "#2cc985"
            )
            self.occupancy_label.configure(text=f"occ {pct}%")
        else:
            self.occupancy_bar.set(0)
            self.occupancy_label.configure(text="CSMA off")
        snr = status.get("last_snr")
        ber = status.get("last_ber")
        self.snr.configure(text=f"SNR {snr:+.1f} dB" if snr is not None else "SNR —")
        self.ber.configure(
            text=f"BER {ber * 100:.2f}%"
            if ber is not None and ber >= 0
            else "BER —"
        )
