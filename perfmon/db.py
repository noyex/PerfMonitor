"""SQLite schema. All `ts` times are unix epoch (seconds, float, UTC)."""
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (            -- every 1 s, whole system
    ts REAL PRIMARY KEY,
    cpu_total REAL, cpu_core_max REAL,           -- % load (core_max = most loaded thread)
    cpu_clock_mhz REAL, cpu_temp REAL, cpu_power_w REAL,
    dpc_pct REAL, interrupt_pct REAL, ctx_switch_s REAL, proc_queue REAL,
    ram_used_gb REAL, ram_pct REAL, commit_gb REAL, commit_limit_gb REAL,
    hard_faults_s REAL,                          -- Memory\\Pages Input/sec (reads from pagefile / disk)
    gpu_load REAL, gpu_3d REAL, gpu_vram_mb REAL, gpu_vram_total_mb REAL,
    gpu_clock_mhz REAL, gpu_mem_clock_mhz REAL, gpu_temp REAL, gpu_hotspot REAL,
    gpu_mem_temp REAL, gpu_power_w REAL, gpu_fan_rpm REAL,
    disk_read_mbs REAL, disk_write_mbs REAL,
    disk_lat_ms REAL, disk_lat_disk TEXT,        -- worst latency across disks and which disk
    disk_queue REAL, disk_busy_pct REAL,
    net_rx_kbs REAL, net_tx_kbs REAL,
    fg_exe TEXT, fg_hung INTEGER,                -- foreground window / whether Windows considers it hung
    ui_ms REAL,                                  -- shell (explorer) response time to WM_NULL; >=3000 = timeout
    dwm_ms REAL,                                 -- longest DwmFlush in the last second (window compositor)
    user_idle_s REAL,                            -- seconds since last keyboard/mouse input
    game_session INTEGER,                        -- game session id if running
    extra TEXT                                   -- JSON: disks separately, disk temperatures etc.
);
CREATE TABLE IF NOT EXISTS sessions (           -- game session (from start to exe close)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exe TEXT, path TEXT, pid INTEGER,
    start_ts REAL, end_ts REAL, end_reason TEXT, -- exit | crash | collector_stop | unknown
    summary TEXT,                                -- JSON: fps, 1%/0.1% low, stutters, bottleneck...
    context TEXT                                 -- JSON at start: gpu_driver, adrenalin, os_build, bios, game_version, steam build
);
CREATE TABLE IF NOT EXISTS frames (             -- frame aggregate every 1 s from PresentMon
    ts REAL, session INTEGER,
    frames INTEGER, fps REAL, ft_avg REAL, ft_p99 REAL, ft_max REAL,
    cpu_busy REAL, gpu_busy REAL, display_lat REAL, present_mode TEXT, bound TEXT,
    PRIMARY KEY (session, ts)
);
CREATE TABLE IF NOT EXISTS stutters (           -- individual frame stutters
    ts REAL, session INTEGER, ft_ms REAL, median_ms REAL, causes TEXT
);
CREATE TABLE IF NOT EXISTS procs (              -- top processes every ~5 s
    ts REAL, kind TEXT, name TEXT, pid INTEGER,  -- kind: cpu | mem | io
    cpu_pct REAL, mem_mb REAL, io_mbs REAL
);
CREATE TABLE IF NOT EXISTS events (             -- Windows event log (errors, warnings, selected info)
    ts REAL, log TEXT, provider TEXT, event_id INTEGER, level INTEGER, record_id INTEGER,
    message TEXT,
    UNIQUE (log, record_id)
);
CREATE TABLE IF NOT EXISTS incidents (          -- detected problems (freeze, crash, driver reset...)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, end_ts REAL, kind TEXT, severity TEXT, title TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS system_info (        -- config snapshot (drivers, BIOS, settings)
    ts REAL, info TEXT
);
CREATE TABLE IF NOT EXISTS net_samples (        -- network quality every 1 s (perfmon/netmon.py)
    ts REAL PRIMARY KEY,
    session INTEGER,                             -- game session id if running
    gw_ms REAL, gw_loss REAL,                    -- ping to the router (default gateway); loss = 0..1 this second
    isp_ms REAL, isp_loss REAL,                  -- ping to the first operator router behind it
    inet_ms REAL, inet_loss REAL,                -- ping to internet targets (1.1.1.1, 8.8.8.8 averaged)
    game_ms REAL, game_loss REAL,                -- ping to the game server (when detected and it answers ICMP)
    dns_ms REAL,                                 -- uncached DNS lookup time, every 30 s (NULL = not probed / failed)
    link TEXT,                                   -- wifi | ethernet | NULL = no route (disconnected)
    wifi_signal INTEGER, wifi_rssi INTEGER,      -- Wi-Fi signal quality % / dBm
    extra TEXT                                   -- JSON, only what changed: gateway, isp_hop, adapter, wifi_*,
                                                 -- game_ip, nic_errors, dns_failed
);                                               -- *_ms NULL = lost or not probed; *_loss NULL = not probed
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_frames_ts ON frames(ts);
CREATE INDEX IF NOT EXISTS ix_stutters_ts ON stutters(ts);
CREATE INDEX IF NOT EXISTS ix_procs_ts ON procs(ts);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_incidents_ts ON incidents(ts);
CREATE INDEX IF NOT EXISTS ix_net_session ON net_samples(session);
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    else:
        config.ensure_dirs()
        con = sqlite3.connect(config.DB_PATH, timeout=10, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        if "context" not in cols:  # migrate DBs created before the context column existed
            con.execute("ALTER TABLE sessions ADD COLUMN context TEXT")
    con.row_factory = sqlite3.Row
    return con


def get_meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, str(v)))
