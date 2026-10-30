from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from m73gui import userdirs


class UserDirsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        shutil.rmtree(self.home, ignore_errors=True)

    def test_paths_under_home(self):
        self.assertEqual(userdirs.app_dir(), Path(self.home) / ".m73gui")
        self.assertEqual(
            userdirs.settings_path(),
            Path(self.home) / ".m73gui" / "settings.json",
        )

    def test_settings_roundtrip(self):
        data = {"version": 1, "fec_level": 3, "chat_mode": {"modem_type": 2}}
        userdirs.save_settings(data)
        self.assertEqual(userdirs.load_settings(), data)

    def test_missing_file_is_empty(self):
        self.assertEqual(userdirs.load_settings(), {})

    def test_corrupt_file_is_empty(self):
        userdirs.settings_path().parent.mkdir(parents=True)
        userdirs.settings_path().write_text("not json {{{")
        self.assertEqual(userdirs.load_settings(), {})

    def test_non_dict_json_is_empty(self):
        userdirs.settings_path().parent.mkdir(parents=True)
        userdirs.settings_path().write_text("[1, 2, 3]")
        self.assertEqual(userdirs.load_settings(), {})

    def test_save_is_atomic_no_tmp_leftover(self):
        userdirs.save_settings({"a": 1})
        userdirs.save_settings({"a": 2})
        tmp = userdirs.settings_path().with_name("settings.json.tmp")
        self.assertFalse(tmp.exists())
        self.assertEqual(userdirs.load_settings(), {"a": 2})

    def test_received_dir_is_date_folder(self):
        path = userdirs.received_dir()
        today = time.strftime("%Y-%m-%d")
        self.assertTrue(path.endswith(os.path.join("received", today)))
        self.assertTrue(os.path.isdir(path))

    def test_log_dir_created(self):
        d = userdirs.log_dir()
        self.assertTrue(d.is_dir())
        self.assertTrue(str(d).endswith("logs"))


if __name__ == "__main__":
    unittest.main()
