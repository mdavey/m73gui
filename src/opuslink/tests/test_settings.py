import unittest

from opuslink import settings as st


class TierTable(unittest.TestCase):
    def test_ladder_sorted_and_bounded(self):
        rates = [t.bitrate for t in st.TIERS]
        self.assertEqual(rates, sorted(rates))
        self.assertEqual(st.MIN_BITRATE, 6000)
        self.assertEqual(st.MAX_BITRATE, 48000)

    def test_no_48khz_and_tier_shape(self):
        for tier in st.TIERS:
            self.assertIn(tier.sample_rate, (8000, 12000, 16000, 24000))
        swb = [t for t in st.TIERS if t.name == "superwideband"]
        self.assertTrue(swb)
        for tier in swb:
            self.assertEqual((tier.sample_rate, tier.channels), (24000, 2))
            self.assertEqual(tier.application, st.APP_AUDIO)
        voip = [t for t in st.TIERS if tier.application == st.APP_VOIP]
        for tier in voip:
            self.assertEqual(tier.channels, 1)

    def test_frame_bytes_exact_for_all_tiers(self):
        for tier in st.TIERS:
            for dur in st.FRAME_DURATIONS_MS:
                n = st.frame_bytes_for(tier.bitrate, dur)
                self.assertEqual(n * 8000, tier.bitrate * dur)

    def test_frame_bytes_rejects_nonexact(self):
        with self.assertRaises(ValueError):
            st.frame_bytes_for(7001, 60)


class PlanFor(unittest.TestCase):
    def plan(self, bytes_per_packet, airtime_s, **kw):
        return st.plan_for(bytes_per_packet, airtime_s, **kw)

    def test_too_slow_returns_none(self):
        # 6 kbps floor: 1.5 kbps budget (base OFDM normal QPSK 1/2 shape)
        self.assertIsNone(self.plan(490, 2.6))

    def test_bad_inputs(self):
        self.assertIsNone(self.plan(0, 1.0))
        self.assertIsNone(self.plan(-5, 1.0))
        self.assertIsNone(self.plan(1000, 0.0))

    def test_picks_highest_fitting_tier(self):
        # raw ladder (margin off): 2500 B @ 1 s -> 20 kbps exactly.
        plan = self.plan(2500, 1.0, capacity_margin=1.0)
        self.assertEqual(plan.bitrate, 20000)
        self.assertEqual(plan.tier.name, "wideband")
        # one byte less falls to 18 kbps
        plan = self.plan(2499, 1.0, capacity_margin=1.0)
        self.assertEqual(plan.bitrate, 18000)

    def test_capacity_margin_trades_tier_for_headroom(self):
        # a tier at 100 % of raw capacity would need back-to-back packets;
        # the default margin drops it a rung so airtime fits with slack.
        # Boundary inclusive: 2500 B * 8 * 0.8 == 16000 exactly.
        self.assertEqual(self.plan(2500, 1.0).bitrate, 16000)
        self.assertEqual(self.plan(2499, 1.0).bitrate, 14000)

    def test_bad_capacity_margin(self):
        for margin in (0, -0.5, 1.5):
            self.assertIsNone(self.plan(2500, 1.0, capacity_margin=margin))

    def test_top_of_ladder(self):
        plan = self.plan(20000, 2.0)  # 80 kbps
        self.assertEqual(plan.bitrate, 48000)
        self.assertEqual(plan.tier.name, "superwideband")

    def test_plan_arithmetic(self):
        # 8 kbps -> narrowband 8000 (margin off: arithmetic, not selection)
        plan = self.plan(1000, 1.0, capacity_margin=1.0)
        self.assertEqual(plan.frame_bytes, 60)  # 8000*60/8000
        self.assertEqual(plan.frames_per_packet, 1000 // 60)
        self.assertEqual(
            plan.packet_audio_s, plan.frames_per_packet * 0.06
        )
        self.assertEqual(plan.period_s, max(plan.packet_audio_s, 1.0))
        self.assertEqual(plan.latency_est_s, 2.5 * plan.period_s)

    def test_slow_link_extends_period(self):
        # tiny budget but >= bitrate for exactly one frame: period = airtime
        plan = self.plan(45, 10.0)  # 36 bps... below floor
        self.assertIsNone(plan)
        plan = st.plan_for(450, 0.1)  # 36 kbps, airtime 0.1 s
        self.assertEqual(plan.frames_per_packet, 450 // plan.frame_bytes)
        self.assertGreaterEqual(plan.period_s, plan.airtime_s)

    def test_stereo_pcm_sizes(self):
        plan = self.plan(20000, 2.0)
        self.assertEqual(plan.channels, 2)
        self.assertEqual(plan.frame_samples, 1440)
        self.assertEqual(plan.pcm_bytes_per_frame, 1440 * 2 * 2)
        self.assertEqual(
            plan.packet_pcm_bytes, plan.frames_per_packet * 5760
        )


class PlanLabel(unittest.TestCase):
    def test_label_contains_tier_and_rate(self):
        plan = st.plan_for(2500, 1.0, capacity_margin=1.0)
        label = st.plan_label(plan)
        self.assertIn("wideband", label)
        self.assertIn("16 kHz mono VOIP", label)
        self.assertIn("20 kbps", label)
        stereo = st.plan_for(20000, 2.0)
        self.assertIn("stereo", st.plan_label(stereo))


class SettingsByte(unittest.TestCase):
    def test_roundtrip_all_combos(self):
        for tier in st.TIERS:
            for dur in st.FRAME_DURATIONS_MS:
                value = st.settings_byte(tier, tier.channels, tier.application, dur)
                self.assertEqual(
                    st.parse_settings(value),
                    (tier.sample_rate, dur, tier.channels, tier.application),
                )

    def test_top_bit_zero(self):
        for tier in st.TIERS:
            for dur in st.FRAME_DURATIONS_MS:
                value = st.settings_byte(tier, tier.channels, tier.application, dur)
                self.assertEqual(value & 0x80, 0)

    def test_rejects_garbage(self):
        for bad in (0x80, 0xFF, 0x9C, 0x7C):  # 0x7C: sr code 3, dur code 7
            with self.assertRaises(ValueError):
                st.parse_settings(bad)


if __name__ == "__main__":
    unittest.main()
