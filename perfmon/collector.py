"""Background collector. Run elevated at logon: pythonw -m perfmon.collector"""
import ctypes
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import psutil

from . import config, db, gamectx, sysinfo
from .events import EventReader, classify
from .presentmon import FrameCapture
from .sensors import HardwareSensors, PdhCounters
from .watchdog import Watchdog, foreground, user_idle_s

log = logging.getLogger("perfmon")

SAMPLE_COLS = [
    "ts", "cpu_total", "cpu_core_max", "cpu_clock_mhz", "cpu_temp", "cpu_power_w", "dpc_pct",
    "interrupt_pct", "ctx_switch_s", "proc_queue", "ram_used_gb", "ram_pct", "commit_gb",
    "commit_limit_gb", "hard_faults_s", "gpu_load", "gpu_3d", "gpu_vram_mb", "gpu_vram_total_mb",
    "gpu_clock_mhz", "gpu_mem_clock_mhz", "gpu_temp", "gpu_hotspot", "gpu_mem_temp", "gpu_power_w",
    "gpu_fan_rpm", "disk_read_mbs", "disk_write_mbs", "disk_lat_ms", "disk_lat_disk", "disk_queue",
    "disk_busy_pct", "net_rx_kbs", "net_tx_kbs", "fg_exe", "fg_hung", "ui_ms", "dwm_ms",
    "user_idle_s", "game_session", "extra",
]
INSERT_SAMPLE = f"INSERT OR REPLACE INTO samples ({','.join(SAMPLE_COLS)}) VALUES ({','.join('?' * len(SAMPLE_COLS))})"

FREEZE_MS = 2000      # shell/DWM unresponsive this long = freeze incident
TEMP_LIMITS = {"cpu_temp": 95, "gpu_hotspot": 105, "gpu_mem_temp": 100}

SRC_DIR = Path(__file__).resolve().parent
RELOAD_FLAG = config.DATA_DIR / "reload.request"   # touched by `pm collector reload`
DUMP_FLAG = config.DATA_DIR / "sensors_dump.request"   # touched by `pm sensors dump`
DUMP_PATH = config.DATA_DIR / "sensors_dump.json"
RELOAD_CHECK_S = 5
MUTEX_NAME = os.environ.get("PERFMON_MUTEX", "Global\\PerfMonitorCollector")   # env override: tests


def source_stamp():
    """Newest mtime of the collector's modules (ui/ is not imported by the collector)."""
    return max(p.stat().st_mtime for p in SRC_DIR.glob("*.py"))


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class Collector:
    def __init__(self):
        self.cfg = config.load()
        self.con = db.connect()
        self.lock = threading.Lock()   # sqlite connection shared with PresentMon reader threads
        self.running = True
        self.hw = HardwareSensors(self.cfg["lhm_dir"])
        self.pdh = PdhCounters()
        self.wd = Watchdog()
        self.events = EventReader()
        self.name_cache = {}
        self.proc_prev = {}
        self.last_proc = 0.0
        self.last_events = 0.0
        self.last_sysinfo = 0.0
        self.last_retention = 0.0
        self.net_prev = psutil.net_io_counters()
        self.net_prev_t = time.time()
        self.games = {}                # pid -> dict(session, capture, exe)
        self.open_incidents = {}       # kind -> incident id (for ongoing freezes/temps)
        self.cores = psutil.cpu_percent(percpu=True)
        self.reload = False            # set when the loop exits to hand over to a new instance
        self.src_stamp = source_stamp()
        self.last_reload_check = time.time()
        self.reload_deferred_logged = False

    # ---------- helpers ----------
    def exec(self, sql, args=()):
        with self.lock:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur

    def incident(self, kind, severity, title, details=None, ts=None, end_ts=None):
        log.info("INCIDENT %s: %s", kind, title)
        cur = self.exec("INSERT INTO incidents(ts, end_ts, kind, severity, title, details) VALUES (?,?,?,?,?,?)",
                        (ts or time.time(), end_ts, kind, severity, title,
                         json.dumps(details, ensure_ascii=False) if details is not None else None))
        return cur.lastrowid

    def pname(self, pid):
        if pid is None:
            return None
        n = self.name_cache.get(pid)
        if n is None:
            try:
                n = psutil.Process(pid).name()
            except Exception:
                n = "?"
            self.name_cache[pid] = n
        return n

    # ---------- startup ----------
    def on_start(self):
        boot = psutil.boot_time()
        last_ts = self.con.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
        prev_boot = db.get_meta(self.con, "boot_time")
        if last_ts and prev_boot and abs(float(prev_boot) - boot) > 60:
            # New boot since last run: record where data stopped so freezes before power-off are findable
            self.incident("reboot", "info", "Restart systemu (koniec poprzednich danych)",
                          {"last_sample": last_ts, "boot": boot, "gap_s": round(boot - last_ts)}, ts=last_ts)
        with self.lock:
            db.set_meta(self.con, "boot_time", boot)
            db.set_meta(self.con, "collector_started", time.time())
            db.set_meta(self.con, "admin", int(is_admin()))
            db.set_meta(self.con, "hw", json.dumps(self.hw.describe()))
            # Close sessions left open by a previous crash of the collector
            self.con.execute("UPDATE sessions SET end_ts=start_ts, end_reason='unknown' WHERE end_ts IS NULL")
            self.con.commit()
        log.info("collector start, admin=%s", is_admin())

    # ---------- per-second sample ----------
    def sample(self):
        now = time.time()
        vm = psutil.virtual_memory()
        cores = psutil.cpu_percent(percpu=True)
        p = self.pdh.read()
        h = self.hw.read()
        ui_ms, dwm_ms = self.wd.take()
        _, fg_pid, fg_hung = foreground()

        disks = p.get("disks", {})
        worst_lat, worst_disk, rd, wr, queue, busy = 0.0, None, 0.0, 0.0, 0.0, 0.0
        extra_disks = {}
        for inst, d in disks.items():
            lat = (d.get("lat") or 0) * 1000
            if lat > worst_lat:
                worst_lat, worst_disk = lat, inst
            rd += d.get("rd") or 0
            wr += d.get("wr") or 0
            queue = max(queue, d.get("queue") or 0)
            busy = max(busy, 100 - min(100, d.get("idle") or 100))
            extra_disks[inst] = {"lat_ms": round(lat, 2), "rd_mbs": round((d.get("rd") or 0) / 1e6, 2),
                                 "wr_mbs": round((d.get("wr") or 0) / 1e6, 2), "q": d.get("queue")}

        net = psutil.net_io_counters()
        dt = max(0.001, now - self.net_prev_t)
        rx = (net.bytes_recv - self.net_prev.bytes_recv) / dt / 1024
        tx = (net.bytes_sent - self.net_prev.bytes_sent) / dt / 1024
        self.net_prev, self.net_prev_t = net, now

        clock = h.get("cpu_clock_mhz")
        if not clock and p.get("perf_pct"):
            clock = 4500 * p["perf_pct"] / 100  # 7700X base clock * % Processor Performance

        session = next(iter(self.games.values()))["session"] if self.games else None
        extra = {"disks": extra_disks}
        if self.hw.storage_temps:
            extra["disk_temps"] = self.hw.storage_temps

        row = dict(
            ts=now, cpu_total=sum(cores) / len(cores), cpu_core_max=max(cores), cpu_clock_mhz=clock,
            cpu_temp=h.get("cpu_temp"), cpu_power_w=h.get("cpu_power_w"), dpc_pct=p.get("dpc_pct"),
            interrupt_pct=p.get("interrupt_pct"), ctx_switch_s=p.get("ctx_switch_s"), proc_queue=p.get("proc_queue"),
            ram_used_gb=(vm.total - vm.available) / 2**30, ram_pct=vm.percent,
            commit_gb=(p.get("commit_bytes") or 0) / 2**30, commit_limit_gb=(p.get("commit_limit") or 0) / 2**30,
            hard_faults_s=p.get("hard_faults_s"),
            **{k: h.get(k) for k in ("gpu_load", "gpu_3d", "gpu_vram_mb", "gpu_vram_total_mb", "gpu_clock_mhz",
                                      "gpu_mem_clock_mhz", "gpu_temp", "gpu_hotspot", "gpu_mem_temp",
                                      "gpu_power_w", "gpu_fan_rpm")},
            disk_read_mbs=rd / 1e6, disk_write_mbs=wr / 1e6, disk_lat_ms=worst_lat, disk_lat_disk=worst_disk,
            disk_queue=queue, disk_busy_pct=busy, net_rx_kbs=rx, net_tx_kbs=tx,
            fg_exe=self.pname(fg_pid), fg_hung=int(fg_hung), ui_ms=ui_ms, dwm_ms=dwm_ms,
            user_idle_s=user_idle_s(), game_session=session, extra=json.dumps(extra),
        )
        self.exec(INSERT_SAMPLE, [row[c] for c in SAMPLE_COLS])
        self.detect_live(row)

    # ---------- live incident detection ----------
    def _ongoing(self, kind, active, severity, title, row, details_fn):
        iid = self.open_incidents.get(kind)
        if active and iid is None:
            self.open_incidents[kind] = self.incident(kind, severity, title, details_fn(), ts=row["ts"])
        elif not active and iid is not None:
            self.exec("UPDATE incidents SET end_ts=? WHERE id=?", (row["ts"], iid))
            del self.open_incidents[kind]

    def snapshot_procs(self):
        """Top processes right now - attached to freeze incidents."""
        out = []
        for pr in psutil.process_iter(["name", "pid", "memory_info"]):
            try:
                out.append((pr.info["name"], pr.info["pid"], pr.cpu_percent(None), pr.info["memory_info"].rss / 2**20))
            except Exception:
                pass
        out.sort(key=lambda x: -x[2])
        return [{"name": n, "pid": pid, "cpu": round(c, 1), "mem_mb": round(m)} for n, pid, c, m in out[:12]]

    def detect_live(self, r):
        ctx = lambda: {"fg_exe": r["fg_exe"], "ui_ms": round(r["ui_ms"]), "dwm_ms": round(r["dwm_ms"]),
                       "cpu": round(r["cpu_total"]), "gpu": r["gpu_load"], "ram_pct": r["ram_pct"],
                       "disk_lat_ms": round(r["disk_lat_ms"], 1), "user_idle_s": r["user_idle_s"],
                       "top_procs": self.snapshot_procs()}
        self._ongoing("ui_freeze", r["ui_ms"] >= FREEZE_MS, "critical",
                      "Pulpit (explorer) przestał odpowiadać", r, ctx)
        self._ongoing("dwm_stall", r["dwm_ms"] >= FREEZE_MS, "critical",
                      "Kompozytor okien (DWM) się zaciął – obraz zamrożony", r, ctx)
        self._ongoing("app_not_responding", bool(r["fg_hung"]), "serious",
                      f"Okno nie odpowiada: {r['fg_exe']}", r, ctx)
        for k, lim in TEMP_LIMITS.items():
            v = r.get(k)
            self._ongoing(f"hot_{k}", bool(v and v >= lim), "warning", f"Wysoka temperatura: {k} ≥ {lim}°C", r,
                          lambda: {"value": v})
        vram, vt = r.get("gpu_vram_mb"), r.get("gpu_vram_total_mb")
        self._ongoing("vram_full", bool(vram and vt and vram / vt > 0.95), "warning", "VRAM prawie pełny (>95%)", r,
                      lambda: {"vram_mb": vram, "total_mb": vt, "fg_exe": r["fg_exe"]})
        cl = r.get("commit_limit_gb") or 0
        self._ongoing("commit_full", bool(cl and r["commit_gb"] / cl > 0.92), "serious",
                      "Pamięć zadeklarowana (commit) >92% limitu", r, ctx)

    # ---------- processes ----------
    def sample_procs(self):
        now = time.time()
        rows, cur = [], {}
        ncpu = psutil.cpu_count()
        for pr in psutil.process_iter(["pid", "name", "cpu_times", "memory_info"]):
            try:
                i = pr.info
                ct = i["cpu_times"]
                cpu_s = ct.user + ct.system
                try:
                    io = pr.io_counters()
                    iob = io.read_bytes + io.write_bytes
                except Exception:
                    iob = 0
                cur[i["pid"]] = (cpu_s, iob, now)
                prev = self.proc_prev.get(i["pid"])
                if prev:
                    dt = now - prev[2]
                    rows.append((i["name"], i["pid"], (cpu_s - prev[0]) / dt * 100 / ncpu,
                                 i["memory_info"].rss / 2**20, (iob - prev[1]) / dt / 1e6))
            except Exception:
                pass
        self.proc_prev = cur
        if not rows:
            return
        out = []
        for kind, key, n in (("cpu", 2, 8), ("mem", 3, 5), ("io", 4, 5)):
            for name, pid, c, m, io in sorted(rows, key=lambda x: -x[key])[:n]:
                if kind == "io" and io < 0.5:
                    continue
                out.append((now, kind, name, pid, round(c, 2), round(m, 1), round(io, 2)))
        with self.lock:
            self.con.executemany("INSERT INTO procs VALUES (?,?,?,?,?,?,?)", out)
            self.con.commit()

    # ---------- games ----------
    def is_game(self, name, path):
        n = (name or "").lower()
        if n in self.cfg["not_game_exes"] or not n.endswith(".exe"):
            return False
        if n in self.cfg["game_exes"]:
            return True
        pl = (path or "").lower()
        if "\\windowsapps\\" in pl and "xbox" not in pl and "game" not in pl:
            return False  # store apps that are not games
        return any(m in pl for m in self.cfg["game_path_markers"])

    def check_games(self):
        alive = set()
        for pr in psutil.process_iter(["pid", "name", "exe"]):
            try:
                pid, name, exe = pr.info["pid"], pr.info["name"], pr.info["exe"]
            except Exception:
                continue
            if pid in self.games:
                alive.add(pid)
                continue
            if not self.is_game(name, exe):
                continue
            # Ignore helper processes that use almost no memory (launch stubs, updaters)
            try:
                if pr.memory_info().rss < 300 * 2**20 and name.lower() not in self.cfg["game_exes"]:
                    continue
            except Exception:
                continue
            self.start_game(pid, name, exe)
            alive.add(pid)
        for pid in list(self.games):
            if pid not in alive:
                self.end_game(pid, "exit")

    def start_game(self, pid, name, exe):
        try:
            ctx = gamectx.collect(exe, self.hw.gpu.Name if self.hw.gpu is not None else None)
        except Exception as e:
            ctx = {"error": str(e)}
        sid = self.exec("INSERT INTO sessions(exe, path, pid, start_ts, context) VALUES (?,?,?,?,?)",
                        (name, exe, pid, time.time(), json.dumps(ctx))).lastrowid
        cap = None
        if os.path.exists(self.cfg["presentmon_exe"]):
            cap = FrameCapture(self.cfg["presentmon_exe"], name, pid, sid, self.on_frame_second, self.on_stutter)
        self.games[pid] = {"session": sid, "capture": cap, "exe": name, "last_summary": time.time()}
        log.info("game start %s pid=%s session=%s", name, pid, sid)

    def on_frame_second(self, r):
        cols = list(r)
        self.exec(f"INSERT OR REPLACE INTO frames ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  [r[c] for c in cols])

    def on_stutter(self, s):
        self.exec("INSERT INTO stutters(ts, session, ft_ms, median_ms) VALUES (?,?,?,?)",
                  (s["ts"], s["session"], s["ft_ms"], s["median_ms"]))

    def save_summary(self, g):
        summ = g["capture"].summary() if g["capture"] else {"error": "PresentMon niedostępny"}
        self.exec("UPDATE sessions SET summary=? WHERE id=?", (json.dumps(summ), g["session"]))

    def end_game(self, pid, reason):
        g = self.games.pop(pid)
        if g["capture"]:
            g["capture"].stop()
        end = time.time()
        # A crash/hang event for this exe within the last minute means it did not exit cleanly
        crashed = self.exec(
            "SELECT 1 FROM events WHERE ts > ? AND event_id IN (1000,1002) AND message LIKE ? LIMIT 1",
            (end - 90, f"%{g['exe']}%")).fetchone()
        if reason == "exit" and crashed:
            reason = "crash"
        self.save_summary(g)
        self.exec("UPDATE sessions SET end_ts=?, end_reason=? WHERE id=?", (end, reason, g["session"]))
        log.info("game end %s session=%s reason=%s", g["exe"], g["session"], reason)

    # ---------- event log ----------
    def poll_events(self):
        with self.lock:
            last = db.get_meta(self.con, "events_since")
        since = float(last) if last else time.time() - 14 * 86400
        newest = since
        for logname in ("System", "Application"):
            for ev in self.events.read_since(logname, since):
                newest = max(newest, ev["ts"])
                cur = self.exec("INSERT OR IGNORE INTO events(ts, log, provider, event_id, level, record_id, message) "
                                "VALUES (:ts,:log,:provider,:event_id,:level,:record_id,:message)", ev)
                if cur.rowcount:
                    c = classify(ev)
                    if c:
                        self.incident(c[0], c[1], c[2], {"event_id": ev["event_id"], "provider": ev["provider"],
                                                         "message": ev["message"][:1500]}, ts=ev["ts"])
        with self.lock:
            db.set_meta(self.con, "events_since", newest)
            self.con.commit()

    # ---------- housekeeping ----------
    def housekeeping(self, now):
        if now - self.last_sysinfo > 86400:
            self.last_sysinfo = now
            threading.Thread(target=self._sysinfo, daemon=True).start()
        if now - self.last_retention > 3600:
            self.last_retention = now
            cutoff = now - self.cfg["retention_days"] * 86400
            for t in ("samples", "frames", "stutters", "procs", "events"):
                self.exec(f"DELETE FROM {t} WHERE ts < ?", (cutoff,))
            self.name_cache.clear()

    def _sysinfo(self):
        info = sysinfo.collect()
        prev = self.exec("SELECT info FROM system_info ORDER BY ts DESC LIMIT 1").fetchone()
        self.exec("INSERT INTO system_info VALUES (?, ?)", (time.time(), json.dumps(info, ensure_ascii=False)))
        if prev:
            old = json.loads(prev[0])
            changed = {k: {"before": old.get(k), "after": v} for k, v in info.items()
                       if k not in ("last_boot", "volumes", "background_tools", "pagefile") and old.get(k) != v}
            if changed:
                self.incident("config_change", "info", "Zmiana konfiguracji: " + ", ".join(changed), changed)

    # ---------- self reload ----------
    def check_reload(self, now):
        """Hand over to a fresh instance when perfmon/*.py changed or a reload was requested.

        The new process inherits our elevation, so code changes never need a UAC prompt."""
        if now - self.last_reload_check < RELOAD_CHECK_S:
            return
        self.last_reload_check = now
        if DUMP_FLAG.exists():
            DUMP_FLAG.unlink(missing_ok=True)
            tmp = DUMP_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": now, "admin": is_admin(), "sensors": self.hw.dump()},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(DUMP_PATH)
            log.info("sensor dump written to %s", DUMP_PATH)
        requested = RELOAD_FLAG.exists()
        stamp = source_stamp()
        if stamp == self.src_stamp and not requested:
            return
        if not requested and now - stamp < 3:
            return  # files may still be mid-save
        if self.games:
            # Restarting would split the game session and its frame capture in two
            if not self.reload_deferred_logged:
                log.info("reload pending, deferred until the game session ends")
                self.reload_deferred_logged = True
            return
        self.src_stamp = stamp
        RELOAD_FLAG.unlink(missing_ok=True)
        err = self._import_check()
        if err:
            log.error("reload aborted, new code does not import:\n%s", err)
            return
        log.info("reloading collector (%s)", "requested" if requested else "source changed")
        self.reload = True
        self.running = False

    @staticmethod
    def _import_check():
        """Import the new code in a throwaway interpreter; returns the error text or None."""
        try:
            r = subprocess.run([sys.executable.replace("pythonw.exe", "python.exe"), "-c",
                                "import perfmon.collector, perfmon.analyze"],
                               cwd=str(SRC_DIR.parent), capture_output=True, text=True, timeout=120,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            return None if r.returncode == 0 else (r.stderr or r.stdout)[-3000:]
        except Exception as e:
            return str(e)

    # ---------- main loop ----------
    def run(self):
        self.on_start()
        interval = self.cfg["sample_interval_s"]
        nxt = time.time()
        while self.running:
            now = time.time()
            try:
                self.sample()
                if now - self.last_proc >= self.cfg["proc_interval_s"]:
                    self.last_proc = now
                    self.sample_procs()
                    self.check_games()
                    for g in self.games.values():
                        if g["capture"] and now - g["last_summary"] > 60:
                            g["last_summary"] = now
                            self.save_summary(g)
                if now - self.last_events >= self.cfg["event_poll_s"]:
                    self.last_events = now
                    self.poll_events()
                self.housekeeping(now)
                self.check_reload(now)
            except Exception:
                log.exception("loop error")
            nxt += interval
            time.sleep(max(0.0, nxt - time.time()))
            if time.time() - nxt > 5:
                nxt = time.time()  # resumed from sleep: don't spin to catch up
        self.shutdown()

    def shutdown(self):
        for pid in list(self.games):
            self.end_game(pid, "collector_stop")
        self.wd.stop()
        self.hw.close()
        with self.lock:
            db.set_meta(self.con, "collector_stopped", time.time())
            self.con.commit()


def setup_logging():
    config.ensure_dirs()
    h = logging.handlers.RotatingFileHandler(config.LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)
    if sys.stdout and sys.stdout.isatty():
        root.addHandler(logging.StreamHandler())


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)


def single_instance(wait_s=0.0):
    """Named mutex so the scheduled task and a manual run never collect twice.

    Returns the mutex handle, or None if another collector holds it after wait_s seconds
    (a reloaded instance waits for its predecessor to exit)."""
    deadline = time.time() + wait_s
    while True:
        h = _k32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return h
        _k32.CloseHandle(h)
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def spawn_successor():
    """Start a new collector that takes over once we exit; it inherits our token (elevation)."""
    args = [sys.executable, "-m", "perfmon.collector", "--wait-mutex"]
    base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    for flags in (base | subprocess.CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            subprocess.Popen(args, cwd=str(SRC_DIR.parent), creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue  # breakaway is denied when our job object does not allow it
    log.error("could not start the successor collector")
    return False


def main():
    setup_logging()
    mutex = single_instance(90 if "--wait-mutex" in sys.argv else 0)
    if not mutex:
        log.info("collector already running, exiting")
        return
    c = Collector()
    signal.signal(signal.SIGINT, lambda *a: setattr(c, "running", False))
    signal.signal(signal.SIGTERM, lambda *a: setattr(c, "running", False))
    duration = float(os.environ.get("PERFMON_RUN_SECONDS", "0"))
    if duration:
        threading.Timer(duration, lambda: setattr(c, "running", False)).start()
    c.run()
    if c.reload:
        c.con.close()
        _k32.CloseHandle(mutex)
        spawn_successor()


if __name__ == "__main__":
    main()
