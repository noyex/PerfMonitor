"""Process start/stop events (proc_events) and Windows Defender scans: "this stutter came 3 s after TiWorker started".

Collector side: ProcWatch diffs the process snapshot the collector already builds every ~5 s.
Analysis side: pure functions over a sqlite3 connection (row_factory=sqlite3.Row). Strings are Polish."""
import bisect
import json
import logging
import re
import sqlite3

from . import analyze as an

log = logging.getLogger("perfmon.procwatch")

DEFAULT_IGNORE = ["conhost.exe", "cmd.exe", "powershell.exe", "svchost.exe", "RuntimeBroker.exe",
                  "backgroundTaskHost.exe", "dllhost.exe", "WmiPrvSE.exe", "taskhostw.exe"]
DEFAULT_CULPRITS = ["MsMpEng.exe", "TiWorker.exe", "CompatTelRunner.exe", "SearchIndexer.exe", "DismHost.exe",
                    "OneDrive.exe", "MoUsoCoreWorker.exe", "wuauclt.exe", "SgrmBroker.exe",
                    "MicrosoftEdgeUpdate.exe", "GoogleUpdate.exe", "steamwebhelper.exe", "EpicWebHelper.exe",
                    "AorusLcdService.exe", "GCC.exe", "EasyTune.exe"]
SESSION_IGNORE = {"conhost.exe", "dllhost.exe", "wmiprvse.exe"}  # noise even during a game session
BURST_MAX = 150        # more start+stop events in one diff = snapshot glitch / resume from sleep, not real activity
CAUSE_BEFORE_S = 15    # a start this long before the stutter (or...
CAUSE_AFTER_S = 2      # ...this long after it, timestamps are coarse) can explain it
HEAVY_WINDOW_S = 60    # a started process counts as heavy if it loads CPU/GPU within this time after start
HEAVY_PCT = 10
MAX_STARTS = 30
MAX_START_TAGS = 2


# ---------- collector side ----------
class ProcWatch:
    """update(now, snapshot, session) -> list of proc_events row dicts. First call is the baseline (emits nothing).

    snapshot: {pid: (create_time, name, ppid)}; a process is identified by (pid, create_time), so a reused pid
    is a stop + a start. session: current game session id or None."""

    def __init__(self, cfg, exe_fn=None):
        self.ignore = {n.lower() for n in (cfg.get("proc_events_ignore") or DEFAULT_IGNORE)}
        self.burst = cfg.get("proc_events_burst", BURST_MAX)
        self.exe_fn = exe_fn or self._exe
        self.prev = None
        self.prev_ts = None

    @staticmethod
    def _exe(pid):
        try:
            import psutil
            return psutil.Process(pid).exe() or None
        except Exception:
            return None

    @staticmethod
    def _parent(ppid, child_ct, *snaps):
        """Parent name from the first snapshot where that pid is older than the child (guards against pid reuse)."""
        for s in snaps:
            p = s.get(ppid)
            if p and p[0] <= child_ct:
                return p[1]
        return None

    def update(self, now, snapshot, session=None):
        prev, prev_ts, self.prev, self.prev_ts = self.prev, self.prev_ts, dict(snapshot), now
        if prev is None:
            return []
        started = [pid for pid, v in snapshot.items() if pid not in prev or prev[pid][0] != v[0]]
        stopped = [pid for pid, v in prev.items() if pid not in snapshot or snapshot[pid][0] != v[0]]
        if not started and not stopped:
            return []
        if len(started) + len(stopped) > self.burst:
            log.debug("proc_events: %d changes in one diff, skipped", len(started) + len(stopped))
            return []
        skip = SESSION_IGNORE if session is not None else self.ignore
        rows = []
        for pid in stopped:
            ct, name, ppid = prev[pid]
            if (name or "").lower() in skip:
                continue
            rows.append(dict(ts=now, pid=pid, name=name, ppid=ppid, parent_name=self._parent(ppid, ct, prev, snapshot),
                             action="stop", session=session, path=None))
        for pid in started:
            ct, name, ppid = snapshot[pid]
            if (name or "").lower() in skip:
                continue
            rows.append(dict(ts=min(now, max(ct, prev_ts)), pid=pid, name=name, ppid=ppid,
                             parent_name=self._parent(ppid, ct, snapshot, prev), action="start", session=session,
                             path=self.exe_fn(pid) if session is not None else None))
        rows.sort(key=lambda r: r["ts"])
        return rows


# ---------- Defender ----------
# Microsoft-Windows-Windows Defender/Operational (readable without admin). EventData of 1000/1001/1002 has named fields
# in this order: Product Name, Product Version, Scan ID {GUID}, Scan Type Index, Scan Type (localized text),
# Scan Parameters Index (1 quick, 2 full, 3 custom), Scan Parameters (localized), Domain, User, SID, then
# 1000: Scan Resources, Scan Trigger Index, Scan Trigger, ...; 1001: Scan Time Hours/Minutes/Seconds; 1002: Stop Reason.
# The localized text is useless on a non-English system, so we pair start and end by "Scan ID" (the same GUID in the
# 1000 and its 1001/1002; unique per scan, unlike the Scan Type Index which is 1 for every scan).
DEFENDER_LOG_NAME = "Microsoft-Windows-Windows Defender/Operational"
DEFENDER_START, DEFENDER_END = {1000}, {1001, 1002}
_GUID = re.compile(r"^\{[0-9A-Fa-f-]{36}\}$")
_NAMED = {"id": re.compile(r"\bScan ID=([^;\]]+)"), "type_index": re.compile(r"\bScan Type Index=([^;\]]+)"),
          "params_index": re.compile(r"\bScan Parameters Index=([^;\]]+)")}


def defender_scan_info(ev):
    """Defender scan event dict (EventReader.read_since shape) -> {"kind","id","type_index","params_index"} or None.

    Reads the named fields EventReader appends to ev["message"] ("[Scan ID={..}; Scan Type Index=1; ...]"), or
    ev["data"] (JSON list of raw EventData values, or a dict) when present. Language independent."""
    try:
        eid = int(ev.get("event_id"))
    except (TypeError, ValueError):
        return None
    kind = "start" if eid in DEFENDER_START else "end" if eid in DEFENDER_END else None
    if kind is None or "defender" not in f"{ev.get('provider') or ''} {ev.get('log') or ''}".lower():
        return None
    out = {"kind": kind, "id": None, "type_index": None, "params_index": None}
    data = ev.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if isinstance(data, dict):
        out["id"], out["type_index"], out["params_index"] = (
            data.get("Scan ID"), data.get("Scan Type Index"), data.get("Scan Parameters Index"))
    elif isinstance(data, list):
        vals = [str(v) for v in data]
        i = next((i for i, v in enumerate(vals) if _GUID.match(v)), None)
        if i is not None:  # ... Scan ID, Scan Type Index, Scan Type, Scan Parameters Index, ...
            out["id"] = vals[i]
            out["type_index"] = vals[i + 1] if i + 1 < len(vals) else None
            out["params_index"] = vals[i + 3] if i + 3 < len(vals) else None
    msg = ev.get("message") or ""
    for k, rx in _NAMED.items():
        if out[k] is None and (m := rx.search(msg)):
            out[k] = m.group(1).strip()
    return out


def defender_scan(ev):
    """-> ("start"|"end", scan_id) for Defender scan started (1000) / finished (1001) / stopped (1002), else None.

    scan_id is the Scan ID GUID (falls back to "type<Scan Type Index>", then "?") so an end pairs with its start."""
    i = defender_scan_info(ev)
    if not i:
        return None
    return i["kind"], i["id"] or (f"type{i['type_index']}" if i["type_index"] else "?")


# ---------- analysis side ----------
def _rows(con, sql, args=()):
    try:
        return an._rows(con, sql, args)
    except sqlite3.OperationalError:  # table missing in an old DB
        return []


def _lc(names):
    return {(n or "").lower() for n in names or ()}


def _hhmm(time_str):
    return (time_str or "")[11:16]


class StartCauses:
    """Per-session data for the background_start / background_scan stutter causes, loaded once per report."""

    def __init__(self, con, start, end, game_exe, culprits=None):
        self.exe = (game_exe or "").lower()
        self.end = end
        self.culprits = _lc(DEFAULT_CULPRITS if culprits is None else culprits)
        skip = an.NOT_BACKGROUND | {self.exe}
        self.heavy = self._heavy(con, start, end)
        # (ts, name, strength): culprit starts and starts of processes that got heavy within HEAVY_WINDOW_S
        self.starts = []
        for r in _rows(con, "SELECT ts, name, parent_name FROM proc_events WHERE action='start' AND ts BETWEEN ? AND ? "
                            "ORDER BY ts", (start - CAUSE_AFTER_S - 1, end + CAUSE_BEFORE_S)):
            n = (r["name"] or "").lower()
            if not n or n in skip or (self.exe and (r["parent_name"] or "").lower() == self.exe):
                continue  # the game itself / its own helpers are not "background"
            pct = self.heavy_pct(n, r["ts"])
            if n in self.culprits or pct:
                self.starts.append((r["ts"], r["name"], pct or 0))
        self.starts_ts = [s[0] for s in self.starts]
        self.scans = [(r["ts"], r["end_ts"]) for r in _rows(
            con, "SELECT ts, end_ts FROM incidents WHERE kind='defender_scan' AND ts<=? AND (end_ts IS NULL OR end_ts>=?)",
            (end + CAUSE_AFTER_S, start - CAUSE_AFTER_S))]

    def _heavy(self, con, start, end):
        h = {}
        for r in _rows(con, "SELECT ts, name, kind, cpu_pct, gpu_pct FROM procs WHERE ((kind='cpu' AND cpu_pct>?) OR "
                            "(kind='gpu' AND gpu_pct>?)) AND ts BETWEEN ? AND ? ORDER BY ts",
                       (HEAVY_PCT, HEAVY_PCT, start - 1, end + CAUSE_BEFORE_S + HEAVY_WINDOW_S)):
            h.setdefault((r["name"] or "").lower(), []).append(
                (r["ts"], r["cpu_pct"] if r["kind"] == "cpu" else r["gpu_pct"]))
        return h

    def heavy_pct(self, name, ts):
        """Peak CPU/GPU % of the process within HEAVY_WINDOW_S after ts (None if never heavy)."""
        v = [p for t, p in self.heavy.get((name or "").lower(), ()) if ts <= t <= ts + HEAVY_WINDOW_S]
        return max(v) if v else None

    def causes(self, ts):
        """Stutter at ts -> ["background_start:<name>" (max 2, strongest first), "background_scan:defender"]."""
        out = {}
        lo = bisect.bisect_left(self.starts_ts, ts - CAUSE_BEFORE_S)
        hi = bisect.bisect_right(self.starts_ts, ts + CAUSE_AFTER_S)
        for _, name, pct in self.starts[lo:hi]:
            key = name.lower()
            if key not in out or pct > out[key][0]:
                out[key] = (pct, name)
        tags = [f"background_start:{n}" for _, n in sorted(out.values(), key=lambda x: (-x[0], x[1].lower()))]
        tags = tags[:MAX_START_TAGS]
        if any(a - CAUSE_AFTER_S <= ts <= (b if b is not None else self.end) + CAUSE_AFTER_S for a, b in self.scans):
            tags.append("background_scan:defender")
        return tags


def session_proc_events(con, start, end, game_exe, culprits=None):
    """Report section: processes started during the session, culprits among them, Defender scans."""
    exe = (game_exe or "").lower()
    cul = _lc(DEFAULT_CULPRITS if culprits is None else culprits)
    sc = StartCauses(con, start, end, game_exe, culprits)
    skip = an.NOT_BACKGROUND | {exe}
    rows = [r for r in _rows(con, "SELECT ts, name, parent_name, path FROM proc_events WHERE action='start' AND "
                                  "ts BETWEEN ? AND ? ORDER BY ts", (start, end))
            if (r["name"] or "").lower() not in skip]
    starts = [{"time": an.fmt_ts(r["ts"]), "name": r["name"], "parent_name": r["parent_name"], "path": r["path"]}
              for r in rows[:MAX_STARTS]]
    groups = {}
    for r in rows:
        n = (r["name"] or "").lower()
        if exe and (r["parent_name"] or "").lower() == exe:
            continue
        pct = sc.heavy_pct(n, r["ts"])
        if n not in cul and not pct:
            continue
        g = groups.setdefault(n, {"name": r["name"], "starts": 0, "first_time": an.fmt_ts(r["ts"]),
                                  "parent_name": r["parent_name"], "heavy_pct": None})
        g["starts"] += 1
        if pct and (g["heavy_pct"] is None or pct > g["heavy_pct"]):
            g["heavy_pct"] = an._r(pct)
    scans = [{"start_time": an.fmt_ts(r["ts"]), "end_time": an.fmt_ts(r["end_ts"]) or None,
              "duration_s": round(r["end_ts"] - r["ts"]) if r["end_ts"] else None}
             for r in _rows(con, "SELECT ts, end_ts FROM incidents WHERE kind='defender_scan' AND ts<=? AND "
                                 "(end_ts IS NULL OR end_ts>=?) ORDER BY ts", (end, start))]
    return {"starts": starts, "culprits": list(groups.values()), "defender_scans": scans}


def findings(rep):
    pe = rep.get("proc_events") or {}
    culprits, scans = pe.get("culprits") or [], pe.get("defender_scans") or []
    if not culprits and not scans:
        return []
    cc = (rep.get("stutters") or {}).get("cause_counts") or {}
    hits = sum(v for k, v in cc.items() if k.startswith(("background_start:", "background_scan:")))
    scan_txt = ", ".join(f"{_hhmm(s['start_time'])}-{_hhmm(s['end_time']) or '...'}" for s in scans)
    parts, used = [], False
    for c in culprits:
        n = re.sub(r"\.exe$", "", c["name"] or "?", flags=re.I)
        extra = f"{c['starts']}x" if c["starts"] > 1 else ""
        if scans and n.lower() == "msmpeng":
            extra = ", ".join(x for x in (extra, f"skan Defendera {scan_txt}") if x)
            used = True
        parts.append(f"{n} ({extra})" if extra else n)
    if scans and not used:
        parts.append(f"skan Defendera ({scan_txt})")
    detail = ", ".join(parts[:6])
    if hits:
        detail += f". Powiązane stuttery: {hits}"
    detail += ". Zaplanuj skan/aktualizacje poza godzinami grania."
    title = "Podczas sesji wystartowały procesy w tle" if culprits else "Podczas sesji działał skan Windows Defender"
    return [{"severity": "warning" if hits >= 2 else "info", "title": title, "detail": detail}]


def events_list(con, t0, t1, limit=500):
    """Rows for `pm procs --events FROM TO`, oldest first."""
    return [{"ts": r["ts"], "time": an.fmt_ts(r["ts"]), "action": r["action"], "name": r["name"], "pid": r["pid"],
             "parent_name": r["parent_name"], "session": r["session"]}
            for r in _rows(con, "SELECT ts, action, name, pid, parent_name, session FROM proc_events "
                                "WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT ?", (t0, t1, int(limit)))]
