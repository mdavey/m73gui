import unittest

from rsfec import geometry as geo


class LevelTable(unittest.TestCase):
    def test_parity_formula(self):
        self.assertEqual(geo.parity_blocks(1, 1), 1)
        self.assertEqual(geo.parity_blocks(1, 7), 1)
        self.assertEqual(geo.parity_blocks(1, 8), 2)
        self.assertEqual(geo.parity_blocks(2, 5), 1)
        self.assertEqual(geo.parity_blocks(3, 3), 1)
        self.assertEqual(geo.parity_blocks(4, 127), 127)

    def test_groups_fit_gf256_codeword_limit(self):
        for level in (1, 2, 3, 4):
            dmax = geo.d_max(level)
            self.assertLessEqual(dmax + geo.parity_blocks(level, dmax), 255)
            # d_max must be maximal: one more data block would break the limit
            over = dmax + 1 + (geo.parity_blocks(level, dmax)
                               if level != 4
                               else geo.parity_blocks(level, dmax) + 1)
            self.assertGreater(over, 255)

    def test_invalid_level(self):
        for bad in (0, 5, -1):
            with self.assertRaises(ValueError):
                geo.parity_blocks(bad, 1)
            with self.assertRaises(ValueError):
                geo.decode_groups(bad, 4)

    def test_data_blocks_out_of_range(self):
        with self.assertRaises(ValueError):
            geo.parity_blocks(1, 0)
        with self.assertRaises(ValueError):
            geo.parity_blocks(1, geo.d_max(1) + 1)


class EncodeLayout(unittest.TestCase):
    def test_single_group(self):
        groups = geo.encode_groups(1, 10)
        self.assertEqual(groups, [geo.Group(0, 10, 2)])

    def test_boundary_exactly_one_group(self):
        dmax = geo.d_max(2)
        groups = geo.encode_groups(2, dmax)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].data_blocks, dmax)

    def test_boundary_spills_to_second_group(self):
        dmax = geo.d_max(2)
        groups = geo.encode_groups(2, dmax + 1)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1].data_blocks, 1)
        self.assertEqual(groups[1].start_block, groups[0].total_blocks)

    def test_ranges_and_coverage(self):
        groups = geo.encode_groups(3, 400)
        total = 0
        data_total = 0
        for g in groups:
            self.assertEqual(list(g.data_range)[:1], [g.start_block])
            self.assertEqual(len(g.data_range), g.data_blocks)
            self.assertEqual(len(g.parity_range), g.parity_blocks)
            self.assertEqual(g.parity_range.start, g.data_range.stop)
            total += g.total_blocks
            data_total += g.data_blocks
        self.assertEqual(data_total, 400)
        self.assertEqual(total, geo.total_blocks_for(3, 400))
        self.assertEqual(groups[-1].start_block + groups[-1].total_blocks, total)

    def test_zero_data_blocks_is_empty(self):
        self.assertEqual(geo.encode_groups(1, 0), [])


class DecodeSolver(unittest.TestCase):
    def test_roundtrip_matches_encode(self):
        for level in (1, 2, 3, 4):
            dmax = geo.d_max(level)
            sizes = list(range(1, 40))
            sizes += [dmax - 1, dmax, dmax + 1, 2 * dmax, 2 * dmax + 1,
                      5 * dmax + 7]
            for d in sizes:
                total = geo.total_blocks_for(level, d)
                self.assertEqual(
                    geo.decode_groups(level, total),
                    geo.encode_groups(level, d),
                    f"level {level}, {d} data blocks",
                )

    def test_malformed_block_counts(self):
        # one block can't hold data+parity; and 9 blocks has no single-group
        # solution at level 1 (parity jumps 7->8 to 2 blocks of parity)
        with self.assertRaises(ValueError):
            geo.decode_groups(1, 1)
        with self.assertRaises(ValueError):
            geo.decode_groups(1, 0)
        with self.assertRaises(ValueError):
            geo.decode_groups(1, 9)
        # level 4 groups are always even-sized
        with self.assertRaises(ValueError):
            geo.decode_groups(4, 253)


if __name__ == "__main__":
    unittest.main()
