import json
import sqlite3
import unittest

from perfmon import analyze as an
from perfmon import procwatch as pw

CFG = {"proc_events_ignore": pw.DEFAULT_IGNORE}


def snap(*procs):
    """snap((pid, create_time, name, ppid), ...) -> {pid: (create_time, name, ppid)}"""
    return {p[0]: (p[1], p[2], p[3]) for p in procs}


class ProcWatchTest(unittest.TestCase):
    def setUp(self):
        self.pwatch = pw.ProcWatch(CFG, exe_fn=lambda pid: f"C:\\x\\{pid}.exe")
        self.base = snap((4, 1.0, "System", 0), (100, 10.0, "explorer.exe", 4))

    def test_baseline_emits_nothing(self):
        self.assertEqual(self.pwatch.update(1000, self.base, None), [])
        self.assertEqual(self.pwatch.update(1005, self.base, None), [])

    def test_start_and_stop(self):
        self.pwatch.update(1000, self.base, None)
        s2 = dict(self.base)
        s2[200] = (1002.0, "TiWorker.exe", 100)
        rows = self.pwatch.update(1005, s2, None)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["action"], r["name"], r["pid"], r["ppid"], r["parent_name"], r["session"], r["path"]),
                         ("start", "TiWorker.exe", 200, 100, "explorer.exe", None, None))
        self.assertEqual(r["ts"], 1002.0)  # create_time inside the interval is used
        del s2[200]
        rows = self.pwatch.update(1010, s2, None)
        self.assertEqual([(r["action"], r["name"], r["ts"], r["parent_name"]) for r in rows],
                         [("stop", "TiWorker.exe", 1010, "explorer.exe")])

    def test_start_ts_clamped(self):
        self.pwatch.update(1000, self.base, None)
        s2 = dict(self.base)
        s2[200] = (500.0, "old.exe", 100)   # create_time before the previous snapshot (was inaccessible before)
        s2[201] = (2000.0, "future.exe", 100)  # clock skew
        ts = {r["name"]: r["ts"] for r in self.pwatch.update(1005, s2, None)}
        self.assertEqual(ts, {"old.exe": 1000, "future.exe": 1005})

    def test_pid_reuse_is_stop_plus_start(self):
        s1 = snap((100, 10.0, "explorer.exe", 4), (300, 900.0, "a.exe", 100))
        self.pwatch.update(1000, s1, None)
        s2 = snap((100, 10.0, "explorer.exe", 4), (300, 1003.0, "b.exe", 100))  # same pid, new create_time
        rows = self.pwatch.update(1005, s2, None)
        self.assertEqual(sorted((r["action"], r["name"]) for r in rows), [("start", "b.exe"), ("stop", "a.exe")])

    def test_ignore_outside_session_only(self):
        self.pwatch.update(1000, self.base, None)
        s2 = dict(self.base)
        for i, n in enumerate(("conhost.exe", "SVCHOST.EXE", "cmd.exe", "tool.exe")):
            s2[200 + i] = (1002.0, n, 100)
        self.assertEqual([r["name"] for r in self.pwatch.update(1005, s2, None)], ["tool.exe"])
        # in a session: only conhost/dllhost/WmiPrvSE are dropped; svchost/cmd are recorded, with exe path
        w = pw.ProcWatch(CFG, exe_fn=lambda pid: f"C:\\x\\{pid}.exe")
        w.update(1000, self.base, 7)
        s3 = dict(self.base)
        for i, n in enumerate(("conhost.exe", "svchost.exe", "cmd.exe", "WmiPrvSE.exe", "dllhost.exe")):
            s3[200 + i] = (1002.0, n, 100)
        rows = w.update(1005, s3, 7)
        self.assertEqual(sorted(r["name"] for r in rows), ["cmd.exe", "svchost.exe"])
        self.assertTrue(all(r["session"] == 7 and r["path"] == f"C:\\x\\{r['pid']}.exe" for r in rows))

    def test_stop_has_no_path_in_session(self):
        w = pw.ProcWatch(CFG, exe_fn=lambda pid: "p")
        s1 = dict(self.base)
        s1[200] = (900.0, "x.exe", 100)
        w.update(1000, s1, 3)
        rows = w.update(1005, self.base, 3)
        self.assertEqual([(r["action"], r["path"]) for r in rows], [("stop", None)])

    def test_parent_name_after_parent_exit(self):
        s1 = dict(self.base)
        s1[150] = (900.0, "launcher.exe", 100)
        self.pwatch.update(1000, s1, None)
        # child starts and the parent exits within the same interval: parent name comes from the previous snapshot
        s2 = dict(self.base)
        s2[200] = (1002.0, "child.exe", 150)
        rows = self.pwatch.update(1005, s2, None)
        by = {r["name"]: r for r in rows}
        self.assertEqual(by["child.exe"]["parent_name"], "launcher.exe")
        self.assertEqual(by["launcher.exe"]["parent_name"], "explorer.exe")

    def test_parent_pid_reused_is_not_matched(self):
        self.pwatch.update(1000, self.base, None)
        s2 = dict(self.base)
        s2[100] = (1001.0, "other.exe", 4)       # pid 100 got reused AFTER the child's parent was created
        s2[200] = (500.0, "child.exe", 100)      # child older than the current pid-100 owner
        rows = {r["name"]: r for r in self.pwatch.update(1005, s2, None)}
        self.assertEqual(rows["child.exe"]["parent_name"], "explorer.exe")  # falls back to the previous snapshot

    def test_burst_is_dropped(self):
        w = pw.ProcWatch({"proc_events_burst": 5}, exe_fn=lambda pid: None)
        w.update(1000, self.base, None)
        s2 = dict(self.base)
        for i in range(10):
            s2[300 + i] = (1002.0, f"p{i}.exe", 100)
        self.assertEqual(w.update(1005, s2, None), [])
        self.assertEqual(w.update(1010, s2, None), [])  # and the new snapshot is the baseline


def defender_msg(scan_id, extra=""):
    """Message as EventReader builds it: localized text + [named EventData fields]."""
    return ("Skanowanie programu antywirusowego Microsoft Defender zostało rozpoczęte. [Product Name=Program antywirusowy "
            f"Microsoft Defender; Product Version=4.18.26090.9; Scan ID={scan_id}; Scan Type Index=1; "
            f"Scan Type=Narzędzia chroniące przed złośliwym oprogramowaniem; Scan Parameters Index=1{extra}]")


GUID = "{3D71D276-4300-4F43-9E62-56152E77D8BA}"


def dev(eid, **kw):
    e = dict(ts=1.0, log="Microsoft-Windows-Windows Defender/Operational", provider="Microsoft-Windows-Windows Defender",
             event_id=eid, level=4, record_id=1, message=defender_msg(GUID))
    e.update(kw)
    return e


class DefenderTest(unittest.TestCase):
    def test_start_end_stop_pair_by_scan_id(self):
        self.assertEqual(pw.defender_scan(dev(1000)), ("start", GUID))
        self.assertEqual(pw.defender_scan(dev(1001)), ("end", GUID))
        self.assertEqual(pw.defender_scan(dev(1002, level=3)), ("end", GUID))

    def test_other_events_ignored(self):
        for eid in (1116, 1117, 1150, 5007):
            self.assertIsNone(pw.defender_scan(dev(eid)))
        self.assertIsNone(pw.defender_scan(dev(1000, provider="Disk", log="System")))
        self.assertIsNone(pw.defender_scan({"event_id": "x"}))

    def test_data_list_and_json(self):
        vals = ["Program antywirusowy Microsoft Defender", "4.18.26090.9", GUID, "1", "Narzędzia", "2", "Pełne skanowanie"]
        for data in (vals, json.dumps(vals)):
            i = pw.defender_scan_info(dev(1000, message="", data=data))
            self.assertEqual((i["id"], i["type_index"], i["params_index"]), (GUID, "1", "2"))

    def test_data_dict(self):
        i = pw.defender_scan_info(dev(1001, message="", data={"Scan ID": GUID, "Scan Type Index": "1"}))
        self.assertEqual(pw.defender_scan(dev(1001, message="", data={"Scan ID": GUID})), ("end", GUID))
        self.assertEqual(i["id"], GUID)

    def test_fallback_without_scan_id(self):
        self.assertEqual(pw.defender_scan(dev(1000, message="[Scan Type Index=2]")), ("start", "type2"))
        self.assertEqual(pw.defender_scan(dev(1000, message="Skanowanie rozpoczęte")), ("start", "?"))

    def test_params_from_message(self):
        self.assertEqual(pw.defender_scan_info(dev(1000, message=defender_msg(GUID).replace("Parameters Index=1",
                                                                                           "Parameters Index=2")))["params_index"], "2")


def make_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE proc_events(ts REAL, pid INTEGER, name TEXT, ppid INTEGER, parent_name TEXT, action TEXT, session INTEGER, path TEXT);
    CREATE TABLE procs(ts REAL, kind TEXT, name TEXT, pid INTEGER, cpu_pct REAL, mem_mb REAL, io_mbs REAL, gpu_pct REAL, vram_mb REAL);
    CREATE TABLE incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, end_ts REAL, kind TEXT, severity TEXT, title TEXT, details TEXT);
    CREATE TABLE stutters(ts REAL, session INTEGER, ft_ms REAL, median_ms REAL, causes TEXT);
    """)
    return con


def ev(con, ts, name, action="start", parent="explorer.exe", session=1, path=None, pid=1):
    con.execute("INSERT INTO proc_events VALUES (?,?,?,?,?,?,?,?)", (ts, pid, name, 0, parent, action, session, path))


def proc(con, ts, name, kind="cpu", pct=50):
    con.execute("INSERT INTO procs VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, kind, name, 1, pct if kind == "cpu" else None, 0, 0, pct if kind == "gpu" else None, 0))


T0, T1 = 10_000.0, 12_000.0


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.con = make_db()
        c = self.con
        ev(c, T0 + 100, "TiWorker.exe", path="C:\\Windows\\TiWorker.exe")        # culprit, not seen heavy
        ev(c, T0 + 200, "randomtool.exe")                                         # not culprit, becomes heavy
        proc(c, T0 + 205, "RandomTool.exe", "cpu", 40)
        ev(c, T0 + 300, "quiet.exe")                                              # neither
        proc(c, T0 + 305, "quiet.exe", "cpu", 2)
        ev(c, T0 + 400, "lateheavy.exe")                                          # heavy only after 60 s -> not heavy
        proc(c, T0 + 470, "lateheavy.exe", "cpu", 90)
        ev(c, T0 + 500, "Game.exe")                                               # the game itself
        ev(c, T0 + 501, "EACHelper.exe", parent="Game.exe")                       # game's own helper
        proc(c, T0 + 505, "EACHelper.exe", "cpu", 80)
        ev(c, T0 + 600, "gpuhog.exe")
        proc(c, T0 + 610, "gpuhog.exe", "gpu", 70)
        ev(c, T0 + 600, "OneDrive.exe")
        ev(c, T0 + 610, "TiWorker.exe", action="stop")
        ev(c, T0 + 700, "TiWorker.exe", path="C:\\Windows\\TiWorker.exe")
        c.execute("INSERT INTO incidents(ts,end_ts,kind,severity,title) VALUES (?,?,?,?,?)",
                  (T0 + 1000, T0 + 1100, "defender_scan", "info", "scan"))
        c.execute("INSERT INTO incidents(ts,end_ts,kind,severity,title) VALUES (?,?,?,?,?)",
                  (T0 + 1500, None, "defender_scan", "info", "open scan"))
        c.execute("INSERT INTO incidents(ts,end_ts,kind,severity,title) VALUES (?,?,?,?,?)",
                  (T0 + 1300, T0 + 1310, "freeze", "serious", "other kind"))
        c.commit()
        self.sc = pw.StartCauses(c, T0, T1, "Game.exe", pw.DEFAULT_CULPRITS)

    def test_culprit_start_in_window(self):
        self.assertEqual(self.sc.causes(T0 + 103), ["background_start:TiWorker.exe"])
        self.assertEqual(self.sc.causes(T0 + 115), ["background_start:TiWorker.exe"])
        self.assertEqual(self.sc.causes(T0 + 116), [])       # > 15 s after the start
        self.assertEqual(self.sc.causes(T0 + 99), ["background_start:TiWorker.exe"])  # up to 2 s before (coarse ts)
        self.assertEqual(self.sc.causes(T0 + 97), [])

    def test_heavy_non_culprit(self):
        self.assertEqual(self.sc.causes(T0 + 210), ["background_start:randomtool.exe"])
        self.assertEqual(self.sc.causes(T0 + 310), [])        # quiet process
        self.assertEqual(self.sc.causes(T0 + 410), [])        # heavy only after the 60 s window
        self.assertEqual(self.sc.causes(T0 + 615), ["background_start:gpuhog.exe", "background_start:OneDrive.exe"])

    def test_game_and_its_children_excluded(self):
        self.assertEqual(self.sc.causes(T0 + 505), [])

    def test_max_two_strongest_first(self):
        ev(self.con, T0 + 604, "extra.exe")
        proc(self.con, T0 + 606, "extra.exe", "cpu", 20)
        ev(self.con, T0 + 604, "culprit2.exe")
        sc = pw.StartCauses(self.con, T0, T1, "Game.exe", pw.DEFAULT_CULPRITS + ["culprit2.exe"])
        # gpuhog 70, extra 20, OneDrive 0, culprit2 0 -> top 2
        self.assertEqual(sc.causes(T0 + 615), ["background_start:gpuhog.exe", "background_start:extra.exe"])

    def test_stop_events_are_not_causes(self):
        self.assertEqual(self.sc.causes(T0 + 612), ["background_start:gpuhog.exe", "background_start:OneDrive.exe"])
        self.assertNotIn("background_start:TiWorker.exe", self.sc.causes(T0 + 612))

    def test_defender_scan_cause(self):
        self.assertEqual(self.sc.causes(T0 + 1050), ["background_scan:defender"])
        self.assertEqual(self.sc.causes(T0 + 1099), ["background_scan:defender"])
        self.assertEqual(self.sc.causes(T0 + 1300), [])
        # open-ended scan covers until session end
        self.assertEqual(self.sc.causes(T0 + 1900), ["background_scan:defender"])

    def test_case_insensitive_culprits(self):
        sc = pw.StartCauses(self.con, T0, T1, "game.exe", ["TIWORKER.EXE"])
        self.assertEqual(sc.causes(T0 + 103), ["background_start:TiWorker.exe"])

    def test_missing_tables(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        sc = pw.StartCauses(con, T0, T1, "Game.exe")
        self.assertEqual(sc.causes(T0 + 5), [])
        self.assertEqual(pw.session_proc_events(con, T0, T1, "Game.exe"), {"starts": [], "culprits": [], "defender_scans": []})
        self.assertEqual(pw.events_list(con, T0, T1), [])

    def test_session_proc_events(self):
        r = pw.session_proc_events(self.con, T0, T1, "Game.exe", pw.DEFAULT_CULPRITS)
        names = [s["name"] for s in r["starts"]]
        self.assertNotIn("Game.exe", names)
        self.assertIn("quiet.exe", names)
        self.assertEqual(r["starts"][0], {"time": an.fmt_ts(T0 + 100), "name": "TiWorker.exe", "parent_name": "explorer.exe",
                                          "path": "C:\\Windows\\TiWorker.exe"})
        cul = {c["name"]: c for c in r["culprits"]}
        self.assertEqual(sorted(cul), ["OneDrive.exe", "TiWorker.exe", "gpuhog.exe", "randomtool.exe"])
        self.assertEqual(cul["TiWorker.exe"]["starts"], 2)
        self.assertEqual(cul["TiWorker.exe"]["first_time"], an.fmt_ts(T0 + 100))
        self.assertEqual(cul["TiWorker.exe"]["parent_name"], "explorer.exe")
        self.assertEqual(cul["randomtool.exe"]["heavy_pct"], 40.0)
        self.assertEqual(r["defender_scans"], [
            {"start_time": an.fmt_ts(T0 + 1000), "end_time": an.fmt_ts(T0 + 1100), "duration_s": 100},
            {"start_time": an.fmt_ts(T0 + 1500), "end_time": None, "duration_s": None}])

    def test_starts_capped(self):
        for i in range(50):
            ev(self.con, T0 + 800 + i, f"p{i}.exe")
        r = pw.session_proc_events(self.con, T0, T1, "Game.exe", pw.DEFAULT_CULPRITS)
        self.assertEqual(len(r["starts"]), 30)

    def test_events_list(self):
        r = pw.events_list(self.con, T0 + 90, T0 + 620)
        self.assertEqual(r[0], {"ts": T0 + 100, "time": an.fmt_ts(T0 + 100), "action": "start", "name": "TiWorker.exe",
                                "pid": 1, "parent_name": "explorer.exe", "session": 1})
        self.assertEqual(r[-1]["action"], "stop")
        self.assertEqual(len(pw.events_list(self.con, T0, T1, limit=3)), 3)


class FindingsTest(unittest.TestCase):
    def rep(self, culprits, scans, cc):
        return {"proc_events": {"starts": [], "culprits": culprits, "defender_scans": scans},
                "stutters": {"cause_counts": cc}}

    def test_nothing(self):
        self.assertEqual(pw.findings({}), [])
        self.assertEqual(pw.findings(self.rep([], [], {"disk": 5})), [])

    def test_warning_with_scan_and_culprits(self):
        f = pw.findings(self.rep(
            [{"name": "MsMpEng.exe", "starts": 1}, {"name": "TiWorker.exe", "starts": 2}],
            [{"start_time": "2026-10-27 21:14:03", "end_time": "2026-10-27 21:31:40", "duration_s": 1057}],
            {"background_start:TiWorker.exe": 2, "background_scan:defender": 1, "disk": 4}))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "warning")
        self.assertEqual(f[0]["title"], "Podczas sesji wystartowały procesy w tle")
        self.assertIn("MsMpEng (skan Defendera 21:14-21:31)", f[0]["detail"])
        self.assertIn("TiWorker (2x)", f[0]["detail"])
        self.assertIn("Powiązane stuttery: 3", f[0]["detail"])
        self.assertIn("poza godzinami grania", f[0]["detail"])

    def test_info_when_few_stutters(self):
        f = pw.findings(self.rep([{"name": "OneDrive.exe", "starts": 1}], [], {"background_start:OneDrive.exe": 1}))
        self.assertEqual(f[0]["severity"], "info")
        self.assertIn("OneDrive.", f[0]["detail"] + ".")
        f = pw.findings(self.rep([{"name": "OneDrive.exe", "starts": 1}], [], {}))
        self.assertEqual(f[0]["severity"], "info")
        self.assertNotIn("Powiązane", f[0]["detail"])

    def test_scan_only(self):
        f = pw.findings(self.rep([], [{"start_time": "2026-10-27 18:14:47", "end_time": None, "duration_s": None}],
                                 {"background_scan:defender": 3}))
        self.assertEqual(f[0]["severity"], "warning")
        self.assertIn("skan Defendera (18:14-...)", f[0]["detail"])


if __name__ == "__main__":
    unittest.main()


class ReviewFixesTest(unittest.TestCase):
    def test_start_before_session_and_stale_open_scan(self):
        con = make_db()
        ev(con, T0 - 10, "TiWorker.exe")                                          # 10 s before the session
        con.execute("INSERT INTO incidents(ts,end_ts,kind,severity,title) VALUES (?,?,?,?,?)",
                    (T0 - pw.SCAN_MAX_S - 100, None, "defender_scan", "info", "never closed"))
        sc = pw.StartCauses(con, T0, T1, "Game.exe", pw.DEFAULT_CULPRITS)
        self.assertEqual(sc.causes(T0 + 3), ["background_start:TiWorker.exe"])
        self.assertEqual(sc.causes(T0 + 600), [])                                 # stale open scan ignored
        self.assertEqual(pw.session_proc_events(con, T0, T1, "Game.exe")["defender_scans"], [])
