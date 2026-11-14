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
from collections import deque
from ctypes import wintypes
from pathlib import Path

import psutil

from . import (bottleneck, config, crashinfo, db, diskinfo, etwbox, gamectx, gameplay, gpuproc, marks, memleak, ocanalyze, ocmon,
               presentmon, procwatch, shadercache, sysinfo)
from .episodes import Tracker as EpisodeTracker, dwm_visible
from .events import LOGS, EventReader, classify
from .netmon import INCIDENT_KINDS as NET_INCIDENTS, NetMonitor
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
    "user_idle_s", "game_session", "extra", "pagefile_pct", "cpu_vcore", "gpu_voltage", "vrm_temp", "cpu_eff_max",
]
INSERT_SAMPLE = f"INSERT OR REPLACE INTO samples ({','.join(SAMPLE_COLS)}) VALUES ({','.join('?' * len(SAMPLE_COLS))})"
DETAIL_COLS = [
    "ts", "session", "cpu_eff", "cpu_clk", "cpu_core_w", "thread_load", "cpu_vcore", "cpu_vsoc", "cpu_vmisc",
    "cpu_pkg_w", "tctl", "tccd", "vrm_temp", "vsoc_temp", "dimm_temp", "cpu_fan", "gpu_clk", "gpu_mem_clk", "gpu_v",
    "gpu_w", "gpu_load", "gpu_temp", "gpu_hot", "gpu_mem_temp", "gpu_fan",
]
INSERT_DETAIL = f"INSERT OR REPLACE INTO hw_detail ({','.join(DETAIL_COLS)}) VALUES ({','.join('?' * len(DETAIL_COLS))})"

FREEZE_MS = 2000      # shell/DWM unresponsive this long = freeze incident
TEMP_LIMITS = {"cpu_temp": 95, "gpu_hotspot": 105, "gpu_mem_temp": 100, "vrm_temp": 100}
VSOC_MAX = 1.30       # V: upper safe limit of Zen 4 SoC voltage
OC_ALERT_S = 5        # UV/OC alerts fire only after the condition held this long
STRETCH_MAX = 0.97    # effective / reference clock of a fully loaded core below this = clock stretching
CRASH_LATE_S = 75     # how long after a game exits a WER crash event may still show up
BSOD_LOCK = threading.Lock()
ANALYSIS_VERSION = "2"
BOTTLENECK_EVERY_S = 300   # summary.bottleneck refresh while a game runs (and once at its end)

SRC_DIR = Path(__file__).resolve().parent
RELOAD_FLAG = config.DATA_DIR / "reload.request"   # touched by `pm collector reload`
DUMP_FLAG = config.DATA_DIR / "sensors_dump.request"   # touched by `pm sensors dump`
DUMP_PATH = config.DATA_DIR / "sensors_dump.json"
DUMPS_FLAG = config.DATA_DIR / "dumps.request"   # `pm dumps enable|disable` (WER LocalDumps, HKLM)
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
        self.gpu_procs = gpuproc.GpuProcs()
        self.game_sampler = gpuproc.GameProcSampler()
        self.last_gpu_proc = 0.0
        self.procwatch = procwatch.ProcWatch(self.cfg)
        self.shader = shadercache.ShaderWatch(self.cfg.get("shader_cache_dirs") or shadercache.DEFAULT_DIRS,
                                              self.cfg.get("shader_scan_interval_s", 10))
        self.shader_pid = None         # game whose shader caches ShaderWatch measures
        self.etw = etwbox.EtwBox(self.cfg, self.save_etw_capture)
        self.etw_pending = []          # (due ts, capture id) waiting for analysis
        self.etw_proc = None           # running analysis process
        self.net = NetMonitor(self.cfg, self.on_net_second, self._net_incident, self._close_incident)
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
        self.episodes = EpisodeTracker()   # flapping conditions: one incident per episode (perfmon/episodes.py)
        self.cores = psutil.cpu_percent(percpu=True)
        self.not_games = set()         # (pid, create_time) of processes already ruled out as games
        self.pending_crash_checks = [] # (session, exe, end_ts) re-checked for late WER crash events
        self.reload = False            # set when the loop exits to hand over to a new instance
        self.src_stamp = source_stamp()
        self.last_reload_check = time.time()
        self.reload_deferred_logged = False
        self.import_check = None       # {"requested", "err"} while the new code is being import-checked
        self.mark_queue = deque()      # hotkey presses (ts), filled by the hotkey thread
        self.hotkey = marks.Hotkey(self.cfg.get("hotkey_mark"), self.mark_queue.append, self.cfg.get("hotkey_beep", True))
        self.manual = {}               # session id -> kind of open `pm stress start` / idle-soak sessions
        self.last_detail = 0.0
        self.oc_since = {}             # UV/OC alert key -> ts the condition started (debounce)
        self.gpu_clk_hist = []         # (ts, MHz) of the GPU core clock under full load, last 60 s
        self.gpu_od = (ocmon.GpuOverdrive(self.hw.gpu.Name if self.hw.gpu is not None else None)
                       if self.cfg["gpu_od_poll_s"] else None)
        self.last_gpu_od = 0.0
        self.last_oc_stats = 0.0
        self.vacuuming = False

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

    def _net_incident(self, kind, severity, title, details, ts):
        return self.incident(kind, severity, title, details, ts=ts)

    def _close_incident(self, iid, ts):
        self.exec("UPDATE incidents SET end_ts=? WHERE id=?", (ts, iid))

    def on_net_second(self, r):
        cols = list(r)
        self.exec(f"INSERT OR REPLACE INTO net_samples ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  [r[c] for c in cols])

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
        new_boot = bool(prev_boot and abs(float(prev_boot) - boot) > 60)
        if last_ts and new_boot:
            # New boot since last run: record where data stopped so freezes before power-off are findable
            self.incident("reboot", "info", "Restart systemu (koniec poprzednich danych)",
                          {"last_sample": last_ts, "boot": boot, "gap_s": round(boot - last_ts)}, ts=last_ts)
        with self.lock:
            db.set_meta(self.con, "boot_time", boot)
            db.set_meta(self.con, "collector_started", time.time())
            db.set_meta(self.con, "admin", int(is_admin()))
            db.set_meta(self.con, "hw", json.dumps(self.hw.describe()))
            # Close sessions left open by a previous crash of the collector
            # (end = last data we have for them, e.g. right before a hard reset)
            self.con.execute(
                "UPDATE sessions SET end_reason='unknown', end_ts=MAX(start_ts, "
                "COALESCE((SELECT MAX(ts) FROM frames WHERE session=sessions.id), 0), "
                "COALESCE((SELECT MAX(ts) FROM samples WHERE game_session=sessions.id), 0)) WHERE end_ts IS NULL"
                # `pm stress` sessions survive a collector reload, not a reboot
                + ("" if new_boot else " AND COALESCE(kind, 'game') NOT IN ('manual', 'idle_soak')"))
            # Ongoing network incidents left open the same way end where network data stopped
            self.con.execute(
                f"UPDATE incidents SET end_ts=MAX(ts, COALESCE((SELECT MAX(ts) FROM net_samples), ts)) "
                f"WHERE end_ts IS NULL AND kind IN ({','.join('?' * len(NET_INCIDENTS))})", NET_INCIDENTS)
            self.con.commit()
        presentmon.cleanup_stale_sessions()   # ETW sessions leaked by earlier collectors block new captures
        self.vacuum_setup()
        self.upgrade_analysis()
        log.info("collector start, admin=%s", is_admin())

    def upgrade_analysis(self):
        """Stored results follow the current analysis rules: recomputed once after an upgrade."""
        with self.lock:
            if db.get_meta(self.con, "analysis_version", "0") == ANALYSIS_VERSION:
                return
        try:
            n = bottleneck.refresh_all(self.con, self.merge_summary)
            with self.lock:
                etwbox.fix_old_headlines(self.con)
                db.set_meta(self.con, "analysis_version", ANALYSIS_VERSION)
                self.con.commit()
            log.info("analysis upgraded to %s (%s sessions)", ANALYSIS_VERSION, n)
        except Exception:
            log.exception("analysis upgrade")

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

        session = next(iter(self.games.values()))["session"] if self.games else next(iter(self.manual), None)
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
            pagefile_pct=p.get("pagefile_pct"),
            **{k: h.get(k) for k in ("cpu_vcore", "gpu_voltage", "vrm_temp", "cpu_eff_max")},
        )
        self.exec(INSERT_SAMPLE, [row[c] for c in SAMPLE_COLS])
        det = h.get("detail")
        if det:
            det = dict(det, ts=now, session=session, thread_load=[round(c) for c in cores])
            if session is not None or now - self.last_detail >= self.cfg["detail_interval_s"]:
                self.last_detail = now
                self.exec(INSERT_DETAIL, [json.dumps(det.get(c)) if isinstance(det.get(c), list) else det.get(c)
                                          for c in DETAIL_COLS])
            self.detect_oc(row, det)
        self.detect_live(row)
        self.etw.on_sample(row["ts"], row["dpc_pct"], session)

    # ---------- live incident detection ----------
    def _ongoing(self, kind, active, severity, title, row, details_fn):
        iid = self.open_incidents.get(kind)
        if active and iid is None:
            self.open_incidents[kind] = self.incident(kind, severity, title, details_fn(), ts=row["ts"])
            self.episodes.start(kind, row)
            if kind in ("ui_freeze", "dwm_stall"):
                self.etw.trigger(kind)
        elif active:
            self.episodes.hit(kind, row)
        elif iid is not None and self.episodes.off(kind, row["ts"]):
            self._end_episode(kind, iid, row["ts"])

    def _end_episode(self, kind, iid, now):
        """Close an ongoing incident; its details get the episode's count and worst value."""
        end, patch = self.episodes.close(kind, now)
        old = self.exec("SELECT details FROM incidents WHERE id=?", (iid,)).fetchone()
        try:
            det = json.loads(old[0]) if old and old[0] else {}
        except ValueError:
            det = {}
        if isinstance(det, dict):
            det.update(patch)
        self.exec("UPDATE incidents SET end_ts=?, details=? WHERE id=?", (end, json.dumps(det, ensure_ascii=False), iid))
        self.open_incidents.pop(kind, None)

    def _held(self, key, active, now):
        """True once `active` has held for OC_ALERT_S seconds (debounce for UV/OC alerts)."""
        if not active:
            self.oc_since.pop(key, None)
            return False
        return now - self.oc_since.setdefault(key, now) >= OC_ALERT_S

    def detect_oc(self, r, d):
        now = r["ts"]
        vsoc = d.get("cpu_vsoc")
        self._ongoing("vsoc_high", self._held("vsoc", bool(vsoc and vsoc > VSOC_MAX), now), "serious",
                      f"Napięcie SoC powyżej {VSOC_MAX:.2f} V", r, lambda: {"vsoc": vsoc})
        # clock stretching: every SMT thread of a core fully loaded, effective clock clearly below the reference
        loads, eff, clk = d.get("thread_load") or [], d.get("cpu_eff") or [], d.get("cpu_clk") or []
        tpc = max(1, len(loads) // len(clk)) if clk else 1
        stretched = {}
        for i, (e, c) in enumerate(zip(eff, clk)):
            th = loads[i * tpc:(i + 1) * tpc]
            if e and c and th and min(th) >= 95 and e / c < STRETCH_MAX:
                stretched[i] = round(100 * (1 - e / c), 1)
        self._ongoing("clock_stretching", self._held("stretch", bool(stretched), now), "warning",
                      "Clock stretching CPU: " + ", ".join(f"rdzeń {k} −{v}%" for k, v in stretched.items()), r,
                      lambda: {"cores": stretched, "vcore": d.get("cpu_vcore"), "pkg_w": d.get("cpu_pkg_w"),
                               "session": r["game_session"]})
        # sudden GPU clock drop at the same (full) load, vs the median of the last 60 s at full load
        gl, gc = d.get("gpu_load"), d.get("gpu_clk")
        hist = self.gpu_clk_hist = [(t, v) for t, v in self.gpu_clk_hist if now - t <= 60]
        ref = sorted(v for _, v in hist)[len(hist) // 2] if len(hist) >= 20 else None
        drop = bool(gl is not None and gl >= 95 and gc and ref and gc < 0.85 * ref)
        if gl is not None and gl >= 95 and gc and not drop:
            hist.append((now, gc))
        self._ongoing("gpu_clock_drop", self._held("gpu_drop", drop, now), "warning",
                      "Nagły spadek zegara GPU przy pełnym obciążeniu", r,
                      lambda: {"clock_mhz": gc, "median_60s": ref, "gpu_temp": d.get("gpu_temp"),
                               "hotspot": d.get("gpu_hot"), "power_w": d.get("gpu_w")})

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
        # a stall nobody sees (display asleep while the user is away) is not a freeze: see perfmon/episodes.py
        stall = r["dwm_ms"] >= FREEZE_MS and dwm_visible(r["user_idle_s"], r["game_session"])
        self._ongoing("dwm_stall", stall, "critical",
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

    # ---------- user marks ----------
    def process_marks(self):
        reqs = []
        while self.mark_queue:
            reqs.append({"ts": self.mark_queue.popleft(), "text": None, "source": "hotkey"})
        reqs += marks.take_requests()
        for r in reqs:
            session = next(iter(self.games.values()))["session"] if self.games else None
            with self.lock:
                det = marks.context(self.con, r["ts"], session=session)
            det.update(text=r["text"], source=r["source"], top_procs=self.snapshot_procs())
            self.incident("user_mark", "info", marks.title(r["text"]), det, ts=r["ts"])
            self.etw.trigger("user_mark", session)

    # ---------- processes ----------
    def sample_procs(self):
        now = time.time()
        rows, cur = [], {}
        ncpu = psutil.cpu_count()
        snap = {}   # pid -> (create_time, name, ppid) for the process start/stop log
        for pr in psutil.process_iter(["pid", "name", "cpu_times", "memory_info", "ppid", "create_time"]):
            try:
                i = pr.info
                if i["pid"] == 0:
                    continue  # "System Idle Process": idle time, not load
                if i["create_time"] is not None:   # psutil gives None when it cannot read it
                    snap[i["pid"]] = (i["create_time"], i["name"], i["ppid"])
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
                out.append((now, kind, name, pid, round(c, 2), round(m, 1), round(io, 2), None, None))
        session = next(iter(self.games.values()))["session"] if self.games else None
        pe = self.procwatch.update(now, snap, session)
        game_rows = []
        if now - self.last_gpu_proc >= self.cfg.get("gpu_proc_interval_s", 5) - 0.5:
            self.last_gpu_proc = now
            out += self.sample_gpu(now, {r[1]: r for r in rows}, game_rows)
        with self.lock:
            self.con.executemany("INSERT INTO procs(ts, kind, name, pid, cpu_pct, mem_mb, io_mbs, gpu_pct, vram_mb) "
                                 "VALUES (?,?,?,?,?,?,?,?,?)", out)
            if pe:
                self.con.executemany("INSERT INTO proc_events(ts, pid, name, ppid, parent_name, action, session, path) "
                                     "VALUES (:ts,:pid,:name,:ppid,:parent_name,:action,:session,:path)", pe)
            for g in game_rows:
                self.con.execute(f"INSERT OR REPLACE INTO game_proc ({','.join(g)}) VALUES ({','.join('?' * len(g))})",
                                 list(g.values()))
            self.con.commit()

    def sample_gpu(self, now, by_pid, game_rows):
        """procs rows kind='gpu' (top 5 without the game) + game_proc rows of running games."""
        gpu = self.gpu_procs.read()
        for pid, g in self.games.items():
            spid = next(reversed(g.get("children", {})), pid)   # the child that presents (sp26-cod.exe), not the launcher
            r = self.game_sampler.sample(spid, g["session"], gpu.get(spid), now)
            if r:
                if pid == self.shader_pid:
                    r["shader_bytes"] = self.shader.latest()[1]
                game_rows.append(r)
        out = []
        for pid, d in gpuproc.top_background(gpu, self.game_pids()):
            r = by_pid.get(pid)
            out.append((now, "gpu", r[0] if r else self.pname(pid), pid, round(r[2], 2) if r else None,
                        round(r[3], 1) if r else None, None, round(d["gpu"], 1),
                        round(d["ded_mb"]) if d["ded_mb"] is not None else None))
        return out

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

    def game_pids(self):
        """PIDs of every running game process, children included."""
        return set(self.games).union(*(g.get("children", {}) for g in self.games.values()))

    def game_ancestor(self, pr):
        """PID of the running game this process descends from (cod.exe -> sp26-cod.exe), else None."""
        try:
            for p in pr.parents()[:6]:
                if p.pid in self.games:
                    return p.pid
        except psutil.Error:
            pass
        return None

    def check_games(self):
        alive, live_pids = set(), set()
        children = {c for g in self.games.values() for c in g["children"]}
        for pr in psutil.process_iter(["pid", "name", "create_time"]):
            try:
                pid, name = pr.info["pid"], pr.info["name"]
                key = (pid, pr.info["create_time"])
            except Exception:
                continue
            live_pids.add(key)
            if pid in self.games:
                alive.add(pid)
                continue
            if pid in children:
                continue   # attached to a game below
            if key in self.not_games:
                continue  # already ruled out; skip the costly exe lookup
            try:
                exe = pr.exe()
            except Exception:
                exe = None
            stress = ocmon.stress_kind(name, exe, self.cfg)
            if stress and any(g.get("stress") == stress[0] for g in self.games.values()):
                self.not_games.add(key)   # helper exe of a test that already has a session
                continue
            if stress:
                self.start_game(pid, name, exe, stress=stress)
                alive.add(pid)
                continue
            if not self.is_game(name, exe):
                self.not_games.add(key)
                continue
            # Ignore helper processes that use almost no memory (launch stubs, updaters)
            try:
                if pr.memory_info().rss < 300 * 2**20 and name.lower() not in self.cfg["game_exes"]:
                    continue
            except Exception:
                continue
            parent = self.game_ancestor(pr)
            if parent is not None:
                # a game process spawned by a running game is part of its session, not a session of its own
                # (CoD: cod.exe is the launcher, the child sp26-cod.exe renders; PresentMon must follow it)
                self.attach_child(parent, pid, name)
                children.add(pid)
                continue
            self.start_game(pid, name, exe)
            alive.add(pid)
        self.not_games &= live_pids
        live = {k[0] for k in live_pids}
        for pid, g in list(self.games.items()):
            if pid not in alive:
                self.end_game(pid, "exit")
                continue
            for c in [c for c in g["children"] if c not in live]:
                del g["children"][c]
                self.game_sampler.forget(c)
                log.info("game child end pid=%s session=%s", c, g["session"])   # PresentMon keeps its exe name
        # sessions opened and closed by `pm stress start|idle|stop` (the CLI writes them)
        self.manual = {r[0]: r[1] for r in self.exec(
            "SELECT id, kind FROM sessions WHERE kind IN ('manual','idle_soak') AND end_ts IS NULL ORDER BY id")}

    def attach_child(self, parent_pid, pid, name):
        g = self.games[parent_pid]
        g["children"][pid] = name
        log.info("game child start %s pid=%s session=%s", name, pid, g["session"])
        self.retarget(g)

    def retarget(self, g):
        if g["capture"]:
            g["capture"].retarget(g["children"].values())

    def start_game(self, pid, name, exe, stress=None):
        """Game session, or with stress=(name, uses_3d) a stress-test session (frames only for 3D tests)."""
        try:
            ctx = gamectx.collect(exe, self.hw.gpu.Name if self.hw.gpu is not None else None)
        except Exception as e:
            ctx = {"error": str(e)}
        if stress:
            ctx["stress_test"] = stress[0]
        start = time.time()
        with self.lock:
            profile = ocanalyze.active_profile_id(self.con, start)
        sid = self.exec("INSERT INTO sessions(exe, path, pid, start_ts, context, kind, profile_id) VALUES (?,?,?,?,?,?,?)",
                        (name, exe, pid, start, json.dumps(ctx), "stress" if stress else None, profile)).lastrowid
        cap = None
        if os.path.exists(self.cfg["presentmon_exe"]) and (not stress or stress[1]):
            cap = FrameCapture(self.cfg["presentmon_exe"], name, pid, sid, self.on_frame_second, self.on_stutter)
        self.games[pid] = {"session": sid, "capture": cap, "exe": name, "path": exe, "start": start,
                           "last_summary": time.time(), "pid": pid, "children": {},
                           "stress": stress[0] if stress else None}
        if not stress:
            self.shader.set_game(exe)   # the newest game: usually the only one
            self.shader_pid = pid
            self.net.set_game(pid, name, sid)
        log.info("game start %s pid=%s session=%s", name, pid, sid)

    def on_frame_second(self, r):
        cols = list(r)
        self.exec(f"INSERT OR REPLACE INTO frames ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  [r[c] for c in cols])

    def on_stutter(self, s):
        self.exec("INSERT INTO stutters(ts, session, ft_ms, median_ms) VALUES (?,?,?,?)",
                  (s["ts"], s["session"], s["ft_ms"], s["median_ms"]))
        self.etw.on_stutter(s["ts"], s["session"])

    def save_summary(self, g, final=False):
        summ = g["capture"].summary() if g["capture"] else {"error": "PresentMon niedostępny"}
        now = time.time()
        if final or now - g.get("last_bottleneck", 0) >= BOTTLENECK_EVERY_S:
            g["last_bottleneck"] = now
            try:
                with self.lock:   # ~30 ms for a 3 h session
                    b = bottleneck.compute(self.con, g["session"], g["start"], now)
                if b:
                    summ["bottleneck"] = {"kind": b["kind"], "why": b["why"], **b["metrics"]}
                    summ["vram_shared_max_mb"] = b["metrics"]["vram_shared_max_mb"]
                    summ["gameplay"] = gameplay.stats(self.con, g["session"], g["start"], now) or {}
            except Exception:
                log.exception("bottleneck for session %s", g["session"])
        self.merge_summary(g["session"], summ)   # keep keys added elsewhere (crash, ...)

    def end_game(self, pid, reason):
        g = self.games.pop(pid)
        for c in (pid, *g["children"]):
            self.game_sampler.forget(c)
        if pid == self.shader_pid:
            self.shader_pid = next(iter(self.games), None)
            self.shader.set_game(self.games[self.shader_pid]["path"] if self.shader_pid else None)
        if not self.games:
            self.net.set_game(None, None, None)
        elif self.net.game and self.net.game[0] == pid:
            other, og = next(iter(self.games.items()))
            self.net.set_game(other, og["exe"], og["session"])
        if g["capture"]:
            g["capture"].stop()
        end = time.time()
        if reason == "exit":
            if self._crash_event(g["exe"], end):
                reason = "crash"
            # WER writes 1000/1002 (and the report) seconds after the process dies and we poll the log every 30 s
            self.pending_crash_checks.append((g["session"], g["exe"], end))
        self.save_summary(g, final=True)
        self.save_mem_growth(g["session"], g["exe"], end)
        self.exec("UPDATE sessions SET end_ts=?, end_reason=? WHERE id=?", (end, reason, g["session"]))
        if reason == "crash":
            self.save_crash(g["session"], g["exe"], end)
        log.info("game end %s session=%s reason=%s", g["exe"], g["session"], reason)

    def merge_summary(self, sid, extra):
        with self.lock:
            r = self.con.execute("SELECT summary FROM sessions WHERE id=?", (sid,)).fetchone()
            try:
                summ = json.loads(r[0]) if r and r[0] else {}
            except ValueError:
                summ = {}
            summ.update(extra)
            self.con.execute("UPDATE sessions SET summary=? WHERE id=?", (json.dumps(summ, ensure_ascii=False), sid))
            self.con.commit()

    def save_mem_growth(self, sid, exe, end):
        try:
            with self.lock:
                start = self.con.execute("SELECT start_ts FROM sessions WHERE id=?", (sid,)).fetchone()[0]
                m = memleak.assess(self.con, sid, start, end)
            if not m:
                return
            self.merge_summary(sid, {"mem_growth_mb_h": m["mem_growth_mb_h"], "mem_growth_r2": m["mem_growth_r2"]})
            if m["leak"]:
                self.incident("game_mem_leak", "warning", memleak.title(exe, m), dict(m, session=sid), ts=end)
        except Exception:
            log.exception("memory growth for session %s", sid)

    # ---------- ETW black box ----------
    def save_etw_capture(self, row):
        """etwbox worker thread: store the dump, queue its analysis."""
        row = dict(row, summary=json.dumps(row["summary"], ensure_ascii=False))
        cid = self.exec("INSERT INTO etw_captures(ts, trigger, session, path, size_mb, status, summary) "
                        "VALUES (:ts,:trigger,:session,:path,:size_mb,:status,:summary)", row).lastrowid
        if row["status"] == "captured" and self.etw.s["analyze"]:
            self.etw_pending.append((time.time() + etwbox.ANALYZE_DELAY_S, cid))

    def etw_tick(self, now):
        for action in etwbox.take_requests():
            log.info("ETW request: %s", action)
            self.etw.control(action)
        self.etw.ensure(bool(self.games), now)
        if self.etw_proc is not None and self.etw_proc.poll() is None:
            return
        self.etw_proc = None
        due = [p for p in self.etw_pending if p[0] <= now]
        if due and (not self.games or self.etw.s["analyze_during_game"]):
            self.etw_pending.remove(due[0])
            self.etw_proc = etwbox.spawn_analysis(due[0][1])
            log.info("ETW capture %s: analysis started", due[0][1])

    def save_crash(self, sid, exe, end):
        try:
            with self.lock:
                start = self.con.execute("SELECT start_ts FROM sessions WHERE id=?", (sid,)).fetchone()[0]
                crash = crashinfo.build(self.con, exe, end, start, CRASH_LATE_S)
            if crash:
                self.merge_summary(sid, {"crash": crash})
                log.info("session %s crash: %s %s", sid, crash.get("module"), crash.get("exception_code"))
        except Exception:
            log.exception("crash details for session %s", sid)

    def _crash_event(self, exe, end):
        """A crash/hang event for this exe around the exit means it did not exit cleanly."""
        return self.exec(
            "SELECT 1 FROM events WHERE ts BETWEEN ? AND ? AND event_id IN (1000,1002) "
            "AND provider IN ('Application Error','Application Hang') AND message LIKE ? LIMIT 1",
            (end - 90, end + CRASH_LATE_S, f"%{exe}%")).fetchone() is not None

    def recheck_crashes(self, now):
        """Re-check sessions that ended 'exit' once later event-log polls have arrived."""
        keep = []
        for sid, exe, end in self.pending_crash_checks:
            if now - end < CRASH_LATE_S:
                keep.append((sid, exe, end))
            elif self._crash_event(exe, end):
                if self.exec("UPDATE sessions SET end_reason='crash' WHERE id=? AND end_reason='exit'", (sid,)).rowcount:
                    log.info("session %s reclassified as crash (late WER event)", sid)
                self.exec("DELETE FROM oc_session_stats WHERE session=?", (sid,))   # re-aggregated with the crash
                self.save_crash(sid, exe, end)   # now with the late WER 1001 / Report.wer
        self.pending_crash_checks = keep

    # ---------- event log ----------
    def poll_events(self):
        with self.lock:
            last = db.get_meta(self.con, "events_since")
        since = float(last) if last else time.time() - 14 * 86400
        newest = since
        for logname in LOGS:
            for ev in self.events.read_since(logname, since):
                newest = max(newest, ev["ts"])
                cur = self.exec("INSERT OR IGNORE INTO events(ts, log, provider, event_id, level, record_id, message, data) "
                                "VALUES (:ts,:log,:provider,:event_id,:level,:record_id,:message,:data)", ev)
                if cur.rowcount and logname == procwatch.DEFENDER_LOG_NAME:
                    self.defender_event(ev)
                if cur.rowcount:
                    c = classify(ev)
                    if c:
                        det = {"event_id": ev["event_id"], "provider": ev["provider"], "message": ev["message"][:1500]}
                        if c[0] in ("whea_corrected", "whea_fatal"):
                            det["whea"] = ocmon.parse_whea(ev["message"], self.cfg["apic_ids_per_core"])
                        iid = self.incident(c[0], c[1], c[2], det, ts=ev["ts"])
                        if c[0] == "bsod" and os.path.exists(crashinfo.CDB):
                            threading.Thread(target=self._bsod_cause, args=(iid, ev["ts"]), daemon=True).start()
        with self.lock:
            db.set_meta(self.con, "events_since", newest)
            self.con.commit()

    def defender_event(self, ev):
        """Defender scan start (1000) opens a defender_scan incident, its 1001/1002 (same Scan ID) closes it."""
        info = procwatch.defender_scan_info(ev)
        if not info:
            return
        sid = procwatch.defender_scan(ev)[1]
        if info["kind"] == "start":
            kind = {"1": "szybki", "2": "pełny", "3": "niestandardowy"}.get(str(info["params_index"]), "")
            self.incident("defender_scan", "info", f"Skan Windows Defender {kind}".strip(), {"scan_id": sid}, ts=ev["ts"])
        else:
            self.exec("UPDATE incidents SET end_ts=? WHERE kind='defender_scan' AND end_ts IS NULL AND details LIKE ?",
                      (ev["ts"], f'%"{sid}"%'))

    def _bsod_cause(self, iid, ts):
        """Optional: `cdb !analyze -v` on the minidump of this BSOD (only when Debugging Tools are installed)."""
        dump = crashinfo.minidump_near(ts)
        if not dump:
            return
        with BSOD_LOCK:   # a backfill can find several BSODs at once
            r = crashinfo.bsod_cause(dump)
        if r:
            with self.lock:
                d = json.loads(self.con.execute("SELECT details FROM incidents WHERE id=?", (iid,)).fetchone()[0] or "{}")
            d.update(r)
            self.exec("UPDATE incidents SET details=? WHERE id=?", (json.dumps(d, ensure_ascii=False), iid))

    # ---------- housekeeping ----------
    def housekeeping(self, now):
        if now - self.last_sysinfo > 86400:
            self.last_sysinfo = now
            threading.Thread(target=self._sysinfo, daemon=True).start()
        if now - self.last_retention > 3600:
            self.last_retention = now
            cutoff = now - self.cfg["retention_days"] * 86400
            for t in ("samples", "frames", "stutters", "procs", "events", "game_proc", "proc_events", "net_samples",
                      "report_cache"):
                self.exec(f"DELETE FROM {t} WHERE ts < ?", (cutoff,))
            with self.lock:
                etwbox.retention(self.con, self.etw.s, now)
                # rows outlive their trace file (kept for the analysis) until the general retention
                self.con.execute("DELETE FROM etw_captures WHERE ts < ? AND path IS NULL", (cutoff,))
                self.con.commit()
            self.name_cache.clear()
            self.detail_retention(now)
        if self.gpu_od and now - self.last_gpu_od > self.cfg["gpu_od_poll_s"]:
            self.last_gpu_od = now
            self.check_gpu_od(now)
        if now - self.last_oc_stats > 60:
            self.last_oc_stats = now
            self.save_oc_stats()

    # ---------- UV/OC ----------
    def check_gpu_od(self, now):
        """Store the Adrenalin tuning when it changes; a change = incident asking to tag the profile."""
        cur = self.gpu_od.read()
        if not cur or (self.gpu_od.ok and "named" not in cur):
            return   # ADL failed this time (driver busy / after a TDR): not a settings change
        prev = self.exec("SELECT settings FROM gpu_od_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        prev = json.loads(prev[0]) if prev else None
        diffs = ocmon.describe_change(prev, cur) if prev else []
        if prev is None or diffs:
            self.exec("INSERT INTO gpu_od_snapshots VALUES (?, ?)", (now, json.dumps(cur)))
        if diffs:
            self.incident("gpu_settings_change", "info", "Wykryto zmianę ustawień GPU (Adrenalin) - oznacz profil "
                          "(pm oc add / activate)", {"changes": diffs, "settings": cur.get("named")}, ts=now)

    def save_oc_stats(self):
        """Per-session UV/OC aggregates of finished sessions (they outlive the pruned hw_detail). Waits until
        late crash events / incidents had time to arrive (crash re-check, event polls)."""
        rows = self.exec(
            "SELECT id, exe, start_ts, end_ts, kind, profile_id FROM sessions s WHERE end_ts IS NOT NULL AND end_ts < ? "
            "AND NOT EXISTS (SELECT 1 FROM oc_session_stats o WHERE o.session=s.id) ORDER BY id DESC LIMIT 5",
            (time.time() - 180,)).fetchall()
        for sid, exe, start, end, kind, profile in rows:
            try:
                with self.lock:
                    st = ocanalyze.session_stats(self.con, sid)
                row = (sid, st["profile_id"], st["kind"], st["exe"], st["start_ts"], st["end_ts"], json.dumps(st["stats"]))
            except Exception as e:
                log.warning("oc stats for session %s: %s", sid, e)   # stored as an error so it is not retried forever
                row = (sid, profile, kind or "game", exe, start, end, json.dumps({"error": str(e)}))
            self.exec("INSERT OR REPLACE INTO oc_session_stats VALUES (?,?,?,?,?,?,?)", row)

    def detail_retention(self, now):
        days = self.cfg["detail_retention_days"]
        try:
            size = sum(p.stat().st_size for p in config.DATA_DIR.glob(config.DB_PATH.name + "*"))
        except OSError:
            size = 0
        if size > self.cfg["db_guard_gb"] * 2**30:
            days = min(days, 14)
            if not self.exec("SELECT 1 FROM incidents WHERE kind='db_size' AND ts > ?", (now - 7 * 86400,)).fetchone():
                self.incident("db_size", "warning", f"Baza perf.db ma {size / 2**30:.1f} GB - retencja szczegółów "
                              "UV/OC skrócona do 14 dni", {"bytes": size}, ts=now)
        self.exec("DELETE FROM hw_detail WHERE ts < ?", (now - days * 86400,))
        with self.lock:
            last = float(db.get_meta(self.con, "last_vacuum", 0))
        incremental = self.exec("PRAGMA auto_vacuum").fetchone()[0] == 2
        if incremental and now - last > 7 * 86400 and not self.vacuuming:
            self.vacuuming = True
            threading.Thread(target=self._incremental_vacuum, daemon=True).start()

    def _incremental_vacuum(self):
        """Free pages in small steps so the sampling thread never waits long for the write lock."""
        con = db.connect()
        try:
            for _ in range(10000):
                if not con.execute("PRAGMA freelist_count").fetchone()[0]:
                    break
                con.execute("PRAGMA incremental_vacuum(500)").fetchall()   # each fetched row frees one page
                time.sleep(0.05)
            db.set_meta(con, "last_vacuum", time.time())
            con.commit()
        except Exception:
            log.exception("incremental vacuum")
        finally:
            con.close()
            self.vacuuming = False

    def vacuum_setup(self):
        """One-time switch to auto_vacuum=INCREMENTAL (needs a full VACUUM: only while the DB is still small)."""
        if self.con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
            return
        try:
            if config.DB_PATH.stat().st_size > 500 * 2**20:
                log.info("perf.db too large for the one-time VACUUM, incremental vacuum stays off")
                return
            with self.lock:
                self.con.execute("PRAGMA auto_vacuum=INCREMENTAL")
                self.con.execute("VACUUM")
            log.info("auto_vacuum=INCREMENTAL enabled")
        except Exception:
            log.exception("vacuum setup")

    def _sysinfo(self):
        info = sysinfo.collect()
        prev = self.exec("SELECT info FROM system_info ORDER BY ts DESC LIMIT 1").fetchone()
        self.exec("INSERT INTO system_info VALUES (?, ?)", (time.time(), json.dumps(info, ensure_ascii=False)))
        if prev:
            old = json.loads(prev[0])
            changed = {k: {"before": old.get(k), "after": v} for k, v in info.items()
                       if k in old and k not in ("last_boot", "volumes", "background_tools", "pagefile", "disk_health", "disk_map")
                       and old.get(k) is not None and old.get(k) != v}   # null = was unreadable, not a change
            if changed:
                self.incident("config_change", "info", "Zmiana konfiguracji: " + ", ".join(changed), changed)
            for kind, sev, title, det in diskinfo.health_changes(old, info):
                self.incident(kind, sev, title, det)

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
        if DUMPS_FLAG.exists():
            action = DUMPS_FLAG.read_text(encoding="utf-8").strip()
            DUMPS_FLAG.unlink(missing_ok=True)
            if action in ("enable", "disable"):
                try:
                    log.info("WER LocalDumps %s: %s", action, crashinfo.set_local_dumps(action == "enable"))
                except OSError as e:
                    log.error("WER LocalDumps %s failed: %s", action, e)
        requested = RELOAD_FLAG.exists()
        stamp = source_stamp()
        if stamp == self.src_stamp and not requested:
            return
        if not requested and now - stamp < 10:
            return  # files may still be mid-save (an edit session touches several files)
        if (self.games or self.pending_crash_checks) and not requested:
            # Restarting would split the game session in two / lose the late crash re-check.
            # An explicit `pm collector reload` goes through anyway (e.g. a misdetected "game").
            if not self.reload_deferred_logged:
                log.info("reload pending, deferred until the game session ends")
                self.reload_deferred_logged = True
            return
        if self.import_check is None:
            # Import-check off the sampling thread (spawning an interpreter takes seconds)
            self.src_stamp = stamp
            RELOAD_FLAG.unlink(missing_ok=True)
            chk = self.import_check = {"requested": requested}
            threading.Thread(target=lambda: chk.update(err=self._import_check()), daemon=True).start()

    def finish_reload(self):
        chk = self.import_check
        if chk is None or "err" not in chk:
            return  # still checking
        if not chk["requested"] and (self.games or self.pending_crash_checks):
            return  # a game started meanwhile (keep the result for later)
        self.import_check = None
        if chk["err"]:
            log.error("reload aborted, new code does not import:\n%s", chk["err"])
            return
        log.info("reloading collector (%s)", "requested" if chk["requested"] else "source changed")
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
        self.net.start()
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
                    self.recheck_crashes(now)
                self.process_marks()
                self.etw_tick(now)
                self.housekeeping(now)
                self.check_reload(now)
                self.finish_reload()
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
        # Close ongoing incidents: an open one would later read as "lasted until the next reset"
        for kind, iid in list(self.open_incidents.items()):
            self._end_episode(kind, iid, time.time())
        self.open_incidents.clear()
        self.hotkey.stop()
        self.shader.stop()
        self.etw.stop()
        self.net.stop()
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
