import unittest

from opuslink import codec
from opuslink.settings import TIERS, frame_bytes_for


def sine_pcm(samples: int, channels: int, amp: int = 8000) -> bytes:
    import math
    import struct as st

    out = bytearray()
    for i in range(samples):
        v = int(amp * math.sin(2 * math.pi * 440 * i / 16000))
        for _ in range(channels):
            out += st.pack("<h", v)
    return bytes(out)


class CBRFrameSizes(unittest.TestCase):
    def test_exact_sizes_all_tiers(self):
        for tier in TIERS:
            enc = codec.make_encoder(tier, 60)
            pcm = bytes(tier.sample_rate * 60 // 1000 * tier.channels * 2)
            frame = codec.encode_frame(enc, pcm, tier.sample_rate * 60 // 1000)
            self.assertEqual(
                len(frame), frame_bytes_for(tier.bitrate, 60), tier.name
            )

    def test_durations_20_and_40(self):
        tier = TIERS[5]  # wideband 16k mono 16 kbps
        for dur in (20, 40, 60):
            enc = codec.make_encoder(tier, dur)
            n = tier.sample_rate * dur // 1000
            frame = codec.encode_frame(enc, bytes(n * 2), n)
            self.assertEqual(len(frame), frame_bytes_for(tier.bitrate, dur))


class Roundtrip(unittest.TestCase):
    def test_sine_encodes_and_decodes(self):
        tier = TIERS[5]
        enc = codec.make_encoder(tier, 60)
        dec = codec.make_decoder(tier)
        n = 16000 * 60 // 1000
        pcm_in = sine_pcm(n, tier.channels)
        frame = codec.encode_frame(enc, pcm_in, n)
        pcm_out = codec.decode_frame(dec, frame, n)
        self.assertEqual(len(pcm_out), n * tier.channels * 2)
        self.assertNotEqual(pcm_out, bytes(len(pcm_out)))  # not silence

    def test_stereo_roundtrip(self):
        tier = TIERS[-1]  # 24 kHz stereo
        enc = codec.make_encoder(tier, 60)
        dec = codec.make_decoder(tier)
        n = 24000 * 60 // 1000
        frame = codec.encode_frame(enc, sine_pcm(n, 2), n)
        pcm_out = codec.decode_frame(dec, frame, n)
        self.assertEqual(len(pcm_out), n * 2 * 2)

    def test_plc_returns_framedata_silence(self):
        tier = TIERS[5]
        dec = codec.make_decoder(tier)
        n = 16000 * 60 // 1000
        plc = codec.decode_frame(dec, b"", n)
        self.assertEqual(len(plc), n * 2)
        plc2 = codec.decode_frame(dec, None, n)
        self.assertEqual(len(plc2), n * 2)


if __name__ == "__main__":
    unittest.main()
