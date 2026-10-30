"""rsfec: Reed-Solomon erasure-coded file container.

Pack a file into a flat stream of fixed-size blocks carrying whole-file
Reed-Solomon FEC with block interleaving; unpack recovers it from the
stream even when blocks went missing in transit.
"""

from .container import (
    PackResult,
    Unpacked,
    missing_blocks,
    pack,
    pack_bytes,
    unpack,
)
from .errors import CorruptedError, HeaderError, RsfecError
from .geometry import FecLevel

__all__ = [
    "PackResult",
    "Unpacked",
    "pack",
    "pack_bytes",
    "unpack",
    "missing_blocks",
    "FecLevel",
    "RsfecError",
    "HeaderError",
    "CorruptedError",
]
