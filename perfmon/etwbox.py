"""ETW black box: a kernel trace (DPC/ISR, image loads, disk I/O, processes) kept in a circular in-memory buffer by
WPR and written to data/traces when something goes wrong (user mark, frozen desktop/DWM, DPC storm, stutter burst).

The capture is analysed later in a separate low-priority process (`python -m perfmon.etwbox analyze ID`, also
`pm etw analyze ID`): tracerpt converts the .etl to CSV (no xperf/WPA without the ADK) and the last `dump_last_s`
seconds are aggregated into the top DPC/ISR drivers and disk I/O per process. Needs admin (the collector has it)."""
import bisect
import csv
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import config

log = logging.getLogger("perfmon.etwbox")

CREATE_NO_WINDOW = 0x08000000
IDLE_PRIORITY_CLASS = 0x00000040
INSTANCE = "PerfMonBox"
PROFILE = Path(__file__).parent / "etw" / "perfmon.wprp"
TRACES = config.DATA_DIR / "traces"
REQUEST = config.DATA_DIR / "etw.request"
COOLDOWN_S = 120
ANALYZE_DELAY_S = 30       # let the disk settle after writing the .etl
RETRY_S = 600              # after a failed wpr -start (no admin, another kernel logger, ...)
BURST_WINDOW_S = 10
DPC_TRIGGER_S = 2
MAX_DPC_US = 1_000_000   # no DPC/ISR runs this long, the watchdog would bugcheck


def settings(cfg):
    """cfg["etw"] over the defaults (config.load() replaces the whole dict when the user sets any key)."""
    base = config.DEFAULTS["etw"]
    user = cfg.get("etw") if isinstance(cfg.get("etw"), dict) else {}
    s = {**base, **user}
    s["triggers"] = {**base["triggers"], **(user.get("triggers") or {})}
    return s


def _run(args, timeout=180):
    r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def wpr(*args, timeout=180):
    return _run(["wpr", *args, "-instancename", INSTANCE], timeout)   # -instancename must be the last parameter


def profile_copy(buffer_mb):
    """The repo profile with Buffers = buffer_mb (1 MB buffers), written next to the traces."""
    TRACES.mkdir(parents=True, exist_ok=True)
    xml = re.sub(r'(<Buffers Value=")\d+', rf"\g<1>{max(16, int(buffer_mb))}", PROFILE.read_text(encoding="utf-8"))
    p = TRACES / "perfmon.wprp"
    if not p.exists() or p.read_text(encoding="utf-8") != xml:
        p.write_text(xml, encoding="utf-8")
    return p


def request(action):
    """`pm etw start|stop` -> the collector (wpr needs admin)."""
    config.ensure_dirs()
    with open(REQUEST, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "action": action}) + "\n")


def take_requests():
    try:
        txt = REQUEST.read_text(encoding="utf-8")
        REQUEST.unlink()
    except OSError:
        return []
    out = []
    for line in txt.splitlines():
        try:
            out.append(json.loads(line)["action"])
        except (ValueError, KeyError, TypeError):
            pass
    return out


class EtwBox:
    """Owns the WPR session. All wpr calls run on one worker thread, so the collector loop never waits for them.

    on_capture(row) is called from the worker with {ts, trigger, session, path, size_mb, status, summary}."""

    def __init__(self, cfg, on_capture):
        self.s = settings(cfg)
        self.on_capture = on_capture
        self.q = queue.Queue()
        self.active = False         # wpr session believed running
        self.want = False
        self.manual = None          # True/False from pm etw start|stop, until the collector restarts
        self.pending = False        # a sync is queued
        self.failed_until = 0.0
        self.last_dump = 0.0
        self.stutters = []          # recent stutter ts for the burst trigger
        self.dpc_since = None
        self.warned = False
        self.t = threading.Thread(target=self._worker, daemon=True, name="etwbox")
        self.t.start()

    # ---------- collector side ----------
    def ensure(self, game_running, now=None):
        now = now or time.time()
        mode = self.s["mode"]
        want = self.manual if self.manual is not None else (mode == "always" or (mode == "game" and game_running))
        if want != self.want or (want and not self.active and not self.pending and now >= self.failed_until):
            self.want = want
            if not self.pending:
                self.pending = True
                self.q.put(("sync",))

    def control(self, action):
        if action in ("start", "stop"):
            self.manual = action == "start"
            self.failed_until = 0.0

    def trigger(self, name, session=None, now=None):
        """Dump the buffer for this trigger (if enabled, recording and not in cooldown). True when queued."""
        now = now or time.time()
        if not self.s["triggers"].get(name) or not self.active or now - self.last_dump < COOLDOWN_S:
            return False
        self.last_dump = now
        log.info("ETW capture trigger %s", name)
        self.q.put(("dump", name, session, now))
        return True

    def on_stutter(self, ts, session):
        n = self.s["triggers"].get("stutter_burst")
        if not n:
            return False
        self.stutters = [t for t in self.stutters if ts - t <= BURST_WINDOW_S] + [ts]
        if len(self.stutters) >= int(n):
            self.stutters = []
            return self.trigger("stutter_burst", session)
        return False

    def on_sample(self, ts, dpc_pct, session):
        lim = self.s["triggers"].get("dpc_pct")
        if not lim or dpc_pct is None or dpc_pct < lim:
            self.dpc_since = None
            return False
        self.dpc_since = self.dpc_since or ts
        if ts - self.dpc_since >= DPC_TRIGGER_S:
            self.dpc_since = None
            return self.trigger("dpc_pct", session)
        return False

    def stop(self):
        self.q.put(("quit",))
        self.t.join(timeout=60)

    # ---------- worker ----------
    def _worker(self):
        wpr("-cancel", timeout=60)   # a session left behind by a collector that died
        while True:
            cmd = self.q.get()
            try:
                if cmd[0] == "quit":
                    if self.active:
                        wpr("-cancel", timeout=60)
                    self.active = False
                    break
                if cmd[0] == "sync":
                    self.pending = False
                    if self.want and not self.active:
                        self._start()
                    elif not self.want and self.active:
                        wpr("-cancel", timeout=60)
                        self.active = False
                        log.info("ETW black box stopped")
                elif cmd[0] == "dump":
                    self._dump(*cmd[1:])
            except Exception:
                log.exception("etwbox %s", cmd[0])
            finally:
                self.q.task_done()

    def _start(self):
        rc, out = wpr("-start", f"{profile_copy(self.s['buffer_mb'])}!PerfMonBox", timeout=120)
        if rc != 0 and "already" in out.lower():
            wpr("-cancel", timeout=60)
            rc, out = wpr("-start", f"{profile_copy(self.s['buffer_mb'])}!PerfMonBox", timeout=120)
        self.active = rc == 0
        if self.active:
            log.info("ETW black box recording (%s MB in memory)", self.s["buffer_mb"])
        else:
            self.failed_until = time.time() + RETRY_S
            (log.warning if not self.warned else log.debug)("wpr -start failed (%s): %s", rc, out[-300:])
            self.warned = True

    def _dump(self, trigger, session, ts):
        if not self.active:
            return
        TRACES.mkdir(parents=True, exist_ok=True)
        path = TRACES / f"{time.strftime('%Y%m%d_%H%M%S', time.localtime(ts))}_{trigger}.etl"
        t0 = time.time()
        rc, out = wpr("-stop", str(path), "-skipPdbGen", timeout=300)
        self.active = False
        ok = rc == 0 and path.exists()
        summary = {"stop_s": round(time.time() - t0, 1)}
        if not ok:
            summary["error"] = out[-300:]
        self.on_capture({"ts": ts, "trigger": trigger, "session": session, "path": str(path) if ok else None,
                         "size_mb": round(path.stat().st_size / 2**20, 1) if ok else None,
                         "status": "captured" if ok else "failed", "summary": summary})
        log.info("ETW capture %s -> %s (%s s, rc=%s)", trigger, path.name if ok else "failed", summary["stop_s"], rc)
        if self.want:
            self._start()


def retention(con, s, now=None):
    """Delete trace files older than retention_days, then the oldest until the total is under max_gb.
    The rows (and their analysis) stay; path becomes NULL. Returns the number of deleted files."""
    now = now or time.time()
    rows = con.execute("SELECT id, ts, path, size_mb FROM etw_captures WHERE path IS NOT NULL ORDER BY ts").fetchall()
    total = sum((r[3] or 0) for r in rows)
    gone = []
    for cid, ts, path, size in rows:
        if now - ts > s["retention_days"] * 86400 or total > s["max_gb"] * 1024:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("trace %s: %s", path, e)
                continue
            total -= size or 0
            gone.append(cid)
    for cid in gone:
        con.execute("UPDATE etw_captures SET path=NULL WHERE id=?", (cid,))
    return len(gone)


# --------------------------------------------------------------------------
# analysis: tracerpt CSV -> top DPC/ISR drivers and disk I/O
# --------------------------------------------------------------------------
# tracerpt CSV: 19 header columns then the event's user data in MOF order.
C_NAME, C_TYPE, C_OPCODE, C_PID, C_TID, C_CLOCK, C_DATA = 0, 1, 6, 9, 10, 16, 19
DPC_OPS, ISR_OPS = {"66", "68", "69"}, {"67", "50"}     # ThreadDPC, DPC, TimerDPC / ISR, ISR-MSI
IMAGE_OPS, PROC_OPS, THREAD_OPS = {"10", "3", "4"}, {"1", "3", "4"}, {"1", "3", "4"}
DISK_OPS = {"10": "read", "11": "write"}
TOP = 10


def _int(v):
    try:
        return int(str(v).strip().strip('"'), 0)
    except (TypeError, ValueError):
        return None


def _tail_clock(path, nbytes=2 << 20):
    """Largest Clock-Time in the last nbytes of the CSV (tracerpt writes events in time order)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - nbytes))
        lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    best = None
    for row in csv.reader(lines, skipinitialspace=True):
        if len(row) > C_CLOCK:
            c = _int(row[C_CLOCK])
            if c is not None and (best is None or c > best):
                best = c
    return best


def _proc_name(data):
    """ImageFileName of a Process event: the first file-name-like field after the fixed numeric ones."""
    for v in data[7:]:
        v = v.strip().strip('"')
        if re.fullmatch(r"[^\\/:*?\"<>|]+\.[A-Za-z0-9]{2,4}", v) and not v.lower().startswith("0x"):
            return v
    return None


class _Agg:
    __slots__ = ("n", "raw_sum", "raw_max")

    def __init__(self):
        self.n, self.raw_sum, self.raw_max = 0, 0, None

    def add(self, raw):
        self.n += 1
        self.raw_sum += raw
        self.raw_max = raw if self.raw_max is None or raw > self.raw_max else self.raw_max


def parse_csv(path, last_s=60):
    """Aggregate a tracerpt CSV. DPC/ISR duration = event time - InitialTime; both clocks are reduced to 100 ns
    units and their offset is the smallest difference seen (durations are relative to the shortest DPC, ~1 us)."""
    end = _tail_clock(path)
    perf_freq, clock_type, start_ft, lost = 10_000_000, 1, None, 0
    images = []                                   # (base, end, name) kernel images
    dpc, isr = defaultdict(_Agg), defaultdict(_Agg)
    thread_pid, pid_name = {}, {}
    disk = defaultdict(lambda: {"bytes": 0, "ios": 0, "max_ms": 0.0, "read": 0, "write": 0})
    events = 0
    min_diff = None
    filetime_clock = None
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f, skipinitialspace=True):
            if len(row) <= C_DATA:
                continue
            name, op = row[C_NAME].strip(), row[C_OPCODE].strip()
            data = row[C_DATA:]
            if name == "EventTrace":
                if row[C_TYPE].strip() == "Header" and len(data) >= 7:
                    pf, ct, st = _int(data[-6]), _int(data[-4]), _int(data[-5])
                    perf_freq = pf if pf and 10_000 <= pf <= 10**10 else perf_freq
                    clock_type, start_ft = ct or clock_type, st
                    lost = _int(data[11]) or 0 if len(data) > 11 else 0
                continue
            clock = _int(row[C_CLOCK])
            if clock is None:
                continue
            if filetime_clock is None:   # tracerpt prints FILETIME for converted clocks, raw ticks otherwise
                filetime_clock = bool(start_ft and abs(clock - start_ft) < 30 * 86400 * 10**7)
            if name == "Image" and op in IMAGE_OPS:
                base, size = _int(data[0]), _int(data[1])
                if base and size and base >= 1 << 63:     # kernel-mode image
                    images.append((base, base + size, os.path.basename(data[-1].strip().strip('"')) or "?"))
                continue
            if name == "Process" and op in PROC_OPS and len(data) > 1:
                pid = _int(data[1])
                if pid is not None:
                    pid_name[pid] = _proc_name(data) or pid_name.get(pid)
                continue
            if name == "Thread" and op in THREAD_OPS and len(data) > 1:
                pid, tid = _int(data[0]), _int(data[1])
                if tid is not None:
                    thread_pid[tid] = pid
                continue
            if end is not None and clock < end - _units(last_s, filetime_clock, perf_freq):
                continue
            if name == "PerfInfo" and (op in DPC_OPS or op in ISR_OPS) and len(data) >= 2:
                init, routine = _int(data[0]), _int(data[1])
                if init is None or routine is None:
                    continue
                if filetime_clock and clock_type == 1:
                    init = init * 10_000_000 // perf_freq
                diff = clock - init
                min_diff = diff if min_diff is None or diff < min_diff else min_diff
                if diff - min_diff > _units(MAX_DPC_US / 1e6, filetime_clock, perf_freq):
                    continue   # start and end paired across a clock jump: seen as "56497207 ms"
                (dpc if op in DPC_OPS else isr)[routine].add(diff)
                events += 1
            elif name == "DiskIo" and op in DISK_OPS and len(data) >= 9:
                size, resp, tid = _int(data[2]) or 0, _int(data[7]) or 0, _int(data[8])
                pid = thread_pid.get(tid)
                d = disk[pid]
                d["bytes"] += size
                d["ios"] += 1
                d[DISK_OPS[op]] += size
                d["max_ms"] = max(d["max_ms"], resp * 1000 / perf_freq)
                events += 1
    images.sort()
    starts = [i[0] for i in images]

    def driver(addr):
        k = bisect.bisect_right(starts, addr) - 1
        return images[k][2] if k >= 0 and addr < images[k][1] else "?"

    to_us = (lambda v: v / 10) if filetime_clock else (lambda v: v * 1e6 / perf_freq)

    def top(aggs):
        by = defaultdict(lambda: [0, 0, 0])       # driver -> [count, sum raw, max raw] (offset removed)
        for routine, a in aggs.items():
            b = by[driver(routine)]
            b[0] += a.n
            b[1] += a.raw_sum - a.n * min_diff
            b[2] = max(b[2], a.raw_max - min_diff)
        rows = [{"driver": k, "count": v[0], "sum_us": round(to_us(v[1])), "max_us": round(to_us(v[2]))}
                for k, v in by.items()]
        return sorted(rows, key=lambda r: -r["sum_us"])[:TOP]

    window = None
    if end is not None and start_ft and filetime_clock:
        window = round(min(last_s, (end - start_ft) / 1e7), 1)
    procs = [{"process": pid_name.get(pid) or (f"pid {pid}" if pid is not None else "?"),
              "mb": round(d["bytes"] / 2**20, 1), "read_mb": round(d["read"] / 2**20, 1),
              "write_mb": round(d["write"] / 2**20, 1), "ios": d["ios"], "max_ms": round(d["max_ms"], 1)}
             for pid, d in disk.items()]
    return {
        "window_s": window, "events": events, "events_lost": lost, "images": len(images),
        "dpc_top": top(dpc) if min_diff is not None else [], "isr_top": top(isr) if min_diff is not None else [],
        "disk_top": sorted(procs, key=lambda r: -r["mb"])[:TOP],
        "disk_slowest": sorted(procs, key=lambda r: -r["max_ms"])[:5],
    }


def _units(seconds, filetime_clock, perf_freq):
    return int(seconds * (10_000_000 if filetime_clock else perf_freq))


def headline(summary):
    """(severity, title) of the etw_capture incident."""
    d = (summary.get("dpc_top") or []) + (summary.get("isr_top") or [])
    worst = max(d, key=lambda r: r["max_us"], default=None)
    disk = (summary.get("disk_slowest") or [None])[0]
    parts = []
    sev = "info"
    if worst:
        parts.append(f"{'DPC' if worst in summary.get('dpc_top', []) else 'ISR'}: {worst['driver']} "
                     f"{worst['max_us'] / 1000:.1f} ms max")
        sev = "serious" if worst["max_us"] >= 5000 else "warning" if worst["max_us"] >= 1000 else "info"
    if disk and disk["max_ms"] >= 100:
        parts.append(f"dysk: {disk['process']} {disk['max_ms']:.0f} ms")
        sev = "warning" if sev == "info" else sev
    return sev, "ETW: " + (", ".join(parts) if parts else "brak długich DPC/ISR")


def fix_old_headlines(con):
    """Incidents stored before impossible DPC/ISR durations were dropped."""
    for iid, title in con.execute("SELECT id, title FROM incidents WHERE kind='etw_capture'").fetchall():
        m = re.search(r"(DPC|ISR): \S+ ([\d.]+) ms max", title)
        if m and float(m[2]) * 1000 >= MAX_DPC_US:
            con.execute("UPDATE incidents SET severity='info', title=? WHERE id=?",
                        (f"ETW: pominięty błędny pomiar {m[1]}", iid))


def analyze(con, cid, keep_csv=False):
    """tracerpt + parse_csv for capture `cid`; stores the summary and an etw_capture incident. Returns the summary."""
    r = con.execute("SELECT ts, trigger, path, summary FROM etw_captures WHERE id=?", (cid,)).fetchone()
    if not r:
        raise ValueError(f"Brak zrzutu ETW {cid}")
    ts, trigger, path, old = r[0], r[1], r[2], json.loads(r[3]) if r[3] else {}
    if not path or not os.path.exists(path):
        raise ValueError(f"Plik zrzutu {cid} nie istnieje (retencja?)")
    s = settings(config.load())
    out = os.path.splitext(path)[0] + ".csv"
    t0 = time.time()
    try:
        tr = subprocess.run(["tracerpt", path, "-o", out, "-of", "CSV", "-y"], capture_output=True, text=True,
                            errors="replace", timeout=1800, creationflags=CREATE_NO_WINDOW | IDLE_PRIORITY_CLASS)
        if tr.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(f"tracerpt {tr.returncode}: {(tr.stdout + tr.stderr).strip()[-300:]}")
        t_conv = time.time() - t0
        csv_mb = os.path.getsize(out) / 2**20
        summ = parse_csv(out, s["dump_last_s"])
    except Exception as e:
        old.update(error=str(e)[:300])
        con.execute("UPDATE etw_captures SET status='failed', summary=? WHERE id=?", (json.dumps(old), cid))
        con.commit()
        raise
    finally:
        if not keep_csv and os.path.exists(out):
            os.remove(out)
    summ.update(old, convert_s=round(t_conv, 1), analysis_s=round(time.time() - t0, 1), csv_mb=round(csv_mb, 1))
    summ.pop("error", None)
    con.execute("UPDATE etw_captures SET status='analyzed', summary=? WHERE id=?", (json.dumps(summ), cid))
    sev, title = headline(summ)
    con.execute("INSERT INTO incidents(ts, end_ts, kind, severity, title, details) VALUES (?,?,?,?,?,?)",
                (ts, None, "etw_capture", sev, f"{title} ({trigger})",
                 json.dumps({"capture": cid, "trigger": trigger, "dpc_top": summ["dpc_top"][:3],
                             "isr_top": summ["isr_top"][:3], "disk_top": summ["disk_top"][:3]}, ensure_ascii=False)))
    con.commit()
    return summ


def spawn_analysis(cid):
    """Analyse in a separate idle-priority process (keeps the collector's GIL free)."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe") and os.path.exists(exe[:-5] + ".exe"):
        exe = exe[:-5] + ".exe"
    return subprocess.Popen([exe, "-m", "perfmon.etwbox", "analyze", str(cid)], cwd=str(Path(__file__).parent.parent),
                            creationflags=CREATE_NO_WINDOW | IDLE_PRIORITY_CLASS,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def nearest_capture(con, ts, within_s):
    """id of the capture taken within_s after ts (its buffer covers ts), else None."""
    r = con.execute("SELECT id FROM etw_captures WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT 1",
                    (ts, ts + within_s)).fetchone()
    return r[0] if r else None


def tools_available():
    return {"wpr": bool(shutil.which("wpr")), "tracerpt": bool(shutil.which("tracerpt")),
            "xperf": bool(shutil.which("xperf"))}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "analyze":
        from . import db
        logging.basicConfig(filename=str(config.LOG_PATH), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        c = db.connect()
        try:
            res = analyze(c, int(sys.argv[2]))
            log.info("ETW capture %s analysed in %s s (%s MB CSV)", sys.argv[2], res["analysis_s"], res["csv_mb"])
        except Exception:
            log.exception("ETW capture %s analysis", sys.argv[2])
            sys.exit(1)
