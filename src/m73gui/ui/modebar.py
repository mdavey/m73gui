"""Compact per-tab broadcast mode row (Chat / Files).

The row is the tab's *intent*, not live modem state: edits are applied to the
modem only when the user presses Send (see App.apply_mode_then), and remote
config changes never overwrite it."""

from __future__ import annotations

import customtkinter as ctk

from ..constants import (
    CODE_RATES,
    FRAME_SIZE_NAMES,
    MFSK_MODE_NAMES,
    MODEM_TYPES,
    OFDM_MODULATIONS,
    ROBUST_MODE_NAMES,
)
from .theme import FG, FG_DIM, FONTS

# fields that identify a transmit mode; live config may carry ints or strings
MODE_FIELDS = (
    "modem_type",
    "modulation",
    "code_rate",
    "frame_size",
    "mfsk_mode",
    "robust_mode",
)

MODULATION_OPTIONS = [(m, m) for m in OFDM_MODULATIONS]
CODE_RATE_OPTIONS = [(r, r) for r in CODE_RATES]
FRAME_SIZE_OPTIONS = [(FRAME_SIZE_NAMES[i], str(i)) for i in (0, 1, 2)]
MFSK_OPTIONS = [(name, str(i)) for i, name in enumerate(MFSK_MODE_NAMES)]
ROBUST_OPTIONS = [(name, str(i)) for i, name in enumerate(ROBUST_MODE_NAMES)]


def _norm(value) -> str:
    return str(value).strip().lower()


def mode_differs(fields: dict, config: dict) -> bool:
    """True when pending *fields* differ from live *config* (int/string
    insensitive, on the fields the row actually covers)."""
    return any(
        _norm(config.get(k)) != _norm(v) or config.get(k) is None
        for k, v in fields.items()
    )


def changed_mode_fields(fields: dict, config: dict) -> dict:
    """Subset of *fields* the modem does not already have."""
    return {k: v for k, v in fields.items() if _norm(config.get(k)) != _norm(v)}


class ModeRow(ctk.CTkFrame):
    """One line: OFDM/MFSK/ROBUST segmented button + mode-specific menus."""

    def __init__(self, master, on_change=None, label: str = "Mode") -> None:
        super().__init__(master, fg_color="transparent")
        self.on_change = on_change
        self._loading = False

        if label:
            ctk.CTkLabel(self, text=label, font=FONTS["small"],
                         text_color=FG_DIM).pack(side="left", padx=(0, 6))
        self.seg = ctk.CTkSegmentedButton(
            self, values=list(MODEM_TYPES), width=180,
            command=self._on_modem_type,
        )
        self.seg.set(MODEM_TYPES[0])
        self.seg.pack(side="left")

        def menu(spec, width):
            box = ctk.CTkOptionMenu(
                self, values=[d for d, _ in spec], width=width,
                font=FONTS["small"], command=self._fire,
            )
            box.set(spec[0][0])
            return box

        self.controls: dict[str, tuple[ctk.CTkOptionMenu, list]] = {
            "modulation": (menu(MODULATION_OPTIONS, 100), MODULATION_OPTIONS),
            "code_rate": (menu(CODE_RATE_OPTIONS, 80), CODE_RATE_OPTIONS),
            "frame_size": (menu(FRAME_SIZE_OPTIONS, 90), FRAME_SIZE_OPTIONS),
            "mfsk_mode": (menu(MFSK_OPTIONS, 110), MFSK_OPTIONS),
            "robust_mode": (menu(ROBUST_OPTIONS, 120), ROBUST_OPTIONS),
        }
        self._by_mode = {
            0: ("modulation", "code_rate", "frame_size"),
            1: ("mfsk_mode",),
            2: ("robust_mode",),
        }
        self._apply_visibility()

    # -- state ---------------------------------------------------------------

    def modem_type(self) -> int:
        try:
            return MODEM_TYPES.index(self.seg.get())
        except ValueError:
            return 0

    def fields(self) -> dict:
        """Current selection in get_config representation."""
        mtype = self.modem_type()
        out: dict = {"modem_type": mtype}
        for name in self._by_mode[mtype]:
            box, spec = self.controls[name]
            for display, value in spec:
                if display == box.get():
                    out[name] = (
                        value if name in ("modulation", "code_rate")
                        else int(value)
                    )
                    break
        return out

    def set_from_config(self, cfg: dict) -> None:
        """Silently load live config or persisted settings into the row."""
        self._loading = True
        try:
            mtype = cfg.get("modem_type")
            if mtype is not None and 0 <= int(mtype) < len(MODEM_TYPES):
                self.seg.set(MODEM_TYPES[int(mtype)])
            for name, (box, spec) in self.controls.items():
                if cfg.get(name) is None:
                    continue
                want = _norm(cfg[name])
                for display, value in spec:
                    if _norm(value) == want:
                        box.set(display)
                        break
            self._apply_visibility()
        except (TypeError, ValueError):
            pass
        finally:
            self._loading = False

    # -- internals -----------------------------------------------------------

    def _on_modem_type(self, _value) -> None:
        self._apply_visibility()
        self._fire()

    def _fire(self, *_args) -> None:
        # CTkOptionMenu/CTkSegmentedButton pass the selected value to command
        if not self._loading and self.on_change:
            self.on_change()

    def _apply_visibility(self) -> None:
        want = self.modem_type()
        for mtype, names in self._by_mode.items():
            for name in names:
                box = self.controls[name][0]
                if mtype == want:
                    box.pack(side="left", padx=(4, 0))
                else:
                    box.pack_forget()
