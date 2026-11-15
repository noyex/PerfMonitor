import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from perfmon import config


class ConfigTest(unittest.TestCase):
    def test_installed_build_ignores_code_paths_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"lhm_dir": "C:\\elsewhere", "presentmon_exe": "C:\\elsewhere\\p.exe",
                                        "ui_port": 9999}), encoding="utf-8")
            with mock.patch.object(config, "CONFIG_PATH", path):
                with mock.patch.object(config, "FROZEN", True):
                    cfg = config.load()
                self.assertEqual(cfg["lhm_dir"], config.DEFAULTS["lhm_dir"])
                self.assertEqual(cfg["presentmon_exe"], config.DEFAULTS["presentmon_exe"])
                self.assertEqual(cfg["ui_port"], 9999)
                self.assertEqual(config.load()["lhm_dir"], "C:\\elsewhere")   # a source checkout: the user's choice


if __name__ == "__main__":
    unittest.main()
