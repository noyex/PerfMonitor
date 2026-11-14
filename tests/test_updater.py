import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from perfmon import __version__, updater


class _Resp(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


def _release(version, assets=("PerfMonitor-Setup-{v}.exe", "SHA256SUMS.txt")):
    return {"tag_name": f"v{version}", "html_url": f"https://github.com/{updater.REPO}/releases/tag/v{version}",
            "assets": [{"name": n.format(v=version), "browser_download_url": f"https://dl/{n.format(v=version)}",
                        "size": 3} for n in assets]}


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        updater._cache.update(ts=0.0, release=None)
        updater._job.update(state="idle", progress=0.0, error=None)

    def test_parse_version(self):
        self.assertGreater(updater.parse_version("v0.10.0"), updater.parse_version("0.9.9"))
        self.assertEqual(updater.parse_version("1.2.3"), (1, 2, 3))
        self.assertLess(updater.parse_version("nightly"), updater.parse_version("0.0.1"))

    def _latest(self, payload):
        with mock.patch.object(updater, "_get", return_value=_Resp(json.dumps(payload).encode())):
            return updater.latest_release(force=True)

    def test_latest_release_needs_installer_and_sums(self):
        rel = self._latest(_release("9.0.0"))
        self.assertEqual(rel["version"], "9.0.0")
        self.assertEqual(rel["setup_name"], "PerfMonitor-Setup-9.0.0.exe")
        self.assertIsNone(self._latest(_release("9.0.0", assets=("PerfMonitor-Setup-{v}.exe",))))

    def test_latest_release_offline_is_none_and_cached(self):
        with mock.patch.object(updater, "_get", side_effect=OSError("offline")) as get:
            self.assertIsNone(updater.latest_release(force=True))
            self.assertIsNone(updater.latest_release())
        self.assertEqual(get.call_count, 1)

    def test_status_offers_only_newer_and_only_installed_build(self):
        newer = {"version": "999.0.0", "url": "u"}
        with mock.patch.object(updater, "latest_release", return_value=newer):
            with mock.patch.object(updater.config, "FROZEN", True):
                self.assertEqual(updater.status()["available"], "999.0.0")
            with mock.patch.object(updater.config, "FROZEN", False):
                self.assertIsNone(updater.status()["available"])
        with mock.patch.object(updater, "latest_release", return_value={"version": __version__, "url": "u"}), \
                mock.patch.object(updater.config, "FROZEN", True):
            self.assertIsNone(updater.status()["available"])

    def _download(self, body, digest):
        rel = {"setup": "https://dl/s", "sums": "https://dl/sums", "setup_name": "PerfMonitor-Setup-9.0.0.exe",
               "size": len(body)}
        sums = f"{digest}  PerfMonitor-Setup-9.0.0.exe\n".encode()
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(updater, "_get", side_effect=[_Resp(sums), _Resp(body)]):
            return updater.download(rel, tmp), tmp

    def test_download_checks_sha256(self):
        body = b"installer"
        path, _ = self._download(body, hashlib.sha256(body).hexdigest())
        self.assertEqual(path.read_bytes(), body)
        with self.assertRaises(ValueError):
            self._download(body, "0" * 64)

    def test_install_refused_in_source_checkout(self):
        with mock.patch.object(updater.config, "FROZEN", False):
            with self.assertRaises(ValueError):
                updater.start_install()


if __name__ == "__main__":
    unittest.main()
