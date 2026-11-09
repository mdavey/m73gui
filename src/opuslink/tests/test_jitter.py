import unittest

from opuslink.jitter import JitterBuffer


def pcm_bytes(n: int, fill: int = 0xAB) -> bytes:
    return bytes([fill]) * n


class Priming(unittest.TestCase):
    def test_silence_until_primed(self):
        jb = JitterBuffer(prime_bytes=100, max_bytes=300)
        self.assertFalse(jb.primed)
        jb.feed(pcm_bytes(99))
        data, short = jb.pull(50)
        self.assertEqual(data, bytes(50))  # pure zeros
        self.assertEqual(short, 50)
        jb.feed(pcm_bytes(1))  # now exactly primed
        data, short = jb.pull(50)
        self.assertTrue(jb.primed)
        self.assertEqual(short, 0)
        self.assertEqual(data[:20], pcm_bytes(20))

    def test_zero_pull(self):
        jb = JitterBuffer(prime_bytes=10, max_bytes=20)
        self.assertEqual(jb.pull(0), (b"", 0))


class Playing(unittest.TestCase):
    def test_underrun_reports_short_and_reprimes(self):
        jb = JitterBuffer(prime_bytes=10, max_bytes=100)
        jb.feed(pcm_bytes(15))
        jb.pull(12)  # primed, 3 remain
        self.assertTrue(jb.primed)
        data, short = jb.pull(10)
        self.assertEqual(short, 7)
        self.assertEqual(len(data), 10)
        self.assertEqual(data[:3], pcm_bytes(3))
        self.assertEqual(data[3:], bytes(7))
        self.assertEqual(jb.underruns, 1)
        self.assertFalse(jb.primed)  # re-prime for the next burst

    def test_empty_pull_after_clean_drain_is_an_underrun(self):
        jb = JitterBuffer(prime_bytes=4, max_bytes=100)
        jb.feed(pcm_bytes(4))
        data, short = jb.pull(4)  # takes all exactly, no shortfall yet
        self.assertEqual((data, short), (pcm_bytes(4), 0))
        _, short = jb.pull(4)
        self.assertEqual(short, 4)
        self.assertEqual(jb.underruns, 1)  # first dry pull = underrun
        self.assertFalse(jb.primed)
        _, short = jb.pull(4)  # waiting silence afterwards is not counted
        self.assertEqual(jb.underruns, 1)


class Overrun(unittest.TestCase):
    def test_drops_oldest_and_counts(self):
        jb = JitterBuffer(prime_bytes=10, max_bytes=50)
        jb.feed(b"A" * 30)
        jb.feed(b"B" * 30)
        self.assertEqual(jb.buffered(), 50)
        self.assertEqual(jb.dropped_bytes, 10)
        data, short = jb.pull(40)
        self.assertEqual(short, 0)
        self.assertEqual(data, b"A" * 20 + b"B" * 20)  # oldest 'A' went first

    def test_feed_empty_noop(self):
        jb = JitterBuffer(prime_bytes=10, max_bytes=50)
        jb.feed(b"")
        self.assertEqual(jb.fed_bytes, 0)


class Construction(unittest.TestCase):
    def test_bad_sizes(self):
        with self.assertRaises(ValueError):
            JitterBuffer(prime_bytes=0, max_bytes=10)
        with self.assertRaises(ValueError):
            JitterBuffer(prime_bytes=20, max_bytes=10)


if __name__ == "__main__":
    unittest.main()
