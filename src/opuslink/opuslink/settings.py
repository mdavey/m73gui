"""Encoder tier table and capacity planner.

The ladder is a fixed list of (sample rate, channels, application, bitrate)
tiers the software may pick from — 48 kHz is never offered. CBR encoding
pads every frame to exactly ``bitrate * duration_ms / 8000`` bytes (verified
against libopus for every tier), which is what makes fixed-size framing in
:mod:`opuslink.framing` possible.

The planner takes the per-packet byte budget and the modem frame airtime and
picks the highest tier that can stream in real time; ``None`` means the mode
cannot carry audio at all.
"""

from __future__ import annotations

from dataclasses import dataclass

APP_VOIP = 0
APP_AUDIO = 1

# Wire codes are list indices.
SAMPLE_RATES = (8000, 12000, 16000, 24000)
FRAME_DURATIONS_MS = (20, 40, 60)
DEFAULT_DURATION_MS = 60

CHANNEL_NAMES = {1: "mono", 2: "stereo"}
APP_NAMES = {APP_VOIP: "VOIP", APP_AUDIO: "AUDIO"}

# Bandwidth tier names follow Opus's own (by sample rate).
TIER_NAMES = {
    8000: "narrowband",
    12000: "mediumband",
    16000: "wideband",
    24000: "superwideband",
}


@dataclass(frozen=True)
class Tier:
    name: str
    sample_rate: int
    channels: int
    application: int
    bitrate: int


# Ascending bitrate — the planner picks the highest entry that fits.
TIERS: tuple[Tier, ...] = (
    Tier("narrowband", 8000, 1, APP_VOIP, 6000),
    Tier("narrowband", 8000, 1, APP_VOIP, 8000),
    Tier("narrowband", 8000, 1, APP_VOIP, 10000),
    Tier("mediumband", 12000, 1, APP_VOIP, 12000),
    Tier("mediumband", 12000, 1, APP_VOIP, 14000),
    Tier("wideband", 16000, 1, APP_VOIP, 16000),
    Tier("wideband", 16000, 1, APP_VOIP, 18000),
    Tier("wideband", 16000, 1, APP_VOIP, 20000),
    Tier("superwideband", 24000, 2, APP_AUDIO, 24000),
    Tier("superwideband", 24000, 2, APP_AUDIO, 28000),
    Tier("superwideband", 24000, 2, APP_AUDIO, 32000),
    Tier("superwideband", 24000, 2, APP_AUDIO, 36000),
    Tier("superwideband", 24000, 2, APP_AUDIO, 48000),
)

MIN_BITRATE = TIERS[0].bitrate
MAX_BITRATE = TIERS[-1].bitrate

# Real inter-packet interval > airtime: CSMA sense/backoff, turnaround, host
# hops. Streaming at the raw capacity edge guarantees chronic buffer overflow,
# so the planner only sees this fraction of it (worst case a tier sits at 80%
# usage => 25 % time slack for the turnaround).
CAPACITY_MARGIN = 0.8


def frame_bytes_for(bitrate: int, duration_ms: int) -> int:
    """Exact CBR frame size. Raises if the combination is not byte-exact."""
    num = bitrate * duration_ms
    if num % 8000:
        raise ValueError(f"{bitrate} bps @{duration_ms} ms is not byte-exact")
    return num // 8000


@dataclass(frozen=True)
class AudioPlan:
    tier: Tier
    duration_ms: int
    frames_per_packet: int
    airtime_s: float
    packet_audio_s: float
    period_s: float
    latency_est_s: float

    @property
    def bitrate(self) -> int:
        return self.tier.bitrate

    @property
    def sample_rate(self) -> int:
        return self.tier.sample_rate

    @property
    def channels(self) -> int:
        return self.tier.channels

    @property
    def application(self) -> int:
        return self.tier.application

    @property
    def frame_bytes(self) -> int:
        return frame_bytes_for(self.tier.bitrate, self.duration_ms)

    @property
    def frame_samples(self) -> int:
        return self.tier.sample_rate * self.duration_ms // 1000

    @property
    def pcm_bytes_per_frame(self) -> int:
        return self.frame_samples * self.tier.channels * 2

    @property
    def packet_pcm_bytes(self) -> int:
        return self.frames_per_packet * self.pcm_bytes_per_frame


def plan_for(
    bytes_per_packet: int,
    airtime_s: float,
    duration_ms: int = DEFAULT_DURATION_MS,
    capacity_margin: float = CAPACITY_MARGIN,
) -> AudioPlan | None:
    """Best tier for one data packet of ``bytes_per_packet`` sent every
    ``airtime_s`` seconds, or None when the link is too slow for audio.

    Only ``capacity_margin`` (default 0.8) of the raw capacity counts as
    available: the modem cannot actually start a new packet every airtime
    (CSMA, turnaround), and audio that fills faster than packets leave is
    chronically dropped."""
    if bytes_per_packet <= 0 or airtime_s <= 0 or not 0 < capacity_margin <= 1:
        return None
    capacity = bytes_per_packet * 8 / airtime_s * capacity_margin
    best: Tier | None = None
    for tier in TIERS:  # ascending bitrate
        if tier.bitrate <= capacity:
            best = tier
    if best is None:
        return None
    frame_bytes = frame_bytes_for(best.bitrate, duration_ms)
    frames = bytes_per_packet // frame_bytes
    if frames < 1:
        return None
    packet_audio_s = frames * duration_ms / 1000
    period_s = max(packet_audio_s, airtime_s)
    # Typical (not worst-case) end-to-end: on average half a fill period
    # before the sample is packetized, one airtime, and ~one packet's worth
    # riding in the receiver's jitter buffer.
    return AudioPlan(
        tier=best,
        duration_ms=duration_ms,
        frames_per_packet=frames,
        airtime_s=airtime_s,
        packet_audio_s=packet_audio_s,
        period_s=period_s,
        latency_est_s=2.5 * period_s,
    )


def plan_label(plan: AudioPlan) -> str:
    ch = CHANNEL_NAMES.get(plan.channels, f"{plan.channels}ch")
    app = APP_NAMES.get(plan.application, "?")
    return (
        f"{plan.tier.name} · {plan.sample_rate // 1000} kHz {ch} {app}"
        f" · CBR {plan.bitrate / 1000:g} kbps"
    )


# --------------------------------------------------------------------------
# Settings byte: sample_rate code (3b) | duration code (2b) | channels (1b)
#                | application (1b); top bit always 0 so garbage bytes are
# rejected up front.

def settings_byte(tier_or_sr, channels: int, application: int,
                  duration_ms: int) -> int:
    sr = getattr(tier_or_sr, "sample_rate", tier_or_sr)
    b = (SAMPLE_RATES.index(sr) << 5) | (FRAME_DURATIONS_MS.index(duration_ms) << 3)
    b |= ((channels - 1) & 1) << 2
    b |= application & 1
    return b


def parse_settings(value: int) -> tuple[int, int, int, int]:
    """Return (sample_rate, duration_ms, channels, application)."""
    if value & 0x80:
        raise ValueError(f"bad settings byte {value:#04x}")
    sr_code, dur_code, ch_bit, app = (
        (value >> 5) & 7, (value >> 3) & 3, (value >> 2) & 1, value & 1
    )
    if sr_code >= len(SAMPLE_RATES):
        raise ValueError(f"unknown sample rate code {sr_code}")
    if dur_code >= len(FRAME_DURATIONS_MS):
        raise ValueError(f"unknown frame duration code {dur_code}")
    return SAMPLE_RATES[sr_code], FRAME_DURATIONS_MS[dur_code], 1 + ch_bit, app
