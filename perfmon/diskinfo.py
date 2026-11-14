"""Disk health (SMART-like reliability counters) and per-disk stats for the session report.

Pure helpers: the PowerShell disk health fragment for sysinfo, health change detection, event message parsing,
per-session disk stats from samples.extra, report findings. Human-readable strings are Polish."""
import re
import sqlite3

from . import analyze as an

# Fragment for the [ordered]@{ ... } hashtable of sysinfo.DISK_HEALTH_PS (Get-StorageReliabilityCounter needs admin: nulls otherwise)
HEALTH_PS = (r'disk_health = @(Get-PhysicalDisk | % { $d=$_; $r = $d | Get-StorageReliabilityCounter; '
             r'[ordered]@{device=$d.DeviceId; name=$d.FriendlyName; bus="$($d.BusType)"; temp=$r.Temperature; '
             r'wear=$r.Wear; read_unc=$r.ReadErrorsUncorrected; write_unc=$r.WriteErrorsUncorrected; '
             r'read_total=$r.ReadErrorsTotal; power_on_h=$r.PowerOnHours; flush_lat_max=$r.FlushLatencyMax; '
             r'read_lat_max=$r.ReadLatencyMax; write_lat_max=$r.WriteLatencyMax} })')

WEAR_SERIOUS = 90      # % of life used: incident
WEAR_WARN = 80         # % of life used: `pm health` warning
TEMP_WARN = 70         # degrees C: `pm health` warning
SLOW_BUS = ("sata", "ata")
SLOW_NAMES = ("goodram", "cx400", "ssdpr")


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _health(info):
    return [d for d in (info or {}).get("disk_health") or [] if isinstance(d, dict)]


def _key(d):
    return str(d.get("device")) if d.get("device") is not None else d.get("name")


def _dname(d):
    return d.get("name") or f"nr {d.get('device')}"


# ---------- health changes (incidents) ----------
def health_changes(prev, cur):
    """(kind, severity, title, details) for new uncorrected errors / worn-out disks between two system_info dicts."""
    old = {_key(d): d for d in _health(prev)}
    out = []
    for d in _health(cur):
        p = old.get(_key(d))
        name = _dname(d)
        if p:
            grown = {}
            for fld, label in (("read_unc", "odczyt"), ("write_unc", "zapis")):
                a, b = _num(p.get(fld)), _num(d.get(fld))
                if a is not None and b is not None and b > a:
                    grown[label] = (int(a), int(b))
            if grown:
                txt = ", ".join(f"{k} {a} -> {b}" for k, (a, b) in grown.items())
                out.append(("disk_health", "serious", f"Dysk {name}: przybyło błędów nieskorygowanych ({txt})",
                            {"device": d.get("device"), "name": name, "before": {f: p.get(f) for f in ("read_unc", "write_unc")},
                             "after": {f: d.get(f) for f in ("read_unc", "write_unc")}}))
        w, pw = _num(d.get("wear")), _num(p.get("wear")) if p else None
        if w is not None and w >= WEAR_SERIOUS and (pw is None or pw < WEAR_SERIOUS):
            out.append(("disk_health", "serious", f"Dysk {name}: zużycie nośnika {int(w)}% (limit {WEAR_SERIOUS}%)",
                        {"device": d.get("device"), "name": name, "wear": w, "prev_wear": pw}))
    return out


# ---------- event message parsing ----------
_RAIDPORT = re.compile(r"\\Device\\RaidPort(\d+)", re.I)
_HARDDISK = re.compile(r"\\Device\\Harddisk(\d+)(?!\d|Volume)", re.I)
_DISKWORD = re.compile(r"\b(?:Disk|Dysk\w*)\s+(\d+)\b", re.I)   # "for Disk 1 (PDO name", "dla dysku 1 (nazwa PDO"


def _texts(message, data):
    return [message or ""] + [str(x) for x in data or [] if x is not None]


def raidport_disk(message, data=None):
    """Disk/port number N from \\Device\\RaidPortN, \\Device\\HarddiskN\\DRn or "Disk N" (also Polish "dysku N")."""
    ts = _texts(message, data)
    for rx in (_RAIDPORT, _HARDDISK, _DISKWORD):
        for t in ts:
            m = rx.search(t)
            if m:
                return int(m.group(1))
    return None


_VOL_LETTER = re.compile(r"\b(?:volume|wolumin\w*)\w*[\s:]*(?:[^\s:()\\]+\s+)?\(?([A-Za-z]):(?![\w])", re.I)
_VOL_BARE = re.compile(r"^\s*\(?([A-Za-z]):\\?\)?\s*$")
_VOL_DEV = re.compile(r"\\Device\\HarddiskVolume\d+", re.I)


def ntfs_volume(message, data=None):
    """Volume of an Ntfs event: "C:" from "Wolumin C:" / "volume Windows (C:)" / "VolumeId: C:", else \\Device\\HarddiskVolumeN."""
    ts = _texts(message, data)
    for t in ts:
        m = _VOL_LETTER.search(t)
        if m:
            return m.group(1).upper() + ":"
    for t in ts[1:]:
        m = _VOL_BARE.match(t)
        if m:
            return m.group(1).upper() + ":"
    for t in ts:
        m = _VOL_DEV.search(t)
        if m:
            return m.group(0)
    return None


# ---------- per-session disk stats ----------
_DISKS_SQL = ("SELECT je.key AS disk, json_extract(je.value, '$.lat_ms') AS lat, json_extract(je.value, '$.rd_mbs') AS rd "
              "FROM samples s, json_each(CASE WHEN json_valid(s.extra) THEN s.extra ELSE '{}' END, '$.disks') je "
              "WHERE s.ts BETWEEN ? AND ?")


def _latest_info(con, end):
    for sql, args in (("SELECT info FROM system_info WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (end,)),
                      ("SELECT info FROM system_info ORDER BY ts DESC LIMIT 1", ())):
        r = con.execute(sql, args).fetchone()
        if r:
            info = an._json(r[0])
            if isinstance(info, dict):
                return info
    return {}


def _letter(path):
    m = re.match(r"^\s*([A-Za-z]):", path or "")
    return m.group(1).upper() + ":" if m else None


def session_disk(con, start, end, game_path, session=None):
    """Per-disk latency/read volume over [start, end], and which disk the game lives on."""
    acc = {}
    for r in con.execute(_DISKS_SQL, (start, end)):
        if r[0] == "_Total":
            continue
        a = acc.setdefault(r[0], ([], [0.0]))
        if r[1] is not None:
            a[0].append(r[1])
        a[1][0] += r[2] or 0
    per_disk = [{"disk": k, "lat_p95_ms": an._r(an._pct(v[0], 0.95), 2), "lat_p99_ms": an._r(an._pct(v[0], 0.99), 2),
                 "lat_max_ms": an._r(max(v[0]), 2) if v[0] else None, "read_mb": an._r(v[1][0])}
                for k, v in sorted(acc.items())]
    drive = _letter(game_path)
    game_disk = next((p["disk"] for p in per_disk if drive and drive in p["disk"].split()[1:]), None)
    info = _latest_info(con, end)
    num = None
    if game_disk:
        num = game_disk.split()[0]
    elif drive:
        num = next((str(m.get("disk")) for m in info.get("disk_map") or [] if (m.get("letter") or "").upper() + ":" == drive), None)
    hd = next((d for d in _health(info) if num is not None and str(d.get("device")) == num), None)
    name = hd.get("name") if hd else None
    bus = (hd.get("bus") or None) if hd else None
    q, args = ("WHERE session=?", (session,)) if session is not None else ("WHERE ts BETWEEN ? AND ?", (start, end))
    try:
        g = an._one(con, f"SELECT AVG(io_read_mbs) AS a, MAX(io_read_mbs) AS m FROM game_proc {q}", args)
    except sqlite3.OperationalError:     # DB without the game_proc table
        g = None
    return {"per_disk": per_disk, "game_drive": drive, "game_disk": game_disk, "game_disk_name": name,
            "game_disk_bus": bus, "game_read": {"avg_mbs": an._r(g and g["a"], 2), "max_mbs": an._r(g and g["m"], 1)}}


# ---------- findings ----------
def _slow_disk(d):
    bus = (d.get("game_disk_bus") or "").lower()
    name = (d.get("game_disk_name") or "").lower()
    return bus in SLOW_BUS or any(n in name for n in SLOW_NAMES)


def findings(rep):
    """Report findings; reads rep["disk"] (session_disk output) and rep["stutters"]["cause_counts"]."""
    f = []
    add = lambda sev, title, detail: f.append({"severity": sev, "title": title, "detail": detail})
    cc = (rep.get("stutters") or {}).get("cause_counts") or {}
    d = rep.get("disk") or {}
    n_disk, n_reset = cc.get("disk", 0), cc.get("disk_reset", 0)
    if n_disk + n_reset >= 3 and _slow_disk(d):
        name = d.get("game_disk_name") or d.get("game_disk") or "wolny dysk"
        bus = d.get("game_disk_bus")
        lat = next((p for p in d.get("per_disk") or [] if p["disk"] == d.get("game_disk")), None)
        lat_txt = f" Opóźnienie dysku: p99 {lat['lat_p99_ms']} ms, max {lat['lat_max_ms']} ms." if lat and lat.get("lat_p99_ms") is not None else ""
        add("warning", f"Gra na wolnym dysku ({name}{', ' + bus if bus else ''}), {n_disk + n_reset} stutterów dyskowych",
            f"Gra ładuje dane z {d.get('game_drive') or 'dysku'} ({name}).{lat_txt} -> przenieś grę na Samsung 990 PRO (NVMe).")
    if n_reset >= 1:
        add("serious", "Reset kontrolera NVMe / ponowienia I/O podczas gry",
            f"{n_reset} stutterów w momencie resetu kontrolera dysku lub ponowienia operacji I/O (stornvme 129 / disk 153). "
            "Zaktualizuj firmware SSD (Samsung Magician), sprawdź kabel SATA / osadzenie dysku w slocie M.2, wyłącz ASPM / "
            "oszczędzanie energii łącza PCIe w planie zasilania (Link State Power Management = Off) i sprawdź temperaturę dysku.")
    return f


# ---------- `pm health` ----------
def health_rows(info):
    cols = ("temp", "wear", "read_unc", "write_unc", "power_on_h", "read_lat_max", "write_lat_max")
    return [{"name": _dname(d), "bus": d.get("bus"), **{c: d.get(c) for c in cols}} for d in _health(info)]


def health_warnings(info):
    w = []
    for d in _health(info):
        name = _dname(d)
        wear, temp = _num(d.get("wear")), _num(d.get("temp"))
        if wear is not None and wear >= WEAR_WARN:
            w.append(f"{name}: zużycie nośnika {int(wear)}% - zaplanuj wymianę dysku i kopię zapasową.")
        errs = [(lbl, int(v)) for lbl, f in (("odczyt", "read_unc"), ("zapis", "write_unc"))
                if (v := _num(d.get(f))) is not None and v > 0]
        if errs:
            w.append(f"{name}: błędy nieskorygowane ({', '.join(f'{l} {v}' for l, v in errs)}) - sprawdź SMART i kable/slot.")
        if temp is not None and temp >= TEMP_WARN:
            w.append(f"{name}: temperatura {int(temp)} C - sprawdź chłodzenie / radiator M.2.")
    return w
