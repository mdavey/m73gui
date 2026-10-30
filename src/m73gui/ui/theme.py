"""Appearance setup, shared fonts, and dark ttk styles."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

from ..constants import COL_IDLE, COL_OFF, COL_RX, COL_TX

APP_FONT = "TkDefaultFont"


class _LazyFonts(dict):
    """CTkFont objects need a Tk root, so build them on first use."""

    def _ensure(self) -> None:
        if not self:
            self.update(
                {
                    "title": ctk.CTkFont(size=16, weight="bold"),
                    "badge": ctk.CTkFont(size=12, weight="bold"),
                    "body": ctk.CTkFont(size=12),
                    "small": ctk.CTkFont(size=10),
                    "mono": ctk.CTkFont(family="TkFixedFont", size=11),
                    "mono_small": ctk.CTkFont(family="TkFixedFont", size=9),
                    "big": ctk.CTkFont(size=28, weight="bold"),
                }
            )

    def __getitem__(self, key):
        self._ensure()
        return super().__getitem__(key)


FONTS = _LazyFonts()

BG = "#1c1f26"
BG_CARD = "#242a35"
FG = "#e6e6e6"
FG_DIM = "#9aa4b2"


def setup(appearance: str) -> None:
    ctk.set_appearance_mode(appearance)
    ctk.set_default_color_theme("dark-blue")


def style_treeview(tree: ttk.Treeview) -> None:
    """Apply a dark style to a ttk Treeview."""
    style = ttk.Style(tree.master)
    style_name = "m73.Treeview"
    style.configure(
        style_name,
        background="#14171d",
        fieldbackground="#14171d",
        foreground=FG,
        rowheight=22,
        borderwidth=0,
    )
    style.configure(
        f"{style_name}.Heading",
        background="#242a35",
        foreground=FG_DIM,
        borderwidth=0,
        relief="flat",
    )
    style.map(
        style_name,
        background=[("selected", "#3a5a8f")],
        foreground=[("selected", "#ffffff")],
    )
    style.configure("m73.Vertical.TScrollbar", background="#2c3340")
    tree.configure(style=style_name)
    tree.tag_configure("rx", foreground="#7fc4ff")
    tree.tag_configure("tx", foreground="#8fd694")
    tree.tag_configure("history", foreground="#6e7681")
    tree.tag_configure("warn", foreground="#e3b341")


def state_color(channel_state: str) -> str:
    return {
        "idle": COL_IDLE,
        "tx": COL_TX,
        "rx": COL_RX,
    }.get(channel_state, COL_OFF)


def modal(parent: tk.Misc, title: str, width: int = 720, height: int = 520):
    """Create a centered modal Toplevel with a title bar."""
    top = ctk.CTkToplevel(parent)
    top.title(title)
    top.geometry(f"{width}x{height}")
    top.transient(parent.winfo_toplevel())

    def _grab() -> None:
        try:
            top.grab_set()
        except tk.TclError:
            pass

    top.after(10, _grab)
    top.configure(fg_color=BG)
    return top
