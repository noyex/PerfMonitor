import json
import time
import unittest
from datetime import datetime, timedelta

from perfmon import ocanalyze as oc
from perfmon import ocassist as oa
from tests.test_bottleneck import new_db

DAY = 86400
NOW = time.time()
# local noon of the day 6 days ago: sample blocks never straddle midnight
BASE = datetime.fromtimestamp(NOW - 6 * DAY).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


def add_session(con, start, minutes, kind=None, exe="game.exe", fps=140.0, bn="cpu", end_reason="exit"):
    summ = {"fps_avg": fps, "fps_1low": fps * 0.7, "bottleneck": {"kind": bn}}
    con.execute("INSERT INTO sessions(exe, start_ts, end_ts, end_reason, summary, kind) VALUES (?,?,?,?,?,?)",
                (exe, start, start + minutes * 60, end_reason, json.dumps(summ), kind))


def add_incident(con, ts, kind, details=None):
    con.execute("INSERT INTO incidents(ts, kind, severity, title, details) VALUES (?,?,?,?,?)",
                (ts, kind, "critical", kind, json.dumps(details or {})))


def world(steps=(), stock_h=11.0, stock_fps=140.0, bn="cpu", stock_days=3):
    """Stock baseline (stock_h of gameplay, `stock_days` days of samples) followed by tuned profiles.
    steps: dicts(cpu, gpu, notes, game_h, fps, tests {name: min}, incidents [(kind, details)])."""
    con = new_db()
    con.executemany("INSERT INTO samples(ts, user_idle_s) VALUES (?,?)",
                    [(BASE + k * DAY + i, 0 if i % 3 else 400) for k in range(stock_days) for i in range(4000)])
    stock = oc.create_profile(con, "Stock", cpu={"expo": True})
    oc.activate(con, stock, ts=BASE - 3600)
    for k in range(4):
        add_session(con, BASE + (k % 3) * DAY + (k // 3) * 5400 + 600, stock_h * 15, fps=stock_fps, bn=bn)
    for i, st in enumerate(steps):
        t = BASE + 3 * DAY + 6 * 3600 * (i + 1)
        pid = oc.create_profile(con, st.get("name", f"P{i}"), cpu=st.get("cpu"), gpu=st.get("gpu"), notes=st.get("notes"),
                                status=st.get("status", "testing"))
        oc.activate(con, pid, ts=t)
        cur = t + 600
        if st.get("game_h", 2.5):
            add_session(con, cur, st.get("game_h", 2.5) * 60, fps=st.get("fps", stock_fps + 5 * (i + 1)), bn=bn)
            cur += st.get("game_h", 2.5) * 3600 + 60
        for name, mins in (st.get("tests") or {}).items():
            add_session(con, cur, mins, kind="stress", exe=name)
            cur += mins * 60 + 60
        for kind, det in st.get("incidents") or []:
            add_incident(con, t + 1800, kind, det)
    oc.retag_sessions(con)
    con.commit()
    return con


def tables(con):
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {n: con.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}


GPU_OK = {"3DMark Steel Nomad": 25}


def uv_world(uv, **kw):
    prev = [{"gpu": {"voltage_offset_mv": v}, "tests": GPU_OK} for v in oa.UV_LADDER if v > uv]
    return world(prev + [{"gpu": {"voltage_offset_mv": uv}, "tests": GPU_OK, **kw}])


class DecisionOrderTest(unittest.TestCase):
    def test_no_profile_gives_baseline_command(self):
        con = new_db()
        con.execute("INSERT INTO samples(ts) VALUES (?)", (BASE,))
        r = oa.next_step(con)
        self.assertEqual(r["phase"], "baseline")
        self.assertFalse(r["blocked"])
        self.assertEqual(r["step"]["command"], "pm oc add Stock --expo on --activate")

    def test_recent_resets_block_with_dedupe_and_until(self):
        con = world()
        last = NOW - 2 * DAY
        for dt in (0, 120, 400):      # KP41 + 6008 within 10 min = one reset
            add_incident(con, last + dt, "unclean_shutdown")
        add_incident(con, NOW - 1 * DAY, "dwm_stall")
        add_incident(con, NOW - 20 * DAY, "bsod")      # older than 7 days: ignored
        r = oa.next_step(con)
        self.assertTrue(r["blocked"])
        self.assertEqual(r["phase"], "blocked")
        by = {b["what"]: b for b in r["blockers"]}
        reset = by[oa.KIND_LABEL["unclean_shutdown"]]
        self.assertTrue(reset["detail"].startswith("1×"))
        self.assertEqual(reset["until_ts"], last + 400 + 7 * DAY)
        self.assertIn(oa.KIND_LABEL["dwm_stall"], by)
        self.assertNotIn(oa.KIND_LABEL["bsod"], by)
        self.assertEqual(r["until_ts"], max(b["until_ts"] for b in r["blockers"] if "until_ts" in b))
        self.assertIsNone(r["step"])

    def test_short_stock_baseline_blocks_with_progress(self):
        r = oa.next_step(world(stock_h=4.0, stock_days=2))
        self.assertTrue(r["blocked"])
        self.assertEqual(r["blockers"][0]["what"], "Za mało danych bazowych na Stock")
        vals = {c["check"]: c["value"] for c in r["test"]["criteria"]}
        self.assertEqual(vals["≥ 10 h gry na Stock"], 4.0)
        self.assertEqual(vals["≥ 3 dni danych"], 2)

    def test_instability_before_tuning_blocks(self):
        con = world()
        add_incident(con, BASE + 600, "whea_corrected", {"whea": {"core": 2, "unit": "core"}})   # on Stock, > 7 days not needed
        r = oa.next_step(con)
        self.assertTrue(r["blocked"])
        self.assertTrue(any(b["what"] == "Niestabilność przed strojeniem" for b in r["blockers"]))

    def test_ready_baseline_starts_with_gpu_30mv(self):
        r = oa.next_step(world())
        self.assertEqual(r["phase"], "tune")
        self.assertFalse(r["blocked"])
        self.assertEqual(r["step"]["value"], "-30 mV")
        self.assertIn("--gpu-uv -30", r["step"]["command"])
        self.assertTrue(r["step"]["command"].startswith("pm oc add ") and r["step"]["command"].endswith("--activate"))
        self.assertIn("Adrenalin", r["step"]["where"])
        self.assertTrue(any("3DMark" in c["check"] for c in r["test"]["criteria"]))
        self.assertIn("-50", r["next_if_ok"])

    def test_gate_incomplete_shows_missing(self):
        r = oa.next_step(world([{"gpu": {"voltage_offset_mv": -30}, "game_h": 1.0}]))
        self.assertEqual(r["phase"], "verify")
        bad = [c["check"] for c in r["test"]["criteria"] if not c["ok"]]
        self.assertEqual(len(bad), 2)
        self.assertIn("3DMark Steel Nomad stress ≥ 20 min", bad)
        self.assertIn("3DMark", r["step"]["command"])

    def test_ladder(self):
        for uv, nxt in ((-30, -50), (-50, -70), (-70, -80), (-80, -90), (-90, -100)):
            r = oa.next_step(uv_world(uv))
            self.assertEqual(r["phase"], "tune", (uv, r["phase"]))
            self.assertIn(f"--gpu-uv {nxt}", r["step"]["command"], uv)
            self.assertEqual(r["step"]["value"], f"{nxt} mV")

    def test_soft_stop_at_100_moves_on_and_warns(self):
        r = oa.next_step(uv_world(-90))
        self.assertIn("miękki limit", r["step"]["note"])
        r = oa.next_step(uv_world(-100))
        self.assertEqual(r["step"]["value"], "+10%")
        self.assertIn("--gpu-pl 10", r["step"]["command"])
        self.assertIn("--gpu-uv -100", r["step"]["command"])      # keeps the previous settings
        self.assertTrue(any("niepewne" in x for x in r["step"]["skipped"]))

    def test_vram_after_power_limit(self):
        con = world([{"gpu": {"voltage_offset_mv": -100, "power_limit_pct": 10}, "tests": GPU_OK}])
        r = oa.next_step(con)
        self.assertIn("--vram 2700", r["step"]["command"])
        self.assertIn("--vram-fast off", r["step"]["command"])
        self.assertTrue(any("OCCT" in c["check"] for c in r["test"]["criteria"]))

    def test_no_gain_says_stay_with_previous(self):
        con = world([{"gpu": {"voltage_offset_mv": -30}, "tests": GPU_OK, "fps": 140.0}])
        r = oa.next_step(con)
        self.assertEqual(r["phase"], "no_gain")
        self.assertIn("nic nie dał", r["step"]["note"])
        self.assertEqual(r["step"]["command"], "pm oc activate 1")
        self.assertIn("--status rejected", r["step"]["reject_command"])

    def test_gpu_bound_makes_cpu_steps_optional(self):
        cfg = {"gpu": {"voltage_offset_mv": -100, "power_limit_pct": 10, "vram_mhz": 2700, "vram_fast_timing": False},
               "tests": {"3DMark Steel Nomad": 25, "OCCT": 25}}
        r = oa.next_step(world([cfg], bn="gpu"))
        self.assertEqual(r["phase"], "tune")
        self.assertIn("--co-all -10", r["step"]["command"])
        self.assertTrue(r["step"]["optional"])
        self.assertIn("opcjonalny", r["step"]["note"])
        self.assertFalse(oa.next_step(world([cfg], bn="cpu"))["step"]["optional"])

    def test_per_core_step_skips_suspect_core(self):
        gpu = {"voltage_offset_mv": -100, "power_limit_pct": 10, "vram_mhz": 2700, "vram_fast_timing": False}
        co_tests = {"CoreCycler": 500, "y-cruncher": 35}
        con = world([{"gpu": gpu, "tests": {"3DMark Steel Nomad": 25, "OCCT": 25}},
                     {"gpu": gpu, "cpu": {"co_per_core": [-10] * 8}, "tests": co_tests}])
        r = oa.next_step(con)
        self.assertEqual(r["phase"], "tune")
        self.assertIn("rdzeń 0", r["step"]["title"])
        self.assertIn("--co -15,-10,-10,-10,-10,-10,-10,-10", r["step"]["command"])

    def test_all_steps_done_suggests_full_stability(self):
        gpu = {"voltage_offset_mv": -100, "power_limit_pct": 10, "vram_mhz": 2700, "vram_fast_timing": False}
        co = {"co_per_core": [-20] * 8}
        con = world([
            {"gpu": gpu, "tests": {"3DMark Steel Nomad": 25, "OCCT": 25}},
            {"gpu": gpu, "cpu": {"co_per_core": [-10] * 8}, "tests": {"CoreCycler": 500, "y-cruncher": 35}},
            {"gpu": gpu, "cpu": co, "tests": {"CoreCycler": 70, "y-cruncher": 35}},
            {"gpu": gpu, "cpu": {**co, "pbo": dict(oa.PBO_LIMITS)}, "tests": {"CoreCycler": 70, "y-cruncher": 35}},
            {"gpu": gpu, "cpu": {**co, "pbo": dict(oa.PBO_LIMITS)}, "notes": "tREFI 32768",
             "tests": {"OCCT": 65, "y-cruncher": 35}},
        ])
        r = oa.next_step(con)
        self.assertEqual(r["phase"], "done")
        self.assertEqual(r["step"]["command"], "pm oc stability")
        self.assertIn("--status stable", r["next_if_ok"])
        self.assertIn("nie zrobi", r["next_if_ok"])


class RollbackTest(unittest.TestCase):
    def test_driver_reset_weakens_gpu_offset(self):
        con = world([{"gpu": {"voltage_offset_mv": -30}, "tests": GPU_OK},
                     {"gpu": {"voltage_offset_mv": -50}, "incidents": [("gpu_driver", {})]}])
        r = oa.next_step(con)
        self.assertEqual(r["phase"], "rollback")
        self.assertEqual(r["step"]["value"], "-30 mV")
        self.assertIn("--gpu-uv -30", r["step"]["command"])
        self.assertEqual(r["step"]["reject_command"], "pm oc edit 3 --status rejected")
        self.assertIn("pm oc activate 2", r["rollback_if_bad"])

    def test_whea_on_core_weakens_that_core(self):
        co = [-10, -10, -10, -15, -10, -10, -10, -10]
        con = world([{"cpu": {"co_per_core": [-10] * 8}, "tests": {"CoreCycler": 500, "y-cruncher": 35}},
                     {"cpu": {"co_per_core": co}, "incidents": [("whea_corrected", {"whea": {"core": 3, "unit": "core"}})]}])
        r = oa.next_step(con)
        self.assertEqual(r["phase"], "rollback")
        self.assertIn("rdzeniu 3", r["step"]["title"])
        self.assertIn("--co -10,-10,-10,-11,-10,-10,-10,-10", r["step"]["command"])
        self.assertIn("--status rejected", r["step"]["reject_command"])

    def test_whea_soc_points_at_expo_not_curve_optimizer(self):
        con = world([{"cpu": {"co_per_core": [-10] * 8}},
                     {"cpu": {"co_per_core": [-10] * 8, "soc_v": 1.2}, "incidents": [("whea_corrected", {"whea": {"unit": "soc"}})]}])
        r = oa.next_step(con)
        self.assertEqual(r["phase"], "rollback")
        self.assertIn("EXPO", r["step"]["title"])
        self.assertIn("1.30", r["step"]["value"])

    def test_reset_after_tuning_rolls_back_instead_of_blocking(self):
        con = world([{"gpu": {"voltage_offset_mv": -30}}])
        add_incident(con, NOW - 3600, "unclean_shutdown")
        r = oa.next_step(con)
        self.assertEqual(r["phase"], "rollback")
        self.assertFalse(r["blocked"])


class ReadOnlyTest(unittest.TestCase):
    def test_no_writes(self):
        scenarios = [new_db(), world(), world([{"gpu": {"voltage_offset_mv": -30}}]),
                     world([{"gpu": {"voltage_offset_mv": -30}, "incidents": [("gpu_driver", {})]}])]
        add_incident(scenarios[1], NOW - DAY, "dwm_stall")
        for con in scenarios:
            con.execute("INSERT OR IGNORE INTO samples(ts) VALUES (?)", (BASE,))
            con.commit()
            before = tables(con)
            changes = con.total_changes
            oa.next_step(con)
            self.assertEqual(tables(con), before)
            self.assertEqual(con.total_changes, changes)


if __name__ == "__main__":
    unittest.main()
