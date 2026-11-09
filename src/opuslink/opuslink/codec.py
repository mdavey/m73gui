"""Thin libopus wrappers — every opuslib quirk lives here.

CBR with in-band FEC and DTX off: our transport either delivers a whole
data packet or loses it entirely, and the modem's own FEC is the only
protection. Packet-loss concealment happens on the RX side when the jitter
buffer underruns (decode of an empty packet).
"""

from __future__ import annotations

import opuslib_next

from .settings import APP_VOIP

_APP = {
    0: opuslib_next.APPLICATION_VOIP,
    1: opuslib_next.APPLICATION_AUDIO,
}


def make_encoder(tier, duration_ms: int = 60) -> opuslib_next.Encoder:
    enc = opuslib_next.Encoder(
        tier.sample_rate, tier.channels, _APP[tier.application]
    )
    enc.vbr = 0
    enc.bitrate = tier.bitrate
    enc.inband_fec = 0
    enc.packet_loss_perc = 0
    if tier.application == APP_VOIP:
        enc.signal = opuslib_next.SIGNAL_VOICE
    return enc


def make_decoder(tier) -> opuslib_next.Decoder:
    return opuslib_next.Decoder(tier.sample_rate, tier.channels)


def encode_frame(enc, pcm: bytes, frame_samples: int) -> bytes:
    return enc.encode(pcm, frame_samples)


def decode_frame(dec, data: bytes | None, frame_samples: int) -> bytes:
    """Decode one frame; empty/None data runs packet-loss concealment."""
    return dec.decode(data or b"", frame_samples)
