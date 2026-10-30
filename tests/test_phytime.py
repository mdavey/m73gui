from __future__ import annotations

import unittest

from m73gui import phytime
from m73gui.constants import ROBUST_MODE_MTU


class FrameSecondsTest(unittest.TestCase):
    def test_ofdm_qpsk_normal(self):
        # QPSK base 4 data symbols -> x4 normal frame = 16; 41/300 s each
        cfg = {
            "modem_type": 0,
            "modulation": "QPSK",
            "code_rate": "1/2",
            "frame_size": 1,
        }
        self.assertAlmostEqual(phytime.frame_seconds(cfg), 19 * 41 / 300)

    def test_ofdm_variants(self):
        ofdm = {"modem_type": 0, "code_rate": "1/2"}
        short = dict(ofdm, modulation="QPSK", frame_size=0)
        self.assertAlmostEqual(phytime.frame_seconds(short), 7 * 41 / 300)
        long64 = dict(ofdm, modulation="QAM64", frame_size=2)
        self.assertAlmostEqual(phytime.frame_seconds(long64), 47 * 41 / 300)
        rep = dict(ofdm, modulation="BPSK", code_rate="1/2x2", frame_size=1)
        self.assertAlmostEqual(phytime.frame_seconds(rep), 35 * 41 / 300)
        post = dict(ofdm, modulation="QPSK", frame_size=1, postamble=True)
        self.assertAlmostEqual(
            phytime.frame_seconds(post), 19 * 41 / 300 + 0.4
        )
        micro = dict(ofdm, modulation="QPSK", frame_size=3)
        self.assertLess(phytime.frame_seconds(micro), 0.6)

    def test_ofdm_accepts_string_frame_size(self):
        cfg = {
            "modem_type": 0,
            "modulation": "QPSK",
            "code_rate": "1/2",
            "frame_size": "normal",
        }
        self.assertAlmostEqual(phytime.frame_seconds(cfg), 19 * 41 / 300)

    def test_mfsk_fixed(self):
        cfg = {"modem_type": 1, "mfsk_mode": 2}
        self.assertAlmostEqual(phytime.frame_seconds(cfg), 138 * 1536 / 48000)

    def test_robust_matches_documented_bitrates(self):
        # RDM-1200: 512 B payload over its frame time is ~1150 bps per docs
        cfg = {"modem_type": 2, "robust_mode": 0}
        self.assertAlmostEqual(phytime.frame_seconds(cfg), 178 * 0.02)
        self.assertAlmostEqual(512 * 8 / phytime.frame_seconds(cfg), 1150, delta=10)
        # RDM-300S (short frame, 172 B) ~194 bps
        cfg = {"modem_type": 2, "robust_mode": 7}
        self.assertAlmostEqual(172 * 8 / phytime.frame_seconds(cfg), 194, delta=5)

    def test_unknown_mode_falls_back(self):
        self.assertEqual(
            phytime.frame_seconds({"modem_type": 0, "modulation": "??"}),
            phytime._DEFAULT_FRAME_S,
        )
        self.assertEqual(
            phytime.frame_seconds({"modem_type": 2, "robust_mode": 99}),
            phytime._DEFAULT_FRAME_S,
        )


class PayloadBytesForTest(unittest.TestCase):
    # Values cross-checked against modem73 phy/common.hh Common::setup,
    # phy/mfsk_modem.hh MFSKParams, phy/robust_modem.hh RobustParams.
    OFDM = {"modem_type": 0, "code_rate": "1/2", "frame_size": 1}

    def ofdm(self, mod, rate="1/2", frame=1):
        return phytime.payload_bytes_for(
            {"modem_type": 0, "modulation": mod, "code_rate": rate,
             "frame_size": frame}
        )

    def test_ofdm_spot_values(self):
        self.assertEqual(self.ofdm("QPSK"), 512)  # matches live get_config
        self.assertEqual(self.ofdm("QPSK", frame=0), 128)
        self.assertEqual(self.ofdm("QPSK", frame=2), 1024)
        self.assertEqual(self.ofdm("BPSK"), 256)  # odd symbols: order +1
        self.assertEqual(self.ofdm("8PSK"), 1024)
        self.assertEqual(self.ofdm("QAM16", "3/4"), 1536)
        self.assertEqual(self.ofdm("QAM64", "5/6", frame=2), 6816)  # order 16

    def test_ofdm_order17_rejected(self):
        self.assertEqual(self.ofdm("QAM4096", "5/6"), 6816)  # normal -> 16
        self.assertEqual(self.ofdm("QAM4096", "5/6", frame=2), 0)  # long -> 17
        self.assertEqual(self.ofdm("QAM1024", frame=2), 0)

    def test_ofdm_repeat2_needs_order_le_15(self):
        self.assertEqual(self.ofdm("BPSK", "1/2x2"), 256)  # same polar bits
        self.assertEqual(self.ofdm("QAM4096", "1/2x2"), 0)  # order 16
        self.assertEqual(self.ofdm("BPSK", "1/4x2"), 128)

    def test_ofdm_micro_is_qpsk_half_only(self):
        self.assertEqual(self.ofdm("QPSK", "1/2", frame=3), 32)
        self.assertEqual(self.ofdm("QPSK", "3/4", frame=3), 0)
        self.assertEqual(self.ofdm("8PSK", frame=3), 0)

    def test_ofdm_string_frame_size_and_invalid(self):
        self.assertEqual(self.ofdm("QPSK", frame="normal"), 512)
        self.assertEqual(self.ofdm("NOPE"), 0)
        self.assertEqual(self.ofdm("QPSK", rate="7/8"), 0)

    def test_mfsk(self):
        for mode, want in enumerate((21, 29, 37, 57)):
            got = phytime.payload_bytes_for(
                {"modem_type": 1, "mfsk_mode": mode}
            )
            self.assertEqual(got, want, f"MFSK mode {mode}")
        self.assertEqual(
            phytime.payload_bytes_for({"modem_type": 1, "mfsk_mode": 9}), 0
        )

    def test_robust(self):
        got = phytime.payload_bytes_for({"modem_type": 2, "robust_mode": 0})
        self.assertEqual(got, ROBUST_MODE_MTU[0] + 2)
        self.assertEqual(
            phytime.payload_bytes_for({"modem_type": 2, "robust_mode": 5}), 172
        )
        self.assertEqual(
            phytime.payload_bytes_for({"modem_type": 2, "robust_mode": 99}), 0
        )

    def test_empty_config_defaults_to_ofdm_invalid(self):
        self.assertEqual(phytime.payload_bytes_for({}), 0)


class EstimateTest(unittest.TestCase):
    def test_estimate_includes_pacing(self):
        cfg = {
            "modem_type": 0,
            "modulation": "QPSK",
            "code_rate": "1/2",
            "frame_size": 1,
        }
        per = phytime.frame_seconds(cfg) + phytime.CHUNK_OVERHEAD_S
        self.assertAlmostEqual(
            phytime.estimate_transfer_seconds(cfg, 10),
            (10 + phytime.CONTROL_FRAMES) * per,
        )

    def test_zero_chunks(self):
        self.assertEqual(phytime.estimate_transfer_seconds({}, 0), 0.0)
        self.assertEqual(phytime.estimate_transfer_seconds({}, -3), 0.0)


class HumanDurationTest(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(phytime.human_duration(9.4), "9s")
        self.assertEqual(phytime.human_duration(59.5), "1m 00s")
        self.assertEqual(phytime.human_duration(65), "1m 05s")
        self.assertEqual(phytime.human_duration(1500), "25m 00s")
        self.assertEqual(phytime.human_duration(3725), "1h 02m")


if __name__ == "__main__":
    unittest.main()
