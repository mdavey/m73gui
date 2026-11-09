"""Packet Log tab: table of RX/TX frames with details and raw dump."""

from __future__ import annotations

import collections
import json
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

from ..constants import PACKET_TABLE_MAX_ROWS
from ..state import RX, TX, PacketRow
from . import theme
from .theme import BG, FG, FG_DIM, FONTS

COLUMNS = [
    ("time", "Time", 80),
    ("dir", "Dir", 40),
    ("call", "Call", 80),
    ("mode", "Mode", 110),
    ("bytes", "Bytes", 60),
    ("snr", "SNR", 60),
    ("type", "Type", 90),
    ("summary", "Summary", 400),
]

TYPE_FILTERS = ["All", "Chat", "Files", "Audio", "Control", "Raw"]
DIR_FILTERS = ["All", "RX", "TX"]


def _group(kind: str) -> str:
    if kind == "chat":
        return "Chat"
    if kind.startswith("file"):
        return "Files"
    if kind == "audio":
        return "Audio"
    if kind in ("control", "malformed"):
        return "Control"
    return "Raw"


def hexdump(data: bytes, limit: int = 512) -> str:
    out = []
    shown = data[:limit]
    for off in range(0, len(shown), 16):
        chunk = shown[off : off + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off:04x}  {hexpart}  |{text}|")
    if len(data) > limit:
        out.append(f"... {len(data) - limit} more bytes")
    return "\n".join(out)


class PacketsTab(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color=BG)
        self.app = app
        self.rows: collections.deque[PacketRow] = collections.deque(
            maxlen=PACKET_TABLE_MAX_ROWS
        )
        self.by_iid: dict[str, PacketRow] = {}
        self._counter = 0

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(8, 2))
        self.type_filter = ctk.CTkOptionMenu(
            bar, values=TYPE_FILTERS, width=100, command=lambda _=None: self.rebuild()
        )
        self.type_filter.pack(side="left")
        self.dir_filter = ctk.CTkOptionMenu(
            bar, values=DIR_FILTERS, width=80, command=lambda _=None: self.rebuild()
        )
        self.dir_filter.pack(side="left", padx=(6, 0))
        self.autoscroll = ctk.CTkSwitch(bar, text="Auto-scroll", width=100)
        self.autoscroll.select()
        self.autoscroll.pack(side="left", padx=(12, 0))
        self.clear_btn = ctk.CTkButton(bar, text="Clear", width=70, command=self.clear)
        self.clear_btn.pack(side="right")
        self.count_label = ctk.CTkLabel(
            bar, text="0 frames", font=FONTS["small"], text_color=FG_DIM
        )
        self.count_label.pack(side="right", padx=8)

        middle = ctk.CTkFrame(self, fg_color="transparent")
        middle.pack(fill="both", expand=True, padx=10, pady=4)
        middle.grid_rowconfigure(0, weight=3)
        middle.grid_rowconfigure(1, weight=1)
        middle.grid_columnconfigure(0, weight=1)

        table_frame = ctk.CTkFrame(middle, fg_color="#14171d", corner_radius=8)
        table_frame.grid(row=0, column=0, sticky="nsew")
        self.tree = ttk.Treeview(
            table_frame,
            columns=[c[0] for c in COLUMNS],
            show="headings",
            selectmode="browse",
        )
        theme.style_treeview(self.tree)
        for key, label, width in COLUMNS:
            self.tree.heading(key, text=label)
            anchor = "e" if key in ("bytes", "snr") else "w"
            self.tree.column(key, width=width, anchor=anchor, stretch=key == "summary")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._open_modal)

        self.details = ctk.CTkTextbox(
            middle,
            font=FONTS["mono_small"],
            fg_color="#14171d",
            text_color=FG,
            wrap="word",
            state="disabled",
        )
        self.details.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

    # -- data ------------------------------------------------------------

    def add_row(self, row: PacketRow) -> None:
        self.rows.append(row)
        if self._visible(row):
            self._insert(row)
        if self.autoscroll.get():
            self._scroll_to_end()
        self._update_count()

    def _scroll_to_end(self) -> None:
        """Scroll to the newest row. Some Tk builds reject the 'end' pseudo-id,
        so name the real last item instead."""
        children = self.tree.get_children()
        if not children:
            return
        try:
            self.tree.see(children[-1])
        except tk.TclError:
            pass

    def _visible(self, row: PacketRow) -> bool:
        tf = self.type_filter.get()
        if tf != "All" and _group(row.kind) != tf:
            return False
        df = self.dir_filter.get()
        if df != "All" and row.direction != df:
            return False
        return True

    def _insert(self, row: PacketRow) -> None:
        self._counter += 1
        iid = f"r{self._counter}"
        tag = "history" if row.history else ("tx" if row.direction == TX else "rx")
        snr = f"{row.snr:.1f}" if row.snr is not None and row.snr == row.snr else "—"
        self.tree.insert(
            "",
            "end",
            iid=iid,
            values=(
                row.time_str(),
                row.direction,
                row.callsign or "—",
                row.mode or "—",
                row.size,
                snr,
                row.kind,
                row.summary,
            ),
            tags=(tag,),
        )
        self.by_iid[iid] = row

    def rebuild(self) -> None:
        selected = self._selected_row()
        self.tree.delete(*self.tree.get_children())
        self.by_iid.clear()
        for row in self.rows:
            if self._visible(row):
                self._insert(row)
        if selected is not None:
            for iid, r in self.by_iid.items():
                if r is selected:
                    self.tree.selection_set(iid)
                    break
        self._update_count()

    def clear(self) -> None:
        self.rows.clear()
        self.by_iid.clear()
        self.tree.delete(*self.tree.get_children())
        self._update_count()

    def _update_count(self) -> None:
        total = len(self.rows)
        shown = len(self.by_iid)
        text = f"{shown} of {total} frames"
        if total >= PACKET_TABLE_MAX_ROWS:
            text += " (capped)"
        self.count_label.configure(text=text)

    # -- details / modal ----------------------------------------------------

    def _selected_row(self) -> PacketRow | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.by_iid.get(sel[0])

    def _on_select(self, _event=None) -> None:
        row = self._selected_row()
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        if row is not None:
            self.details.insert("1.0", self._describe_short(row))
        self.details.configure(state="disabled")

    def _describe_short(self, row: PacketRow) -> str:
        head = (
            f"{row.time_str()}  {row.direction}  {row.kind}  {row.size} B"
            + ("  [history, no payload]" if row.history else "")
        )
        meta = json.dumps(row.meta, indent=1, default=str)
        payload = ""
        if row.payload is not None:
            payload = "\n\npayload (" + str(len(row.payload)) + " B):\n" + hexdump(row.payload, 128)
        return head + "\n" + meta + payload

    def _open_modal(self, _event=None) -> None:
        row = self._selected_row()
        if row is None:
            return
        top = theme.modal(self, f"Frame {row.time_str()}", 760, 560)
        outer = ctk.CTkFrame(top, fg_color=BG)
        outer.pack(fill="both", expand=True, padx=6, pady=6)
        text = ctk.CTkTextbox(
            outer, font=FONTS["mono_small"], fg_color="#14171d",
            text_color=FG, wrap="none",
        )
        text.pack(fill="both", expand=True)
        body = [
            f"Time: {row.time_str()}   Dir: {row.direction}   Type: {row.kind}",
            f"Size: {row.size} B   SNR: {row.snr if row.snr is not None else '—'}",
            f"Summary: {row.summary}",
            "\n--- metadata JSON ---",
            json.dumps(row.meta, indent=2, default=str),
        ]
        if row.payload is not None:
            body.append("\n--- protocol decode ---")
            body.append(describe_payload(row.payload))
            body.append("\n--- hex/ASCII ---")
            body.append(hexdump(row.payload, 2048))
        else:
            body.append("\n(no payload captured for this frame)")
        text.insert("1.0", "\n".join(body))


def describe_payload(payload: bytes) -> str:
    from .. import protocol
    from ..constants import KIND_AUDIO, KIND_CHAT, KIND_FILE_BLOCK

    try:
        env = protocol.decode_envelope(payload)
    except protocol.ProtocolError as exc:
        return f"M73G frame with unsupported version: {exc}"
    if env is None:
        return "Raw payload (not an M73G frame)"
    lines = [
        f"M73G v1 kind={env.kind:#02x} transfer_id={env.transfer_id} "
        f"seq={env.seq} body={len(env.body)} B"
    ]
    try:
        if env.kind == KIND_CHAT:
            part = protocol.decode_chat(env)
            lines.append(
                f"chat from {part.from_call}: part {part.index + 1}/{part.parts}: "
                f"{part.text!r}"
            )
        elif env.kind == KIND_FILE_BLOCK:
            fec, total, block = protocol.decode_file_block(env)
            lines.append(
                f"file_block {env.seq + 1}/{total}: fec=L{fec} {len(block)} B"
            )
        elif env.kind == KIND_AUDIO:
            body = protocol.decode_audio(env)
            ch = "stereo" if body.tier.channels == 2 else "mono"
            lines.append(
                f"audio from {body.callsign or '?'}: {body.tier.name} · "
                f"{body.tier.sample_rate // 1000} kHz {ch} · "
                f"{body.tier.bitrate // 1000} kbps · "
                f"{body.frame_count} frames × {body.frame_bytes} B @ {body.duration_ms} ms"
            )
    except protocol.ProtocolError as exc:
        lines.append(f"malformed body: {exc}")
    return "\n".join(lines)
