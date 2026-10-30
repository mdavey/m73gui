import hashlib
import random
import tempfile
import unittest
from pathlib import Path

import rsfec
from rsfec import geometry


def make_content(n, seed=1):
    return bytes(((i * 31 + seed * 17) ^ (i >> 3)) & 0xFF for i in range(n))


def drop(stream, B, idxs):
    ba = bytearray(stream)
    for i in idxs:
        ba[i * B:(i + 1) * B] = bytes(B)
    return bytes(ba)


def corrupt_block(stream, B, i):
    ba = bytearray(stream)
    off = i * B
    for j in range(0, B, max(1, B // 16)):
        ba[off + j] ^= 0xA5
    return bytes(ba)


class LosslessRoundtrip(unittest.TestCase):
    def _roundtrip(self, size, B, level, name="t.bin"):
        content = make_content(size)
        res = rsfec.pack_bytes(content, name=name, block_size=B, fec=level)
        self.assertEqual(res.block_size, B)
        self.assertEqual(res.fec, level)
        self.assertEqual(res.data_blocks + res.parity_blocks, res.total_blocks)
        self.assertEqual(len(res.stream), res.total_blocks * B)
        out = rsfec.unpack(res.stream, block_size=B, fec=level)
        self.assertEqual(out.data, content)
        self.assertEqual(out.name, name)
        self.assertEqual(out.sha1_hex, hashlib.sha1(content).hexdigest())
        self.assertEqual(out.size, size)
        return res

    def test_grid(self):
        for level in (1, 2, 3, 4):
            for size in (0, 1, 77, 78, 500, 4096):
                with self.subTest(level=level, size=size):
                    self._roundtrip(size, 128, level)

    def test_block_exact_boundaries(self):
        # name "t.bin" -> header 51 bytes; 51+77 and 51+205 fill blocks exactly
        self._roundtrip(77, 128, 2)
        self._roundtrip(205, 128, 2)

    def test_multi_group(self):
        self._roundtrip(8500, 64, 4)   # 134 data blocks -> groups of 127 + 7
        self._roundtrip(15000, 64, 1)  # 235 data blocks -> 223 + 12

    def test_large_stream(self):
        self._roundtrip(99000, 1500, 1)

    def test_pack_from_path_uses_basename(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub file.bin"
            p.write_bytes(b"abc123")
            res = rsfec.pack(p, block_size=100, fec=2)
            out = rsfec.unpack(res.stream, block_size=100, fec=2)
            self.assertEqual(out.name, "sub file.bin")
            self.assertEqual(out.data, b"abc123")

    def test_pack_sanitizes_name(self):
        res = rsfec.pack_bytes(b"x", name="/etc/passwd", block_size=64, fec=1)
        self.assertEqual(rsfec.unpack(res.stream, block_size=64, fec=1).name,
                         "passwd")

    def test_counts_consistent(self):
        res = rsfec.pack_bytes(make_content(5000), name="c.bin",
                               block_size=256, fec=3)
        # header for "c.bin" is 18 + 5 + 8 + 20 = 51 bytes
        self.assertEqual(res.data_blocks, -(-(51 + 5000) // 256))
        self.assertEqual(res.total_blocks,
                         geometry.total_blocks_for(3, res.data_blocks))


class Recovery(unittest.TestCase):
    def test_max_loss_first_p_blocks_each_level(self):
        for level in (1, 2, 3, 4):
            content = make_content(3000, seed=level)
            res = rsfec.pack_bytes(content, name="t.bin", block_size=128,
                                   fec=level)
            missing = []
            for g in geometry.decode_groups(level, res.total_blocks):
                missing += list(g.data_range)[: g.parity_blocks]
            out = rsfec.unpack(drop(res.stream, 128, missing), block_size=128,
                               fec=level)
            self.assertEqual(out.data, content, f"level {level}")

    def test_random_scattered(self):
        rng = random.Random(20261030)
        for level in (1, 2, 3, 4):
            content = make_content(3000, seed=level)
            res = rsfec.pack_bytes(content, name="t.bin", block_size=128,
                                   fec=level)
            missing = []
            for g in geometry.decode_groups(level, res.total_blocks):
                pool = list(g.data_range) + list(g.parity_range)
                missing += rng.sample(pool, g.parity_blocks)
            out = rsfec.unpack(drop(res.stream, 128, missing), block_size=128,
                               fec=level)
            self.assertEqual(out.data, content, f"level {level}")

    def test_burst_straddling_group_boundary(self):
        # L4 B=64: group0 = blocks 0..253, group1 = 254..267
        content = make_content(8500)
        res = rsfec.pack_bytes(content, name="t.bin", block_size=64, fec=4)
        burst = list(range(248, 260))  # 6 in each group, both within parity
        out = rsfec.unpack(drop(res.stream, 64, burst), block_size=64, fec=4)
        self.assertEqual(out.data, content)

    def test_too_many_missing_raises_with_counts(self):
        res = rsfec.pack_bytes(make_content(3000), name="t.bin",
                               block_size=128, fec=1)  # p = 4
        with self.assertRaises(rsfec.CorruptedError) as cm:
            rsfec.unpack(drop(res.stream, 128, [0, 1, 2, 3, 4]),
                         block_size=128, fec=1)
        self.assertIn("at most 4", str(cm.exception))

    def test_declared_missing_on_garbage_block(self):
        # transport declares a block missing that actually arrived corrupted
        content = make_content(2000)
        res = rsfec.pack_bytes(content, name="t.bin", block_size=128, fec=2)
        stream = corrupt_block(res.stream, 128, 1)
        out = rsfec.unpack(stream, block_size=128, fec=2, missing={1})
        self.assertEqual(out.data, content)

    def test_single_corrupted_block_undeclared_recovers(self):
        content = make_content(2000)
        res = rsfec.pack_bytes(content, name="t.bin", block_size=128, fec=2)
        stream = corrupt_block(res.stream, 128, 1)
        out = rsfec.unpack(stream, block_size=128, fec=2)
        self.assertEqual(out.data, content)
        self.assertEqual(out.sha1_hex, hashlib.sha1(content).hexdigest())

    def test_budget_missing_costs_one_corruption_costs_two(self):
        content = make_content(2000)
        res = rsfec.pack_bytes(content, name="t.bin", block_size=128, fec=2)  # p=4
        # 1 missing (costs 1) + 1 corrupt (costs 2) = 3 <= p: recovers
        stream = drop(corrupt_block(res.stream, 128, 2), 128, [5])
        self.assertEqual(rsfec.unpack(stream, block_size=128, fec=2).data,
                         content)
        # 2 missing (2) + 2 corrupt (4) = 6 > p: fails cleanly
        stream = corrupt_block(corrupt_block(drop(res.stream, 128, [0, 1]),
                                             128, 2), 128, 3)
        with self.assertRaises(rsfec.CorruptedError):
            rsfec.unpack(stream, block_size=128, fec=2)

    def test_auto_detected_zero_plus_undeclared_corruption(self):
        content = make_content(2000, seed=3)
        res = rsfec.pack_bytes(content, name="t.bin", block_size=128, fec=3)
        # missing=None: the dropped block is auto-detected as an erasure and
        # the untouched corrupted block is located by the decoder itself
        stream = corrupt_block(drop(res.stream, 128, [4]), 128, 2)
        out = rsfec.unpack(stream, block_size=128, fec=3)
        self.assertEqual(out.data, content)

    def test_genuine_zero_block_plus_missing(self):
        # name "t.bin" -> header 51; data offset 205 makes stream block 2
        # payload bytes 256..384 entirely zero (legitimate data)
        content = b"C" * 205 + b"\x00" * 128 + b"D" * 100
        res = rsfec.pack_bytes(content, name="t.bin", block_size=128, fec=3)
        stream = drop(res.stream, 128, [0])  # also lose the header block
        out = rsfec.unpack(stream, block_size=128, fec=3)
        self.assertEqual(out.data, content)

    def test_all_zero_file(self):
        content = bytes(400)
        res = rsfec.pack_bytes(content, name="zeros.bin", block_size=128,
                               fec=2)
        self.assertEqual(rsfec.unpack(res.stream, block_size=128,
                                      fec=2).data, content)
        # explicit declaration copes with further losses
        out = rsfec.unpack(drop(res.stream, 128, [3]), block_size=128, fec=2,
                           missing={3})
        self.assertEqual(out.data, content)

    def test_all_zero_file_auto_detect_limitation(self):
        # documented: with auto-detect, an all-zero file has more zero blocks
        # than parity capacity -> clean failure, not silent corruption
        content = bytes(300)
        res = rsfec.pack_bytes(content, name="zeros.bin", block_size=64,
                               fec=1)  # D=6, p=1
        # drop the header block so the fast path can't succeed silently
        stream = drop(res.stream, 64, [0])
        with self.assertRaises(rsfec.CorruptedError):
            rsfec.unpack(stream, block_size=64, fec=1)

    def test_missing_helper(self):
        res = rsfec.pack_bytes(make_content(1000), name="t.bin",
                               block_size=128, fec=1)
        self.assertEqual(rsfec.missing_blocks(res.stream, 128), set())
        self.assertEqual(rsfec.missing_blocks(drop(res.stream, 128, [2, 5]),
                                              128),
                         {2, 5})


class BadInput(unittest.TestCase):
    def setUp(self):
        self.res = rsfec.pack_bytes(make_content(1000), name="t.bin",
                                    block_size=128, fec=2)

    def test_empty_stream(self):
        with self.assertRaises(rsfec.HeaderError):
            rsfec.unpack(b"", block_size=128, fec=2)

    def test_partial_block(self):
        with self.assertRaises(rsfec.HeaderError):
            rsfec.unpack(self.res.stream[:-1], block_size=128, fec=2)

    def test_truncated_stream(self):
        with self.assertRaises(rsfec.RsfecError):
            rsfec.unpack(self.res.stream[:-128], block_size=128, fec=2)

    def test_wrong_fec_level(self):
        with self.assertRaises(rsfec.HeaderError):
            rsfec.unpack(self.res.stream, block_size=128, fec=4)

    def test_wrong_block_size(self):
        with self.assertRaises(rsfec.RsfecError):
            rsfec.unpack(self.res.stream, block_size=64, fec=2)

    def test_missing_index_out_of_range(self):
        with self.assertRaises(rsfec.HeaderError):
            rsfec.unpack(self.res.stream, block_size=128, fec=2,
                         missing={99999})

    def test_invalid_args(self):
        with self.assertRaises(ValueError):
            rsfec.pack_bytes(b"x", name="a", block_size=0, fec=1)
        with self.assertRaises(ValueError):
            rsfec.pack_bytes(b"x", name="a", block_size=70000, fec=1)
        with self.assertRaises(ValueError):
            rsfec.pack_bytes(b"x", name="a", block_size=128, fec=9)

    def test_random_bytes_not_a_stream(self):
        with self.assertRaises(rsfec.RsfecError):
            rsfec.unpack(make_content(6400, seed=9), block_size=128, fec=2)


class Save(unittest.TestCase):
    def test_save_writes_file(self):
        content = make_content(500)
        res = rsfec.pack_bytes(content, name="out.bin", block_size=128, fec=1)
        out = rsfec.unpack(res.stream, block_size=128, fec=1)
        with tempfile.TemporaryDirectory() as d:
            path = out.save(d)
            self.assertEqual(path.name, "out.bin")
            self.assertEqual(path.read_bytes(), content)

    def test_save_creates_dir_and_sanitizes(self):
        out = rsfec.Unpacked(name="../evil.bin", data=b"x", sha1_hex="0" * 40)
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "new" / "dir"
            path = out.save(target)
            self.assertEqual(path.parent, target)
            self.assertEqual(path.name, "evil.bin")


if __name__ == "__main__":
    unittest.main()
