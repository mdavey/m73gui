"""Audio packet body codec.

Fixed-size CBR frames need no length prefixes; a small self-describing
header lets any single packet bootstrap a receiver:

    version u8 | settings u8 | bitrate u16 | frame_bytes u16 | frame_count u16
    from_len u8 | callsign bytes | frame_count * frame_bytes

The 14-byte M73G envelope (session id in ``transfer_id``, packet counter in
``seq``) is added by the m73gui protocol layer; this module owns the body.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Sequence

from .settings import (
    TIER_NAMES,
    Tier,
    frame_bytes_for,
    parse_settings,
    settings_byte,
)

VERSION = 0
CALLSIGN_MAX_LEN = 16
HEADER = struct.Struct("<BBHHHB")
HEADER_LEN = HEADER.size  # 9


class FramingError(ValueError):
    pass


@dataclass(frozen=True)
class AudioBody:
    callsign: str
    tier: Tier          # reconstructed; frame count/dur in fields below
    duration_ms: int
    frame_count: int
    frames: tuple[bytes, ...]

    @property
    def frame_bytes(self) -> int:
        return len(self.frames[0]) if self.frames else 0


def pack_body(
    tier: Tier, duration_ms: int, callsign: str, frames: Sequence[bytes]
) -> bytes:
    frame_count = len(frames)
    if frame_count < 1:
        raise FramingError("no frames to pack")
    if frame_count > 0xFFFF:
        raise FramingError(f"{frame_count} frames exceeds u16 count")
    frame_bytes = frame_bytes_for(tier.bitrate, duration_ms)
    for f in frames:
        if len(f) != frame_bytes:
            raise FramingError(
                f"frame of {len(f)} B != CBR size {frame_bytes} B"
            )
    from_bytes = callsign.encode("ascii", "ignore")[:CALLSIGN_MAX_LEN]
    return (
        HEADER.pack(
            VERSION,
            settings_byte(tier, tier.channels, tier.application, duration_ms),
            tier.bitrate,
            frame_bytes,
            frame_count,
            len(from_bytes),
        )
        + from_bytes
        + b"".join(frames)
    )


def unpack_body(body: bytes) -> AudioBody:
    if len(body) < HEADER_LEN:
        raise FramingError("audio body shorter than header")
    version, settings, bitrate, frame_bytes, frame_count, from_len = (
        HEADER.unpack_from(body, 0)
    )
    if version != VERSION:
        raise FramingError(f"unsupported audio body version {version}")
    try:
        sample_rate, duration_ms, channels, application = parse_settings(settings)
    except ValueError as exc:
        raise FramingError(str(exc)) from None
    if frame_count < 1:
        raise FramingError("frame_count == 0")
    if frame_bytes < 1:
        raise FramingError("frame_bytes == 0")
    try:
        exact = frame_bytes_for(bitrate, duration_ms)
    except ValueError:
        exact = -1
    if frame_bytes != exact:
        raise FramingError(
            f"frame_bytes {frame_bytes} inconsistent with {bitrate} bps "
            f"@ {duration_ms} ms"
        )
    if from_len > CALLSIGN_MAX_LEN:
        raise FramingError(f"callsign length {from_len} too long")
    frames_start = HEADER_LEN + from_len
    data = body[frames_start:]
    if len(data) != frame_count * frame_bytes:
        raise FramingError(
            f"body carries {len(data)} B, expected "
            f"{frame_count} * {frame_bytes} = {frame_count * frame_bytes} B"
        )
    callsign = body[HEADER_LEN:frames_start].decode("ascii", "replace")
    tier = Tier(
        name=TIER_NAMES.get(sample_rate, "unknown"),
        sample_rate=sample_rate,
        channels=channels,
        application=application,
        bitrate=bitrate,
    )
    frames = tuple(
        data[i * frame_bytes : (i + 1) * frame_bytes] for i in range(frame_count)
    )
    return AudioBody(
        callsign=callsign,
        tier=tier,
        duration_ms=duration_ms,
        frame_count=frame_count,
        frames=frames,
    )
