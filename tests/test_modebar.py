from __future__ import annotations

import unittest

from m73gui.ui.modebar import changed_mode_fields, mode_differs

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


if __name__ == "__main__":
    unittest.main()
