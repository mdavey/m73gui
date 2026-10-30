"""Pin the reedsolo behaviours _codec.py relies on (1.x API, not docs)."""

import unittest

import reedsolo


class ReedsoloGuard(unittest.TestCase):
    def setUp(self):
        self.data = bytes(range(70))
        self.codec = reedsolo.RSCodec(10)
        self.encoded = self.codec.encode(self.data)

    def test_encode_is_systematic_data_first(self):
        self.assertEqual(len(self.encoded), len(self.data) + 10)
        self.assertEqual(bytes(self.encoded[: len(self.data)]), self.data)

    def test_decode_returns_triple_data_first(self):
        out = self.codec.decode(self.encoded)
        self.assertIsInstance(out, tuple)
        self.assertEqual(len(out), 3)
        self.assertEqual(bytes(out[0]), self.data)
        self.assertEqual(bytes(out[1]), bytes(self.encoded))

    def test_erasures_in_data_region(self):
        cw = bytearray(self.encoded)
        cw[5] = cw[6] = 0
        out = self.codec.decode(cw, erase_pos=[5, 6])
        self.assertEqual(bytes(out[0]), self.data)

    def test_erasures_in_parity_region(self):
        cw = bytearray(self.encoded)
        cw[75] = cw[79] = 0xFF
        out = self.codec.decode(cw, erase_pos=[75, 79])
        self.assertEqual(bytes(out[0]), self.data)

    def test_full_capacity_mixed_erasures(self):
        cw = bytearray(self.encoded)
        pos = [0, 1, 69, 70, 71, 72, 73, 74, 75, 76]
        for p in pos:
            cw[p] = 0
        out = self.codec.decode(cw, erase_pos=pos)
        self.assertEqual(bytes(out[0]), self.data)

    def test_over_capacity_raises(self):
        cw = bytearray(self.encoded)
        pos = list(range(11))
        for p in pos:
            cw[p] = 0
        with self.assertRaises(reedsolo.ReedSolomonError):
            self.codec.decode(cw, erase_pos=pos)

    def test_nsym_extremes_used_by_fec_levels(self):
        for nsym, d in ((1, 223), (32, 223), (128, 127)):
            codec = reedsolo.RSCodec(nsym)
            data = bytes((i * 7) & 0xFF for i in range(d))
            enc = codec.encode(data)
            self.assertLessEqual(len(enc), 255)
            pos = [0, d - 1] if nsym >= 2 else [0]
            cw = bytearray(enc)
            for p in pos:
                cw[p] = 0
            out = codec.decode(cw, erase_pos=pos)
            self.assertEqual(bytes(out[0]), data, f"nsym={nsym}")


if __name__ == "__main__":
    unittest.main()
