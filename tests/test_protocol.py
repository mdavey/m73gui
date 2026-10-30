from __future__ import annotations

import struct
import unittest

from m73gui import protocol as p
from m73gui.constants import (
    HEADER_LEN,
    KIND_CHAT,
    KIND_FILE_CHUNK,
    KIND_FILE_END,
    KIND_FILE_START,
    M73G_MAGIC,
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class EnvelopeTest(unittest.TestCase):
    def test_roundtrip_all_kinds(self):
        for kind in (KIND_CHAT, KIND_FILE_START, KIND_FILE_CHUNK, KIND_FILE_END):
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


class FileStartTest(unittest.TestCase):
    SETTINGS = {
        "modem_type": 2,
        "modulation": "QAM64",
        "code_rate": "3/4",
        "frame_size": "normal",
        "mfsk_mode": 1,
        "robust_mode": 5,
    }

    def test_fits_170_byte_payload_with_max_name(self):
        name = "n" * 64
        frame = p.encode_file_start(
            1, 1234, 12, 100, name, b"\xab" * 32, self.SETTINGS, flags=2
        )
        self.assertLessEqual(len(frame), 170 - 2)
        self.assertEqual(p.file_start_max_size(), 135)

    def test_roundtrip(self):
        frame = p.encode_file_start(
            7, 2**40, 150, 300, "data.txt", b"\x01" * 32, self.SETTINGS, flags=3
        )
        fs = p.decode_file_start(p.decode_envelope(frame))
        self.assertEqual(fs.file_size, 2**40)
        self.assertEqual(fs.chunk_size, 150)
        self.assertEqual(fs.total_chunks, 300)
        self.assertEqual(fs.name, "data.txt")
        self.assertEqual(fs.sha256, b"\x01" * 32)
        self.assertEqual(fs.modem_type, 2)
        self.assertEqual(fs.modulation, 4)  # QAM64 index
        self.assertEqual(fs.code_rate, 2)  # 3/4 index
        self.assertEqual(fs.mfsk_mode, 1)
        self.assertEqual(fs.robust_mode, 5)
        self.assertEqual(fs.flags, 3)

    def test_unknown_strings_map_to_zero(self):
        frame = p.encode_file_start(
            7, 10, 10, 1, "f", b"\x00" * 32,
            {"modulation": "NOPE", "code_rate": "9/9"}, flags=0,
        )
        fs = p.decode_file_start(p.decode_envelope(frame))
        self.assertEqual(fs.modulation, 0)
        self.assertEqual(fs.code_rate, 0)

    def test_bad_lengths_rejected(self):
        env = p.Envelope(KIND_FILE_START, 1, 0, b"\x00" * 20)
        with self.assertRaises(p.ProtocolError):
            p.decode_file_start(env)
        # name_len inconsistent with body
        body = struct.pack("<QIIB", 5, 5, 1, 90) + b"x" * 10 + b"\x00" * 40
        env = p.Envelope(KIND_FILE_START, 1, 0, body)
        with self.assertRaises(p.ProtocolError):
            p.decode_file_start(env)

    def test_bad_sha_rejected(self):
        with self.assertRaises(p.ProtocolError):
            p.encode_file_start(1, 1, 1, 1, "f", b"short", {}, 0)


class ChunkSizeTest(unittest.TestCase):
    def test_formula(self):
        self.assertEqual(p.chunk_size_for(170), 150)
        self.assertEqual(p.chunk_size_for(256), 236)
        self.assertEqual(p.chunk_size_for(512), 492)
        self.assertEqual(p.chunk_size_for(6144), 6124)

    def test_too_small(self):
        with self.assertRaises(p.ProtocolError):
            p.chunk_size_for(16)


class FileChunkEndTest(unittest.TestCase):
    def test_chunk_roundtrip(self):
        data = bytes(range(256)) * 4
        frame = p.encode_file_chunk(3, 17, data)
        env = p.decode_envelope(frame)
        self.assertEqual(env.kind, KIND_FILE_CHUNK)
        self.assertEqual(env.seq, 17)
        out, ok = p.decode_file_chunk(env)
        self.assertTrue(ok)
        self.assertEqual(out, data)

    def test_chunk_crc_detects_corruption(self):
        data = b"payload"
        frame = bytearray(p.encode_file_chunk(3, 0, data))
        frame[-1] ^= 0xFF
        out, ok = p.decode_file_chunk(p.decode_envelope(bytes(frame)))
        self.assertFalse(ok)

    def test_end_roundtrip(self):
        sha = bytes(range(32))
        frame = p.encode_file_end(3, 1234, sha)
        total, out_sha = p.decode_file_end(p.decode_envelope(frame))
        self.assertEqual(total, 1234)
        self.assertEqual(out_sha, sha)

    def test_end_bad_length(self):
        env = p.Envelope(KIND_FILE_END, 1, 0, b"\x00" * 10)
        with self.assertRaises(p.ProtocolError):
            p.decode_file_end(env)


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
