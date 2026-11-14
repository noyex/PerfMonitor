import copy
import unittest

from perfmon import advisor
from tests.test_bottleneck import new_db


def report(sid, causes=(), bg=(), minutes=10, gpu_pct=90, modes=None, shared=500):
    return {
        "summary": {"gameplay": {"seconds": minutes * 60, "usable": True},
                    "bottleneck": {"kind": "gpu", "gpu_bound_pct": gpu_pct},
                    "present": {"modes": modes or {"Hardware: Independent Flip": 100.0}, "click_lat_avg": 12.0},
                    "vram_shared_max_mb": shared},
        "stutters": {"cause_ranking": [{"tag": t, "n": 9, "stutter_pct": 30.0, "base_pct": 5.0, "lift": 6.0,
                                        "verdict": v} for t, v in causes]},
        "gpu_background": [{"name": n, "avg_pct": p, "max_pct": p, "seen_pct": 100, "vram_avg_mb": 100} for n, p in bg],
        "hardware": {"commit_peak_gb": 40.0, "commit_limit_gb": 41.0, "disk_lat_max_ms": 200},
    }


def data(**kw):
    d = {"now": 1.8e9, "info": {"fast_startup": 0, "game_dvr": 0, "memory_integrity": 0, "hags": 2, "rebar": True,
                                "gpus": [], "displays": [], "background_tools": []},
         "has_info": True, "game": "cod.exe", "reports": [report(i, minutes=10, gpu_pct=40) for i in range(3)],
         "health": {"days": 14, "unclean_shutdowns": [], "recurring_crashes": []}, "bg_cpu": None, "bg_cpu_top": []}
    d.update(kw)
    return d


def keys(r):
    return [i["key"] for i in r["items"]]


class AdvisorTest(unittest.TestCase):
    def test_empty_db(self):
        r = advisor.advise(new_db())
        self.assertEqual(r["items"], [])
        self.assertIsNone(r["bottleneck"]["kind"])

    def test_fast_startup_fires_and_disappears(self):
        h = {"days": 14, "unclean_shutdowns": [{"during_shutdown": True}, {"during_shutdown": False}], "recurring_crashes": []}
        d = data(health=h)
        d["info"]["fast_startup"] = 1
        self.assertIn("fast_startup", keys(advisor.build(d)))
        d["info"]["fast_startup"] = 0
        self.assertNotIn("fast_startup", keys(advisor.build(d)))
        d["info"]["fast_startup"] = 1
        d["health"] = dict(h, unclean_shutdowns=[{"during_shutdown": False}])
        self.assertNotIn("fast_startup", keys(advisor.build(d)))

    def test_gigabyte_by_process_or_crash(self):
        d = data()
        d["info"]["background_tools"] = ["Discord"]
        self.assertNotIn("gigabyte_bloat", keys(advisor.build(d)))
        d["info"]["background_tools"] = ["GCC"]
        self.assertIn("gigabyte_bloat", keys(advisor.build(d)))
        d["info"]["background_tools"] = []
        d["health"]["recurring_crashes"] = [{"app": "AorusLcdService.exe", "n": 2}]
        self.assertIn("gigabyte_bloat", keys(advisor.build(d)))

    def test_igpu_monitor_vs_refuted(self):
        d = data()
        d["info"]["gpus"] = [{"name": "iGPU", "igpu": True}, {"name": "dGPU", "igpu": False}]
        d["info"]["displays"] = [{"adapter": "dGPU", "monitor": "A", "mode": "2560x1440@280 Hz"}]
        r = advisor.build(d)
        self.assertNotIn("igpu_monitor", keys(r))
        self.assertIn("igpu_monitor", [n["key"] for n in r["not_worth"]])
        d["info"]["displays"].append({"adapter": "iGPU", "monitor": "B", "mode": "2560x1440@144 Hz"})
        r = advisor.build(d)
        self.assertIn("igpu_monitor", keys(r))
        self.assertNotIn("igpu_monitor", [n["key"] for n in r["not_worth"]])

    def test_background_gpu_amd_is_honest_about_ambient(self):
        d = data(reports=[report(i, causes=[("background_gpu:AMDRSServ.exe", "ambient")], bg=[("AMDRSServ.exe", 60)])
                          for i in range(3)])
        it = next(i for i in advisor.build(d)["items"] if i["key"] == "background_gpu:AMDRSServ.exe")
        self.assertIn("Instant Replay", it["title"])
        self.assertIn("przed/po", it["why"])
        d["reports"] = [report(i, bg=[("AMDRSServ.exe", 3)]) for i in range(3)]
        self.assertFalse([k for k in keys(advisor.build(d)) if k.startswith("background_gpu")])

    def test_composed_threshold(self):
        d = data(reports=[report(i, modes={"Composed: Flip": 90.0, "Hardware: Independent Flip": 10.0}) for i in range(3)])
        self.assertIn("composed", keys(advisor.build(d)))
        d["reports"] = [report(i, modes={"Composed: Flip": 20.0, "Hardware: Independent Flip": 80.0}) for i in range(3)]
        self.assertNotIn("composed", keys(advisor.build(d)))

    def test_cause_needs_two_sessions(self):
        reps = [report(0, causes=[("vram_shared", "confirmed")]), report(1), report(2)]
        self.assertNotIn("vram_shared", keys(advisor.build(data(reports=reps))))
        reps[1] = report(1, causes=[("vram_shared", "confirmed")])
        self.assertIn("vram_shared", keys(advisor.build(data(reports=reps))))
        reps[1] = report(1, causes=[("vram_shared", "ambient")])   # not confirmed = not counted
        self.assertNotIn("vram_shared", keys(advisor.build(data(reports=reps))))

    def test_upscaler_and_not_worth(self):
        reps = [report(i, gpu_pct=95) for i in range(3)]
        r = advisor.build(data(reports=reps, bg_cpu=2.0, bg_cpu_top=[("steam.exe", 1.0)]))
        self.assertIn("upscaler", keys(r))
        self.assertEqual({n["key"] for n in r["not_worth"]}, {"cpu_ram", "autostart"})
        self.assertIn("2.0%", next(n for n in r["not_worth"] if n["key"] == "autostart")["why"])
        r = advisor.build(data(reports=[report(i, gpu_pct=30) for i in range(3)], bg_cpu=12.0))
        self.assertNotIn("upscaler", keys(r))
        self.assertEqual(r["not_worth"], [])

    def test_order_stability_first_memory_integrity_last(self):
        d = data(reports=[report(i, gpu_pct=95, causes=[("vram_shared", "confirmed")]) for i in range(3)],
                 health={"days": 14, "unclean_shutdowns": [{"during_shutdown": True}], "recurring_crashes": []})
        d["info"].update(fast_startup=1, memory_integrity=1, game_dvr=1)
        r = advisor.build(d)
        ks = keys(r)
        self.assertEqual(ks[0], "fast_startup")
        self.assertEqual(ks[-1], "memory_integrity")
        self.assertLess(ks.index("vram_shared"), ks.index("game_dvr"))
        self.assertEqual([i["priority"] for i in r["items"]], list(range(1, len(ks) + 1)))
        for i in r["items"]:
            self.assertTrue({"key", "title", "why", "expected", "risk", "undo", "how", "kind", "priority"} <= set(i))
            self.assertNotRegex(i["expected"], r"\d+\s*%")
            self.assertIn(i["kind"], {"stability", "smoothness", "latency", "fps"})

    def test_unknown_not_items(self):
        d = data()
        d["info"].update(hags=None, rebar=None)
        r = advisor.build(d)
        self.assertEqual({u["key"] for u in r["unknown"]}, {"hags", "rebar"})
        self.assertNotIn("rebar", keys(r))
        d["info"].update(hags=1, rebar=False)
        r = advisor.build(d)
        self.assertEqual(r["unknown"], [])
        self.assertIn("rebar", keys(r))

    def test_unclean_shutdowns_lead_and_disappear(self):
        resets = [{"bugcheck_code": 0, "time": "2026-11-10 01:00:00"}, {"bugcheck_code": 0, "time": "2026-11-11 01:11:01"}]
        d = data(health={"days": 14, "unclean_shutdowns": resets, "recurring_crashes": []})
        d["info"]["fast_startup"] = 1
        r = advisor.build(d)
        self.assertEqual(keys(r)[0], "unclean_shutdowns")
        it = r["items"][0]
        self.assertIn("pm health", it["how"])
        self.assertIn("2 nieoczekiwane wyłączenia", it["why"])
        d["health"]["unclean_shutdowns"] = resets[:1]
        self.assertNotIn("unclean_shutdowns", keys(advisor.build(d)))

    def test_pagefile_space_needs_all_three_conditions(self):
        def d_(auto=True, free=21.7, commits=5, disks=None):
            d = data(health={"days": 14, "unclean_shutdowns": [], "recurring_crashes": [],
                             "incidents_by_kind": {"commit_full": commits}})
            d["info"].update(pagefile=["C:\\pagefile.sys alloc=8704MB peak=143MB"], pagefile_auto=auto, ram_total_gb=31.1,
                             volumes=[{"letter": "C", "free_gb": free}, {"letter": "E", "free_gb": 930.6}],
                             disks=disks or [{"media": "SSD"}, {"media": "SSD"}])
            return d
        it = next(i for i in advisor.build(d_())["items"] if i["key"] == "pagefile_space")
        self.assertIn("21.7 GB", it["why"])
        self.assertIn("E:", it["how"])
        self.assertEqual(it["kind"], "stability")
        self.assertNotIn("E:", next(i for i in advisor.build(d_(disks=[{"media": "SSD"}, {"media": "HDD"}]))["items"]
                                    if i["key"] == "pagefile_space")["how"])   # never suggest a hard drive
        for kw in ({"auto": False}, {"free": 120.0}, {"commits": 1}):
            self.assertNotIn("pagefile_space", keys(advisor.build(d_(**kw))), kw)

    def test_power_plan_is_a_test_not_a_promise(self):
        d = data()
        d["info"]["power_plan"] = "381b4222-f694-41f0-9685-ff5bb260df2e  (Zrównoważony)"   # old snapshot: no guid key
        it = next(i for i in advisor.build(d)["items"] if i["key"] == "power_plan")
        self.assertIn("pm test start", it["how"])
        self.assertIn("--key power_plan", it["how"])
        d["tests"] = {"power_plan": {"id": 3, "status": "done", "verdict": "bez zmian", "time": "2026-11-01"}}
        r = advisor.build(d)
        self.assertNotIn("power_plan", keys(r))
        self.assertIn("power_plan", [n["key"] for n in r["not_worth"]])
        d = data()
        d["info"]["power_plan_guid"] = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"   # High performance already
        self.assertNotIn("power_plan", keys(advisor.build(d)))

    def test_wifi_driver_judged_by_network_data(self):
        w = {"provider": "Netwtw14", "event_id": 6062, "n": 269, "adapter": "Intel(R) Wi-Fi 6E AX210",
             "net_s": 86400, "wifi_pct": 100, "home": {"packet_loss": 4, "latency_spike": 2}}
        r = advisor.build(data(wifi=w))
        self.assertIn("wifi_driver", keys(r))
        self.assertIn("packet_loss 4x", next(i for i in r["items"] if i["key"] == "wifi_driver")["why"])
        r = advisor.build(data(wifi=dict(w, home={})))   # warnings but a clean network: refuted
        self.assertNotIn("wifi_driver", keys(r))
        self.assertIn("wifi_driver", [n["key"] for n in r["not_worth"]])
        r = advisor.build(data(wifi=dict(w, wifi_pct=10)))   # on a cable
        self.assertIn("kablem", next(n for n in r["not_worth"] if n["key"] == "wifi_driver")["why"])
        r = advisor.build(data(wifi=dict(w, net_s=0, wifi_pct=None)))
        self.assertIn("wifi_driver", [u["key"] for u in r["unknown"]])
        self.assertNotIn("wifi_driver", keys(advisor.build(data(wifi=dict(w, n=5)))))

    def test_overlay_needs_two_sessions_and_a_readable_scan(self):
        def with_mods(*mods):
            reps = []
            for i, m in enumerate(mods):
                r = report(i)
                r["config"] = {"modules": {"ts": 1.0, "procs": {"cod.exe": m}}}
                reps.append(r)
            return data(reports=reps)
        ok = {"status": "ok", "flagged": [{"name": "Discord", "category": "overlay"},
                                          {"name": "EasyAntiCheat", "category": "anticheat"}]}
        r = advisor.build(with_mods(ok, ok, {"status": "ok", "flagged": []}))
        self.assertIn("overlay:Discord", keys(r))
        self.assertNotIn("overlay:EasyAntiCheat", keys(r))   # anti-cheat is not something to switch off
        self.assertNotIn("overlay:Discord", keys(advisor.build(with_mods(ok, {"status": "unknown"}, {"status": "unknown"}))))

    def test_frame_gen_on_a_low_base_frame_rate(self):
        def with_fg(avg, pct):
            reps = [report(i) for i in range(3)]
            for r in reps:
                r["fps_timeline"] = {"avg": avg, "frame_gen_pct": pct}
            return data(reports=reps)
        it = next(i for i in advisor.build(with_fg(48, 45))["items"] if i["key"] == "frame_gen_base")
        self.assertIn("48 własnych klatek", it["why"])
        self.assertNotIn("frame_gen_base", keys(advisor.build(with_fg(125, 43))))   # this PC's CoD: enough base FPS
        self.assertNotIn("frame_gen_base", keys(advisor.build(with_fg(48, 0))))

    def test_crash_dump_only_with_resets_and_a_problem(self):
        resets = [{"bugcheck_code": 0, "time": "2026-11-10 01:00:00"}]
        d = data(health={"days": 14, "unclean_shutdowns": resets, "recurring_crashes": []})
        d["info"]["crash_dump"] = {"mode": "none", "problems": ["zapis zrzutów wyłączony (CrashDumpEnabled=0)"]}
        it = next(i for i in advisor.build(d)["items"] if i["key"] == "crash_dump")
        self.assertIn("CrashDumpEnabled=0", it["why"])
        d["info"]["crash_dump"] = {"mode": "small", "problems": []}
        self.assertNotIn("crash_dump", keys(advisor.build(d)))
        d = data()
        d["info"]["crash_dump"] = {"mode": "none", "problems": ["x"]}
        self.assertNotIn("crash_dump", keys(advisor.build(d)))   # nothing crashed: nothing to explain

    def test_power_plan_names_its_details(self):
        d = data()
        d["info"].update(power_plan_guid="381b4222-f694-41f0-9685-ff5bb260df2e",
                         power={"name": "Zrównoważony", "cpu_min_pct": 5, "core_parking_min_pct": 100, "pcie_aspm": "moderate"})
        it = next(i for i in advisor.build(d)["items"] if i["key"] == "power_plan")
        self.assertIn("min. stan procesora 5%", it["why"])
        self.assertIn("umiarkowane oszczędzanie", it["why"])

    def test_does_not_mutate_input(self):
        d = data()
        before = copy.deepcopy(d)
        advisor.build(d)
        self.assertEqual(d, before)


if __name__ == "__main__":
    unittest.main()
