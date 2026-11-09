from __future__ import annotations

import unittest

from m73gui.state import AppState
from m73gui.ui.modebar import (
    MODE_FIELDS,
    changed_mode_fields,
    mode_differs,
    visible_mode_names,
)

LIVE = {
    "modem_type": 0,
    "modulation": "QPSK",
    "code_rate": "1/2",
    "frame_size": 1,
    "mfsk_mode": 1,
    "robust_mode": 0,
    "payload_size": 512,
}


class ModeDiffersTest(unittest.TestCase):
    def test_equal_mode_no_diff(self):
        pending = {"modem_type": 0, "modulation": "QPSK", "code_rate": "1/2",
                   "frame_size": 1}
        self.assertFalse(mode_differs(pending, LIVE))

    def test_int_string_equivalence(self):
        self.assertFalse(mode_differs({"frame_size": "1"}, LIVE))
        self.assertFalse(mode_differs({"modulation": "qpsk"}, LIVE))
        self.assertTrue(mode_differs({"frame_size": 2}, LIVE))

    def test_only_covered_fields_matter(self):
        # an MFSK row must not care about OFDM-only fields
        pending = {"modem_type": 1, "mfsk_mode": 2}
        self.assertTrue(mode_differs(pending, LIVE))  # modem_type differs
        mfsk_live = dict(LIVE, modem_type=1, mfsk_mode=2)
        self.assertFalse(mode_differs(pending, mfsk_live))

    def test_missing_live_config_counts_as_diff(self):
        self.assertTrue(mode_differs({"modulation": "QPSK"}, {}))
        self.assertTrue(mode_differs({"modulation": "QPSK"},
                                     {"modulation": None}))


class ChangedModeFieldsTest(unittest.TestCase):
    def test_subset_typed_values_returned(self):
        pending = {"modem_type": 1, "mfsk_mode": 2, "code_rate": "1/2"}
        self.assertEqual(changed_mode_fields(pending, LIVE),
                         {"modem_type": 1, "mfsk_mode": 2})

    def test_no_change_empty(self):
        self.assertEqual(changed_mode_fields(
            {"modem_type": 0, "frame_size": "1"}, LIVE
        ), {})


class VisibleModeNamesTest(unittest.TestCase):
    def test_base_ofdm_has_no_codewords(self):
        self.assertNotIn("blocks", visible_mode_names(0))

    def test_wide_ofdm_appends_codewords(self):
        self.assertEqual(
            visible_mode_names(0, wide=True),
            ("modulation", "code_rate", "frame_size", "blocks"),
        )

    def test_other_modem_types_unaffected(self):
        self.assertEqual(visible_mode_names(1, wide=True), ("mfsk_mode",))
        self.assertEqual(visible_mode_names(2, wide=True), ("robust_mode",))

    def test_blocks_is_a_mode_field(self):
        # blocks must be adopted/persisted with each tab's mode intent
        self.assertIn("blocks", MODE_FIELDS)

    def test_blocks_diff_detection(self):
        live_wide = {"modem_type": 0, "blocks": 4}
        self.assertFalse(mode_differs({"blocks": "4"}, live_wide))
        self.assertTrue(mode_differs({"blocks": 8}, live_wide))
        self.assertEqual(
            changed_mode_fields({"blocks": 8, "code_rate": "3/4"},
                                dict(live_wide, code_rate="3/4")),
            {"blocks": 8},
        )


class BandwidthLabelTest(unittest.TestCase):
    def label(self, **cfg):
        state = AppState()
        state.config = cfg
        return state.bandwidth_label()

    def test_no_config_hidden(self):
        self.assertEqual(self.label(), "")
        self.assertEqual(self.label(callsign="VK2X"), "")  # no modem_type yet

    def test_base_and_wide(self):
        ofdm = {"modem_type": 0, "modulation": "QPSK"}
        self.assertEqual(self.label(**ofdm), "base")
        self.assertEqual(self.label(**ofdm, bandwidth=0), "base")
        self.assertEqual(self.label(**ofdm, bandwidth=2), "8k")
        self.assertEqual(self.label(**ofdm, bandwidth=3), "16k")

    def test_non_ofdm_hidden(self):
        self.assertEqual(self.label(modem_type=1, bandwidth=2), "")
        self.assertEqual(self.label(modem_type=2), "")

    def test_garbage_bandwidth_falls_back_to_base(self):
        self.assertEqual(
            self.label(modem_type=0, modulation="QPSK", bandwidth="?"), "base"
        )


if __name__ == "__main__":
    unittest.main()
