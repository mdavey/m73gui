"""Thin wrapper around reedsolo 1.x, one RSCodec cached per parity count."""

from __future__ import annotations

from functools import lru_cache

import reedsolo

__all__ = ["encode_parity", "decode_data"]


@lru_cache(maxsize=None)
def _codec(nsym: int) -> reedsolo.RSCodec:
    return reedsolo.RSCodec(nsym)


def encode_parity(data: bytes, nsym: int) -> bytes:
    """Parity symbols for one column codeword (systematic: data first)."""
    encoded = _codec(nsym).encode(data)
    return bytes(encoded[len(data):])


def decode_data(codeword: bytes, nsym: int, erase_pos: list[int]) -> bytes:
    """Corrected data portion of a column codeword at known erasure positions.

    codeword is the full (data + parity) shortened codeword; erase_pos indexes
    it directly. Raises reedsolo.ReedSolomonError when uncorrectable.
    """
    decoded = _codec(nsym).decode(bytes(codeword), erase_pos=list(erase_pos))
    data = decoded[0] if isinstance(decoded, tuple) else decoded
    return bytes(data)[: len(codeword) - nsym]
