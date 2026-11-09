"""Shared dataclasses and app state snapshots."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .constants import BANDWIDTH_NAMES

# get_config reports frame_size numerically; the packet log wants the TUI's letter
_FRAME_LETTERS = {0: "S", 1: "N", 2: "L", 3: "M"}

TX = "TX"
RX = "RX"

INCOMING_ACTIVE = "active"
INCOMING_RECOVERING = "recovering"
INCOMING_COMPLETE = "complete"
INCOMING_FAILED = "failed"

TX_PENDING = "pending"
TX_SENDING = "sending"
TX_SENT = "sent (unconfirmed)"
TX_CANCELLED = "cancelled"
TX_FAILED = "failed"


@dataclass
class PacketRow:
    """One row in the Packet Log table."""

    time: float
    direction: str
    size: int
    kind: str = "raw"  # chat/file-block/raw
    summary: str = ""
    callsign: str = ""
    mode: str = ""
    snr: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    payload: bytes | None = None
    history: bool = False
    seq: int | None = None

    def time_str(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.time))


@dataclass
class OutgoingSnapshot:
    filename: str
    file_size: int
    fec: int = 2
    block_size: int = 0
    total_blocks: int = 0
    sent_blocks: int = 0
    sent_bytes: int = 0
    status: str = TX_PENDING
    message: str = ""

    @property
    def fraction(self) -> float:
        if self.total_blocks <= 0:
            return 1.0 if self.status == TX_SENT else 0.0
        return min(1.0, self.sent_blocks / self.total_blocks)


@dataclass
class IncomingSnapshot:
    transfer_id: int
    sender: str
    name: str = ""
    file_size: int = 0
    block_size: int = 0
    total_blocks: int = 0
    received_blocks: int = 0
    status: str = INCOMING_ACTIVE
    path: str = ""
    message: str = ""

    @property
    def fraction(self) -> float:
        if self.total_blocks <= 0:
            return 1.0 if self.status == INCOMING_COMPLETE else 0.0
        return min(1.0, self.received_blocks / self.total_blocks)


class AppState:
    """Mutable, Tk-thread-owned snapshot of everything the UI renders."""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self.status: dict[str, Any] = {}
        self.control_connected = False
        self.kiss_connected = False
        self.outgoing: OutgoingSnapshot | None = None
        self.incoming: dict[int, IncomingSnapshot] = {}
        self.tx_frames_queued = 0
        self.last_history_seq = 0

    @property
    def callsign(self) -> str:
        return self.config.get("callsign") or "?"

    @property
    def payload_size(self) -> int:
        return int(self.config.get("payload_size", 0) or 0)

    def mode_label(self) -> str:
        cfg = self.config
        mtype = cfg.get("modem_type")
        if mtype == 0:
            frame = cfg.get("frame_size", "normal")
            try:
                letter = _FRAME_LETTERS.get(int(frame), str(frame)[0].upper())
            except (TypeError, ValueError):
                letter = str(frame)[0].upper()
            label = (
                f"{cfg.get('modulation', '?')} {cfg.get('code_rate', '')} "
                f"{letter}"
            )
            try:
                bw = int(cfg.get("bandwidth") or 0)
            except (TypeError, ValueError):
                bw = 0
            if bw > 0:
                label += f" {BANDWIDTH_NAMES.get(bw, f'{bw}k')}"
                try:
                    if int(cfg.get("blocks") or 1) > 1:
                        label += f" x{int(cfg['blocks'])}"
                except (TypeError, ValueError):
                    pass
            return label
        if mtype == 1:
            return str(cfg.get("modulation") or cfg.get("mfsk_mode", "?"))
        if mtype == 2:
            return str(cfg.get("modulation", f"RDM-{cfg.get('robust_mode', '?')}"))
        return "—"

    def bandwidth_label(self) -> str:
        """Running OFDM family name ("base"/"4k"/"8k"/"16k"); empty until the
        config loads or when the station runs MFSK/ROBUST."""
        cfg = self.config
        if not cfg:
            return ""
        try:
            if int(cfg.get("modem_type", -1)) != 0:
                return ""
        except (TypeError, ValueError):
            return ""
        try:
            bw = int(cfg.get("bandwidth") or 0)
        except (TypeError, ValueError):
            bw = 0
        return BANDWIDTH_NAMES.get(bw, f"{bw}k")

    def channel_state(self) -> str:
        return str(self.status.get("channel_state", "unknown"))
