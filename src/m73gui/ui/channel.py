"""Channel tab: live CSMA/status view with sparklines."""

from __future__ import annotations

import customtkinter as ctk

from ..constants import COL_OFF
from . import theme
from .theme import BG, FG, FG_DIM, FONTS
from .widgets import Card, Sparkline

COUNTERS = [
    ("rx_frame_count", "RX frames"),
    ("rx_error_count", "RX errors"),
    ("sync_count", "Sync detections"),
    ("preamble_errors", "Preamble errors"),
    ("crc_errors", "CRC errors"),
    ("symbol_errors", "Symbol errors"),
]

HEALTH = [
    ("audio_connected", "Audio"),
    ("rigctl_connected", "rigctld"),
]


class ChannelTab(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color=BG)
        self.app = app

        scroll = ctk.CTkScrollableFrame(self, fg_color=BG)
        scroll.pack(fill="both", expand=True, padx=6, pady=6)

        top = ctk.CTkFrame(scroll, fg_color="transparent")
        top.pack(fill="x")
        top.grid_columnconfigure((0, 1, 2), weight=1)

        # channel state tile
        tile = Card(top, "Channel state")
        tile.grid(row=0, column=0, sticky="nsew", padx=3, pady=3)
        self.big_state = ctk.CTkLabel(
            tile, text="—", font=FONTS["big"], text_color=COL_OFF
        )
        self.big_state.pack(pady=(2, 4))
        ctk.CTkLabel(
            tile,
            text="CSMA backoff phase is not exposed by modem73",
            font=FONTS["small"],
            text_color=FG_DIM,
            wraplength=280,
        ).pack(pady=(0, 8))

        # channel metrics
        metrics = Card(top, "Live metrics")
        metrics.grid(row=0, column=1, sticky="nsew", padx=3, pady=3)
        self.occ_bar = ctk.CTkProgressBar(metrics, mode="determinate")
        self.occ_bar.set(0)
        self.occ_bar.pack(fill="x", padx=12, pady=(6, 2))
        self.occ_label = ctk.CTkLabel(metrics, text="occupancy —", font=FONTS["body"],
                                      text_color=FG, anchor="w")
        self.occ_label.pack(fill="x", padx=12)
        self._metric_labels = {}
        for key, label in [
            ("population", "Population (300 s)"),
            ("tx_queue", "TX queue depth"),
            ("ptt", "PTT"),
        ]:
            row = ctk.CTkFrame(metrics, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=1)
            ctk.CTkLabel(row, text=label, font=FONTS["body"], text_color=FG).pack(
                side="left"
            )
            value = ctk.CTkLabel(row, text="—", font=FONTS["mono_small"],
                                 text_color=FG_DIM)
            value.pack(side="right")
            self._metric_labels[key] = value

        # link stats
        link = Card(top, "Link quality")
        link.grid(row=0, column=2, sticky="nsew", padx=3, pady=3)
        self._link_labels = {}
        for key, label in [
            ("last_snr", "Last SNR"),
            ("last_ber", "Last BER"),
            ("ber_ema", "BER EMA"),
        ]:
            row = ctk.CTkFrame(link, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=1)
            ctk.CTkLabel(row, text=label, font=FONTS["body"], text_color=FG).pack(
                side="left"
            )
            value = ctk.CTkLabel(row, text="—", font=FONTS["mono_small"],
                                 text_color=FG_DIM)
            value.pack(side="right")
            self._link_labels[key] = value
        ctk.CTkLabel(link, text="occupancy (60 s)", font=FONTS["small"],
                     text_color=FG_DIM, anchor="w").pack(fill="x", padx=12,
                                                         pady=(4, 0))
        self.spark_occ = Sparkline(link, color="#d29922", ceil=100.0)
        self.spark_occ.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkLabel(link, text="SNR (60 s)", font=FONTS["small"],
                     text_color=FG_DIM, anchor="w").pack(fill="x", padx=12)
        self.spark_snr = Sparkline(link, color="#58a6ff", floor=-10, ceil=40,
                                   fill=False)
        self.spark_snr.pack(fill="x", padx=12, pady=(0, 8))

        bottom = ctk.CTkFrame(scroll, fg_color="transparent")
        bottom.pack(fill="x")
        bottom.grid_columnconfigure((0, 1), weight=1)

        counters = Card(bottom, "Counters")
        counters.grid(row=0, column=0, sticky="nsew", padx=3, pady=3)
        self._counter_labels = {}
        for key, label in COUNTERS:
            self._counter_labels[self._stat_row(counters, label)] = key
        self.local_tx = ctk.CTkLabel(counters, text="m73gui frames queued: 0",
                                     font=FONTS["mono_small"], text_color=FG_DIM,
                                     anchor="w")
        self.local_tx.pack(fill="x", padx=12, pady=(2, 8))

        right = ctk.CTkFrame(bottom, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=3, pady=3)

        health = Card(right, "Health")
        health.pack(fill="x")
        self._health_labels = {}
        for key, label in HEALTH:
            self._health_labels[self._stat_row(health, label)] = key
        self.clients_row = ctk.CTkLabel(health, text="KISS clients: —",
                                        font=FONTS["mono_small"],
                                        text_color=FG_DIM, anchor="w")
        self.clients_row.pack(fill="x", padx=12, pady=(2, 8))

        csma = Card(right, "CSMA configuration")
        csma.pack(fill="x", pady=(0, 3))
        self.csma_label = ctk.CTkLabel(
            csma, text="—", font=FONTS["mono_small"], text_color=FG_DIM, anchor="w",
            justify="left",
        )
        self.csma_label.pack(fill="x", padx=12, pady=(2, 8))

    def _stat_row(self, parent, label: str) -> ctk.CTkLabel:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=1)
        ctk.CTkLabel(row, text=label, font=FONTS["body"], text_color=FG).pack(
            side="left"
        )
        value = ctk.CTkLabel(row, text="—", font=FONTS["mono_small"],
                             text_color=FG_DIM)
        value.pack(side="right")
        return value

    # -- update ---------------------------------------------------------------

    def update(self, state) -> None:
        status = state.status
        config = state.config
        cstate = state.channel_state()
        self.big_state.configure(
            text=cstate.upper(), text_color=theme.state_color(cstate)
        )

        if config.get("csma_enabled", False):
            pct = int(status.get("occupancy_pct", 0) or 0)
            self.occ_bar.set(min(1.0, max(0.0, pct / 100.0)))
            self.occ_label.configure(text=f"occupancy {pct} %")
            self.spark_occ.push(float(pct))
        else:
            self.occ_bar.set(0)
            self.occ_label.configure(text="CSMA off — occupancy not meaningful")
            self.spark_occ.push(None)

        self._metric_labels["population"].configure(
            text=str(status.get("population", "—"))
        )
        self._metric_labels["tx_queue"].configure(
            text=str(status.get("tx_queue", "—"))
        )
        self._metric_labels["ptt"].configure(
            text="ON" if status.get("ptt_on") else "off"
        )

        snr = status.get("last_snr")
        ber = status.get("last_ber")
        ema = status.get("ber_ema")
        self._link_labels["last_snr"].configure(
            text=f"{snr:+.1f} dB" if snr is not None else "—"
        )
        self._link_labels["last_ber"].configure(
            text=f"{ber * 100:.2f} %" if ber is not None and ber >= 0 else "—"
        )
        self._link_labels["ber_ema"].configure(
            text=f"{ema * 100:.3f} %" if ema is not None and ema >= 0 else "—"
        )
        self.spark_snr.push(float(snr) if snr is not None else None)

        for label, key in self._counter_labels.items():
            label.configure(text=str(status.get(key, "—")))
        self.local_tx.configure(
            text=f"m73gui frames queued: {state.tx_frames_queued}"
        )

        for label, key in self._health_labels.items():
            ok = status.get(key)
            label.configure(
                text="connected" if ok else "disconnected",
                text_color="#3fb950" if ok else "#f85149",
            )
        clients = status.get("client_count")
        self.clients_row.configure(
            text=f"KISS clients: {clients if clients is not None else '—'} "
            "(includes this GUI)"
        )

        self.csma_label.configure(text=self._csma_summary(config))

    @staticmethod
    def _csma_summary(cfg: dict) -> str:
        if not cfg:
            return "—"
        if not cfg.get("csma_enabled"):
            return "CSMA disabled"
        if cfg.get("csma_ranked") and cfg.get("csma_sync_only") and cfg.get(
            "tx_lead_tone"
        ):
            mode = "ranked"
        elif cfg.get("csma_sync_only"):
            mode = "sync-only"
        else:
            mode = "threshold"
        band = "VHF/UHF" if cfg.get("csma_band") else "HF"
        return (
            f"mode: {mode} · band: {band}\n"
            f"threshold: {cfg.get('carrier_threshold_db', '—')} dB · "
            f"slot: {cfg.get('slot_time_ms', '—')} ms · "
            f"quiet: {cfg.get('csma_quiet_ms', '—')} ms\n"
            f"CW: {cfg.get('csma_cw', '—')} · dither: "
            f"{cfg.get('csma_responder_dither', '—')} ms · burst: "
            f"{cfg.get('csma_burst', '—')}\n"
            f"lead tone: {'on' if cfg.get('tx_lead_tone') else 'off'} · beacon: "
            f"{cfg.get('beacon_interval_s', '—')} s"
        )
