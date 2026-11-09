"""Import smoke test: every module imports without a display."""

from __future__ import annotations

import importlib
import unittest

MODULES = [
    "m73gui",
    "m73gui.__main__",
    "m73gui.constants",
    "m73gui.protocol",
    "m73gui.state",
    "m73gui.net",
    "m73gui.net.control",
    "m73gui.net.kiss",
    "m73gui.services",
    "m73gui.services.audio_io",
    "m73gui.services.audio_rx",
    "m73gui.services.audio_tx",
    "m73gui.services.monitor",
    "m73gui.services.transfers",
    "m73gui.ui",
    "m73gui.ui.theme",
    "m73gui.ui.widgets",
    "m73gui.ui.header",
    "m73gui.ui.audio",
    "m73gui.ui.chat",
    "m73gui.ui.packets",
    "m73gui.ui.files",
    "m73gui.ui.settings",
    "m73gui.ui.channel",
    "m73gui.app",
]


class ImportTest(unittest.TestCase):
    def test_all_modules_import(self):
        for name in MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_cli_parsing(self):
        from m73gui.__main__ import build_parser, parse_host_port

        parser = build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.address, "127.0.0.1:8073")
        self.assertEqual(args.appearance, "dark")
        self.assertEqual(parse_host_port("example.org", 8001), ("example.org", 8001))
        self.assertEqual(parse_host_port("10.0.0.2:9", 8001), ("10.0.0.2", 9))
        self.assertEqual(parse_host_port("[::1]:8073", 8001), ("::1", 8073))


if __name__ == "__main__":
    unittest.main()
