"""rsfec exception hierarchy."""

from __future__ import annotations

__all__ = ["RsfecError", "HeaderError", "CorruptedError"]


class RsfecError(Exception):
    """Base class for all rsfec errors."""


class HeaderError(RsfecError):
    """The stream is malformed or not a supported rsfec container."""


class CorruptedError(RsfecError):
    """Too many losses to repair, or the repaired content failed integrity checks."""
