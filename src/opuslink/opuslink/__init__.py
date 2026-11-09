"""opuslink: constant-bitrate Opus audio streaming core.

Pure building blocks for m73gui audio broadcast — no Tk, no sound device,
no m73gui imports: the encoder tier table + capacity planner
(:mod:`opuslink.settings`), libopus wrappers (:mod:`opuslink.codec`),
the fixed-frame packet body codec (:mod:`opuslink.framing`) and the
playback jitter buffer (:mod:`opuslink.jitter`).
"""

from .codec import decode_frame, encode_frame, make_decoder, make_encoder
from .framing import (
    HEADER_LEN,
    AudioBody,
    FramingError,
    pack_body,
    unpack_body,
)
from .jitter import JitterBuffer
from .settings import (
    APP_AUDIO,
    APP_VOIP,
    DEFAULT_DURATION_MS,
    TIERS,
    MAX_BITRATE,
    MIN_BITRATE,
    AudioPlan,
    Tier,
    frame_bytes_for,
    parse_settings,
    plan_for,
    plan_label,
    settings_byte,
)

__all__ = [
    "AudioBody",
    "AudioPlan",
    "Tier",
    "JitterBuffer",
    "FramingError",
    "pack_body",
    "unpack_body",
    "make_encoder",
    "make_decoder",
    "encode_frame",
    "decode_frame",
    "plan_for",
    "plan_label",
    "frame_bytes_for",
    "settings_byte",
    "parse_settings",
    "TIERS",
    "APP_VOIP",
    "APP_AUDIO",
    "MIN_BITRATE",
    "MAX_BITRATE",
    "DEFAULT_DURATION_MS",
    "HEADER_LEN",
]
