"""Pack/unpack: whole-file Reed-Solomon FEC with block interleaving.

The packed *stream* is a flat byte string, exactly ``total_blocks *
block_size`` long, ready to be sliced into fixed-size transport payloads.
RS codewords run column-wise across the blocks of each group, so any
``parity_blocks`` blocks of a group may be missing; the receiver presents
them as all-zero blocks (optionally declaring them via ``missing=``).
Corrupted-but-present blocks are repaired too, as unrecognised symbol errors:
a block costs 1 of a group's parity budget when declared missing and 2 when
its corruption must be located by the decoder. Every result is verified
against the header's SHA-1, so a failed recovery never passes silently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import reedsolo

from . import _codec, geometry
from .errors import CorruptedError, HeaderError
from .header import (
    SHA1_LEN,
    _FIXED1,
    _FIXED2,
    build_header,
    parse_header,
    sanitize_name,
)

__all__ = [
    "PackResult",
    "Unpacked",
    "pack",
    "pack_bytes",
    "unpack",
    "missing_blocks",
]


@dataclass(frozen=True)
class PackResult:
    stream: bytes
    total_blocks: int
    data_blocks: int
    parity_blocks: int
    block_size: int
    fec: int


@dataclass(frozen=True)
class Unpacked:
    name: str
    data: bytes
    sha1_hex: str

    @property
    def size(self) -> int:
        return len(self.data)

    def save(self, dest_dir) -> Path:
        """Write the file into dest_dir (existing names are overwritten)."""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / (sanitize_name(self.name) or "unnamed")
        path.write_bytes(self.data)
        return path


def _validate_args(block_size: int, fec: int) -> int:
    if not isinstance(block_size, int) or not 1 <= block_size <= 0xFFFF:
        raise ValueError("block_size must be an integer between 1 and 65535")
    if fec not in (1, 2, 3, 4):
        raise ValueError(f"invalid FEC level {fec!r} (expected 1..4)")
    return int(fec)


def _header_length(name: str) -> int:
    return _FIXED1.size + len(name.encode("utf-8")) + _FIXED2.size + SHA1_LEN


def pack(path, *, block_size: int, fec: int) -> PackResult:
    """Pack a file from disk; the stored name is the path's basename."""
    p = Path(path)
    return pack_bytes(
        p.read_bytes(), name=p.name, block_size=block_size, fec=fec
    )


def pack_bytes(data: bytes, *, name: str, block_size: int, fec: int) -> PackResult:
    level = _validate_args(block_size, fec)
    name = sanitize_name(name)
    digest = hashlib.sha1(data).digest()

    # Header length depends only on the name, so it can precede the layout:
    data_blocks = -(-(_header_length(name) + len(data)) // block_size)
    groups = geometry.encode_groups(level, data_blocks)
    total_blocks = sum(g.total_blocks for g in groups)

    header = build_header(
        fec=level, block_size=block_size, data_blocks=data_blocks,
        total_blocks=total_blocks, name=name, file_size=len(data),
        sha1_digest=digest,
    )
    payload = bytearray(header) + bytes(data)
    payload += bytes(data_blocks * block_size - len(payload))

    stream = bytearray(total_blocks * block_size)
    data_pos = 0
    for g in groups:
        base = g.start_block * block_size
        data_stop = (g.start_block + g.data_blocks) * block_size
        src_base = data_pos * block_size
        src_stop = (data_pos + g.data_blocks) * block_size
        stream[base:data_stop] = payload[src_base:src_stop]
        for j in range(block_size):
            column = payload[src_base + j:src_stop:block_size]
            parity = _codec.encode_parity(column, g.parity_blocks)
            for k, sym in enumerate(parity):
                stream[(g.parity_range.start + k) * block_size + j] = sym
        data_pos += g.data_blocks
    return PackResult(
        stream=bytes(stream),
        total_blocks=total_blocks,
        data_blocks=data_blocks,
        parity_blocks=total_blocks - data_blocks,
        block_size=block_size,
        fec=level,
    )


def missing_blocks(stream, block_size: int) -> set[int]:
    """Indices of all-zero blocks — the fallback erasure declaration."""
    stream = bytes(stream)
    return {
        i
        for i in range(len(stream) // block_size)
        if not any(stream[i * block_size : (i + 1) * block_size])
    }


def _assemble_payload(stream: bytes, groups, block_size: int) -> bytes:
    """Concatenate every group's data blocks (parity skipped)."""
    return b"".join(
        stream[g.start_block * block_size : g.data_range.stop * block_size]
        for g in groups
    )


def _check_args(header, level: int, block_size: int) -> None:
    if header.fec != level:
        raise HeaderError(
            f"stream was packed at FEC level {header.fec}, got fec={level}"
        )
    if header.block_size != block_size:
        raise HeaderError(
            f"stream was packed with block size {header.block_size}, "
            f"got {block_size}"
        )


def _extract(header, payload: bytes, data_blocks: int, total_blocks: int,
             block_size: int) -> Unpacked:
    if header.data_blocks != data_blocks or header.total_blocks != total_blocks:
        raise CorruptedError(
            "header block counts disagree with stream geometry "
            f"(declared {header.data_blocks}/{header.total_blocks}, "
            f"derived {data_blocks}/{total_blocks})"
        )
    hdr_len = _header_length(header.name)
    if hdr_len + header.file_size > len(payload):
        raise CorruptedError("declared file size exceeds stream capacity")
    file_bytes = payload[hdr_len : hdr_len + header.file_size]
    if hashlib.sha1(file_bytes).digest() != header.sha1_digest:
        raise CorruptedError("SHA-1 mismatch: data damaged beyond recovery")
    return Unpacked(
        name=header.name,
        data=file_bytes,
        sha1_hex=header.sha1_digest.hex(),
    )


def unpack(stream, *, block_size: int, fec: int, missing=None) -> Unpacked:
    level = _validate_args(block_size, fec)
    stream = bytes(stream)
    if not stream or len(stream) % block_size:
        raise HeaderError("stream length is not a whole number of blocks")
    total_blocks = len(stream) // block_size
    try:
        groups = geometry.decode_groups(level, total_blocks)
    except ValueError as exc:
        raise HeaderError(str(exc)) from None
    data_blocks = sum(g.data_blocks for g in groups)

    if missing is None:
        erasures = None  # resolved to zero-block scan only if repair needed
    else:
        erasures = {int(i) for i in missing}
        bad = sorted(i for i in erasures if not 0 <= i < total_blocks)
        if bad:
            raise HeaderError(f"missing block indices out of range: {bad[:5]}")

    # Fast path: an intact stream never touches RS math (this also lets
    # legitimately all-zero blocks survive untouched).
    payload = _assemble_payload(stream, groups, block_size)
    header = None
    try:
        header = parse_header(payload)
    except HeaderError:
        pass
    if header is not None:
        _check_args(header, level, block_size)
        try:
            return _extract(header, payload, data_blocks, total_blocks,
                            block_size)
        except CorruptedError:
            pass  # damaged -> full repair path below

    if erasures is None:
        erasures = missing_blocks(stream, block_size)

    repaired = bytearray(payload)
    data_pos = 0
    for g in groups:
        group_end = g.start_block + g.total_blocks
        group_erasures = sorted(
            i - g.start_block for i in erasures if g.start_block <= i < group_end
        )
        if len(group_erasures) > g.parity_blocks:
            raise CorruptedError(
                f"group at block {g.start_block}: {len(group_erasures)} "
                f"blocks missing, at most {g.parity_blocks} recoverable"
            )
        base = g.start_block * block_size
        span = group_end * block_size
        data_stop = (data_pos + g.data_blocks) * block_size
        for j in range(block_size):
            column = stream[base + j:span:block_size]
            try:
                # Also repairs undeclared corruption: reedsolo fixes declared
                # erasures plus unknown errors while erasures + 2*errors <= p;
                # a corrupted block costs exactly one symbol per column.
                corrected = _codec.decode_data(
                    column, g.parity_blocks, group_erasures
                )
            except reedsolo.ReedSolomonError as exc:
                raise CorruptedError(
                    f"uncorrectable group at block {g.start_block}: {exc}"
                ) from None
            repaired[data_pos * block_size + j:data_stop:block_size] = corrected
        data_pos += g.data_blocks

    payload = bytes(repaired)
    try:
        header = parse_header(payload)
    except HeaderError as exc:
        raise CorruptedError(f"could not recover container: {exc}") from None
    _check_args(header, level, block_size)
    return _extract(header, payload, data_blocks, total_blocks, block_size)
