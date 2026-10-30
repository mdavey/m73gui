import hashlib
import unittest

from rsfec import header
from rsfec.errors import HeaderError


def make(name="photo.jpg", fec=3, block_size=1500, d=7, total=10, size=5):
    digest = hashlib.sha1(b"x" * size).digest()
    return header.build_header(
        fec=fec, block_size=block_size, data_blocks=d, total_blocks=total,
        name=name, file_size=size, sha1_digest=digest,
    )


class BuildParse(unittest.TestCase):
    def test_roundtrip(self):
        hdr = header.parse_header(make())
        self.assertEqual(hdr.name, "photo.jpg")
        self.assertEqual(hdr.fec, 3)
        self.assertEqual(hdr.block_size, 1500)
        self.assertEqual(hdr.data_blocks, 7)
        self.assertEqual(hdr.total_blocks, 10)
        self.assertEqual(hdr.file_size, 5)

    def test_utf8_name(self):
        name = "héllo-日本語-📡.jpg"
        hdr = header.parse_header(make(name=name))
        self.assertEqual(hdr.name, name)

    def test_starts_with_magic(self):
        self.assertTrue(make().startswith(b"RSF1"))

    def test_bad_digest_length(self):
        with self.assertRaises(ValueError):
            header.build_header(
                fec=1, block_size=64, data_blocks=1, total_blocks=2,
                name="a", file_size=0, sha1_digest=b"short",
            )

    def test_oversize_name(self):
        with self.assertRaises(ValueError):
            make(name="x" * 65536)


class ParseValidation(unittest.TestCase):
    def test_bad_magic(self):
        raw = b"XXXX" + make()[4:]
        with self.assertRaises(HeaderError):
            header.parse_header(raw)

    def test_bad_version(self):
        raw = make()[:4] + bytes([9]) + make()[5:]
        with self.assertRaises(HeaderError):
            header.parse_header(raw)

    def test_truncated(self):
        raw = make()
        for cut in (3, 18, 25, len(raw) - 1):
            with self.assertRaises(HeaderError):
                header.parse_header(raw[:cut])

    def test_bad_fec_in_header(self):
        raw = make()[:5] + bytes([7]) + make()[6:]
        with self.assertRaises(HeaderError):
            header.parse_header(raw)

    def test_invalid_utf8_name(self):
        raw = bytearray(make())
        raw[header._FIXED1.size] = 0xFF  # first byte of name
        raw = bytes(raw[: header._FIXED1.size + 1]) + raw[header._FIXED1.size + 2 :]
        with self.assertRaises(HeaderError):
            header.parse_header(raw)

    def test_empty_payload(self):
        with self.assertRaises(HeaderError):
            header.parse_header(b"")


class Sanitize(unittest.TestCase):
    def test_paths_reduced_to_basename(self):
        self.assertEqual(header.sanitize_name("/etc/passwd"), "passwd")
        self.assertEqual(header.sanitize_name("C:\\temp\\x.png"), "x.png")
        self.assertEqual(header.sanitize_name("plain.txt"), "plain.txt")

    def test_nul_stripped(self):
        self.assertEqual(header.sanitize_name("a\x00b.txt"), "ab.txt")

    def test_empty_ok(self):
        self.assertEqual(header.sanitize_name(""), "")


if __name__ == "__main__":
    unittest.main()
