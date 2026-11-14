"""Game settings parsers against copies of the real config files of this PC (tests/fixtures/gamesettings)."""
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from perfmon import db, gamemods
from perfmon import gamesettings as gs

FIX = Path(__file__).parent / "fixtures" / "gamesettings"
COD = r"D:\SteamLibrary\steamapps\common\Call of Duty HQ\cod.exe"
BF6 = r"D:\SteamLibrary\steamapps\common\Battlefield 6\bf6.exe"
BODYCAM = r"D:\SteamLibrary\steamapps\common\Bodycam\Bodycam\Binaries\Win64\Bodycam-Win64-Shipping.exe"


class Fake(unittest.TestCase):
    """LOCALAPPDATA / Documents pointing at a temp folder laid out like the real one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.local, self.docs = self.tmp / "Local", self.tmp / "Documents"
        players = self.local / "Activision" / "Call of Duty" / "players"
        (players / "76561198301930877").mkdir(parents=True)
        shutil.copy(FIX / "s.1.0.cod26.txt", players)
        shutil.copy(FIX / "g.cod26.1.0.l.txt", players / "76561198301930877")
        (players / "76561198301930877" / "g.cod26.pm.1.0.l.txt").write_text("Fov@0;1;2 = 60.0\n")   # not the player file
        bf = self.docs / "Battlefield 6" / "settings" / "steam"
        bf.mkdir(parents=True)
        shutil.copy(FIX / "PROFSAVEbf6mp_profile", bf)
        ue = self.local / "Bodycam" / "Saved" / "Config" / "Windows"
        ue.mkdir(parents=True)
        shutil.copy(FIX / "GameUserSettings.ini", ue)
        (ue / "Engine.ini").write_text("[GameNetDriver StatelessConnectHandlerComponent]\nCachedClientID=30\n")
        self.patches = [mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}),
                        mock.patch.object(gs, "documents", return_value=str(self.docs))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Parsers(Fake):
    def test_cod(self):
        s = gs.snapshot("cod.exe", COD)
        self.assertEqual(len(s["files"]), 2)
        self.assertEqual(s["raw"]["AMDFidelityFX"], "FSR 4")   # the migrated (later) duplicate wins
        self.assertEqual(s["normalized"], {
            "resolution": "Auto", "render_scale": 50, "upscaler": "FSR 4", "upscaler_mode": "Ultra Performance",
            "frame_gen": True, "fps_cap": "off", "vsync": "disabled", "display_mode": "Fullscreen borderless window",
            "texture_quality": 3, "ray_tracing": "Off", "preset": "Balanced", "low_latency": True, "hdr": "Off",
            "fov": 120})
        self.assertEqual(gs.snapshot("sp26-cod.exe", COD.replace("cod.exe", r"sp26\sp26-cod.exe"))["hash"], s["hash"])

    def test_bf6_keeps_undocumented_enums_as_numbers(self):
        n = gs.snapshot("bf6.exe", BF6)["normalized"]
        self.assertEqual((n["resolution"], n["render_scale"], n["fps_cap"], n["frame_gen"]), ("2560x1440", 100, "off", False))
        self.assertEqual((n["upscaler"], n["vsync"], n["display_mode"], n["upscaler_mode"]), (1, 1, 1, None))
        self.assertEqual(n["fov"], 89)

    def test_unreal(self):
        s = gs.snapshot("Bodycam-Win64-Shipping.exe", BODYCAM)
        n = s["normalized"]
        self.assertEqual((n["resolution"], n["display_mode"], n["fps_cap"], n["vsync"]), ("2560x1440", "fullscreen", 300, False))
        self.assertEqual((n["texture_quality"], n["preset"], n["render_scale"]), ("epic", "epic", 100))
        self.assertIn("engine.GameNetDriver.CachedClientID", s["raw"])

    def test_unknown_game_and_missing_file(self):
        self.assertIsNone(gs.snapshot("notepad.exe", r"C:\Windows\notepad.exe"))
        shutil.rmtree(self.docs / "Battlefield 6")
        s = gs.snapshot("bf6.exe", BF6)
        self.assertIn("error", s)
        self.assertTrue(s["searched"])

    def test_config_paths(self):
        p = self.tmp / "game" / "settings.json"
        p.parent.mkdir()
        p.write_text('{"video": {"ResolutionSizeX": 1920, "ResolutionSizeY": 1080, "vsync": true}}')
        s = gs.snapshot("game.exe", r"C:\Games\game.exe", {"game_settings_paths": {"game.exe": [str(p)]}})
        self.assertEqual(s["raw"]["video.vsync"], "True")
        self.assertEqual(s["normalized"]["resolution"], "1920x1080")

    def test_volatile_keys_do_not_change_the_hash(self):
        s = self.local / "Activision" / "Call of Duty" / "players" / "s.1.0.cod26.txt"
        h = gs.snapshot("cod.exe", COD)["hash"]
        txt = s.read_text(encoding="utf-8")
        sub = lambda key, val, t=txt: re.sub(rf"(?m)^({key}@[\d;]+ = )\S+", rf"\g<1>{val}", t, count=1)
        s.write_text(sub("Volume", "0.5", sub("WindowX", "10")), encoding="utf-8")
        self.assertEqual(gs.snapshot("cod.exe", COD)["hash"], h)
        s.write_text(sub("CapFps", "true"), encoding="utf-8")
        s2 = gs.snapshot("cod.exe", COD)
        self.assertNotEqual(s2["hash"], h)
        self.assertEqual(s2["normalized"]["fps_cap"], 250)


class Store(Fake):
    def test_store_diffs_against_the_previous_snapshot_of_the_game(self):
        con = sqlite3.connect(":memory:")
        db.migrate(con)
        a = gs.snapshot("cod.exe", COD)
        first = gs.store(con, "cod.exe", a, 100.0)
        self.assertNotIn("changed", first)
        self.assertNotIn("raw", first)
        self.assertEqual(gs.store(con, "COD.exe", a, 200.0)["since_ts"], 100.0)   # unchanged: no new row
        b = dict(a, raw={**a["raw"], "AMDFidelityFX": "FSR 3"}, hash="x")
        third = gs.store(con, "cod.exe", b, 300.0)
        self.assertEqual(third["changed"], {"AMDFidelityFX": ["FSR 4", "FSR 3"]})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM game_settings").fetchone()[0], 2)
        self.assertEqual(gs.store(con, "x.exe", {"error": "e", "searched": []}, 1.0)["error"], "e")


class Modules(unittest.TestCase):
    def test_classify(self):
        mods = [("cod.exe", r"D:\Games\cod.exe"), ("DiscordHook64.dll", r"C:\Users\u\AppData\Local\Discord\DiscordHook64.dll"),
                ("GameOverlayRenderer64.dll", r"C:\Program Files (x86)\Steam\GameOverlayRenderer64.dll"),
                ("dxgi.dll", r"C:\Windows\System32\dxgi.dll"), ("dxgi.dll", r"D:\Games\dxgi.dll"),
                ("RTSSHooks64.dll", r"C:\RTSS\RTSSHooks64.dll"), ("kernel32.dll", r"C:\Windows\System32\kernel32.dll")]
        names = {f["name"]: f["category"] for f in gamemods.classify(mods)}
        self.assertEqual(names, {"Discord": "overlay", "Steam overlay": "overlay", "proxy dxgi.dll": "injector",
                                 "RTSS / Afterburner": "overlay"})

    def test_access_denied_is_unknown(self):
        with mock.patch.object(gamemods, "modules", side_effect=OSError(5, "Odmowa dostępu.")):
            r = gamemods.scan(1234)
        self.assertEqual(r["status"], "unknown")
        self.assertIn("brak dostępu", r["reason"])

    def test_scan_own_process_and_changes(self):
        r = gamemods.scan(os.getpid())
        self.assertEqual(r["status"], "ok")
        self.assertGreater(r["count"], 10)
        a = {"status": "ok", "flagged": [{"name": "Discord"}]}
        b = {"status": "ok", "flagged": [{"name": "Steam overlay"}]}
        self.assertEqual(gamemods.changed(a, b), ["załadowano Steam overlay", "usunięto Discord"])
        self.assertEqual(gamemods.changed({"status": "unknown"}, b), [])


if __name__ == "__main__":
    unittest.main()
