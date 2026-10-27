import logging
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from perfmon import shadercache as sc


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = os.path.realpath(self._td.name)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def library(self, appid="123", installdir="MyGame"):
        steamapps = self.p("Lib", "steamapps")
        exe_dir = os.path.join(steamapps, "common", installdir, "bin")
        write(os.path.join(exe_dir, "game.exe"))
        write(os.path.join(steamapps, f"appmanifest_{appid}.acf"),
              f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"installdir"\t\t"{installdir}"\n}}\n'.encode())
        return steamapps, os.path.join(exe_dir, "game.exe")


class DirSizeTest(TmpCase):
    def test_recursive_sum(self):
        write(self.p("a", "f1"), b"x" * 100)
        write(self.p("a", "b", "f2"), b"y" * 50)
        write(self.p("c", "f3"), b"z" * 7)
        self.assertEqual(sc.dir_size([self.p("a"), self.p("c")]), (157, 3))

    def test_missing_and_empty(self):
        os.makedirs(self.p("empty"))
        self.assertEqual(sc.dir_size([self.p("empty"), self.p("nope")]), (0, 0))

    def test_budget_exceeded(self):
        write(self.p("a", "f1"))
        self.assertEqual(sc.dir_size([self.p("a")], budget_s=0), (None, None))

    def test_does_not_follow_links(self):
        write(self.p("real", "f"), b"x" * 10)
        os.makedirs(self.p("a"))
        try:
            os.symlink(self.p("real"), self.p("a", "link"), target_is_directory=True)
        except OSError:   # no symlink privilege: use a junction instead
            r = subprocess.run(["cmd", "/c", "mklink", "/J", self.p("a", "link"), self.p("real")], capture_output=True)
            if r.returncode:
                self.skipTest("cannot create link")
        self.assertEqual(sc.dir_size([self.p("a")]), (0, 0))

    def test_entry_errors_swallowed(self):
        write(self.p("a", "f1"), b"x" * 5)
        real = os.scandir

        def fake(path):
            if path.endswith("denied"):
                raise PermissionError(path)
            return real(path)

        os.makedirs(self.p("a", "denied"))
        with mock.patch("os.scandir", fake):
            self.assertEqual(sc.dir_size([self.p("a")]), (5, 1))


class SteamTest(TmpCase):
    def test_appid_from_manifest(self):
        _, exe = self.library()
        self.assertEqual(sc.steam_appid(exe), "123")

    def test_appid_from_txt_in_parent(self):
        steamapps, exe = self.library()
        write(os.path.join(steamapps, "common", "MyGame", "steam_appid.txt"), b"480\r\n")
        self.assertEqual(sc.steam_appid(exe), "480")

    def test_appid_txt_next_to_exe_outside_library(self):
        write(self.p("Games", "x", "game.exe"))
        write(self.p("Games", "x", "steam_appid.txt"), b"99")
        self.assertEqual(sc.steam_appid(self.p("Games", "x", "game.exe")), "99")
        self.assertIsNone(sc.steam_appid(self.p("Games", "nothing.exe")))
        self.assertIsNone(sc.steam_appid(None))

    def test_txt_above_install_root_ignored(self):
        steamapps, exe = self.library()
        write(os.path.join(steamapps, "common", "steam_appid.txt"), b"1")
        self.assertEqual(sc.steam_appid(exe), "123")

    def test_shader_dir(self):
        steamapps, exe = self.library()
        self.assertIsNone(sc.steam_shader_dir(exe))   # not created yet
        os.makedirs(os.path.join(steamapps, "shadercache", "123"))
        self.assertEqual(sc.steam_shader_dir(exe), os.path.join(steamapps, "shadercache", "123"))
        self.assertIsNone(sc.steam_shader_dir(self.p("Games", "x", "game.exe")))


class ResolveTest(TmpCase):
    def test_env_steam_dedupe_and_missing(self):
        steamapps, exe = self.library()
        sd = os.path.join(steamapps, "shadercache", "123")
        os.makedirs(sd)
        os.makedirs(self.p("local", "AMD", "DxCache"))
        with mock.patch.dict(os.environ, {"PM_TEST_LOCAL": self.p("local")}):
            pats = [r"%PM_TEST_LOCAL%\AMD\DxCache", r"%PM_TEST_LOCAL%\AMD\dxcache",
                    r"%PM_TEST_LOCAL%\AMD\VkCache", "{steam}", "{steam}"]
            self.assertEqual(sc.resolve_dirs(pats, exe), [self.p("local", "AMD", "DxCache"), sd])
            self.assertEqual(sc.resolve_dirs(pats, None), [self.p("local", "AMD", "DxCache")])

    def test_defaults_shape(self):
        self.assertEqual(sc.DEFAULT_DIRS[-1], "{steam}")
        self.assertEqual(len(sc.DEFAULT_DIRS), 6)


class WatchTest(TmpCase):
    def wait_for(self, w, cond, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond(w.latest()):
                return True
            time.sleep(0.01)
        return False

    def test_growth_and_stop(self):
        steamapps, exe = self.library()
        sd = os.path.join(steamapps, "shadercache", "123")
        write(os.path.join(sd, "a.bin"), b"x" * 100)
        w = sc.ShaderWatch(["{steam}"], interval_s=0.05)
        self.addCleanup(w.stop)
        self.assertEqual(w.latest(), (None, None))
        time.sleep(0.15)
        self.assertEqual(w.latest(), (None, None))   # no game set: no scanning
        w.set_game(exe)
        self.assertTrue(self.wait_for(w, lambda r: r[1] == 100))
        write(os.path.join(sd, "b.bin"), b"y" * 400)
        self.assertTrue(self.wait_for(w, lambda r: r[1] == 500))
        w.set_game(None)
        self.assertEqual(w.latest(), (None, None))
        write(os.path.join(sd, "c.bin"), b"z")
        time.sleep(0.2)
        self.assertEqual(w.latest(), (None, None))
        w.stop()
        self.assertFalse(w._t.is_alive())

    def test_first_scan_is_immediate(self):
        steamapps, exe = self.library()
        write(os.path.join(steamapps, "shadercache", "123", "a"), b"x" * 3)
        w = sc.ShaderWatch(["{steam}"], interval_s=60)
        self.addCleanup(w.stop)
        w.set_game(exe)
        self.assertTrue(self.wait_for(w, lambda r: r[1] == 3, timeout=2))

    def test_budget_hit_gives_null_and_warns_once(self):
        steamapps, exe = self.library()
        write(os.path.join(steamapps, "shadercache", "123", "a"), b"x")
        w = sc.ShaderWatch(["{steam}"], interval_s=0.02, budget_s=0)
        self.addCleanup(w.stop)
        with self.assertLogs("perfmon.shadercache", level=logging.WARNING) as cm:
            w.set_game(exe)
            self.assertTrue(self.wait_for(w, lambda r: r[0] is not None))
            time.sleep(0.15)   # several more scans
        self.assertEqual(w.latest()[1], None)
        self.assertIsNotNone(w.latest()[0])
        self.assertEqual(len(cm.output), 1)


if __name__ == "__main__":
    unittest.main()
