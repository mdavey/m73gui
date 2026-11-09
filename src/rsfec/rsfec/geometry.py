"""FEC level table and group-layout solver.

Pure functions, no I/O. A container stream is a sequence of *groups*; each
group holds d data blocks and p parity blocks laid out back-to-back
(``[data x d][parity x p]``). RS codewords run column-wise across the d+p
blocks of a group, so any p blocks of a group can be missing.

Layout model (mirrored by encode and decode): groups are packed as full
groups plus at most one short tail group.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "FecLevel",
    "Group",
    "d_max",
    "parity_blocks",
    "encode_groups",
    "decode_groups",
    "total_blocks_for",
]


class FecLevel(IntEnum):
    """FEC strength; approximate fraction of transmitted blocks that are data."""

    L0 = 0  # raw blocks, no parity (the container SHA-1 still verifies)
    L1 = 1  # ~7/8
    L2 = 2  # ~5/6
    L3 = 3  # ~3/4
    L4 = 4  # ~1/2


# level -> (max data blocks per group, parity divisor r with p = ceil(d / r)).
# d_max is the largest d with d + ceil(d / r) <= 255 (GF(256) codeword limit).
# L0 carries no parity at all: one block lost means the file is lost.
_LEVELS: dict[int, tuple[int, int]] = {
    0: (255, 0),
    1: (223, 7),
    2: (212, 5),
    3: (191, 3),
    4: (127, 1),
}


def _params(level: int) -> tuple[int, int]:
    if level not in _LEVELS:
        raise ValueError(f"invalid FEC level {level!r} (expected 0..4)")
    return _LEVELS[int(level)]


def d_max(level: int) -> int:
    """Maximum data blocks one group at this level can hold."""
    return _params(level)[0]


def parity_blocks(level: int, data_blocks: int) -> int:
    """Parity blocks required for data_blocks at this level."""
    dmax, r = _params(level)
    if not 1 <= data_blocks <= dmax:
        raise ValueError(
            f"data_blocks must be 1..{dmax} for FEC level {level}, got {data_blocks}"
        )
    if r == 0:
        return 0
    return (data_blocks + r - 1) // r


@dataclass(frozen=True)
class Group:
    """Position of one group inside the block stream."""

    start_block: int
    data_blocks: int
    parity_blocks: int

    @property
    def total_blocks(self) -> int:
        return self.data_blocks + self.parity_blocks

    @property
    def data_range(self) -> range:
        return range(self.start_block, self.start_block + self.data_blocks)

    @property
    def parity_range(self) -> range:
        end = self.start_block + self.total_blocks
        return range(self.start_block + self.data_blocks, end)


def encode_groups(level: int, data_block_count: int) -> list[Group]:
    """Group layout for a payload occupying data_block_count data blocks."""
    dmax = _params(level)[0]
    if data_block_count < 0:
        raise ValueError("data_block_count must be >= 0")
    groups: list[Group] = []
    pos = 0
    rem = data_block_count
    while rem > 0:
        d = min(dmax, rem)
        p = parity_blocks(level, d)
        groups.append(Group(pos, d, p))
        pos += d + p
        rem -= d
    return groups


def decode_groups(level: int, total_blocks: int) -> list[Group]:
    """Derive the group layout from the stream's block count alone.

    Consumes full groups while the remainder is larger than one, then solves
    the tail for d with d + parity(d) == remainder (f(d) is strictly
    increasing, so the tail size is unique). Raises ValueError when the
    block count cannot form a valid stream at this level.
    """
    dmax = _params(level)[0]
    total_blocks = int(total_blocks)
    if total_blocks < 1:
        raise ValueError("stream must contain at least one block")
    full = dmax + parity_blocks(level, dmax)
    groups: list[Group] = []
    rem, pos = total_blocks, 0
    while rem > 0:
        if rem > full:
            d = dmax
        else:
            d = next(
                (c for c in range(1, dmax + 1)
                 if c + parity_blocks(level, c) == rem),
                0,
            )
            if not d:
                raise ValueError(
                    f"{total_blocks} blocks cannot form a valid stream "
                    f"at FEC level {level}"
                )
        p = parity_blocks(level, d)
        groups.append(Group(pos, d, p))
        pos += d + p
        rem -= d + p
    return groups


def total_blocks_for(level: int, data_block_count: int) -> int:
    """Total stream blocks (data + parity) for a payload of the given size."""
    return sum(g.total_blocks for g in encode_groups(level, data_block_count))
