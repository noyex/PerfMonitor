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


def _release(version, assets=("NXFrame-Setup-{v}.exe", "SHA256SUMS.txt")):
    return {"tag_name": f"v{version}", "html_url": f"https://github.com/{updater.REPO}/releases/tag/v{version}",
            "assets": [{"name": n.format(v=version), "size": 3,
                        "browser_download_url": f"{updater.ASSET_PREFIX}v{version}/{n.format(v=version)}"}
                       for n in assets]}


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        updater._cache.update(ts=0.0, release=None, ok=False)
        updater._job.update(state="idle", progress=0.0, error=None)

    def test_parse_version(self):
        self.assertGreater(updater.parse_version("v0.10.0"), updater.parse_version("0.9.9"))
        self.assertEqual(updater.parse_version("1.2.3"), (1, 2, 3))
        self.assertLess(updater.parse_version("nightly"), updater.parse_version("0.0.1"))

    def _latest(self, payload):
        updater._cache["ts"] = 0.0
        with mock.patch.object(updater, "_get", return_value=_Resp(json.dumps(payload).encode())):
            return updater.latest_release(force=True)

    def test_latest_release_needs_installer_and_sums(self):
        rel = self._latest(_release("9.0.0"))
        self.assertEqual(rel["version"], "9.0.0")
        self.assertEqual(rel["setup_name"], "NXFrame-Setup-9.0.0.exe")
        self.assertIsNone(self._latest(_release("9.0.0", assets=("NXFrame-Setup-{v}.exe",))))

    def test_assets_from_elsewhere_are_ignored(self):
        payload = _release("9.0.0")
        payload["assets"][0]["browser_download_url"] = "https://example.com/NXFrame-Setup-9.0.0.exe"
        self.assertIsNone(self._latest(payload))
        payload = _release("9.0.0", assets=("NXFrame-Setup-{v}.exe\n", "SHA256SUMS.txt"))
        self.assertIsNone(self._latest(payload))

    def test_failed_check_keeps_the_known_release(self):
        self.assertEqual(self._latest(_release("9.0.0"))["version"], "9.0.0")
        updater._cache["ts"] = 0.0
        with mock.patch.object(updater, "_get", side_effect=OSError("offline")):
            self.assertEqual(updater.latest_release()["version"], "9.0.0")

    def test_forced_check_is_rate_limited(self):
        with mock.patch.object(updater, "_get", side_effect=OSError("offline")) as get:
            for _ in range(5):
                updater.latest_release(force=True)
        self.assertEqual(get.call_count, 1)

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
        rel = {"setup": "https://dl/s", "sums": "https://dl/sums", "setup_name": "NXFrame-Setup-9.0.0.exe",
               "size": len(body)}
        sums = f"{digest}  NXFrame-Setup-9.0.0.exe\n".encode()
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(updater, "_get", side_effect=[_Resp(sums), _Resp(body)]):
            return updater.download(rel, tmp)

    def test_download_and_verify_sha256(self):
        body = b"installer"
        path, expected = self._download(body, hashlib.sha256(body).hexdigest())
        self.assertEqual(path.read_bytes(), body)
        with updater.open_verified(path, expected) as real:
            self.assertEqual(Path(real), path.resolve())       # the path behind the handle, links resolved
            with self.assertRaises(PermissionError):           # locked against changes while it runs
                path.write_bytes(b"evil")
        path, expected = self._download(body, "0" * 64)
        with self.assertRaises(ValueError), updater.open_verified(path, expected):
            self.fail("ran an installer whose checksum does not match")
        self.assertFalse(path.exists())

    def test_launch_reports_how_it_went(self):
        body = b"installer"
        path, expected = self._download(body, hashlib.sha256(body).hexdigest())
        with mock.patch.object(updater, "run_installer") as run:
            self.assertEqual(updater.launch(str(path), expected), 0)
            self.assertEqual(Path(run.call_args[0][0]), path.resolve())
            self.assertEqual(updater.launch(str(path), "0" * 64), updater.RC_CHECKSUM)
            self.assertEqual(run.call_count, 1)
        with mock.patch.object(updater, "run_installer", side_effect=OSError("declined")):
            self.assertEqual(updater.launch(str(path.with_name("missing.exe")), expected), updater.RC_NOT_STARTED)

    def test_install_reports_a_launcher_failure(self):
        rel = {"version": "999.0.0", "url": "u"}
        with mock.patch.object(updater, "latest_release", return_value=rel), \
                mock.patch.object(updater, "download", return_value=(Path("x.exe"), "0" * 64)), \
                mock.patch.object(updater, "_launch_detached", return_value=updater.RC_NOT_STARTED):
            updater._install()
        self.assertEqual(updater._job["state"], "error")
        self.assertIn("nie został uruchomiony", updater._job["error"])

    def test_failed_check_is_retried_sooner(self):
        now = updater.time.time()
        with mock.patch.object(updater, "_get", side_effect=OSError("offline")) as get:
            updater.latest_release(force=True)
            with mock.patch.object(updater.time, "time", return_value=now + updater.RETRY_S + 1):
                updater.latest_release()
        self.assertEqual(get.call_count, 2)

    def test_install_refused_in_source_checkout(self):
        with mock.patch.object(updater.config, "FROZEN", False):
            with self.assertRaises(ValueError):
                updater.start_install()


if __name__ == "__main__":
    unittest.main()
