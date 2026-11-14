"""Game disk temperature: NVMe composite + hottest sensor (samples.extra disk_temps / disk_hot, every 10 s in sessions)
against the drive's own Warning Temperature (meta storage_limits, written by the collector from LHM).

An NVMe drive at its warning temperature starts throttling (the 990 PRO: Warning 81 C, Critical 84 C), which shows
up in games as asset-streaming stutter. SATA drives report no thresholds: TEMP_WARN_DEFAULT stands in.
Human-readable strings are Polish."""
import bisect
import json

from . import analyze as an
from . import db, diskinfo

LIMITS_KEY = "storage_limits"
TEMP_WARN_DEFAULT = diskinfo.TEMP_WARN   # no thresholds reported (SATA)
HOT_MARGIN = 5             # within this many degrees of the warning temperature = hot (throttling starts near it)
WINDOW_S = 30              # temperatures are read every 10 s in sessions, 30 s outside them


def limits(con):
    try:
        return json.loads(db.get_meta(con, LIMITS_KEY, "{}")) or {}
    except ValueError:
        return {}


def game_disk_name(con, path, end):
    """Physical disk name (as LHM / Get-PhysicalDisk show it) of the game's drive letter, from the config snapshot."""
    drive = diskinfo._letter(path)
    if not drive:
        return None
    info = diskinfo._latest_info(con, end)
    num = next((str(m.get("disk")) for m in info.get("disk_map") or []
                if (m.get("letter") or "").upper() + ":" == drive), None)
    hd = next((d for d in diskinfo._health(info) if num is not None and str(d.get("device")) == num), None)
    return hd.get("name") if hd else None


def _same(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


class DiskTemp:
    """Temperatures of the game disk over a session; hot(ts) for the stutter causes."""

    def __init__(self, con, game_path, start, end):
        self.lim = limits(con)
        name = game_disk_name(con, game_path, end)
        rows = con.execute("SELECT ts, json_extract(extra, '$.disk_temps'), json_extract(extra, '$.disk_hot') FROM samples "
                           "WHERE ts BETWEEN ? AND ? AND extra LIKE '%disk_temps%' ORDER BY ts",
                           (start - WINDOW_S, end + WINDOW_S)).fetchall()
        self.name = name
        self.ts, self.temp, self.hot_t = [], [], []
        parsed = {}   # the reading changes every 10-30 s, samples repeat it every second: parse each once
        for ts, t, h in rows:
            if (t, h) not in parsed:
                try:
                    parsed[(t, h)] = json.loads(t or "{}"), json.loads(h or "{}")
                except ValueError:
                    parsed[(t, h)] = {}, {}
            t, h = parsed[(t, h)]
            key = next((k for k in t if _same(k, name)), None)
            if key is None and name is None:   # game disk unknown: only an NVMe drive (has limits) can be it
                nv = [k for k in t if k in self.lim]
                key = nv[0] if len(nv) == 1 else None
            if key is None:
                continue
            self.name = key
            self.ts.append(ts)
            self.temp.append(t[key])
            self.hot_t.append(max(t[key], h.get(key) or 0))
        _LAST.clear()
        _LAST[(id(con), game_path, start, end)] = self
        l = self.lim.get(self.name) or {}
        self.warn, self.crit = l.get("warn"), l.get("crit")
        self.threshold = (self.warn or TEMP_WARN_DEFAULT) - HOT_MARGIN

    def hot(self, ts):
        lo, hi = bisect.bisect_left(self.ts, ts - WINDOW_S), bisect.bisect_right(self.ts, ts + WINDOW_S)
        return any(v >= self.threshold for v in self.hot_t[lo:hi])

    def summary(self):
        if not self.ts:
            return None
        hot = sum(1 for v in self.hot_t if v >= self.threshold)
        return {"disk": self.name, "composite_max_c": an._r(max(self.temp)), "hottest_max_c": an._r(max(self.hot_t)),
                "warn_c": self.warn, "crit_c": self.crit, "hot_threshold_c": self.threshold,
                "hot_time_pct": an._r(100 * hot / len(self.hot_t))}


_LAST = {}   # the report builds StutterExt (with a DiskTemp) right before extend(): reuse it


def session_summary(con, game_path, start, end):
    d = _LAST.get((id(con), game_path, start, end)) or DiskTemp(con, game_path, start, end)
    _LAST.clear()
    return d.summary()


def findings(rep):
    d = rep.get("disk_temp") or {}
    n = ((rep.get("stutters") or {}).get("cause_counts") or {}).get("disk_hot", 0)
    mx = d.get("hottest_max_c")
    if not mx or mx < (d.get("hot_threshold_c") or 999):
        return []
    warn = d.get("warn_c")
    at_warn = warn is not None and mx >= warn
    sev = "serious" if at_warn and n >= 3 else "warning"
    lim = f" (próg ostrzegawczy dysku {warn:g}°C, krytyczny {d.get('crit_c'):g}°C)" if warn and d.get("crit_c") else ""
    st = f" {n} stutterów przy gorącym dysku." if n else ""
    return [{"severity": sev, "title": f"Dysk z grą {'na progu throttlingu' if at_warn else 'blisko progu throttlingu'}: "
                                       f"do {mx:g}°C",
             "detail": f"{d.get('disk')}: najgorętszy czujnik do {mx:g}°C, composite do {d.get('composite_max_c')}°C{lim}, "
                       f"{d.get('hot_time_pct')}% czasu sesji przy progu.{st} Gorący NVMe zwalnia (thermal "
                       "throttling) i gra doczytuje dane z opóźnieniem: radiator M.2 (z płyty lub osobny), przepływ "
                       "powietrza przy slocie pod kartą graficzną."}]
