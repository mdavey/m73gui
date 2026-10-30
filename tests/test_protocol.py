from __future__ import annotations

import struct
import unittest

from m73gui import protocol as p
from m73gui.constants import (
    HEADER_LEN,
    KIND_CHAT,
    KIND_FILE_BLOCK,
    M73G_MAGIC,
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class EnvelopeTest(unittest.TestCase):
    def test_roundtrip_all_kinds(self):
        for kind in (KIND_CHAT, KIND_FILE_BLOCK):
            env = p.encode_envelope(kind, 0xDEADBEEF, 42, b"\x00\x01body")
            self.assertEqual(env[:4], M73G_MAGIC)
            self.assertEqual(len(env), HEADER_LEN + 6)
            dec = p.decode_envelope(env)
            self.assertIsNotNone(dec)
            self.assertEqual(dec.kind, kind)
            self.assertEqual(dec.transfer_id, 0xDEADBEEF)
            self.assertEqual(dec.seq, 42)
            self.assertEqual(dec.body, b"\x00\x01body")

    def test_short_data_is_raw(self):
        self.assertIsNone(p.decode_envelope(b""))
        self.assertIsNone(p.decode_envelope(b"M73G"))
        self.assertIsNone(p.decode_envelope(b"hello world"))

    def test_bad_magic_is_raw(self):
        self.assertIsNone(p.decode_envelope(b"XXXX" + b"\x01" * 20))

    def test_version_mismatch_raises(self):
        bad = b"M73G" + b"\x02" + b"\x00" * 30
        with self.assertRaises(p.ProtocolError):
            p.decode_envelope(bad)

    def test_transfer_id_nonzero(self):
        for _ in range(50):
            self.assertNotEqual(p.new_transfer_id(), 0)


class ChatTest(unittest.TestCase):
    def test_split_codepoint_boundaries(self):
        text = "héllo wörld — ünïcode ✓"
        for max_bytes in (2, 3, 5, 7):
            parts = p.split_text_utf8(text, max_bytes)
            self.assertEqual("".join(parts), text)
            for part in parts:
                # only a lone oversized code point may exceed the budget
                ok = len(part.encode("utf-8")) <= max_bytes or len(part) == 1
                self.assertTrue(ok, f"{part!r} too big for {max_bytes}")

    def test_empty_text_one_part(self):
        self.assertEqual(p.split_text_utf8("", 10), [""])

    def test_max_text_bytes(self):
        # payload 170, from 6: 170 - 2 - 14 - 1 - 6 - 2 = 145
        self.assertEqual(p.chat_max_text_bytes(170, 6), 145)

    def test_multi_part_roundtrip(self):
        payload_size = 170
        text = "×" * 300  # 2 bytes each UTF-8
        frames = p.make_chat_frames("VK2ABC", text, 7, payload_size)
        self.assertGreater(len(frames), 1)
        parts = []
        for frame in frames:
            self.assertLessEqual(len(frame), payload_size - 2)
            env = p.decode_envelope(frame)
            self.assertEqual(env.kind, KIND_CHAT)
            self.assertEqual(env.transfer_id, 7)
            parts.append(p.decode_chat(env))
        self.assertEqual({pt.parts for pt in parts}, {len(frames)})
        self.assertEqual(sorted(pt.index for pt in parts), list(range(len(frames))))

        clock = FakeClock()
        asm = p.ChatReassembler(reveal_after_s=10.0, now_fn=clock)
        result = None
        for part in parts:
            result = asm.add(part)
        self.assertIsNotNone(result)
        text_out, complete = result
        self.assertTrue(complete)
        self.assertEqual(text_out, "VK2ABC: " + text)

    def test_duplicates_ignored(self):
        clock = FakeClock()
        asm = p.ChatReassembler(reveal_after_s=10.0, now_fn=clock)
        body = bytes([4]) + b"VK1A" + struct.pack("<H", 2) + b"ab"
        part = p.decode_chat(p.decode_envelope(p.encode_envelope(KIND_CHAT, 1, 0, body)))
        self.assertIsNone(asm.add(part))
        self.assertIsNone(asm.add(part))  # duplicate: still incomplete

    def test_partial_sweep_once(self):
        clock = FakeClock()
        asm = p.ChatReassembler(reveal_after_s=10.0, now_fn=clock)
        # forge a 3-part message, deliver 2 parts
        for i in range(2):
            body = bytes([4]) + b"VK2B" + struct.pack("<H", 3) + f"p{i}".encode()
            asm.add(p.decode_chat(p.decode_envelope(p.encode_envelope(KIND_CHAT, 99, i, body))))
        self.assertEqual(asm.sweep(), [])
        clock.t += 11
        revealed = asm.sweep()
        self.assertEqual(len(revealed), 1)
        self.assertIn("p0p1 (incomplete)", revealed[0])
        self.assertEqual(asm.sweep(), [])  # revealed only once

    def test_bad_chat_bodies(self):
        env = p.Envelope(KIND_CHAT, 1, 0, b"")
        with self.assertRaises(p.ProtocolError):
            p.decode_chat(env)
        body = bytes([0]) + b"\x00\x00"
        env = p.Envelope(KIND_CHAT, 1, 0, body)
        with self.assertRaises(p.ProtocolError):
            p.decode_chat(env)


class FileBlockTest(unittest.TestCase):
    def test_roundtrip(self):
        data = bytes(range(256)) * 4
        frame = p.encode_file_block(3, 17, 2, 400, data)
        env = p.decode_envelope(frame)
        self.assertEqual(env.kind, KIND_FILE_BLOCK)
        self.assertEqual(env.transfer_id, 3)
        self.assertEqual(env.seq, 17)
        fec, total, block = p.decode_file_block(env)
        self.assertEqual(fec, 2)
        self.assertEqual(total, 400)
        self.assertEqual(block, data)
        self.assertEqual(len(frame), HEADER_LEN + 5 + len(data))

    def test_encode_rejects_bad_args(self):
        with self.assertRaises(p.ProtocolError):
            p.encode_file_block(1, 0, 0, 5, b"x")   # fec below range
        with self.assertRaises(p.ProtocolError):
            p.encode_file_block(1, 0, 5, 5, b"x")   # fec above range
        with self.assertRaises(p.ProtocolError):
            p.encode_file_block(1, 0, 2, 0, b"x")   # total zero
        with self.assertRaises(p.ProtocolError):
            p.encode_file_block(1, 0, 2, 5, b"")    # empty block

    def test_decode_rejects_bad_bodies(self):
        with self.assertRaises(p.ProtocolError):
            p.decode_file_block(p.Envelope(KIND_FILE_BLOCK, 1, 0, b"\x00" * 5))
        body = struct.pack("<BI", 9, 5) + b"x" * 32  # bad fec level
        with self.assertRaises(p.ProtocolError):
            p.decode_file_block(p.Envelope(KIND_FILE_BLOCK, 1, 0, body))
        body = struct.pack("<BI", 2, 0) + b"x" * 32  # total zero
        with self.assertRaises(p.ProtocolError):
            p.decode_file_block(p.Envelope(KIND_FILE_BLOCK, 1, 0, body))


class BlockSizeTest(unittest.TestCase):
    def test_formula(self):
        # payload - KISS(2) - envelope(14) - body header(5)
        self.assertEqual(p.block_size_for(170), 149)
        self.assertEqual(p.block_size_for(256), 235)
        self.assertEqual(p.block_size_for(512), 491)
        self.assertEqual(p.block_size_for(6144), 6123)

    def test_too_small(self):
        with self.assertRaises(p.ProtocolError):
            p.block_size_for(52)  # 31 B block < 32 floor
        with self.assertRaises(p.ProtocolError):
            p.block_size_for(16)

    def test_floor_boundary_ok(self):
        self.assertEqual(p.block_size_for(53), 32)


class FileTypeExtensionTest(unittest.TestCase):
    def test_safe_extensions(self):
        for name in ("photo.PNG", "notes.md", "track.flac", "page.pdf",
                     "zone.mmpz", "recording.s7s", "old.jpeg"):
            self.assertIsNone(p.file_type_extension(name), name)

    def test_unusual_and_missing(self):
        self.assertEqual(p.file_type_extension("payload.exe"), ".exe")
        self.assertEqual(p.file_type_extension("script.SH"), ".sh")
        self.assertEqual(p.file_type_extension("README"), "(no extension)")
        self.assertEqual(p.file_type_extension("weird.tar.gz"), ".gz")


class SanitizeTest(unittest.TestCase):
    def test_traversal(self):
        self.assertEqual(p.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(p.sanitize_filename("a\\..\\b\\secret.txt"), "secret.txt")
        self.assertEqual(p.sanitize_filename("/absolute/path/file.bin"), "file.bin")

    def test_control_chars_and_dots(self):
        self.assertEqual(p.sanitize_filename("...hidden"), "hidden")
        self.assertEqual(p.sanitize_filename("bad\x01na\x1fme.txt"), "badname.txt")

    def test_empty_fallback(self):
        self.assertEqual(p.sanitize_filename(""), "file.bin")
        self.assertEqual(p.sanitize_filename("..."), "file.bin")

    def test_long_name_bounded(self):
        name = p.sanitize_filename("ü" * 200 + ".txt")
        self.assertLessEqual(len(name.encode("utf-8")), 64)
        self.assertTrue(name.endswith(".txt"))
        name.encode("utf-8").decode("utf-8")  # valid UTF-8

    def test_max_name_exact(self):
        name = p.sanitize_filename("a" * 64)
        self.assertEqual(name, "a" * 64)


if __name__ == "__main__":
    unittest.main()
