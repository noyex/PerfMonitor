"""ETW black box: a kernel trace (DPC/ISR, image loads, disk I/O, processes) kept in a circular in-memory buffer by
WPR and written to data/traces when something goes wrong (user mark, frozen desktop/DWM, DPC storm, stutter burst).

The capture is analysed later in a separate low-priority process (`python -m perfmon.etwbox analyze ID`, also
`pm etw analyze ID`): tracerpt converts the .etl to CSV (no xperf/WPA without the ADK), streamed through a named pipe
instead of a multi-GB file, and the last `dump_last_s` seconds are aggregated into the top DPC/ISR drivers and disk I/O
per process and file. Needs admin (the collector has it)."""
import bisect
import csv
import ctypes
import heapq
import itertools
import json
import logging
import msvcrt
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from array import array
from pathlib import Path

from . import config

log = logging.getLogger("perfmon.etwbox")

CREATE_NO_WINDOW = 0x08000000
IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
INSTANCE = "PerfMonBox"
PROFILE = Path(__file__).parent / "etw" / "perfmon.wprp"
TRACES = config.DATA_DIR / "traces"
REQUEST = config.DATA_DIR / "etw.request"
COOLDOWN_S = 120
ANALYZE_DELAY_S = 30       # let the disk settle after writing the .etl
RETRY_S = 600              # after a failed wpr -start (no admin, another kernel logger, ...)
BURST_WINDOW_S = 10
DUMP_SETTLE_S = 10        # after wpr -stop: stutters until then are still the dump's (disk flush, wpr -start)
DPC_TRIGGER_S = 2
MAX_DPC_US = 1_000_000   # no DPC/ISR runs this long, the watchdog would bugcheck
ORPHAN_S = 3600           # a trace file no row points at yet may still be written by wpr -stop


def settings(cfg):
    """cfg["etw"] over the defaults (config.load() replaces the whole dict when the user sets any key)."""
    base = config.DEFAULTS["etw"]
    user = cfg.get("etw") if isinstance(cfg.get("etw"), dict) else {}
    s = {**base, **user}
    s["triggers"] = {**base["triggers"], **(user.get("triggers") or {})}
    return s


def _run(args, timeout=180, flags=0):
    r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                       creationflags=CREATE_NO_WINDOW | flags)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def wpr(*args, timeout=180, flags=0):
    return _run(["wpr", *args, "-instancename", INSTANCE], timeout, flags)   # -instancename must be the last parameter


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
        self.lock = threading.Lock()   # trigger() runs on the collector and the PresentMon reader threads
        self.last_dump = 0.0
        self.last_burst = 0.0       # burst_cooldown_s (>> a dump) also keeps the dump's own stutters from re-triggering
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
        with self.lock:
            if not self.s["triggers"].get(name) or not self.active or now - self.last_dump < COOLDOWN_S:
                return False
            if name == "stutter_burst":
                if now - self.last_burst < self.s["burst_cooldown_s"]:
                    return False
                self.last_burst = now
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

    def _free_burst(self, trigger):
        """Nothing was captured: the next stutter burst may try again."""
        if trigger == "stutter_burst":
            with self.lock:
                self.last_burst = 0.0

    def _dump(self, trigger, session, ts):
        if not self.active:   # stopped between the trigger and here (the game exited)
            self._free_burst(trigger)
            return
        TRACES.mkdir(parents=True, exist_ok=True)
        path = TRACES / f"{time.strftime('%Y%m%d_%H%M%S', time.localtime(ts))}_{trigger}.etl"
        t0 = time.time()
        try:
            # below normal: the merge and rundown compete with the game for CPU
            rc, out = wpr("-stop", str(path), "-skipPdbGen", timeout=300, flags=BELOW_NORMAL_PRIORITY_CLASS)
        except (OSError, subprocess.TimeoutExpired) as e:   # still recorded as a failed capture, with the reason
            rc, out = -1, f"{type(e).__name__}: {e}"
        self.active = False
        ok = rc == 0 and path.exists()
        summary = {"stop_s": round(time.time() - t0, 1)}
        if not ok:
            summary["error"] = tool_error("wpr -stop", rc, out) if rc else f"wpr -stop: brak pliku {path.name}"
            self._free_burst(trigger)
        self.on_capture({"ts": ts, "trigger": trigger, "session": session, "path": str(path) if ok else None,
                         "size_mb": round(path.stat().st_size / 2**20, 1) if ok else None,
                         "status": "captured" if ok else "failed", "summary": summary})
        log.info("ETW capture %s -> %s (%s s, rc=%s)", trigger, path.name if ok else "failed", summary["stop_s"], rc)
        if self.want:
            self._start()


def retention(con, s, now=None, folder=None):
    """Keep the whole traces folder under max_gb, not just the files etw_captures points at:
    - intermediate .csv files (left by a crashed or old-style analysis) are always deleted,
    - files no row references (orphans) once they are ORPHAN_S old (younger ones may still be written by wpr -stop),
    - .etl older than retention_days, then the oldest until the folder fits max_gb; analysed captures go before
      unanalysed ones, their summary already holds what the trace was kept for.
    The rows stay with path NULL; a capture deleted before it was analysed is marked failed with the reason.
    Returns the number of deleted files."""
    now = now or time.time()
    folder = Path(folder or TRACES)
    if not folder.is_dir():
        return 0
    rows = con.execute("SELECT id, ts, path, status FROM etw_captures WHERE path IS NOT NULL").fetchall()
    # only files inside this folder: a DB copy run with another data dir still points at the live traces
    base = os.path.normcase(os.path.abspath(folder))
    known = {k: r for r in rows if os.path.dirname(k := os.path.normcase(os.path.abspath(r[2]))) == base}
    deleted, total = 0, 0
    for p in folder.iterdir():
        key = os.path.normcase(os.path.abspath(p))
        if p.name == "perfmon.wprp" or key in known:
            continue
        try:
            st = p.stat()
            if p.suffix.lower() == ".csv" or now - st.st_mtime > ORPHAN_S:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                deleted += 1
                log.info("ETW: removed intermediate/orphan %s (%.0f MB)", p.name, st.st_size / 2**20)
            elif p.is_file():
                total += st.st_size
        except OSError as e:
            log.warning("trace %s: %s", p, e)
    files = []
    for key, (cid, ts, path, status) in known.items():
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None                       # deleted by hand: only the row is fixed below
        files.append((status == "captured", ts, cid, path, size))
        total += size or 0
    gone = []
    for _, ts, cid, path, size in sorted(files, key=lambda f: (now - f[1] <= s["retention_days"] * 86400, f[0], f[1])):
        if size is not None and now - ts <= s["retention_days"] * 86400 and total <= s["max_gb"] * 2**30:
            continue
        try:
            if size is not None:
                os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("trace %s: %s", path, e)
            continue
        total -= size or 0
        deleted += size is not None
        gone.append(cid)
    for cid in gone:
        con.execute("UPDATE etw_captures SET path=NULL WHERE id=?", (cid,))
    # never analysed and now without a file: nothing will ever pick them up again
    for cid, summ in con.execute("SELECT id, summary FROM etw_captures WHERE status='captured' AND path IS NULL"):
        _fail(con, cid, _loads(summ), "Plik zrzutu usunięty (retencja) przed analizą")
    return deleted


def _loads(s):
    try:
        return json.loads(s) if s else {}
    except ValueError:
        return {}


def _fail(con, cid, summ, error):
    summ["error"] = str(error)[:400]
    con.execute("UPDATE etw_captures SET status='failed', summary=? WHERE id=?",
                (json.dumps(summ, ensure_ascii=False), cid))


# --------------------------------------------------------------------------
# analysis: tracerpt CSV (streamed through a named pipe) -> top DPC/ISR drivers and disk I/O
# --------------------------------------------------------------------------
# tracerpt CSV: 19 header columns then the event's user data in MOF order.
C_NAME, C_TYPE, C_VERSION, C_OPCODE, C_PID, C_TID, C_CLOCK, C_DATA = 0, 1, 3, 6, 9, 10, 16, 19
DPC_OPS, ISR_OPS = {"66", "68", "69"}, {"67", "50"}     # ThreadDPC, DPC, TimerDPC / ISR, ISR-MSI
IMAGE_OPS = {"10", "3", "4"}
PROC_OPS = {"1", "2", "3", "4", "39"}                    # Start, End, DCStart, DCEnd, Defunct
THREAD_OPS = {"1", "2", "3", "4"}
FILE_NAME_OPS = {"0", "32", "35", "36"}                  # FileIo Name/FileCreate/FileDelete/FileRundown
DISK_OPS = {"10": "read", "11": "write"}
TOP = 10
FT_UNIX = 11644473600                                    # seconds from 1601 (FILETIME) to 1970
MAX_ATTEMPTS = 3                                         # analysis runs that died (killed, crash) before giving up
# tracerpt exit codes seen in failed analyses
TOOL_RC = {0x40010004: "proces przerwany (zabity)", 0x80070005: "odmowa dostępu", 0x80070070: "brak miejsca na dysku",
           0x800700E7: "potok zajęty", 0x80070020: "plik używany przez inny proces", 0x80070002: "brak pliku"}


def _int(v):
    try:
        return int(str(v).strip().strip('"'), 0)
    except (TypeError, ValueError):
        return None


def _str(v):
    return v.strip().strip('"')


def _proc_name(data, version=4):
    """ImageFileName of a Process event: the field after the SID (8 since version 4 added Flags, 7 in version 3);
    the command line after it can contain quotes that break tracerpt's CSV, so the tail is no anchor.
    Older layouts: the first file-name-like field after the fixed ones."""
    i = 8 if version >= 4 else 7 if version == 3 else None
    if i is not None and len(data) > i and _str(data[i]) and not _str(data[i]).lower().startswith("0x"):
        return _str(data[i])
    for v in data[7:]:
        v = _str(v)
        if re.fullmatch(r"[^\\/:*?\"<>|]+\.[A-Za-z0-9]{2,4}", v) and not v.lower().startswith("0x"):
            return v
    return None


def parse_rows(lines, last_s=60, trigger_ts=None, devices=None):
    """Aggregate tracerpt CSV lines (any iterable of str), single pass so the CSV can be streamed.

    The analysis window is the last `last_s` seconds before the trace's EndTime from the EventTrace header (the
    first row). DPC/ISR duration = event time - InitialTime; both clocks are reduced to 100 ns units and their offset
    is the smallest difference seen (durations are relative to the shortest DPC, ~1 us).
    Disk I/O carries only the issuing thread id and the file object: threads -> processes and file objects -> paths
    come from Thread/Process/FileIo events, mostly the rundown at the END of the trace, so they are resolved after
    the pass. `at_s` fields are seconds relative to trigger_ts (negative = before the trigger)."""
    perf_freq, clock_type, start_ft, end_ft, lost = 10_000_000, 1, None, None, 0
    images = []                                         # (base, end, name) kernel images
    dpc, isr = {}, {}                                   # routine -> (array of event time - InitialTime, array of times)
    thread_pid, pid_name, file_name = {}, {}, {}
    disk = {}               # (pid or None, tid when pid unknown yet, file object) -> [bytes, ios, rd, wr, max resp, at]
    events = 0
    filetime_clock = None
    cutoff = None
    for row in csv.reader(lines, skipinitialspace=True):
        if len(row) <= C_DATA:
            continue
        name, op = row[C_NAME].strip(), row[C_OPCODE].strip()
        data = row[C_DATA:]
        if name == "EventTrace":
            if row[C_TYPE].strip() == "Header" and len(data) >= 7:
                pf, ct, st = _int(data[-6]), _int(data[-4]), _int(data[-5])
                perf_freq = pf if pf and 10_000 <= pf <= 10**10 else perf_freq
                clock_type, start_ft, end_ft = ct or clock_type, st, _int(data[4])
                lost = (_int(data[11]) or 0) if len(data) > 11 else 0
            continue
        clock = _int(row[C_CLOCK])
        if clock is None:
            continue
        if filetime_clock is None:   # tracerpt prints FILETIME for converted clocks, raw ticks otherwise
            filetime_clock = bool(start_ft and abs(clock - start_ft) < 30 * 86400 * 10**7)
            if end_ft and start_ft and end_ft > start_ft:
                # raw ticks: the first event is about the trace start, the header's times are FILETIME
                cutoff = (end_ft - int(last_s * 10**7) if filetime_clock else
                          clock + int(((end_ft - start_ft) / 1e7 - last_s) * perf_freq))
        if name == "Image" and op in IMAGE_OPS:
            base, size = _int(data[0]), _int(data[1])
            if base and size and base >= 1 << 63:     # kernel-mode image
                images.append((base, base + size, os.path.basename(_str(data[-1])) or "?"))
            continue
        if name == "Process" and op in PROC_OPS and len(data) > 1:
            pid = _int(data[1])
            if pid is not None:
                pid_name[pid] = _proc_name(data, _int(row[C_VERSION]) or 0) or pid_name.get(pid)
            continue
        if name == "Thread" and op in THREAD_OPS and len(data) > 1:
            pid, tid = _int(data[0]), _int(data[1])
            if tid is not None and pid is not None:
                thread_pid[tid] = pid
            continue
        if name == "FileIo" and op in FILE_NAME_OPS and len(data) >= 2:
            file_name[_str(data[0])] = _str(data[-1])
            continue
        if cutoff is not None and clock < cutoff:
            continue
        if name == "PerfInfo" and (op in DPC_OPS or op in ISR_OPS) and len(data) >= 2:
            init, routine = _int(data[0]), _int(data[1])
            if init is None or routine is None:
                continue
            if filetime_clock and clock_type == 1:
                init = init * 10_000_000 // perf_freq
            a = (dpc if op in DPC_OPS else isr).get(routine)
            if a is None:
                a = (dpc if op in DPC_OPS else isr)[routine] = (array("q"), array("q"))
            a[0].append(clock - init)
            a[1].append(clock)
            events += 1
        elif name == "DiskIo" and op in DISK_OPS and len(data) >= 9:
            size, resp, tid, fobj = _int(data[2]) or 0, _int(data[7]) or 0, _int(data[8]), _str(data[5])
            pid = thread_pid.get(tid)   # a thread started inside the trace; else the end-of-trace rundown
            k = (pid, None if pid is not None else tid, fobj)
            d = disk.get(k)
            if d is None:
                d = disk[k] = [0, 0, 0, 0, -1, None]
            d[0] += size
            d[1] += 1
            d[2 if op == "10" else 3] += size
            if resp > d[4]:
                d[4], d[5] = resp, clock
            events += 1
    images.sort()
    starts = [i[0] for i in images]

    def driver(addr):
        k = bisect.bisect_right(starts, addr) - 1
        return images[k][2] if k >= 0 and addr < images[k][1] else "?"

    to_us = (lambda v: v / 10) if filetime_clock else (lambda v: v * 1e6 / perf_freq)
    trig_ft = (trigger_ts + FT_UNIX) * 10**7 if trigger_ts and filetime_clock else None

    def at(clock):
        return round((clock - trig_ft) / 1e7, 2) if trig_ft and clock else None

    # The offset of the two clocks = the shortest DPC; a low percentile, because start and end paired across a clock
    # jump give absurd values both ways (seen: "56497207 ms"), and those are dropped against the final offset
    n = sum(len(vals) for aggs in (dpc, isr) for vals, _ in aggs.values())
    offset = heapq.nsmallest(n // 1000 + 1, (v for aggs in (dpc, isr) for vals, _ in aggs.values() for v in vals))[-1]         if n else None
    limit = _units(MAX_DPC_US / 1e6, filetime_clock, perf_freq)

    def top(aggs):
        by = {}                                   # driver -> [count, sum raw, max raw, clock of the max]
        for routine, (vals, clocks) in aggs.items():
            b = None
            for v, c in zip(vals, clocks):
                if abs(v - offset) > limit:
                    continue
                v = max(0, v - offset)
                if b is None:
                    b = by.setdefault(driver(routine), [0, 0, -1, None])
                b[0] += 1
                b[1] += v
                if v > b[2]:
                    b[2], b[3] = v, c
        rows = [{"driver": k, "count": v[0], "sum_us": round(to_us(v[1])), "max_us": round(to_us(v[2])),
                 "max_at_s": at(v[3])} for k, v in by.items()]
        return sorted(rows, key=lambda r: -r["sum_us"])[:TOP]

    def proc(k):
        pid = k[0] if k[0] is not None else thread_pid.get(k[1])
        return pid_name.get(pid) or (f"pid {pid}" if pid is not None else "?")

    def path(fobj):
        p = file_name.get(fobj)
        if not p:
            return None
        for dev, letter in (devices or {}).items():
            if p.lower().startswith(dev.lower() + "\\"):
                return letter + p[len(dev):]
        return p

    procs, files = {}, {}
    for k, d in disk.items():
        pname, fname = proc(k), path(k[2])
        for agg, key in ((procs, pname), (files, (fname, pname))):
            a = agg.setdefault(key, [0, 0, 0, 0, -1, None, None])
            a[0] += d[0]
            a[1] += d[1]
            a[2] += d[2]
            a[3] += d[3]
            if d[4] > a[4]:
                a[4], a[5], a[6] = d[4], d[5], fname
    ms = lambda raw: round(raw * 1000 / perf_freq, 1)
    mb = lambda b: round(b / 2**20, 1)
    proc_rows = [{"process": p, "mb": mb(a[0]), "read_mb": mb(a[2]), "write_mb": mb(a[3]), "ios": a[1],
                  "max_ms": ms(a[4]), "max_at_s": at(a[5]), "max_file": a[6]} for p, a in procs.items()]
    file_rows = [{"file": f or "?", "process": p, "mb": mb(a[0]), "read_mb": mb(a[2]), "write_mb": mb(a[3]),
                  "ios": a[1], "max_ms": ms(a[4])} for (f, p), a in files.items()]
    window = None
    if cutoff is not None:
        window = round(min(last_s, (end_ft - start_ft) / 1e7), 1)
    total = sum(d[0] for d in disk.values())
    return {
        "window_s": window, "events": events, "events_lost": lost, "images": len(images),
        "trigger_at_s": round((end_ft - trig_ft) / -1e7, 1) if trig_ft and end_ft else None,
        "dpc_top": top(dpc), "isr_top": top(isr),
        "disk_top": sorted(proc_rows, key=lambda r: -r["mb"])[:TOP],
        "disk_slowest": sorted(proc_rows, key=lambda r: -r["max_ms"])[:5],
        "disk_files": sorted(file_rows, key=lambda r: -r["mb"])[:TOP],
        "disk_unattributed_pct": round(100 * sum(d[0] for k, d in disk.items() if proc(k) == "?") / total, 1)
        if total else None,
    }


def parse_csv(path, last_s=60, trigger_ts=None, devices=None):
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return parse_rows(f, last_s, trigger_ts, devices)


def _units(seconds, filetime_clock, perf_freq):
    return int(seconds * (10_000_000 if filetime_clock else perf_freq))


def dos_devices():
    """\\Device\\HarddiskVolumeN -> C: for the file paths of disk I/O."""
    out = {}
    buf = ctypes.create_unicode_buffer(1024)
    for c in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        if ctypes.windll.kernel32.QueryDosDeviceW(f"{c}:", buf, len(buf)):
            out[buf.value] = f"{c}:"
    return out


def tool_error(tool, rc, out):
    """A short, diagnosable reason: the exit code (hex, with a known meaning) and the last real output line
    (tracerpt prints its input file list and a progress bar before the error)."""
    rc &= 0xFFFFFFFF
    lines = [ln.strip() for ln in re.sub(r"\d+\.\d+%", "\n", out or "").splitlines()]
    lines = [ln for ln in lines if ln and not set(ln) <= set("-") and not ln.lower().endswith(".etl")]
    hint = TOOL_RC.get(rc)
    return f"{tool} 0x{rc:08X}" + (f" ({hint})" if hint else "") + (f": {' '.join(lines[-2:])[:200]}" if lines else "")


PIPE_ACCESS_DUPLEX, PIPE_UNLIMITED_INSTANCES, ERROR_PIPE_CONNECTED, ERROR_NO_DATA = 3, 255, 535, 232
INVALID_HANDLE = ctypes.c_void_p(-1).value
TRACERPT = ["tracerpt"]


def tracerpt_lines(etl, timeout=1800, stats=None):
    """Yield the CSV lines of `tracerpt etl` streamed through a named pipe: no multi-GB intermediate file (a 256 MB
    trace used to become a 2.6 GB CSV on the same disk). tracerpt opens its output several times (an empty probe,
    the dump, a reopen after writing; a failed open = exit code 1 or 0x800700E7), so every connection gets its own
    reader thread and two instances always listen. Raises RuntimeError when tracerpt fails.
    stats (dict) receives bytes and the exit code."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateNamedPipeW.restype = k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateNamedPipeW.argtypes = [ctypes.c_wchar_p] + [ctypes.c_uint32] * 6 + [ctypes.c_void_p]
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    name = rf"\\.\pipe\perfmon_etw_{os.getpid()}_{threading.get_ident()}_{time.monotonic_ns()}"
    chunks = queue.Queue(maxsize=32)       # bounded: a slow parser throttles tracerpt instead of filling memory
    lock, active = threading.Lock(), [0]   # instances not finished yet (listening or being read)
    done, closed, out = threading.Event(), threading.Event(), [b""]
    conn_ids = itertools.count()

    def put(item):
        while not closed.is_set():
            try:
                return chunks.put(item, timeout=0.5)
            except queue.Full:
                pass

    def listen():
        h = k32.CreateNamedPipeW(name, PIPE_ACCESS_DUPLEX, 0, PIPE_UNLIMITED_INSTANCES, 1 << 16, 1 << 20, 0, None)
        if h in (None, INVALID_HANDLE):
            raise OSError(ctypes.get_last_error(), "CreateNamedPipe")
        with lock:
            active[0] += 1
        threading.Thread(target=serve, args=(h, next(conn_ids)), daemon=True, name="tracerpt-pipe").start()

    def serve(h, conn):
        try:
            # ERROR_NO_DATA: the client already opened and closed it (the probe); still an answered open
            if not k32.ConnectNamedPipe(h, None) and ctypes.get_last_error() not in (ERROR_PIPE_CONNECTED,
                                                                                      ERROR_NO_DATA):
                k32.CloseHandle(h)
                return
            if not done.is_set():
                try:
                    listen()
                except OSError as e:
                    log.warning("tracerpt pipe: %s", e)
            with open(msvcrt.open_osfhandle(h, os.O_RDONLY), "rb", buffering=0) as f:
                while b := f.read(1 << 20):
                    put((conn, b))
        except OSError as e:
            log.warning("tracerpt pipe: %s", e)
        finally:
            with lock:
                active[0] -= 1
            put(None)                       # wakes the consumer to check whether everything is finished

    def watch():
        try:
            out[0] = proc.communicate(timeout=timeout)[0] or b""
        except subprocess.TimeoutExpired:
            proc.kill()
            out[0] = proc.communicate()[0] or b""
        done.set()
        for _ in range(600):                 # end the waits of instances nobody will open now (30 s at most)
            if not active[0]:
                break
            c = k32.CreateFileW(name, 0x40000000, 0, None, 3, 0, None)
            if c not in (None, INVALID_HANDLE):
                k32.CloseHandle(c)
            time.sleep(0.05)
        put(None)

    listen()
    listen()
    proc = subprocess.Popen(TRACERPT + [str(etl), "-o", name, "-of", "CSV", "-y"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW | IDLE_PRIORITY_CLASS)
    w = threading.Thread(target=watch, daemon=True, name="tracerpt")
    w.start()
    n, rest, src = 0, b"", None
    try:
        while True:
            item = chunks.get()
            if item is None:
                if done.is_set() and not active[0] and chunks.empty():
                    break
                continue
            if src is None:
                src = item[0]
            elif item[0] != src:           # only one open carries the dump; never splice another into it
                log.warning("tracerpt: data on a second pipe connection dropped (%s bytes)", len(item[1]))
                continue
            b = item[1]
            n += len(b)
            lines = (rest + b).split(b"\n")
            rest = lines.pop()
            for ln in lines:
                yield ln.decode("utf-8", "replace") + "\n"
        if rest:
            yield rest.decode("utf-8", "replace")
    finally:
        closed.set()
        if proc.poll() is None:
            proc.kill()
        w.join(timeout=30)
    rc = proc.returncode
    if stats is not None:
        stats.update(bytes=n, rc=rc)
    if rc != 0 or not n:
        raise RuntimeError(tool_error("tracerpt", rc if rc is not None else -1,
                                      out[0].decode("oem", "replace") or "brak danych"))


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


def analyze(con, cid):
    """tracerpt (streamed) + parse_rows for capture `cid`; stores the summary and an etw_capture incident.
    Failures are stored as status failed + summary.error. Returns the summary."""
    r = con.execute("SELECT ts, trigger, path, summary FROM etw_captures WHERE id=?", (cid,)).fetchone()
    if not r:
        raise ValueError(f"Brak zrzutu ETW {cid}")
    ts, trigger, path, old = r[0], r[1], r[2], _loads(r[3])
    if not path or not os.path.exists(path):
        raise ValueError(f"Plik zrzutu {cid} nie istnieje (retencja?)")
    # counted before the run: one that dies (killed, out of memory) is retried at most MAX_ATTEMPTS times
    old["attempts"] = old.get("attempts", 0) + 1
    con.execute("UPDATE etw_captures SET summary=? WHERE id=?", (json.dumps(old, ensure_ascii=False), cid))
    con.commit()
    s = settings(config.load())
    t0 = time.time()
    st = {}
    try:
        try:
            devices = dos_devices()
        except (OSError, AttributeError):
            devices = {}
        summ = parse_rows(tracerpt_lines(path, stats=st), s["dump_last_s"], ts, devices)
    except Exception as e:
        _fail(con, cid, old, e if isinstance(e, RuntimeError) else f"analiza: {type(e).__name__}: {e}")
        con.commit()
        raise
    summ.update(old, analysis_s=round(time.time() - t0, 1), csv_mb=round(st.get("bytes", 0) / 2**20, 1))
    summ.pop("error", None)
    con.execute("UPDATE etw_captures SET status='analyzed', summary=? WHERE id=?",
                (json.dumps(summ, ensure_ascii=False), cid))
    sev, title = headline(summ)
    # a re-analysis (pm etw analyze) replaces the capture's incident instead of adding a second one
    con.execute("DELETE FROM incidents WHERE kind='etw_capture' AND json_extract(details, '$.capture')=?", (cid,))
    con.execute("INSERT INTO incidents(ts, end_ts, kind, severity, title, details) VALUES (?,?,?,?,?,?)",
                (ts, None, "etw_capture", sev, f"{title} ({trigger})",
                 json.dumps({"capture": cid, "trigger": trigger, "dpc_top": summ["dpc_top"][:3],
                             "isr_top": summ["isr_top"][:3], "disk_top": summ["disk_top"][:3],
                             "disk_files": summ["disk_files"][:3]}, ensure_ascii=False)))
    con.commit()
    return summ


def next_to_analyze(con, now=None):
    """id of the newest capture still waiting for analysis (status captured, file present, settled for
    ANALYZE_DELAY_S, fewer than MAX_ATTEMPTS runs), None when the backlog is empty.
    Captures whose runs all died are marked failed here."""
    now = now or time.time()
    for cid, summ in con.execute("SELECT id, summary FROM etw_captures WHERE status='captured' AND path IS NOT NULL "
                                 "AND ts < ? ORDER BY ts DESC", (now - ANALYZE_DELAY_S,)).fetchall():
        sm = _loads(summ)
        if sm.get("attempts", 0) < MAX_ATTEMPTS:
            return cid
        _fail(con, cid, sm, f"Analiza przerwana {sm['attempts']} razy (proces zabity lub zawieszony)")
        con.commit()
    return None


def undo_attempt(con, cid):
    """A run stopped on purpose (a game started) does not count towards MAX_ATTEMPTS."""
    r = con.execute("SELECT summary FROM etw_captures WHERE id=? AND status='captured'", (cid,)).fetchone()
    if r:
        sm = _loads(r[0])
        sm["attempts"] = max(0, sm.get("attempts", 0) - 1)
        con.execute("UPDATE etw_captures SET summary=? WHERE id=?", (json.dumps(sm, ensure_ascii=False), cid))
        con.commit()


def spawn_analysis(cid):
    """Analyse in a separate idle-priority process (keeps the collector's GIL free)."""
    return subprocess.Popen(config.module_argv("perfmon.etwbox", "analyze", cid, console=True),
                            cwd=str(Path(__file__).parent.parent),
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
            log.info("ETW capture %s analysed in %s s (%s MB CSV streamed)", sys.argv[2], res["analysis_s"],
                     res["csv_mb"])
        except Exception:
            log.exception("ETW capture %s analysis", sys.argv[2])
            sys.exit(1)
