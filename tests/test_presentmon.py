import io
import time
import sqlite3
import threading
import unittest
from unittest import mock
from collections import Counter, deque
from array import array

from perfmon import db, present
from perfmon import presentmon as pmn

V2_HEADER = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,"
             "PresentMode,FrameType,CPUStartTime,FrameTime,CPUBusy,CPUWait,GPULatency,GPUTime,GPUBusy,GPUWait,"
             "DisplayLatency,DisplayedTime,AnimationError,AnimationTime,ClickToPhotonLatency,AllInputToPhotonLatency")


def v2_row(t_ms, ft, mode="Hardware: Independent Flip", ftype="Application", displayed="6.9", click="NA",
           tearing=1, sync=0):
    return (f"game.exe,1,0x1,DXGI,{sync},512,{tearing},{mode},{ftype},{t_ms},{ft},{ft * 0.6},0.1,1,{ft * 0.9},"
            f"{ft * 0.9},0,12,{displayed},0,0,{click},NA")


class FakeProc:
    def __init__(self, text, rc=0):
        self.stdout = io.BytesIO(text.encode())
        self.stderr = io.BytesIO(b"")
        self.rc = rc

    def wait(self, timeout=None):
        return self.rc

    def poll(self):
        return self.rc

    def terminate(self):
        pass


def capture():
    """FrameCapture without starting PresentMon."""
    c = object.__new__(pmn.FrameCapture)
    c.session_id, c.seconds, c.stutters = 1, [], []
    c.on_second, c.on_stutter = c.seconds.append, c.stutters.append
    c.all_ft, c.recent, c.bucket, c.bucket_sec = array("f"), deque(maxlen=120), [], None
    c.offset = c.time_unit = c.prev_t = c.error = c.fields = None
    c.lines, c.stopped, c.track_frame_type = 0, False, True
    c.p_modes, c.p_runtime, c.p_sync, c.p, c.p_lock = Counter(), Counter(), Counter(), Counter(), threading.Lock()
    c.started = 0
    c.retarget_pending, c.session_name = False, "PerfMon_1_1"
    c.game_exe, c.pid, c.names = "cod.exe", 1, []
    return c


class PresentMonParseTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(pmn, "stop_etw_session")   # would run logman
        self.etw_stop = p.start()
        self.addCleanup(p.stop)

    def test_v2_columns_per_second(self):
        rows = [v2_row(i * 10.0, 10.0) for i in range(100)]       # 1 s of 100 fps
        rows[5] = v2_row(50.0, 10.0, displayed="NA")                # one frame never shown
        rows[7] = v2_row(70.0, 10.0, ftype="AMD_AFMF", click="22.5")
        c = capture()
        c._read(FakeProc(V2_HEADER + "\n" + "\n".join(rows) + "\n"))
        c._flush()
        self.assertIn("FrameType", c.fields)
        sec = c.seconds[0]
        self.assertEqual(sec["runtime"], "DXGI")
        self.assertEqual(sec["sync_interval"], 0)
        self.assertEqual(sec["tearing"], 1)
        self.assertEqual(sec["dropped"], 1)
        self.assertEqual(sec["click_lat"], 22.5)
        self.assertIsNone(sec["input_lat"])
        self.assertAlmostEqual(sec["frame_gen_pct"], 1.0)
        p = c.summary()["present"]
        self.assertEqual(p["modes"], {"Hardware: Independent Flip": 100.0})
        self.assertEqual(p["tearing_pct"], 100.0)
        self.assertEqual(p["dropped_pct"], 1.0)
        self.assertEqual(p["frame_gen_pct"], 1.0)

    def test_missing_columns_stay_none(self):
        text = "Application,ProcessID,PresentMode,CPUStartTime,FrameTime,CPUBusy,GPUBusy\n" + "\n".join(
            f"g.exe,1,Composed: Flip,{i * 16.6},16.6,5,15" for i in range(70)) + "\n"
        c = capture()
        c._read(FakeProc(text))
        c._flush()
        sec = c.seconds[0]
        for k in ("runtime", "sync_interval", "tearing", "dropped", "click_lat", "input_lat", "frame_gen_pct"):
            self.assertIsNone(sec[k], k)
        p = c.summary()["present"]
        self.assertEqual(p["modes"], {"Composed: Flip": 100.0})
        self.assertIsNone(p["frame_gen_pct"])
        self.assertIsNone(p["tearing_pct"])

    def test_fallback_without_track_frame_type(self):
        c = capture()
        good = FakeProc(V2_HEADER + "\n" + "\n".join(v2_row(i * 10.0, 10.0) for i in range(120)) + "\n")
        spawned = []

        def spawn():
            spawned.append(c.track_frame_type)
            c.proc = good
            return True
        c._spawn = spawn
        c.proc = FakeProc("error: unrecognized option --track_frame_type\n", rc=1)
        c.started = time.time()
        c._run()
        self.assertEqual(spawned, [False])
        self.assertFalse(c.track_frame_type)
        self.assertGreater(len(c.seconds), 0)

    def test_targets_pid_until_a_child_appears(self):
        c = capture()
        c.exe_path = "pm.exe"
        a = c._args()
        self.assertEqual(a[a.index("--process_id") + 1], "1")
        self.assertNotIn("--process_name", a)

    def test_retarget_restarts_with_exe_names(self):
        c = capture()
        c.exe_path = "pm.exe"
        c.proc = FakeProc("")      # the first process ends because retarget() terminated it
        argv = []

        def spawn():
            argv.append(c._args())
            c.proc = FakeProc(V2_HEADER + "\n" + "\n".join(v2_row(i * 10.0, 10.0) for i in range(100)) + "\n")
            c.started = time.time()
            return True
        c._spawn = spawn
        c.retarget(["sp26-cod.exe"])
        c.retarget(["sp26-cod.exe"])   # the child respawned with a new pid: same name, no second restart
        c.retarget([])                 # the child exited: its name is kept for the next respawn
        c.proc = FakeProc("")
        c._run()
        self.assertEqual(len(argv), 1)
        a = argv[0]
        self.assertEqual([a[i + 1] for i, x in enumerate(a) if x == "--process_name"], ["cod.exe", "sp26-cod.exe"])
        self.assertNotIn("--process_id", a)
        self.assertEqual(c.seconds[0]["frames"], 100)   # frames of the new trace are kept
        self.assertFalse(c.retarget_pending)
        c.stopped = True
        c.proc = FakeProc("")
        c._run()
        self.etw_stop.assert_called_once_with("PerfMon_1_1")   # a stopped capture must not leak its ETW session

    def test_no_fallback_after_rows_or_stop(self):
        c = capture()
        c._spawn = lambda: self.fail("must not respawn")
        c.proc = FakeProc("", rc=1)
        c.stopped = True
        c._run()


class PresentAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        db.migrate(self.con)
        self.con.row_factory = sqlite3.Row
        rows = [(i, 1, 100, "Hardware: Independent Flip", 1, 0) for i in range(70)] + \
               [(100 + i, 1, 60, "Composed: Flip", 0, 0) for i in range(30)] + \
               [(200 + i, 2, 100, "Composed: Copy with GPU GDI", 0, 2) for i in range(10)]
        self.con.executemany("INSERT INTO frames(ts, session, frames, present_mode, tearing, dropped) "
                             "VALUES (?,?,?,?,?,?)", rows)

    def test_classify(self):
        self.assertEqual(present.classify("Hardware Composed: Independent Flip"), "iflip")
        self.assertEqual(present.classify("Hardware: Legacy Flip"), "fse")
        self.assertEqual(present.classify("Composed: Copy with GPU GDI"), "composed")
        self.assertIsNone(present.classify("Unknown"))

    def test_labels(self):
        self.assertEqual(present.labels(self.con, [1, 2, 3]), {1: "mixed", 2: "Composed"})

    def test_session_present_from_frames_and_finding(self):
        p = present.session_present(self.con, {"id": 1}, {})
        self.assertAlmostEqual(p["composed_pct"], 20.5, places=1)   # 1800 of 8800 frames
        self.assertEqual(p["label"], "mixed")
        self.assertAlmostEqual(p["tearing_pct"], 79.5, places=1)
        f = present.findings({"present": p})
        self.assertEqual(f[0]["severity"], "info")
        self.assertIn("DWM", f[0]["title"])

    def test_summary_present_preferred(self):
        p = present.session_present(self.con, {"id": 1}, {"present": {"modes": {"Hardware: Independent Flip": 99.0,
                                                                               "Composed: Flip": 1.0}}})
        self.assertEqual(p["label"], "IFlip")
        self.assertEqual(present.findings({"present": p}), [])
        self.assertIn("IFlip", present.describe(p))


class StaleSessionCleanupTest(unittest.TestCase):
    def test_localized_logman_output_does_not_crash(self):
        out = "Zbi\xf3r danych\r\nPerfMon_999999_3                Trace    Running\r\n".encode("cp852")
        run = mock.Mock(return_value=mock.Mock(stdout=out))
        with mock.patch.object(pmn.subprocess, "run", run), mock.patch.object(pmn, "_pid_alive", return_value=False),                 mock.patch.object(pmn, "stop_etw_session") as stop:
            self.assertEqual(pmn.cleanup_stale_sessions(), 1)
        stop.assert_called_once_with("PerfMon_999999_3")

    def test_logman_failure_is_swallowed(self):
        with mock.patch.object(pmn.subprocess, "run", side_effect=OSError("no logman")):
            self.assertEqual(pmn.cleanup_stale_sessions(), 0)

if __name__ == "__main__":
    unittest.main()
