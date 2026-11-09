"""M73G application protocol: envelope, chat and rsfec file-block codec.

Compact little-endian binary envelope designed to fit inside the smallest
modem73 payload (170 B RDM frames):

    magic 4s | version u8 | kind u8 | transfer_id u32 | seq u32 | body

Common header is 14 bytes.
"""

from __future__ import annotations

import re
import secrets
import struct
from dataclasses import dataclass

from opuslink import framing as oframing

from .constants import (
    CHAT_FROM_MAX_LEN,
    FILE_NAME_MAX_BYTES,
    HEADER_LEN,
    KISS_OVERHEAD,
    KIND_CHAT,
    KIND_FILE_BLOCK,
    KIND_AUDIO,
    M73G_MAGIC,
    M73G_VERSION,
    SAFE_FILE_EXTS,
)


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
# FILE_BLOCK: fec_level u8 | total_packets u32 | block bytes[B]
#
# A file transfer is just a stream of these: block i of the packed rsfec
# container has envelope seq == i, and every packet repeats the FEC level and
# the packet total so any single frame bootstraps a receiver. No start/end
# frames exist.

FILE_BLOCK_BODY = struct.Struct("<BI")


def encode_file_block(
    transfer_id: int, seq: int, fec: int, total: int, block: bytes
) -> bytes:
    if fec not in (0, 1, 2, 3, 4):
        raise ProtocolError(f"invalid FEC level {fec!r} (expected 0..4)")
    if not 1 <= total <= 0xFFFFFFFF:
        raise ProtocolError("total_packets out of range")
    if not block:
        raise ProtocolError("empty file block")
    return encode_envelope(
        KIND_FILE_BLOCK, transfer_id, seq, FILE_BLOCK_BODY.pack(fec, total) + block
    )


def decode_file_block(env: Envelope) -> tuple[int, int, bytes]:
    """Return (fec_level, total_packets, block) for a FILE_BLOCK envelope."""
    if len(env.body) <= FILE_BLOCK_BODY.size:
        raise ProtocolError("file_block too short")
    fec, total = FILE_BLOCK_BODY.unpack_from(env.body, 0)
    if fec not in (0, 1, 2, 3, 4):
        raise ProtocolError(f"bad FEC level {fec} in file_block")
    if total < 1:
        raise ProtocolError("file_block total_packets == 0")
    return fec, total, env.body[FILE_BLOCK_BODY.size :]


# --------------------------------------------------------------------------
# AUDIO: an opuslink body (settings | bitrate | frame_bytes | frame_count |
# callsign | fixed-size CBR frames).
#
# The envelope transfer_id is the broadcast session id — a new session makes
# receivers reset their decoder — and seq is a strictly increasing packet
# counter used for gap/stale detection. Every packet is self-describing, so
# any single frame bootstraps a receiver (same trick as file blocks).

def encode_audio(
    transfer_id: int, seq: int, tier, duration_ms: int, callsign: str,
    frames,
) -> bytes:
    try:
        body = oframing.pack_body(tier, duration_ms, callsign, frames)
    except oframing.FramingError as exc:
        raise ProtocolError(str(exc)) from None
    return encode_envelope(KIND_AUDIO, transfer_id, seq, body)


def decode_audio(env: Envelope) -> oframing.AudioBody:
    """Return the parsed opuslink body of an AUDIO envelope."""
    try:
        return oframing.unpack_body(env.body)
    except oframing.FramingError as exc:
        raise ProtocolError(str(exc)) from None


# --------------------------------------------------------------------------
# Sizing / sanitization

def audio_budget_for(payload_size: int, callsign_len: int) -> int:
    """Opus-frame bytes available per audio packet (may go small; the
    opuslink planner decides whether anything fits)."""
    return (
        payload_size
        - KISS_OVERHEAD
        - HEADER_LEN
        - oframing.HEADER_LEN
        - callsign_len
    )


def block_size_for(payload_size: int) -> int:
    """Usable rsfec block bytes per KISS frame: payload_size - 21."""
    size = payload_size - KISS_OVERHEAD - HEADER_LEN - FILE_BLOCK_BODY.size
    if size < 32:
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


def file_type_extension(name: str) -> str | None:
    """Return an unusual extension (one outside SAFE_FILE_EXTS), or None.

    A missing extension counts as unusual ("(no extension)")."""
    if "." not in name:
        return "(no extension)"
    ext = name.rpartition(".")[2].lower()
    if ext in SAFE_FILE_EXTS:
        return None
    return f".{ext}"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"  # unreachable
