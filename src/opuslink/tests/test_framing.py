import unittest

from opuslink import framing
from opuslink.settings import TIERS, Tier

VOIP = TIERS[5]  # wideband 16k mono 16k? index: [5] is wideband 16000
STEREO = TIERS[-1]  # superwideband 48k stereo


def make_frames(n: int, size: int) -> list[bytes]:
    return [bytes([(i * 7 + 1) & 0xFF]) * size for i in range(n)]


class PackUnpack(unittest.TestCase):
    def test_roundtrip_mono(self):
        frames = make_frames(12, VOIP.bitrate * 60 // 8000)
        body = framing.pack_body(VOIP, 60, "M7QTH", frames)
        out = framing.unpack_body(body)
        self.assertEqual(out.callsign, "M7QTH")
        self.assertEqual(out.duration_ms, 60)
        self.assertEqual(out.frame_count, 12)
        self.assertEqual(out.frames, tuple(frames))
        self.assertEqual(out.tier.sample_rate, 16000)
        self.assertEqual(out.tier.channels, 1)
        self.assertEqual(out.tier.name, "wideband")

    def test_roundtrip_stereo(self):
        frames = make_frames(3, STEREO.bitrate * 60 // 8000)
        body = framing.pack_body(STEREO, 60, "ABC", frames)
        out = framing.unpack_body(body)
        self.assertEqual((out.tier.sample_rate, out.tier.channels), (24000, 2))
        self.assertEqual(out.tier.application, 1)

    def test_empty_callsign(self):
        frames = make_frames(1, VOIP.bitrate * 60 // 8000)
        out = framing.unpack_body(framing.pack_body(VOIP, 60, "", frames))
        self.assertEqual(out.callsign, "")

    def test_header_overhead(self):
        frames = make_frames(1, VOIP.bitrate * 60 // 8000)
        body = framing.pack_body(VOIP, 60, "ABCD-1", frames)
        self.assertEqual(len(body), framing.HEADER_LEN + 6 + len(frames[0]))

    def test_all_tiers_roundtrip(self):
        for tier in TIERS:
            fb = tier.bitrate * 60 // 8000
            frames = make_frames(2, fb)
            out = framing.unpack_body(framing.pack_body(tier, 60, "T", frames))
            self.assertEqual(out.frames, tuple(frames))


class Validation(unittest.TestCase):
    def good(self):
        return make_frames(4, VOIP.bitrate * 60 // 8000)

    def test_no_frames(self):
        with self.assertRaises(framing.FramingError):
            framing.pack_body(VOIP, 60, "X", [])

    def test_wrong_frame_size(self):
        frames = self.good()
        frames[2] = frames[2][:-1]
        with self.assertRaises(framing.FramingError):
            framing.pack_body(VOIP, 60, "X", frames)

    def test_short_body(self):
        with self.assertRaises(framing.FramingError):
            framing.unpack_body(b"\x00\x01\x02")

    def test_bad_version(self):
        frames = self.good()
        body = bytearray(framing.pack_body(VOIP, 60, "X", frames))
        body[0] = 9
        with self.assertRaises(framing.FramingError):
            framing.unpack_body(bytes(body))

    def test_bad_settings(self):
        frames = self.good()
        body = bytearray(framing.pack_body(VOIP, 60, "X", frames))
        body[1] = 0x80
        with self.assertRaises(framing.FramingError):
            framing.unpack_body(bytes(body))

    def test_zero_frame_count(self):
        frames = self.good()
        body = bytearray(framing.pack_body(VOIP, 60, "X", frames))
        body[6] = 0  # frame_count low byte
        body[7] = 0
        with self.assertRaises(framing.FramingError):
            framing.unpack_body(bytes(body))

    def test_trailing_garbage(self):
        body = framing.pack_body(VOIP, 60, "X", self.good()) + b"\xde\xad"
        with self.assertRaises(framing.FramingError):
            framing.unpack_body(body)

    def test_truncated_frames(self):
        body = framing.pack_body(VOIP, 60, "X", self.good())[:-1]
        with self.assertRaises(framing.FramingError):
            framing.unpack_body(body)

    def test_bitrate_invariant(self):
        frames = self.good()
        body = bytearray(framing.pack_body(VOIP, 60, "X", frames))
        body[2] += 1  # bitrate out of sync with frame_bytes
        with self.assertRaises(framing.FramingError):
            framing.unpack_body(bytes(body))

    def test_callsign_len_overflow(self):
        frames = self.good()
        body = bytearray(framing.pack_body(VOIP, 60, "X", frames))
        body[8] = framing.CALLSIGN_MAX_LEN + 1
        with self.assertRaises(framing.FramingError):
            framing.unpack_body(bytes(body))

    def test_nonascii_callsign_dropped_on_pack(self):
        frames = self.good()
        out = framing.unpack_body(
            framing.pack_body(VOIP, 60, "\u00e9MRC-1", frames)
        )
        self.assertEqual(out.callsign, "MRC-1")


if __name__ == "__main__":
    unittest.main()
