"""Wire format tests for KIND_AUDIO: envelope fields + opuslink body bridge."""

import unittest

import opuslink
from m73gui import protocol as p

TIER = opuslink.TIERS[5]  # wideband 16 kHz mono 16 kbps
FB = opuslink.frame_bytes_for(TIER.bitrate, 60)  # 120


def frames(n: int) -> list[bytes]:
    return [bytes([(i * 31 + 5) & 0xFF]) * FB for i in range(n)]


class EncodeDecode(unittest.TestCase):
    def test_roundtrip(self):
        fr = frames(4)
        payload = p.encode_audio(0xDEADBEEF, 7, TIER, 60, "M7GUI", fr)
        env = p.decode_envelope(payload)
        self.assertEqual(env.kind, p.KIND_AUDIO)
        self.assertEqual(env.transfer_id, 0xDEADBEEF)
        self.assertEqual(env.seq, 7)
        body = p.decode_audio(env)
        self.assertEqual(body.callsign, "M7GUI")
        self.assertEqual(body.frames, tuple(fr))
        self.assertEqual(body.frame_bytes, len(fr[0]))
        self.assertEqual(body.tier.sample_rate, 16000)
        self.assertEqual(body.frame_count, 4)
        self.assertEqual(body.duration_ms, 60)

    def test_budget_arithmetic(self):
        # payload 512: minus 2 KISS, 14 envelope, 9 audio header, 6 callsign
        self.assertEqual(p.audio_budget_for(512, 6), 481)
        budget = p.audio_budget_for(512, 5)
        payload = p.encode_audio(1, 0, TIER, 60, "M7GUI", frames(budget // FB))
        self.assertLessEqual(len(payload) + 2, 512)

    def test_bad_frames_raise_protocol_error(self):
        with self.assertRaises(p.ProtocolError):
            p.encode_audio(1, 0, TIER, 60, "X", [])
        bad = [b"short"]
        with self.assertRaises(p.ProtocolError):
            p.encode_audio(1, 0, TIER, 60, "X", bad)

    def test_mangled_body_raises_protocol_error(self):
        payload = p.encode_audio(1, 0, TIER, 60, "X", frames(2))
        env = p.decode_envelope(payload[:-FB])  # truncate last frame
        with self.assertRaises(p.ProtocolError):
            p.decode_audio(env)

    def test_stereo_roundtrip(self):
        tier = opuslink.TIERS[-1]  # 24 kHz stereo 48 kbps
        fb = opuslink.frame_bytes_for(tier.bitrate, 60)
        fr = [bytes([i + 1]) * fb for i in range(2)]
        env = p.decode_envelope(p.encode_audio(9, 1, tier, 60, "ST", fr))
        body = p.decode_audio(env)
        self.assertEqual(body.tier.channels, 2)
        self.assertEqual(body.tier.application, opuslink.APP_AUDIO)


class PayloadSummary(unittest.TestCase):
    """App._decode_payload renders audio packets for the packet log
    (regression: AudioBody.frame_bytes missing crashed the event pump)."""

    def test_audio_summary(self):
        from m73gui.app import App

        class Fake:
            outgoing_ids: set = set()

        payload = p.encode_audio(5, 2, TIER, 60, "M7GUI", frames(3))
        kind, summary, feed = App._decode_payload(Fake(), payload)
        self.assertEqual(kind, "audio")
        self.assertTrue(feed)
        self.assertIn("16 kbps", summary)
        self.assertIn("3\u00d7120 B", summary)

    def test_self_echo_still_feeds(self):
        from m73gui.app import App

        class Fake:
            outgoing_ids = {5}

        payload = p.encode_audio(5, 0, TIER, 60, "M7GUI", frames(1))
        _kind, summary, feed = App._decode_payload(Fake(), payload)
        self.assertTrue(feed)
        self.assertIn("(self)", summary)


if __name__ == "__main__":
    unittest.main()
