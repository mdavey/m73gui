"""Files tab: outgoing broadcast transfers and incoming reassembly list."""

from __future__ import annotations

import os
import subprocess

import customtkinter as ctk

from .. import protocol
from ..constants import COL_WARN
from ..state import TX_CANCELLED, TX_FAILED, TX_SENT
from . import theme
from .theme import BG, FG, FG_DIM, FONTS
from .widgets import Card


class FilesTab(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color=BG)
        self.app = app
        self.selected_path: str | None = None

        outer = ctk.CTkScrollableFrame(self, fg_color=BG)
        outer.pack(fill="both", expand=True, padx=6, pady=6)

        # ---- outgoing ----
        out_card = Card(outer, "Outgoing broadcast")
        out_card.pack(fill="x", pady=4)
        row1 = ctk.CTkFrame(out_card, fg_color="transparent")
        row1.pack(fill="x", padx=12, pady=(6, 2))
        self.pick_btn = ctk.CTkButton(row1, text="Choose file…", width=120,
                                      command=self._pick)
        self.pick_btn.pack(side="left")
        self.file_label = ctk.CTkLabel(
            row1, text="no file selected", font=FONTS["small"], text_color=FG_DIM
        )
        self.file_label.pack(side="left", padx=10)

        ctk.CTkLabel(
            out_card,
            text="Best-effort broadcast: no delivery confirmation. Large files take "
            "significant airtime.",
            font=FONTS["small"],
            text_color=COL_WARN,
            anchor="w",
        ).pack(fill="x", padx=12)

        row2 = ctk.CTkFrame(out_card, fg_color="transparent")
        row2.pack(fill="x", padx=12, pady=(2, 10))
        self.send_btn = ctk.CTkButton(row2, text="Send", width=90,
                                      command=self._send)
        self.send_btn.pack(side="left")
        self.cancel_btn = ctk.CTkButton(row2, text="Cancel", width=90,
                                        fg_color="#6e3b3b", command=self._cancel)
        self.cancel_btn.pack(side="left", padx=6)
        self.progress = ctk.CTkProgressBar(row2)
        self.progress.set(0)
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.tx_status = ctk.CTkLabel(out_card, text="", font=FONTS["small"],
                                      text_color=FG_DIM, anchor="w")
        self.tx_status.pack(fill="x", padx=12, pady=(0, 8))

        # ---- incoming ----
        in_card = Card(outer, "Incoming transfers")
        in_card.pack(fill="both", expand=True, pady=4)
        from tkinter import ttk

        tree = ttk.Treeview(
            in_card,
            columns=("sender", "name", "progress", "chunks", "status", "missing",
                     "path"),
            show="headings",
            height=7,
        )
        theme.style_treeview(tree)
        spec = [
            ("sender", "Sender", 80),
            ("name", "File", 180),
            ("progress", "Progress", 90),
            ("chunks", "Chunks", 100),
            ("status", "Status", 110),
            ("missing", "Missing", 70),
            ("path", "Saved to", 300),
        ]
        for key, label, width in spec:
            tree.heading(key, text=label)
            tree.column(key, width=width)
        tree.pack(fill="both", expand=True, padx=12, pady=6)
        self.tree = tree

        brow = ctk.CTkFrame(in_card, fg_color="transparent")
        brow.pack(fill="x", padx=12, pady=(0, 10))
        self.open_btn = ctk.CTkButton(brow, text="Open folder", width=110,
                                      command=self._open_folder)
        self.open_btn.pack(side="left")

        # ---- settings ----
        set_card = Card(outer, "File settings")
        set_card.pack(fill="x", pady=4)
        grow = ctk.CTkFrame(set_card, fg_color="transparent")
        grow.pack(fill="x", padx=12, pady=8)
        self.accept_self = ctk.CTkCheckBox(
            grow, text="Accept my own echoed transfers (loopback test)"
        )
        self.accept_self.pack(side="left")
        self.dir_label = ctk.CTkLabel(
            set_card,
            text=f"downloads: {app.downloads_dir}",
            font=FONTS["mono_small"],
            text_color=FG_DIM,
            anchor="w",
        )
        self.dir_label.pack(fill="x", padx=12, pady=(0, 8))

    # -- outgoing -----------------------------------------------------------

    def _pick(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(title="Choose file to broadcast")
        if not path:
            return
        self.selected_path = path
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            self.file_label.configure(text=f"error: {exc}")
            return
        chunk = self.app.chunk_size()
        text = f"{os.path.basename(path)}  ({protocol.human_bytes(size)})"
        if chunk:
            chunks = (size + chunk - 1) // chunk if size else 0
            text += f"  ·  {chunks} chunks × {chunk} B"
        self.file_label.configure(text=text)

    def clear_selection(self) -> None:
        """Forget the picked file and its chunk math (e.g. after a mode change
        altered the frame size)."""
        self.selected_path = None
        self.file_label.configure(text="no file selected")
        self.progress.set(0)
        self.tx_status.configure(text="", text_color=FG_DIM)

    def _send(self) -> None:
        if not self.selected_path:
            self.app.notify("Pick a file first", level="warn")
            return
        err = self.app.send_file(self.selected_path)
        if err:
            self.app.notify(err, level="error")

    def _cancel(self) -> None:
        self.app.cancel_file()

    def update_outgoing(self, snap) -> None:
        if snap is None:
            return
        self.progress.set(snap.fraction)
        text = (
            f"{snap.filename}: {snap.sent_chunks}/{snap.total_chunks or 0} chunks "
            f"({protocol.human_bytes(snap.sent_bytes)} queued) — {snap.message}"
        )
        color = FG_DIM
        if snap.status == TX_SENT:
            color = "#3fb950"
        elif snap.status in (TX_FAILED, TX_CANCELLED):
            color = COL_WARN
        self.tx_status.configure(text=text, text_color=color)
        busy = snap.status not in (TX_SENT, TX_FAILED, TX_CANCELLED)
        self.send_btn.configure(state="disabled" if busy else "normal")
        self.pick_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def set_transfer_blocked(self, blocked: bool) -> None:
        state = "disabled" if blocked else "normal"
        self.send_btn.configure(state=state)

    # -- incoming -----------------------------------------------------------

    def update_incoming(self, snap) -> None:
        iid = f"t{snap.transfer_id}"
        pct = f"{snap.fraction * 100:.0f}%"
        values = (
            snap.sender or "?",
            snap.name,
            pct,
            f"{snap.received_chunks}/{snap.total_chunks}",
            snap.status,
            snap.missing_chunks,
            snap.path or snap.message,
        )
        if self.tree.exists(iid):
            self.tree.item(iid, values=values)
        else:
            self.tree.insert("", 0, iid=iid, values=values)

    def _open_folder(self) -> None:
        path = self.app.downloads_dir
        os.makedirs(path, exist_ok=True)
        try:
            subprocess.Popen(
                ["xdg-open", path], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.app.notify(f"cannot open folder: {exc}", level="error")
