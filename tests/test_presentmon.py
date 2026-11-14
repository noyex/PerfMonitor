import io
import time
import sqlite3
import unittest
from unittest import mock

from perfmon import db, present
from perfmon import presentmon as pmn

V2_HEADER = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,"
             "PresentMode,FrameType,CPUStartTime,FrameTime,CPUBusy,CPUWait,GPULatency,GPUTime,GPUBusy,GPUWait,"
             "DisplayLatency,DisplayedTime,AnimationError,AnimationTime,ClickToPhotonLatency,AllInputToPhotonLatency")


def v2_row(t_ms, ft, mode="Hardware: Independent Flip", ftype="Application", displayed="6.9", click="NA",
           tearing=1, sync=0):
    return (f"game.exe,1,0x1,DXGI,{sync},512,{tearing},{mode},{ftype},{t_ms},{ft},{ft * 0.6},0.1,1,{ft * 0.9},"
            f"{ft * 0.9},0,12,{displayed},0,0,{click},NA")


# exactly what the installed PresentMon 2.6 writes (data/collector.log)
PM26_HEADER = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,"
               "PresentMode,FrameType,TimeInMs,MsBetweenSimulationStart,MsBetweenPresents,MsBetweenDisplayChange,"
               "MsInPresentAPI,MsRenderPresentLatency,MsUntilDisplayed,CPUStartTimeInMs,MsBetweenAppStart,MsCPUBusy,"
               "MsCPUWait,MsGPULatency,MsGPUTime,MsGPUBusy,MsGPUWait,MsAnimationError,AnimationTime,MsFlipDelay,"
               "MsAllInputToPhotonLatency,MsClickToPhotonLatency")


def pm26_row(t_ms, ft, cpu, gpu, ftype="Application", until="8.5", disp_ft=None, anim="-0.5"):
    """One 2.6 row; generated frames get 0 CPU/GPU timing, as PresentMon 2.x writes them."""
    gen = ftype not in ("Application", "NotSet")
    c, g = (0, 0) if gen else (cpu, gpu)
    disp = "NA" if until == "NA" else (disp_ft if disp_ft is not None else ft)
    return (f"cod.exe,1,0x1,DXGI,0,512,1,Hardware: Independent Flip,{ftype},{t_ms},{ft},{ft},{disp},0.2,9,"
            f"{until},{t_ms - c},{0 if gen else ft},{c},{max(0, ft - c)},1,{g + 0.5},{g},0.5,{anim},{t_ms},0.1,NA,NA")


def pm26_csv(rows):
    return FakeProc(PM26_HEADER + "\n" + "\n".join(rows) + "\n")


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
    c._init_stats()
    c.offset = c.time_unit = c.prev_t = c.error = c.fields = None
    c.lines, c.stopped, c.track_frame_type = 0, False, True
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


class PresentMon26Test(unittest.TestCase):
    """Replays rows in the installed PresentMon 2.6 format through the parser and the per-second aggregation."""

    def run_rows(self, rows):
        c = capture()
        c.offset = 1000.0   # anchored: a replay arrives at once, live rows arrive as they happen
        c._read(pm26_csv(rows))
        c._flush()
        return c

    def test_native_gpu_bound_seconds(self):
        rows = [pm26_row(i * 10.0, 10.0, cpu=6.0, gpu=9.5) for i in range(250)]   # 2.5 s at 100 fps
        rows[3] = pm26_row(30.0, 10.0, cpu=6.0, gpu=9.5, until="NA")             # never shown
        c = self.run_rows(rows)
        self.assertEqual([s["frames"] for s in c.seconds], [100, 100, 50])   # TimeInMs places rows, not arrival
        sec = c.seconds[0]
        self.assertEqual(sec["bound"], "GPU")
        self.assertAlmostEqual(sec["gpu_busy"], 9.5)
        self.assertAlmostEqual(sec["cpu_busy"], 6.0)
        self.assertAlmostEqual(sec["cpu_wait"], 4.0)
        self.assertAlmostEqual(sec["gpu_wait"], 0.5)
        self.assertEqual(sec["dropped"], 1)
        self.assertEqual(sec["app_frames"], 100)
        self.assertAlmostEqual(sec["app_fps"], sec["fps"])
        self.assertAlmostEqual(sec["anim_err"], 0.5)
        self.assertAlmostEqual(sec["disp_ft_sd"], 0.0)
        self.assertAlmostEqual(sec["flip_delay"], 0.1)
        summ = c.summary()
        self.assertEqual(summ["app_fps_avg"], summ["fps_avg"])
        self.assertIsNone(summ["frame_gen_pct"] or None)
        self.assertEqual(summ["pacing"]["anim_err_ms"], 0.5)
        self.assertEqual(c.stutters, [])

    def test_frame_generation_counts_application_frames(self):
        # AFMF 2x: 200 presented frames/s, 100 rendered by the game; CPU and GPU busy 6 ms of a 10 ms app frame
        rows = [pm26_row(i * 5.0, 5.0, cpu=6.0, gpu=6.0, ftype="Application" if i % 2 else "AMD_AFMF")
                for i in range(400)]
        c = self.run_rows(rows)
        sec = c.seconds[0]
        self.assertAlmostEqual(sec["fps"], 200.0)
        self.assertAlmostEqual(sec["app_fps"], 100.0)
        self.assertEqual(sec["app_frames"], 100)
        self.assertAlmostEqual(sec["frame_gen_pct"], 50.0)
        self.assertAlmostEqual(sec["cpu_busy"], 6.0)    # the generated rows' zeros are not averaged in
        self.assertEqual(sec["bound"], "limit")         # 6 of 10 ms; against the 5 ms displayed frame it read GPU
        summ = c.summary()
        self.assertEqual(summ["fps_avg"], 200.0)
        self.assertEqual(summ["app_fps_avg"], 100.0)
        self.assertEqual(summ["frame_gen_pct"], 50.0)

    def test_stutter_on_application_frame_with_context(self):
        rows, t = [], 0.0
        for i in range(200):
            gen = i % 2 == 1
            ft = 5.0
            if i == 151:          # the game is 40 ms late: the generated frame waits for it
                ft = 45.0
            t += ft
            rows.append(pm26_row(t, ft, cpu=38.0 if i == 152 else 4.0, gpu=4.0,
                                 ftype="AMD_AFMF" if gen else "Application"))
        c = self.run_rows(rows)
        self.assertEqual(len(c.stutters), 1)
        st = c.stutters[0]
        self.assertAlmostEqual(st["ft_ms"], 50.0)        # 45 ms gap + 5 ms: one application frame
        self.assertAlmostEqual(st["median_ms"], 10.0)
        self.assertEqual(st["generated"], 1)
        self.assertEqual(st["side"], "cpu")
        self.assertAlmostEqual(st["cpu_busy"], 38.0)
        self.assertAlmostEqual(st["anim_err"], 0.5)

    def test_presentation_side_stutter(self):
        rows, t = [], 0.0
        for i in range(100):
            ft = 60.0 if i == 80 else 10.0
            t += ft
            rows.append(pm26_row(t, ft, cpu=5.0, gpu=5.0))
        c = self.run_rows(rows)
        self.assertEqual([s["side"] for s in c.stutters], ["present"])
        self.assertEqual(c.stutters[0]["generated"], 0)

    def test_no_display_tracking_is_not_all_dropped(self):
        c = self.run_rows([pm26_row(i * 10.0, 10.0, cpu=5, gpu=5, until="NA") for i in range(50)])
        self.assertIsNone(c.seconds[0]["dropped"])


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
