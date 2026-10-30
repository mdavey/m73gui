"""Reusable widgets: cards, status chips, labeled entries, sparklines."""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from .theme import BG_CARD, FG, FG_DIM, FONTS


class Card(ctk.CTkFrame):
    """Framed container with a small title."""

    def __init__(self, master, title: str = "", **kwargs) -> None:
        super().__init__(
            master, fg_color=BG_CARD, corner_radius=10, border_width=0, **kwargs
        )
        if title:
            ctk.CTkLabel(
                self, text=title, font=FONTS["badge"], text_color=FG_DIM, anchor="w"
            ).pack(fill="x", padx=12, pady=(10, 0))

    def body(self) -> "Card":
        return self


class StatusChip(ctk.CTkLabel):
    """Small bordered label whose color conveys state (dark badge style:
    colored text and outline on a dark chip, which contrasts far better
    with mid-tone status colors than filled pills do)."""

    def __init__(self, master, text: str = "—", color: str = FG_DIM, **kwargs) -> None:
        self._color = color
        super().__init__(
            master,
            text=text,
            font=FONTS["body"],
            text_color=color,
            fg_color="#1b202a",
            border_width=1,
            border_color=color,
            corner_radius=8,
            padx=10,
            pady=3,
            **kwargs,
        )

    def set(self, text: str, color: str) -> None:
        if text != self.cget("text"):
            self.configure(text=text)
        if color != self._color:
            self._color = color
            self.configure(text_color=color, border_color=color)


class LabeledEntry(ctk.CTkFrame):
    """Label + entry row used by the settings form."""

    def __init__(
        self,
        master,
        label: str,
        width: int = 110,
        hint: str = "",
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self.label_text = label
        self.hint = hint
        self.var = tk.StringVar()
        self.entry = ctk.CTkEntry(self, textvariable=self.var, width=width)
        ctk.CTkLabel(
            self, text=label, font=FONTS["body"], text_color=FG, anchor="w"
        ).pack(side="left")
        self.entry.pack(side="right")

    def get(self) -> str:
        return self.var.get()

    def set(self, value) -> None:
        text = "" if value is None else str(value)
        if text != self.var.get():
            self.var.set(text)


class LabeledSwitch(ctk.CTkFrame):
    def __init__(self, master, label: str) -> None:
        super().__init__(master, fg_color="transparent")
        self.var = tk.BooleanVar()
        self.label_text = label
        self.hint = ""
        ctk.CTkLabel(
            self, text=label, font=FONTS["body"], text_color=FG, anchor="w"
        ).pack(side="left")
        ctk.CTkSwitch(self, text="", variable=self.var, width=44).pack(
            side="right"
        )

    def get(self) -> bool:
        return bool(self.var.get())

    def set(self, value) -> None:
        self.var.set(bool(value))


class Sparkline(ctk.CTkCanvas):
    """60-second rolling line chart."""

    def __init__(
        self,
        master,
        width: int = 340,
        height: int = 60,
        max_samples: int = 120,
        color: str = "#58a6ff",
        floor: float = 0.0,
        ceil: float | None = None,
        fill: bool = True,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg="#14171d",
            highlightthickness=1,
            highlightbackground="#2c3340",
        )
        self.max_samples = max_samples
        self.color = color
        self.floor = floor
        self.ceil = ceil
        self.fill = fill
        self.samples: list[float] = []
        self.bind("<Configure>", lambda e: self.redraw())

    def push(self, value: float | None) -> None:
        if value is None:
            self.samples.append(float("nan"))
        else:
            self.samples.append(float(value))
        if len(self.samples) > self.max_samples:
            del self.samples[: len(self.samples) - self.max_samples]
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        h = max(self.winfo_height() - 2, 10)
        n = len(self.samples)
        if n < 2:
            return
        finite = [s for s in self.samples if s == s]
        if not finite:
            return
        lo = self.floor if self.ceil is not None else min(finite)
        hi = self.ceil if self.ceil is not None else max(finite)
        if hi - lo < 1e-9:
            hi = lo + 1.0
        step = w / (self.max_samples - 1)
        pad = 3
        usable = h - 2 * pad

        def xy(i: int, v: float) -> tuple[float, float]:
            x = (i + self.max_samples - n) * step
            y = pad + (1.0 - (v - lo) / (hi - lo)) * usable
            return x, y

        points: list[tuple[float, float]] = []
        for i, s in enumerate(self.samples):
            if s != s:  # NaN gap
                self._draw(points, w, h)
                points = []
                continue
            points.append(xy(i, s))
        self._draw(points, w, h)

    def _draw(self, points: list[tuple[float, float]], w: int, h: int) -> None:
        if len(points) >= 2:
            flat = [c for pt in points for c in pt]
            if self.fill:
                poly = [points[0][0], h - 2, *flat, points[-1][0], h - 2]
                self.create_polygon(poly, fill=self.color, stipple="gray25",
                                    outline="")
            self.create_line(flat, fill=self.color, width=1)
