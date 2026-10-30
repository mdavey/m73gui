"""Chat tab: broadcast transcript and composer."""

from __future__ import annotations

import customtkinter as ctk

from ..constants import CHAT_TRANSCRIPT_MAX_LINES
from .theme import BG, FG, FG_DIM, FONTS


class ChatTab(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color=BG)
        self.app = app
        self._lines = 0

        self.transcript = ctk.CTkTextbox(
            self, font=FONTS["mono"], wrap="word", fg_color="#14171d",
            text_color=FG, state="disabled",
        )
        self.transcript.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(0, 10))
        self.hint = ctk.CTkLabel(
            row, text="broadcast, no addressing", font=FONTS["small"],
            text_color=FG_DIM,
        )
        self.hint.pack(side="left")
        self.part_info = ctk.CTkLabel(row, text="", font=FONTS["small"], text_color=FG_DIM)
        self.part_info.pack(side="left", padx=10)
        self.clear_btn = ctk.CTkButton(row, text="Clear", width=70, command=self.clear)
        self.clear_btn.pack(side="right")
        self.send_btn = ctk.CTkButton(row, text="Send", width=90, command=self._send)
        self.send_btn.pack(side="right", padx=(0, 6))
        self.entry = ctk.CTkEntry(row, placeholder_text="Type a message…")
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.entry.bind("<Return>", lambda e: self._send())

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.app.send_chat(text)

    def add_line(self, line: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", line + "\n")
        self._lines += 1
        if self._lines > CHAT_TRANSCRIPT_MAX_LINES:
            excess = self._lines - CHAT_TRANSCRIPT_MAX_LINES
            self.transcript.delete("1.0", f"{excess + 1}.0")
            self._lines = CHAT_TRANSCRIPT_MAX_LINES
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def set_part_info(self, text: str) -> None:
        self.part_info.configure(text=text)

    def clear(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._lines = 0

    def set_connected(self, connected: bool) -> None:
        state = "normal" if connected else "disabled"
        self.send_btn.configure(state=state)
        self.entry.configure(state=state)
        if connected:
            self.entry.focus_set()
