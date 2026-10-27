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
    pagefile_pct REAL,                           -- Paging File(_Total) % Usage
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
    ts REAL, kind TEXT, name TEXT, pid INTEGER,  -- kind: cpu | mem | io | gpu
    cpu_pct REAL, mem_mb REAL, io_mbs REAL,
    gpu_pct REAL, vram_mb REAL                   -- GPU engines load (sum, %) / dedicated VRAM (kind gpu)
);
CREATE TABLE IF NOT EXISTS events (             -- Windows event log (errors, warnings, selected info)
    ts REAL, log TEXT, provider TEXT, event_id INTEGER, level INTEGER, record_id INTEGER,
    message TEXT,
    data TEXT,                                   -- raw EventData as JSON ({name: value} or [values]) for crash/disk/WHEA/Defender
    UNIQUE (log, record_id)
);
CREATE TABLE IF NOT EXISTS incidents (          -- detected problems (freeze, crash, driver reset...)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, end_ts REAL, kind TEXT, severity TEXT, title TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS system_info (        -- config snapshot (drivers, BIOS, settings)
    ts REAL, info TEXT
);
CREATE TABLE IF NOT EXISTS game_proc (          -- the game process itself every ~5 s (perfmon/gpuproc.py)
    ts REAL, session INTEGER, pid INTEGER,
    rss_mb REAL, private_mb REAL,                -- working set / private bytes (memory growth = leak)
    thread_max_pct REAL,                         -- busiest thread, % of one core over the interval
    threads INTEGER, handles INTEGER,
    io_read_mbs REAL,                            -- game reads from disk (asset streaming)
    gpu_pct REAL, gpu_3d_pct REAL,               -- GPU engines load of the game (3D+Compute+Copy+Video) / 3D only
    vram_dedicated_mb REAL, vram_shared_mb REAL, -- shared > ~0.5 GB = VRAM spills into system RAM
    shader_bytes INTEGER,                        -- size of the driver/Steam shader caches (growth = compiling)
    PRIMARY KEY (session, ts)
);
CREATE TABLE IF NOT EXISTS proc_events (        -- processes started/stopped (diff of the ~5 s process list, perfmon/procwatch.py)
    ts REAL, pid INTEGER, name TEXT,
    ppid INTEGER, parent_name TEXT,
    action TEXT,                                 -- start | stop
    session INTEGER,                             -- game session id if running
    path TEXT                                    -- exe path, only for starts during a game session
);
CREATE TABLE IF NOT EXISTS etw_captures (       -- ETW black box dumps (perfmon/etwbox.py)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, trigger TEXT, session INTEGER,      -- user_mark | dwm_stall | ui_freeze | dpc_pct | stutter_burst
    path TEXT, size_mb REAL,                     -- path NULL once the file is deleted by retention
    status TEXT,                                 -- captured | analyzed | failed
    summary TEXT                                 -- JSON: top DPC/ISR drivers, disk I/O per process, timings
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_frames_ts ON frames(ts);
CREATE INDEX IF NOT EXISTS ix_stutters_ts ON stutters(ts);
CREATE INDEX IF NOT EXISTS ix_procs_ts ON procs(ts);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_incidents_ts ON incidents(ts);
CREATE INDEX IF NOT EXISTS ix_proc_events_ts ON proc_events(ts);
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=10, check_same_thread=False)
        shim_schema(con)
    else:
        config.ensure_dirs()
        con = sqlite3.connect(config.DB_PATH, timeout=10, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        migrate(con)
    con.row_factory = sqlite3.Row
    return con


def migrate(con):
    con.executescript(SCHEMA)
    # migrate DBs created before these columns existed
    add_columns(con, "sessions", {"context": "TEXT"})
    add_columns(con, "procs", {"gpu_pct": "REAL", "vram_mb": "REAL"})
    add_columns(con, "events", {"data": "TEXT"})
    add_columns(con, "samples", {"pagefile_pct": "REAL"})
    add_columns(con, "frames", {"runtime": "TEXT", "sync_interval": "INTEGER", "tearing": "INTEGER", "dropped": "INTEGER",
                                "click_lat": "REAL", "input_lat": "REAL", "frame_gen_pct": "REAL"})


def _columns(con, table, schema="main"):
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]


def shim_schema(con):
    """Read-only connection to a DB the running collector has not migrated yet (it defers reloads during a game):
    missing tables become empty TEMP tables and missing columns NULL columns of a TEMP view with the table's name
    (unqualified names resolve to temp first), so new analysis code runs on the old schema."""
    mem = sqlite3.connect(":memory:")
    migrate(mem)
    for name, sql in mem.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        try:
            have = _columns(con, name)
            if not have:   # sqlite_master keeps "CREATE TABLE name (...)" without IF NOT EXISTS
                con.execute(sql.replace("CREATE TABLE", "CREATE TEMP TABLE", 1))
                continue
            missing = [c for c in _columns(mem, name) if c not in have]
            if missing:
                con.execute(f"CREATE TEMP VIEW {name} AS SELECT *, {', '.join(f'NULL AS {c}' for c in missing)} "
                            f"FROM main.{name}")
        except sqlite3.Error:
            pass   # analysis then fails loudly on the missing column, as before
    mem.close()


def add_columns(con, table, cols):
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for c, t in cols.items():
        if c not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {c} {t}")


def get_meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, str(v)))
