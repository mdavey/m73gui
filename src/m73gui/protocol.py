"""M73G application protocol: envelope, chat and file encode/decode.

Compact little-endian binary envelope designed to fit inside the smallest
modem73 payload (170 B RDM frames):

    magic 4s | version u8 | kind u8 | transfer_id u32 | seq u32 | body

Common header is 14 bytes.
"""

from __future__ import annotations

import re
import secrets
import struct
import zlib
from dataclasses import dataclass, field

from .constants import (
    CHAT_FROM_MAX_LEN,
    CODE_RATES,
    FILE_NAME_MAX_BYTES,
    FILE_START_FIXED_TAIL,
    FRAME_SIZE_INDEX,
    HEADER_LEN,
    KISS_OVERHEAD,
    KIND_CHAT,
    KIND_FILE_CHUNK,
    KIND_FILE_END,
    KIND_FILE_START,
    M73G_MAGIC,
    M73G_VERSION,
    OFDM_MODULATIONS,
    ROBUST_MODE_MTU,
)

assert FILE_START_FIXED_TAIL + HEADER_LEN + FILE_NAME_MAX_BYTES == 135


class ProtocolError(ValueError):
    pass


def new_transfer_id() -> int:
    while True:
        tid = secrets.randbits(32)
        if tid:
            return tid


# --------------------------------------------------------------------------
# Envelope

@dataclass
class Envelope:
    kind: int
    transfer_id: int
    seq: int
    body: bytes


def encode_envelope(kind: int, transfer_id: int, seq: int, body: bytes) -> bytes:
    return (
        M73G_MAGIC
        + struct.pack("<BBII", M73G_VERSION, kind, transfer_id, seq)
        + body
    )


def decode_envelope(data: bytes) -> Envelope | None:
    """Return the envelope, or None when the payload is not an M73G v1 frame.

    Short/magic-mismatched data decodes as None (raw payload). A frame with a
    known magic but unknown version raises ProtocolError.
    """
    if len(data) < HEADER_LEN or not data.startswith(M73G_MAGIC):
        return None
    version = data[4]
    if version != M73G_VERSION:
        raise ProtocolError(f"unsupported M73G version {version}")
    kind = data[5]
    transfer_id, seq = struct.unpack_from("<II", data, 6)
    return Envelope(kind=kind, transfer_id=transfer_id, seq=seq, body=data[HEADER_LEN:])


# --------------------------------------------------------------------------
# Chat
#
# body: from_len u8 | from bytes | parts u16 | text (UTF-8 fragment)

def chat_max_text_bytes(payload_size: int, from_len: int) -> int:
    return payload_size - KISS_OVERHEAD - HEADER_LEN - 1 - from_len - 2


def split_text_utf8(text: str, max_bytes: int) -> list[str]:
    """Split text into fragments of at most max_bytes UTF-8 bytes, on
    code-point boundaries."""
    if max_bytes < 1:
        raise ProtocolError("chat part budget too small")
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for ch in text:
        ch_size = len(ch.encode("utf-8"))
        if size + ch_size > max_bytes:
            parts.append("".join(current))
            current = []
            size = 0
        current.append(ch)
        size += ch_size
    if current or not parts:
        parts.append("".join(current))
    return parts


def make_chat_frames(
    from_call: str, text: str, transfer_id: int, payload_size: int
) -> list[bytes]:
    from_bytes = from_call.encode("ascii", "ignore")[:CHAT_FROM_MAX_LEN]
    max_text = chat_max_text_bytes(payload_size, len(from_bytes))
    parts = split_text_utf8(text, max_text)
    total = len(parts)
    frames = []
    for index, part in enumerate(parts):
        body = (
            bytes([len(from_bytes)])
            + from_bytes
            + struct.pack("<H", total)
            + part.encode("utf-8")
        )
        frames.append(encode_envelope(KIND_CHAT, transfer_id, index, body))
    return frames


@dataclass
class ChatPart:
    from_call: str
    parts: int
    index: int
    text: str
    transfer_id: int


def decode_chat(env: Envelope) -> ChatPart:
    body = env.body
    if len(body) < 1:
        raise ProtocolError("chat body too short")
    from_len = body[0]
    if from_len < 1 or from_len > CHAT_FROM_MAX_LEN:
        raise ProtocolError("bad chat from_len")
    if len(body) < 1 + from_len + 2:
        raise ProtocolError("chat body truncated")
    from_call = body[1 : 1 + from_len].decode("ascii", "replace")
    (parts,) = struct.unpack_from("<H", body, 1 + from_len)
    if parts < 1:
        raise ProtocolError("chat parts == 0")
    text = body[1 + from_len + 2 :].decode("utf-8", "replace")
    return ChatPart(from_call, parts, env.seq, text, env.transfer_id)


class ChatReassembler:
    """Reassemble multi-part chat messages keyed by (from, transfer_id)."""

    def __init__(self, reveal_after_s: float, now_fn=lambda: 0.0):
        self._messages: dict[tuple[str, int], dict[int, tuple[str, float]]] = {}
        self._revealed: set[tuple[str, int]] = set()
        self._reveal_after_s = reveal_after_s
        self._now = now_fn

    def add(self, part: ChatPart) -> tuple[str, bool] | None:
        """Return (text, complete) when there is something to display."""
        key = (part.from_call, part.transfer_id)
        if part.index >= part.parts:
            raise ProtocolError("chat part index out of range")
        msg = self._messages.setdefault(key, {})
        if part.index in msg:
            return None  # duplicate
        msg[part.index] = (part.text, self._now())
        if len(msg) == part.parts:
            del self._messages[key]
            text = "".join(msg[i][0] for i in range(part.parts))
            return (f"{part.from_call}: {text}", True)
        return None

    def sweep(self) -> list[str]:
        """Reveal stale partial messages once, after the reveal delay."""
        now = self._now()
        out = []
        for key, msg in list(self._messages.items()):
            if key in self._revealed:
                continue
            oldest = min(t for _, t in msg.values())
            if now - oldest >= self._reveal_after_s:
                self._revealed.add(key)
                text = "".join(
                    txt for idx, (txt, _) in sorted(msg.items())
                )
                out.append(f"{key[0]}: {text} (incomplete)")
        return out

    def drop_revealed(self) -> None:
        for key in self._revealed:
            self._messages.pop(key, None)
        self._revealed.clear()


# --------------------------------------------------------------------------
# FILE_START
#
# body: file_size u64 | chunk_size u32 | total_chunks u32 | name_len u8 |
#       name bytes | sha256 32B | modem_type u8 | modulation u8 | code_rate u8 |
#       frame_size u8 | mfsk_mode u8 | robust_mode u8 | flags u8 | reserved u8

@dataclass
class FileStart:
    file_size: int
    chunk_size: int
    total_chunks: int
    name: str
    sha256: bytes
    modem_type: int
    modulation: int
    code_rate: int
    frame_size: int
    mfsk_mode: int
    robust_mode: int
    flags: int
    transfer_id: int = 0

    def target_mtu(self) -> int:
        if self.modem_type == 2:
            return ROBUST_MODE_MTU.get(self.robust_mode, 170)
        return 6144  # OFDM/MFSK large modes; validated against receiver config


def _idx(table: list[str], value: str | None) -> int:
    try:
        return table.index(value)
    except ValueError:
        return 0


def encode_file_start(
    transfer_id: int,
    file_size: int,
    chunk_size: int,
    total_chunks: int,
    name: str,
    sha256: bytes,
    settings: dict,
    flags: int,
) -> bytes:
    name_bytes = sanitize_filename(name).encode("utf-8")[:FILE_NAME_MAX_BYTES]
    if len(sha256) != 32:
        raise ProtocolError("sha256 must be 32 bytes")
    body = struct.pack(
        "<QIIB", file_size, chunk_size, total_chunks, len(name_bytes)
    )
    body += name_bytes + sha256
    body += struct.pack(
        "<8B",
        int(settings.get("modem_type", 0)),
        _idx(OFDM_MODULATIONS, settings.get("modulation")),
        _idx(CODE_RATES, settings.get("code_rate")),
        FRAME_SIZE_INDEX.get(str(settings.get("frame_size", "normal")), 1),
        int(settings.get("mfsk_mode", 0)),
        int(settings.get("robust_mode", 0)),
        flags,
        0,
    )
    return encode_envelope(KIND_FILE_START, transfer_id, 0, body)


def file_start_max_size() -> int:
    return HEADER_LEN + FILE_START_FIXED_TAIL + FILE_NAME_MAX_BYTES


def decode_file_start(env: Envelope) -> FileStart:
    body = env.body
    tail = 32 + 8
    if len(body) < 8 + 4 + 4 + 1 + tail:
        raise ProtocolError("file_start too short")
    file_size, chunk_size, total_chunks, name_len = struct.unpack_from("<QIIB", body, 0)
    if name_len == 0:
        raise ProtocolError("empty file name")
    expected = 17 + name_len + tail
    if len(body) != expected:
        raise ProtocolError(
            f"file_start length {len(body)} != expected {expected}"
        )
    offset = 17
    name = body[offset : offset + name_len].decode("utf-8", "replace")
    offset += name_len
    sha256 = body[offset : offset + 32]
    offset += 32
    fields = struct.unpack_from("<8B", body, offset)
    return FileStart(
        file_size=file_size,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        name=name,
        sha256=sha256,
        modem_type=fields[0],
        modulation=fields[1],
        code_rate=fields[2],
        frame_size=fields[3],
        mfsk_mode=fields[4],
        robust_mode=fields[5],
        flags=fields[6],
        transfer_id=env.transfer_id,
    )


# --------------------------------------------------------------------------
# FILE_CHUNK: crc32 u32 LE | data

def encode_file_chunk(transfer_id: int, index: int, data: bytes) -> bytes:
    body = struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF) + data
    return encode_envelope(KIND_FILE_CHUNK, transfer_id, index, body)


def decode_file_chunk(env: Envelope) -> tuple[bytes, bool]:
    if len(env.body) < 4:
        raise ProtocolError("file_chunk too short")
    (crc,) = struct.unpack_from("<I", env.body, 0)
    data = env.body[4:]
    return data, (zlib.crc32(data) & 0xFFFFFFFF) == crc


# --------------------------------------------------------------------------
# FILE_END: total_chunks u32 | sha256 32B

def encode_file_end(transfer_id: int, total_chunks: int, sha256: bytes) -> bytes:
    if len(sha256) != 32:
        raise ProtocolError("sha256 must be 32 bytes")
    return encode_envelope(
        KIND_FILE_END, transfer_id, 0, struct.pack("<I", total_chunks) + sha256
    )


def decode_file_end(env: Envelope) -> tuple[int, bytes]:
    if len(env.body) != 4 + 32:
        raise ProtocolError("file_end wrong length")
    (total,) = struct.unpack_from("<I", env.body, 0)
    return total, env.body[4:]


# --------------------------------------------------------------------------
# Sizing / sanitization

def chunk_size_for(payload_size: int) -> int:
    """Usable file data bytes per KISS frame: payload_size - 20."""
    size = payload_size - KISS_OVERHEAD - HEADER_LEN - 4
    if size < 1:
        raise ProtocolError(f"payload_size {payload_size} too small")
    return size


_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_filename(name: str) -> str:
    """Basename only, no control chars, no leading dots, max 64 UTF-8 bytes."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_CHARS.sub("", name)
    name = name.lstrip(".")
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return "file.bin"
    encoded = name.encode("utf-8")
    if len(encoded) > FILE_NAME_MAX_BYTES:
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        budget = FILE_NAME_MAX_BYTES - len(("..." + ext).encode("utf-8"))
        keep = ""
        for ch in stem:
            if len((keep + ch).encode("utf-8")) > max(budget, 0):
                break
            keep += ch
        name = keep + "..." + ext
        if len(name.encode("utf-8")) > FILE_NAME_MAX_BYTES:
            name = name.encode("utf-8")[:FILE_NAME_MAX_BYTES].decode(
                "utf-8", "ignore"
            )
    return name


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"  # unreachable
