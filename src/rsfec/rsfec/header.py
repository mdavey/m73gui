"""Container header: build and parse the FEC-protected metadata at payload offset 0.

Big-endian layout::

    magic        4B   b"RSF1"
    version      1B   = 1
    fec_level    1B   1..4
    block_size   2B
    data_blocks  4B
    total_blocks 4B
    name_len     2B
    name         name_len bytes, UTF-8 (basename only)
    file_size    8B
    sha1         20B  (of the file bytes only, not header or padding)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import HeaderError

__all__ = [
    "MAGIC",
    "VERSION",
    "SHA1_LEN",
    "build_header",
    "parse_header",
    "sanitize_name",
    "header_length",
]

MAGIC = b"RSF1"
VERSION = 1
SHA1_LEN = 20

_FIXED1 = struct.Struct(">4sBBHIQH")  # magic..name_len, name follows
_FIXED2 = struct.Struct(">Q")         # file_size, sha1 follows


def header_length(name: str) -> int:
    """Total header size in bytes for a given filename (fixed fields + name)."""
    return _FIXED1.size + len(name.encode("utf-8")) + _FIXED2.size + SHA1_LEN


def sanitize_name(name: str) -> str:
    """Reduce a path or name to a bare, NUL-free filename."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = name.replace("\x00", "").strip()
    return name


def build_header(
    *,
    fec: int,
    block_size: int,
    data_blocks: int,
    total_blocks: int,
    name: str,
    file_size: int,
    sha1_digest: bytes,
) -> bytes:
    if len(sha1_digest) != SHA1_LEN:
        raise ValueError(f"sha1_digest must be {SHA1_LEN} bytes")
    name_bytes = name.encode("utf-8")
    if len(name_bytes) > 0xFFFF:
        raise ValueError("filename too long (max 65535 UTF-8 bytes)")
    return b"".join(
        (
            _FIXED1.pack(
                MAGIC, VERSION, fec, block_size,
                data_blocks, total_blocks, len(name_bytes),
            ),
            name_bytes,
            _FIXED2.pack(file_size),
            sha1_digest,
        )
    )


@dataclass(frozen=True)
class Header:
    fec: int
    block_size: int
    data_blocks: int
    total_blocks: int
    name: str
    file_size: int
    sha1_digest: bytes


def parse_header(payload: bytes) -> Header:
    """Parse a header from the start of an assembled payload."""
    if len(payload) < _FIXED1.size:
        raise HeaderError("stream too short for a header")
    try:
        magic, version, fec, block_size, data_blocks, total_blocks, name_len = (
            _FIXED1.unpack_from(payload)
        )
    except struct.error as exc:  # pragma: no cover - defensive
        raise HeaderError(f"malformed header: {exc}") from None
    if magic != MAGIC:
        raise HeaderError("not an rsfec stream (bad magic)")
    if version != VERSION:
        raise HeaderError(f"unsupported rsfec version {version}")
    if not 0 <= fec <= 4:
        raise HeaderError(f"invalid fec level {fec} in header")
    tail_at = _FIXED1.size + name_len + _FIXED2.size
    if len(payload) < tail_at + SHA1_LEN:
        raise HeaderError("header truncated")
    name_bytes = payload[_FIXED1.size : _FIXED1.size + name_len]
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HeaderError("filename is not valid UTF-8") from None
    file_size = _FIXED2.unpack_from(payload, _FIXED1.size + name_len)[0]
    sha1_digest = payload[tail_at : tail_at + SHA1_LEN]
    return Header(
        fec=fec,
        block_size=block_size,
        data_blocks=data_blocks,
        total_blocks=total_blocks,
        name=name,
        file_size=file_size,
        sha1_digest=sha1_digest,
    )
