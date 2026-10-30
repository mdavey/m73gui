import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "rsfec", *args],
        capture_output=True, text=True, timeout=120,
    )


class CliRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.content = bytes((i * 41) & 0xFF for i in range(2500))
        self.src = self.dir / "original.bin"
        self.src.write_bytes(self.content)
        self.stream = self.dir / "out.rsf"

    def test_pack_unpack_roundtrip(self):
        r = run_cli("pack", str(self.src), "-b", "128", "-l", "2",
                    "-o", str(self.stream), "-v")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("data +", r.stderr)
        self.assertIn("FEC level 2", r.stderr)

        dest = self.dir / "recovered"
        r = run_cli("unpack", str(self.stream), "-b", "128", "-l", "2",
                    "-d", str(dest), "-v")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved", r.stderr)
        self.assertIn("sha1", r.stderr)
        saved = dest / "original.bin"
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), self.content)

    def test_pack_to_stdout(self):
        r = subprocess.run(
            [sys.executable, "-m", "rsfec", "pack", str(self.src),
             "-b", "128", "-l", "2"],
            capture_output=True, timeout=120,
        )
        self.assertEqual(r.returncode, 0)
        out_file = self.dir / "from_stdout.rsf"
        out_file.write_bytes(r.stdout)
        dest = self.dir / "d2"
        r = run_cli("unpack", str(out_file), "-b", "128", "-l", "2",
                    "-d", str(dest))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((dest / "original.bin").read_bytes(), self.content)

    def test_unpack_with_declared_missing(self):
        run_cli("pack", str(self.src), "-b", "128", "-l", "2",
                "-o", str(self.stream))
        ba = bytearray(self.stream.read_bytes())
        B = 128
        ba[B:2 * B] = bytes(B)   # zero out block 1
        self.stream.write_bytes(bytes(ba))
        dest = self.dir / "d3"
        r = run_cli("unpack", str(self.stream), "-b", "128", "-l", "2",
                    "-d", str(dest), "--missing", "1", "-v")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 erasure block(s): [1]", r.stderr)
        self.assertEqual((dest / "original.bin").read_bytes(), self.content)

    def test_unrecoverable_exits_1(self):
        run_cli("pack", str(self.src), "-b", "128", "-l", "2",
                "-o", str(self.stream))
        ba = bytearray(self.stream.read_bytes())
        for i in range(6):  # level 2 of 2500B/128 -> p far below 6
            ba[i * 128:(i + 1) * 128] = bytes(128)
        self.stream.write_bytes(bytes(ba))
        r = run_cli("unpack", str(self.stream), "-b", "128", "-l", "2",
                    "-d", str(self.dir / "d4"))
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("rsfec: "))

    def test_garbage_stream(self):
        junk = self.dir / "junk.bin"
        junk.write_bytes(bytes((i * 97) & 0xFF for i in range(2560)))
        r = run_cli("unpack", str(junk), "-b", "128", "-l", "3",
                    "-d", str(self.dir))
        self.assertEqual(r.returncode, 1)
        self.assertIn("rsfec:", r.stderr)

    def test_invalid_level_rejected_by_argparse(self):
        r = run_cli("pack", str(self.src), "-b", "128", "-l", "9")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
