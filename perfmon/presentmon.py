"""Capturing game frames via PresentMon (CSV on stdout) and per-second aggregation."""
import csv
import io
import logging
import statistics
import subprocess
import threading
import time
from array import array
from collections import Counter, deque

log = logging.getLogger("perfmon.presentmon")

CREATE_NO_WINDOW = 0x08000000


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
    """One instance per game session. Calls on_second(row) and on_stutter(row) from the reading thread."""

    def __init__(self, exe_path: str, game_exe: str, pid: int, session_id: int, on_second, on_stutter):
        self.session_id = session_id
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
        args = [exe_path, "--process_id", str(pid), "--output_stdout", "--no_console_stats",
                "--session_name", f"PerfMon_{session_id}", "--stop_existing_session",
                "--terminate_on_proc_exit"]
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.proc = None
            self.error = str(e)
            log.warning("PresentMon start: %s", e)
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"pm-{game_exe}")
        self.thread.start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _stderr(self):
        try:
            for line in io.TextIOWrapper(self.proc.stderr, encoding="utf-8", errors="replace"):
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
        reader = csv.DictReader(io.TextIOWrapper(self.proc.stdout, encoding="utf-8", errors="replace"))
        try:
            for row in reader:
                self.lines += 1
                ft = _f(row, "FrameTime", "MsBetweenPresents")
                if ft is None or ft <= 0 or ft > 60000:
                    continue
                ts = self._row_time(row, ft)
                frame = dict(
                    ft=ft,
                    cpu=_f(row, "CPUBusy", "MsCPUBusy"),
                    gpu=_f(row, "GPUBusy", "MsGPUActive", "GPUTime"),
                    disp=_f(row, "DisplayLatency", "MsUntilDisplayed"),
                    mode=row.get("PresentMode") or "",
                )
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
        self._flush()

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
        self.on_second(dict(
            ts=float(self.bucket_sec), session=self.session_id, frames=len(fts),
            fps=1000.0 * len(fts) / total if total else None, ft_avg=ft_avg,
            ft_p99=percentile(fts, 0.99), ft_max=fts[-1], cpu_busy=cpu, gpu_busy=gpu,
            display_lat=avg("disp"), present_mode=Counter(f["mode"] for f in b).most_common(1)[0][0],
            bound=bound,
        ))

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
        }

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(5)
            except Exception:
                pass
