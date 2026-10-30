"""Ports, protocol constants, modem tables, and colors."""

from __future__ import annotations

DEFAULT_HOST = "127.0.0.1"
DEFAULT_CONTROL_PORT = 8073
DEFAULT_KISS_PORT = 8001

# --- KISS framing (mirrors modem73's parser) ---
FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD
KISS_CMD_DATA = 0x00
KISS_OVERHEAD = 2  # command byte + trailing slack; modem73 caps KISS writes at payload_size - 2

STATUS_POLL_INTERVAL_S = 0.5
EVENT_PUMP_INTERVAL_MS = 50

# --- M73G application protocol ---
M73G_MAGIC = b"M73G"
M73G_VERSION = 0x01
HEADER_LEN = 14  # magic(4) version(1) kind(1) transfer_id(4) seq(4)

KIND_CHAT = 0x01
KIND_FILE_BLOCK = 0x02  # rsfec-protected file block (fec | total | block)

KIND_NAMES = {
    KIND_CHAT: "chat",
    KIND_FILE_BLOCK: "file-block",
}

FILE_NAME_MAX_BYTES = 64
CHAT_FROM_MAX_LEN = 16
FILE_MAX_BYTES = 1024 * 1024  # whole file is RS-packed in memory: 1 MiB hard cap
FILE_MAX_BLOCKS = 65_535  # per-stream sanity cap (data + parity blocks)

# TX drain wait (file end, queue settling)
TX_DRAIN_TIMEOUT_S = 60.0

# Receiver behavior
INCOMING_IDLE_TIMEOUT_S = 10.0
CHAT_PARTIAL_REVEAL_S = 10.0

# File sender pacing
PACING_POLL_S = 0.15
# keep at most one extra frame queued beyond the one being sent: deep queues
# make cancel take minutes and hog the channel
PACING_HIGH_WATER = 2
PACING_LOW_WATER = 1

# UI caps
PACKET_TABLE_MAX_ROWS = 1000
CHAT_TRANSCRIPT_MAX_LINES = 500
HISTORY_POLL_LIMIT = 100
SPARKLINE_SAMPLES = 120  # 60 s at 500 ms

DOWNLOAD_DIR_NAME = "m73gui"  # under ~/Downloads

# Extensions treated as ordinary text/media/document content; anything else
# gets a caution when sending or receiving.
SAFE_FILE_EXTS = {
    # text
    "txt", "md", "csv", "log", "json", "xml", "yaml", "yml", "ini", "cfg",
    # images
    "png", "jpg", "jpeg", "jxl", "jpxl", "gif", "bmp", "webp", "tif", "tiff",
    # audio
    "wav", "mp3", "flac", "ogg", "opus", "m4a",
    # documents and modem73-ecosystem content
    "pdf", "epub", "s7s", "mmpz", "mid", "midi",
}

# --- Modem tables (match modem73 control port numbering) ---
MODEM_TYPE_NAMES = {0: "OFDM", 1: "MFSK", 2: "ROBUST"}

OFDM_MODULATIONS = [
    "BPSK", "QPSK", "8PSK", "QAM16", "QAM64", "QAM256", "QAM1024", "QAM4096",
]
CODE_RATES = ["1/2", "2/3", "3/4", "5/6", "1/4", "1/2x2", "1/4x2"]
FRAME_SIZE_NAMES = {0: "short", 1: "normal", 2: "long"}
FRAME_SIZE_INDEX = {"short": 0, "normal": 1, "long": 2, "micro": 3}

MFSK_MODE_NAMES = ["MFSK-8", "MFSK-16", "MFSK-32", "MFSK-32R"]

ROBUST_MODE_NAMES = [
    "RDM-1200", "RDM-600", "RDM-300", "RDMN-300", "RDMN-150",
    "RDM-1200S", "RDM-600S", "RDM-300S", "RDMN-300S", "RDMN-150S",
    "RDM-800", "RDM-800S",
]
# Modes 0-4 and 10 carry 512-byte frames (510 B MTU); 5-9 and 11 carry 172-byte (170 B MTU).
ROBUST_MODE_MTU = {i: (510 if i in (0, 1, 2, 3, 4, 10) else 170) for i in range(12)}

# --- Colors ---
COL_IDLE = "#3fb950"
COL_TX = "#f85149"
COL_RX = "#58a6ff"
COL_WARN = "#d29922"
COL_OFF = "#8b949e"
COL_ACCENT = "#58a6ff"
