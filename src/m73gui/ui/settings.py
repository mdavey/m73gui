"""Settings tab: grouped config editor, read-only info, raw JSON."""

from __future__ import annotations

import json
import re

import customtkinter as ctk

from ..constants import (
    CODE_RATES,
    COL_WARN,
    FRAME_SIZE_NAMES,
    MFSK_MODE_NAMES,
    OFDM_MODULATIONS,
    ROBUST_MODE_NAMES,
)
from .theme import BG, FG, FG_DIM, FONTS
from .widgets import Card

CALLSIGN_RE = re.compile(r"^[A-Z0-9/]{1,9}$")

MODULATION_OPTIONS = [(m, m) for m in OFDM_MODULATIONS]
CODE_RATE_OPTIONS = [(r, r) for r in CODE_RATES]
FRAME_SIZE_OPTIONS = [(FRAME_SIZE_NAMES[i], str(i)) for i in (0, 1, 2)]
MFSK_OPTIONS = [(name, str(i)) for i, name in enumerate(MFSK_MODE_NAMES)]
ROBUST_OPTIONS = [(name, str(i)) for i, name in enumerate(ROBUST_MODE_NAMES)]
BAND_OPTIONS = [("HF", "0"), ("VHF/UHF", "1")]
MODEM_TYPES = ["OFDM", "MFSK", "ROBUST"]


class Field:
    """One editable config field bound to a widget."""

    def __init__(self, master, name: str, label: str, kind: str, spec=None):
        self.name = name
        self.kind = kind
        self.spec = spec
        if kind == "switch":
            self.widget = ctk.CTkSwitch(master, text=label)
        elif kind == "option":
            self.widget = ctk.CTkOptionMenu(
                master, values=[display for display, _ in spec], width=130
            )
        elif kind == "entry":
            row = ctk.CTkFrame(master, fg_color="transparent")
            ctk.CTkLabel(row, text=label, font=FONTS["body"], text_color=FG).pack(
                side="left"
            )
            self.widget = ctk.CTkEntry(row, width=110)
            self._row = row
        else:
            raise ValueError(kind)
        if kind != "entry":
            self._row = None

    def pack(self, **kw) -> None:
        (self._row or self.widget).pack(**kw)

    def get_config_value(self):
        """Current widget value in get_config representation, or raises."""
        if self.kind == "switch":
            return bool(self.widget.get())
        if self.kind == "option":
            display = self.widget.get()
            for d, v in self.spec:
                if d == display:
                    return v
            raise ValueError(f"bad option for {self.name}")
        text = self.widget.get().strip()
        return self._validate_text(text)

    def _validate_text(self, text: str):
        spec = self.spec or "str"
        if spec == "callsign":
            text = text.upper()
            if not CALLSIGN_RE.match(text):
                raise ValueError(
                    f"{self.name}: must match [A-Z0-9/]{{1,9}} (got {text!r})"
                )
            return text
        if isinstance(spec, tuple):
            kind_, lo, hi = spec
            try:
                value = int(text) if kind_ == "int" else float(text)
            except ValueError:
                raise ValueError(f"{self.name}: {text!r} is not a number") from None
            if not lo <= value <= hi:
                raise ValueError(f"{self.name}: {value} outside {lo}..{hi}")
            return value
        return text

    def set_config_value(self, value) -> None:
        if self.kind == "switch":
            self.widget.select() if bool(value) else self.widget.deselect()
        elif self.kind == "option":
            want = str(value)
            for d, v in self.spec:
                if str(v) == want:
                    self.widget.set(d)
                    return
            # value not in table: keep current selection
        elif self.name == "tx_drive":
            self.widget.set(f"{float(value) * 100:.0f}")
        elif isinstance(value, float):
            self.widget.set(f"{value:g}")
        else:
            self.widget.set("" if value is None else str(value))


GROUP_SPEC = [
    ("Station", [("callsign", "Callsign", "entry", "callsign")]),
    (
        "Modem",
        [
            ("modulation", "Modulation", "option", MODULATION_OPTIONS),
            ("code_rate", "Code rate", "option", CODE_RATE_OPTIONS),
            ("frame_size", "Frame size", "option", FRAME_SIZE_OPTIONS),
            ("postamble", "Postamble (OFDM)", "switch", None),
            ("mfsk_mode", "MFSK mode", "option", MFSK_OPTIONS),
            ("robust_mode", "ROBUST mode", "option", ROBUST_OPTIONS),
        ],
    ),
    (
        "CSMA",
        [
            ("csma_enabled", "CSMA enabled", "switch", None),
            ("csma_sync_only", "Sync-only busy detect", "switch", None),
            ("csma_ranked", "Ranked turn order", "switch", None),
            ("csma_fast_floor", "Fast sync window", "switch", None),
            ("tx_lead_tone", "Signature lead tone", "switch", None),
            ("beacon_interval_s", "Beacon interval s", "entry", ("int", 45, 90)),
            ("csma_band", "CSMA band", "option", BAND_OPTIONS),
            ("carrier_threshold_db", "Carrier threshold dB", "entry", ("float", -100, 40)),
            ("p_persistence", "P-persistence", "entry", ("int", 0, 255)),
            ("slot_time_ms", "Slot time ms", "entry", ("int", 0, 10000)),
            ("csma_quiet_ms", "Quiet time ms", "entry", ("int", 0, 60000)),
            ("csma_cw", "Contention window", "entry", ("int", 0, 255)),
            ("csma_responder_dither", "Responder dither ms", "entry", ("int", 0, 5000)),
            ("csma_burst", "Burst frames", "entry", ("int", 1, 4)),
        ],
    ),
    (
        "TX",
        [
            ("tx_drive", "TX drive %", "entry", ("int", 5, 100)),
            ("tx_blanking_enabled", "TX blanking", "switch", None),
            ("fragmentation_enabled", "Fragmentation", "switch", None),
        ],
    ),
    (
        "RX decoders",
        [
            ("ofdm_rx_enabled", "OFDM decoder", "switch", None),
            ("mfsk_rx_enabled", "MFSK decoder", "switch", None),
            ("robust_rx_enabled", "ROBUST decoder", "switch", None),
        ],
    ),
]

READONLY_FIELDS = [
    ("audio_input", "Audio input"),
    ("audio_output", "Audio output"),
    ("audio_backend", "Audio backend"),
    ("center_freq", "Center freq Hz"),
    ("payload_size", "Payload size B"),
    ("short_frame", "Short frame (derived)"),
]

NUMERIC_OPTION_FIELDS = {"frame_size", "mfsk_mode", "robust_mode", "csma_band"}

GROUP_FOR_MODE = {
    "modulation": 0,
    "code_rate": 0,
    "frame_size": 0,
    "postamble": 0,
    "mfsk_mode": 1,
    "robust_mode": 2,
}


class SettingsTab(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color=BG)
        self.app = app
        self.snapshot: dict = {}
        self.fields: dict[str, Field] = {}
        self._remote_banner_on = False
        self._loading = False

        scroll = ctk.CTkScrollableFrame(self, fg_color=BG)
        scroll.pack(fill="both", expand=True, padx=6, pady=4)

        self.banner = ctk.CTkLabel(
            scroll, text="", font=FONTS["body"], text_color=COL_WARN, anchor="w"
        )

        # ---- modem type + station ----
        for group_title, fields in GROUP_SPEC:
            card = Card(scroll, group_title)
            card.pack(fill="x", pady=3)
            if group_title == "Modem":
                seg_row = ctk.CTkFrame(card, fg_color="transparent")
                seg_row.pack(fill="x", padx=12, pady=(6, 2))
                ctk.CTkLabel(
                    seg_row, text="Modem type", font=FONTS["body"], text_color=FG
                ).pack(side="left")
                self.modem_seg = ctk.CTkSegmentedButton(
                    seg_row,
                    values=MODEM_TYPES,
                    command=self._on_modem_type,
                )
                self.modem_seg.pack(side="right")
            for name, label, kind, spec in fields:
                field = Field(card, name, label, kind, spec)
                field.pack(fill="x", padx=12, pady=1)
                self.fields[name] = field
            if group_title == "TX":
                self.blank_hint = ctk.CTkLabel(
                    card,
                    text="TX blanking is forced on by modem73 while CSMA is enabled",
                    font=FONTS["small"],
                    text_color=FG_DIM,
                    anchor="w",
                )
                self.blank_hint.pack(fill="x", padx=12)

        # ---- read-only ----
        ro_card = Card(scroll, "Read-only info")
        ro_card.pack(fill="x", pady=3)
        self.readonly_labels: dict[str, ctk.CTkLabel] = {}
        for name, label in READONLY_FIELDS:
            row = ctk.CTkFrame(ro_card, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=1)
            ctk.CTkLabel(row, text=label, font=FONTS["body"], text_color=FG).pack(
                side="left"
            )
            value = ctk.CTkLabel(
                row, text="—", font=FONTS["mono_small"], text_color=FG_DIM
            )
            value.pack(side="right")
            self.readonly_labels[name] = value
        self.derived_label = ctk.CTkLabel(
            ro_card, text="", font=FONTS["mono_small"], text_color=FG_DIM, anchor="w"
        )
        self.derived_label.pack(fill="x", padx=12, pady=(2, 6))

        # ---- raw JSON ----
        raw_card = Card(scroll, "Raw config JSON")
        raw_card.pack(fill="both", pady=3)
        self.raw = ctk.CTkTextbox(
            raw_card, font=FONTS["mono_small"], fg_color="#14171d",
            text_color=FG, height=220, wrap="word",
        )
        self.raw.pack(fill="both", expand=True, padx=12, pady=6)

        # ---- actions ----
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=10, pady=(0, 8))
        self.apply_btn = ctk.CTkButton(actions, text="Apply changes", width=130,
                                       command=self._apply)
        self.apply_btn.pack(side="left")
        self.refresh_btn = ctk.CTkButton(actions, text="Refresh settings", width=130,
                                         command=lambda: app.refresh_settings(),
                                         fg_color="#3a4557")
        self.refresh_btn.pack(side="left", padx=6)
        self.status_label = ctk.CTkLabel(
            actions, text="no settings loaded", font=FONTS["small"],
            text_color=FG_DIM,
        )
        self.status_label.pack(side="left", padx=10)

    # -- sync ---------------------------------------------------------------

    def load(self, config: dict) -> None:
        self._loading = True
        try:
            self.snapshot = dict(config)
            for name, field in self.fields.items():
                if name in config:
                    field.set_config_value(config[name])
            mtype = config.get("modem_type")
            if mtype in (0, 1, 2):
                self.modem_seg.set(MODEM_TYPES[int(mtype)])
            self._apply_mode_visibility()
            for name, label in self.readonly_labels.items():
                value = config.get(name, "—")
                label.configure(text=str(value))
            self.derived_label.configure(
                text=f"modulation: {config.get('modulation', '—')}   "
                f"tx_blanking forced: {bool(config.get('csma_enabled'))}"
            )
            self.raw.delete("1.0", "end")
            self.raw.insert("1.0", json.dumps(config, indent=2, default=str))
            self._set_banner("")
        finally:
            self._loading = False

    def dirty(self) -> bool:
        if not self.snapshot:
            return False
        changed, errors = self.changed_fields()
        return bool(errors) or bool(changed)

    def _collect(self) -> tuple[dict, list[str]]:
        """Changed fields (config representation) and validation errors."""
        fields: dict = {}
        errors: list[str] = []
        want_modem = MODEM_TYPES.index(self.modem_seg.get())
        for name, field in self.fields.items():
            mode = GROUP_FOR_MODE.get(name)
            if mode is not None and mode != want_modem:
                continue
            if name == "tx_drive":
                text = field.widget.get().strip()
                try:
                    value = float(text) / 100.0
                except ValueError:
                    errors.append(f"{name}: {text!r} is not a number")
                    continue
                if not 0.05 <= value <= 1.0:
                    errors.append(f"{name}: {text!r} outside 5..100 %")
                    continue
                fields[name] = value
                continue
            try:
                value = field.get_config_value()
                if name in NUMERIC_OPTION_FIELDS:
                    value = int(value)
                fields[name] = value
            except ValueError as exc:
                errors.append(str(exc))
        fields["modem_type"] = want_modem
        return fields, errors

    def changed_fields(self) -> tuple[dict, list[str]]:
        """Only fields whose value differs from the loaded snapshot."""
        current, errors = self._collect()
        changed = {
            k: v
            for k, v in current.items()
            if k not in self.snapshot or self._norm(self.snapshot[k]) != self._norm(v)
        }
        return changed, errors

    @staticmethod
    def _norm(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 6)
        return value

    # -- actions -------------------------------------------------------------

    def _apply(self) -> None:
        changed, errors = self.changed_fields()
        if errors:
            self.status_label.configure(text="; ".join(errors)[:200], text_color=COL_WARN)
            return
        if not changed:
            self.status_label.configure(text="no changes to apply", text_color=FG_DIM)
            return
        self.app.apply_settings(changed)

    def on_apply_result(self, ok: bool, message: str) -> None:
        color = "#3fb950" if ok else COL_WARN
        self.status_label.configure(text=message, text_color=color)
        if ok:
            self.app.refresh_settings()

    def on_remote_config(self, config: dict) -> None:
        if self._loading:
            return
        if self.dirty():
            self._set_banner(
                "Settings changed remotely — Refresh to reload (your edits kept)"
            )
        else:
            self.load(config)

    def mark_saved(self) -> None:
        self._set_banner("")

    def _set_banner(self, text: str) -> None:
        if text and not self._remote_banner_on:
            self.banner.pack(fill="x", pady=(0, 2))
            self._remote_banner_on = True
        elif not text and self._remote_banner_on:
            self.banner.pack_forget()
            self._remote_banner_on = False
        self.banner.configure(text=text)

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.apply_btn.configure(state=state)

    # -- modem type visibility --------------------------------------------------

    def _on_modem_type(self, _value) -> None:
        self._apply_mode_visibility()

    def _apply_mode_visibility(self) -> None:
        try:
            want = MODEM_TYPES.index(self.modem_seg.get())
        except ValueError:
            return
        for name, mode in GROUP_FOR_MODE.items():
            field = self.fields[name]
            widget = field._row or field.widget
            if mode == want:
                widget.pack(fill="x", padx=12, pady=1)
            else:
                widget.pack_forget()
