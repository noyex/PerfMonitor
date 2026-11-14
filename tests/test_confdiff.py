import unittest

from perfmon import confdiff as cd

GPU = "AMD Radeon RX 9070 XT"


def snap(**kw):
    base = {"os": "Microsoft Windows 11 Home 10.0.26200 build 26200.9711", "power_plan_guid": "381b",
            "power_plan": "381b  (Zr\ufffdwnowa\ufffdony)", "startup_apps": ["Steam", "Discord", "AMDNoiseSuppression"],
            "monitors": ["MO27Q28GR", "C27JG5x"], "gpus": [{"name": GPU, "driver": "32.0.1", "refresh": 280}],
            "hags": None, "last_boot": "2026-11-11T23:33:19"}
    base.update(kw)
    return base


class Diff(unittest.TestCase):
    def test_powershell_era_snapshot_vs_new_is_not_a_change(self):
        new = snap(power_plan="381b  (Zrównoważony)", startup_apps=["AMDNoiseSuppression", "Discord", "Steam"],
                   monitors=["C27JG5x", "MO27Q28GR"], last_boot="2026-11-14T08:00:00",
                   power={"name": "Zrównoważony", "cpu_min_pct": 0}, amd={"anti_lag": True})
        self.assertEqual(cd.diff(snap(), new), [])   # mojibake, reordering, new sections and volatile keys

    def test_driver_change_is_readable(self):
        new = snap(gpus=[{"name": GPU, "driver": "32.0.2", "refresh": 280}])
        (c,) = cd.diff(snap(), new)
        self.assertEqual(c["key"], f"gpus[{GPU}].driver")
        self.assertEqual(c["text"], f"sterownik GPU {GPU}: 32.0.1 → 32.0.2")

    def test_nested_and_bool_values(self):
        old = snap(power={"name": "Zrównoważony", "cpu_min_pct": 5}, amd={"anti_lag": True, "chill": False})
        new = snap(power={"name": "Wysoka wydajność", "cpu_min_pct": 100}, amd={"anti_lag": False, "chill": False})
        texts = [c["text"] for c in cd.diff(old, new)]
        self.assertIn("Anti-Lag: wł. → wył.", texts)
        self.assertIn("min. stan procesora (%): 5 → 100", texts)
        self.assertIn("plan zasilania: Zrównoważony → Wysoka wydajność", texts)

    def test_set_lists_and_whole_items(self):
        old = snap(drivers={"audio": [{"name": "Realtek HD", "version": "6.0.1"}]})
        new = snap(startup_apps=["Steam", "Discord", "AMDNoiseSuppression", "Overwolf"],
                   drivers={"audio": [{"name": "Realtek HD", "version": "6.0.2"}, {"name": "AMD HD Audio", "version": "10"}]})
        texts = [c["text"] for c in cd.diff(old, new)]
        self.assertIn("autostart: dodano Overwolf", texts)
        self.assertIn("sterownik audio Realtek HD: 6.0.1 → 6.0.2", texts)
        self.assertIn("sterownik audio AMD HD Audio: dodano", texts)

    def test_unreadable_is_not_a_change(self):
        old = snap(hags=None, defender={"realtime": True, "games_excluded": None})
        new = snap(hags=2, defender={"realtime": True, "games_excluded": [{"path": "D:\\Steam\\common\\X", "excluded": False}]})
        self.assertEqual(cd.diff(old, new), [])
        self.assertEqual(cd.diff(snap(hags=2), snap(hags=None)), [])

    def test_details_and_title(self):
        new = snap(gpus=[{"name": GPU, "driver": "32.0.2", "refresh": 144}])
        ch = cd.diff(snap(), new)
        d = cd.details(ch)
        self.assertEqual(d[f"gpus[{GPU}].refresh"]["before"], 280)
        self.assertTrue(cd.title(ch).startswith("Zmiana konfiguracji: "))
        self.assertEqual(cd.title(ch * 3, n=3).count(";"), 2)

    def test_game_settings_diff(self):
        old = {"upscaler": "FSR 3", "fps_cap": "off", "frame_gen": False}
        new = {"upscaler": "FSR 4", "fps_cap": 237, "frame_gen": True}
        texts = [c["text"] for c in cd.diff(old, new, volatile=set())]
        self.assertEqual(texts, ["limit FPS: off → 237", "generator klatek: wył. → wł.", "upscaler: FSR 3 → FSR 4"])


if __name__ == "__main__":
    unittest.main()
