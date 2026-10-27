"""Capturing game frames via PresentMon (CSV on stdout) and per-second aggregation."""
import csv
import io
import logging
import os
import re
import statistics
import subprocess
import threading
import time
from array import array
from collections import Counter, deque

log = logging.getLogger("perfmon.presentmon")

CREATE_NO_WINDOW = 0x08000000
FALLBACK_WINDOW_S = 15     # PresentMon exiting this soon without rows = it rejected an option (retry without it)
# FrameType values that are not generated frames (AMD AFMF / Intel XeFG / ... are)
NATIVE_FRAME_TYPES = {"", "NotSet", "Unspecified", "Application", "Repeated"}


def stop_etw_session(name):
    """Stop a PresentMon ETW session by name. A PresentMon killed with TerminateProcess leaves its session running;
    after ~10 of them new ones fail with 'error code 1450' (not enough system resources) and no frames are captured."""
    try:
        subprocess.run(["logman", "stop", name, "-ets"], capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        log.debug("stop ETW session %s: %s", name, e)


def cleanup_stale_sessions():
    """Stop PerfMon_<pid>_<n> ETW sessions left behind by collectors that no longer run (called once at start)."""
    try:
        out = subprocess.run(["logman", "query", "-ets"], capture_output=True, text=True, timeout=30,
                             creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return 0
    stale = [m.group(0) for m in re.finditer(r"^PerfMon_(\d+)_\d+(?=\s)", out, re.M)
             if int(m.group(1)) != os.getpid() and not _pid_alive(int(m.group(1)))]
    for name in stale:
        stop_etw_session(name)
    if stale:
        log.info("stopped %d stale PresentMon ETW sessions", len(stale))
    return len(stale)


def _pid_alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return True


def _s(row, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", "NA"):
            return v
    return None


def _mode(values):
    c = Counter(v for v in values if v is not None)
    return c.most_common(1)[0][0] if c else None


def _avg(values):
    v = [x for x in values if x is not None]
    return sum(v) / len(v) if v else None


def _f(row, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", "NA"):
            try:
                return float(v)
            except ValueError:
                pass
    return None


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


class FrameCapture:
    """One instance per game session. Calls on_second(row) and on_stutter(row) from the reading thread.

    The target is a set of PIDs (the game and its child game processes, e.g. cod.exe -> sp26-cod.exe, which is the one
    that actually presents); retarget() restarts PresentMon when the set changes."""

    def __init__(self, exe_path: str, game_exe: str, pid: int, session_id: int, on_second, on_stutter):
        self.session_id = session_id
        self.exe_path = exe_path
        self.pids = [pid]
        self.retarget_pending = False
        self.session_name = f"PerfMon_{os.getpid()}_{session_id}"
        self.on_second = on_second
        self.on_stutter = on_stutter
        self.all_ft = array("f")       # all session frametimes (for 1% / 0.1% low)
        self.recent = deque(maxlen=120)
        self.bucket = []
        self.bucket_sec = None
        self.offset = None             # wall_clock - PresentMon relative time
        self.time_unit = None
        self.prev_t = None
        self.error = None
        self.lines = 0
        self.stopped = False
        self.track_frame_type = True
        self.fields = None
        # whole-capture present stats for summary()["present"] (frame counts and sums)
        self.p_modes, self.p_runtime, self.p_sync = Counter(), Counter(), Counter()
        self.p = Counter()
        self.p_lock = threading.Lock()
        self.proc = None
        if not self._spawn():
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"pm-{game_exe}")
        self.thread.start()

    def _args(self):
        pid_args = [a for p in self.pids for a in ("--process_id", str(p))]
        return [self.exe_path, *pid_args, "--output_stdout", "--no_console_stats",
                # the PID keeps a test collector (own DB, ids from 1) from stopping the production capture
                "--session_name", self.session_name, "--stop_existing_session",
                "--terminate_on_proc_exit"] + (["--track_frame_type"] if self.track_frame_type else [])

    def _spawn(self):
        args = self._args()
        try:
            self.started = time.time()
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.proc = None
            self.error = str(e)
            log.warning("PresentMon start: %s", e)
            return False
        threading.Thread(target=self._stderr, args=(self.proc,), daemon=True).start()
        return True

    def _stderr(self, proc):
        try:
            for line in io.TextIOWrapper(proc.stderr, encoding="utf-8", errors="replace"):
                line = line.strip()
                if line:
                    log.info("PresentMon: %s", line[:300])
                    if "error" in line.lower() or "access" in line.lower():
                        self.error = line[:300]
        except Exception:
            pass

    def _row_time(self, row, ft):
        t = _f(row, "CPUStartTime", "TimeInSeconds", "CPUStartQPCTime")
        if t is None:
            return time.time()
        if self.time_unit is None and self.prev_t is not None and ft:
            d = t - self.prev_t
            if d > 0:
                self.time_unit = 1000.0 if abs(d - ft) < abs(d * 1000 - ft) else 1.0
        self.prev_t = t
        rel = t / (self.time_unit or 1000.0)
        cand = time.time() - rel
        # smallest delivery delay = best anchor
        self.offset = cand if self.offset is None else min(self.offset, cand)
        return self.offset + rel

    def _run(self):
        while True:
            proc = self.proc
            self._read(proc)
            try:
                rc = proc.wait(5)
            except Exception:
                rc = None
            if self.retarget_pending and not self.stopped:
                self.retarget_pending = False
                self._flush()
                self.offset = self.prev_t = self.time_unit = None   # PresentMon time restarts with the new trace
                log.info("PresentMon retarget -> pids %s", self.pids)
                if self._spawn():
                    continue
                break
            # a PresentMon that does not know --track_frame_type exits at once with a usage error and no rows
            if (self.stopped or not self.track_frame_type or self.lines or not rc
                    or time.time() - self.started > FALLBACK_WINDOW_S):
                break
            log.warning("PresentMon exited (%s) without rows, retrying without --track_frame_type", rc)
            self.track_frame_type = False
            self.error = None
            if not self._spawn():
                break
            if self.stopped:   # stop() ran while spawning and only saw the old process
                self.stop()
                break
        self._flush()
        if self.stopped:
            stop_etw_session(self.session_name)

    def _read(self, proc):
        reader = csv.DictReader(io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace"))
        try:
            for row in reader:
                if self.fields is None:
                    self.fields = set(reader.fieldnames or ())
                    log.info("PresentMon columns: %s", ",".join(reader.fieldnames or ()))
                self.lines += 1
                ft = _f(row, "FrameTime", "MsBetweenPresents")
                if ft is None or ft <= 0 or ft > 60000:
                    continue
                ts = self._row_time(row, ft)
                frame = self._frame(row, ft)
                self.all_ft.append(ft)
                self._stutter_check(ts, ft)
                self.recent.append(ft)
                sec = int(ts)
                if self.bucket_sec is None:
                    self.bucket_sec = sec
                if sec != self.bucket_sec:
                    self._flush()
                    self.bucket_sec = sec
                self.bucket.append(frame)
        except Exception as e:
            log.warning("PresentMon read: %s", e)

    def _frame(self, row, ft):
        """Per-frame fields; a column this PresentMon version does not write stays None."""
        f = self.fields
        displayed = None
        if "DisplayedTime" in f:   # v2: NA / 0 = the frame never reached the screen
            displayed = (_f(row, "DisplayedTime") or 0) > 0
        elif "Dropped" in f:       # v1
            displayed = _f(row, "Dropped") != 1
        sync, tear = _f(row, "SyncInterval"), _f(row, "AllowsTearing")
        return dict(
            ft=ft,
            cpu=_f(row, "CPUBusy", "MsCPUBusy"),
            gpu=_f(row, "GPUBusy", "MsGPUActive", "GPUTime"),
            disp=_f(row, "DisplayLatency", "MsUntilDisplayed"),
            mode=row.get("PresentMode") or "",
            runtime=_s(row, "PresentRuntime", "Runtime"),
            sync=int(sync) if sync is not None else None,
            tearing=int(tear) if tear is not None else None,
            displayed=displayed,
            click=_f(row, "ClickToPhotonLatency", "MsClickToPhotonLatency"),
            input=_f(row, "AllInputToPhotonLatency", "MsAllInputToPhotonLatency"),
            ftype=row.get("FrameType") if "FrameType" in f else None,
        )

    def _stutter_check(self, ts, ft):
        if len(self.recent) < 30:
            return
        med = statistics.median(self.recent)
        if ft > 25 and ft > 2.5 * med and ft - med > 12:
            self.on_stutter(dict(ts=ts, session=self.session_id, ft_ms=ft, median_ms=med))

    def _flush(self):
        b = self.bucket
        self.bucket = []
        if not b:
            return
        fts = sorted(f["ft"] for f in b)
        total = sum(fts)
        avg = lambda k: (sum(f[k] for f in b if f[k] is not None) / max(1, sum(1 for f in b if f[k] is not None))) if any(f[k] is not None for f in b) else None
        ft_avg = total / len(fts)
        cpu, gpu = avg("cpu"), avg("gpu")
        bound = None
        if gpu is not None and cpu is not None and ft_avg > 0:
            if gpu >= 0.85 * ft_avg:
                bound = "GPU"
            elif cpu >= 0.85 * ft_avg:
                bound = "CPU"
            else:
                bound = "limit"  # neither CPU nor GPU is busy the full time: FPS limit / vsync / waiting
        disp = [f["displayed"] for f in b if f["displayed"] is not None]
        typed = [f["ftype"] for f in b if f["ftype"] is not None]
        gen = sum(1 for t in typed if t not in NATIVE_FRAME_TYPES)
        clicks = [f["click"] for f in b if f["click"] is not None]
        inputs = [f["input"] for f in b if f["input"] is not None]
        tear = [f["tearing"] for f in b if f["tearing"] is not None]
        self.on_second(dict(
            ts=float(self.bucket_sec), session=self.session_id, frames=len(fts),
            fps=1000.0 * len(fts) / total if total else None, ft_avg=ft_avg,
            ft_p99=percentile(fts, 0.99), ft_max=fts[-1], cpu_busy=cpu, gpu_busy=gpu,
            display_lat=avg("disp"), present_mode=Counter(f["mode"] for f in b).most_common(1)[0][0],
            bound=bound,
            runtime=_mode(f["runtime"] for f in b), sync_interval=_mode(f["sync"] for f in b),
            tearing=_mode(tear), dropped=sum(1 for d in disp if not d) if disp else None,
            click_lat=_avg(clicks), input_lat=_avg(inputs),
            frame_gen_pct=100.0 * gen / len(typed) if typed else None,
        ))
        with self.p_lock:   # summary() reads these from the collector thread
            self.p_modes.update(f["mode"] for f in b if f["mode"])
            self.p_runtime.update(f["runtime"] for f in b if f["runtime"])
            self.p_sync.update(f["sync"] for f in b if f["sync"] is not None)
            for k, v in (("tear_n", len(tear)), ("tearing", sum(tear)), ("disp_n", len(disp)),
                         ("dropped", sum(1 for d in disp if not d)), ("typed", len(typed)), ("gen", gen),
                         ("click_n", len(clicks)), ("click", sum(clicks)), ("input_n", len(inputs)),
                         ("input", sum(inputs))):
                self.p[k] += v

    def present_summary(self):
        """How the game presented over the whole capture (frame-weighted)."""
        with self.p_lock:
            p, modes = dict(self.p), self.p_modes.most_common()
            runtime, sync = self.p_runtime.most_common(1), self.p_sync.most_common(1)
        nm = sum(c for _, c in modes)
        if not nm:
            return None
        ratio = lambda a, b, k=1: round(100 * p[a] / p[b], k) if p[b] else None
        return {
            "runtime": runtime[0][0] if runtime else None,
            "modes": {m: round(100 * c / nm, 1) for m, c in modes},
            "sync_interval": sync[0][0] if sync else None,
            "tearing_pct": ratio("tearing", "tear_n"),
            "dropped_pct": ratio("dropped", "disp_n", 2),
            "click_lat_avg": round(p["click"] / p["click_n"], 1) if p["click_n"] else None,
            "input_lat_avg": round(p["input"] / p["input_n"], 1) if p["input_n"] else None,
            "frame_gen_pct": ratio("gen", "typed"),
        }

    def summary(self) -> dict:
        fts = sorted(self.all_ft)
        if not fts:
            return {"frames": 0, "error": self.error}
        n = len(fts)
        total = sum(fts)
        p99, p999 = percentile(fts, 0.99), percentile(fts, 0.999)
        return {
            "frames": n,
            "fps_avg": round(1000 * n / total, 1),
            "fps_1low": round(1000 / p99, 1) if p99 else None,
            "fps_01low": round(1000 / p999, 1) if p999 else None,
            "ft_median": round(percentile(fts, 0.5), 2),
            "ft_p99": round(p99, 2),
            "ft_max": round(fts[-1], 1),
            "error": self.error,
            "present": self.present_summary(),
        }

    def retarget(self, pids):
        """Capture exactly these PIDs from now on (restarts PresentMon; a second or two of frames is lost)."""
        pids = sorted(set(pids))
        if not pids or pids == sorted(self.pids) or self.stopped:
            return
        self.pids = pids
        self.retarget_pending = True
        self._kill()

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(5)
            except Exception:
                pass

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        self.stopped = True
        self._kill()
