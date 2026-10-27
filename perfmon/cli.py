"""Command line access to the PerfMonitor database (for AI agents and the user).

Usage: pm <command> [args] [--json]. Output is compact Markdown by default.
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perfmon import analyze as an  # noqa: E402
from perfmon import config, db  # noqa: E402
from perfmon import ocanalyze as oc  # noqa: E402

SEV = {"critical": "[KRYTYCZNE]", "serious": "[POWAZNE]", "warning": "[OSTRZEZENIE]", "info": "[info]"}


# ---------------- output helpers ----------------
def out(s=""):
    sys.stdout.write(str(s) + "\n")


def cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).replace("|", "\\|").replace("\n", " ")


def table(rows, cols=None, maxw=120):
    if not rows:
        out("_(brak danych)_")
        return
    cols = cols or list(rows[0].keys())
    out("| " + " | ".join(cols) + " |")
    out("|" + "|".join("---" for _ in cols) + "|")
    for r in rows:
        cs = [cell(r.get(c)) for c in cols]
        cs = [c if len(c) <= maxw else c[:maxw - 1] + "…" for c in cs]
        out("| " + " | ".join(cs) + " |")


def kv(d, title=None):
    if title:
        out(f"### {title}")
    for k, v in d.items():
        if v is not None and v != {} and v != []:
            out(f"- **{k}**: {cell(v)}")


def findings(fs):
    if not fs:
        return
    out("### Wnioski")
    for f in fs:
        out(f"- {SEV.get(f['severity'], f['severity'])} **{f['title']}** - {f['detail']}")


def add_time(rows, keys=("ts",)):
    for r in rows:
        for k in keys:
            if k in r and isinstance(r[k], (int, float)):
                r[k] = an.fmt_ts(r[k])
    return rows


def dump(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=1, default=str) + "\n")


# ---------------- commands ----------------
def cmd_status(con, a):
    s = an.latest(con)
    if a.json:
        return dump(s)
    smp = s["sample"] or {}
    extra = {}
    try:
        pid = oc.active_profile_id(con)
        if pid is not None:
            extra["profil OC"] = (oc.profile(con, pid) or {}).get("name")
    except sqlite3.Error:
        pass
    try:
        files = [Path(str(config.DB_PATH) + sfx) for sfx in ("", "-wal", "-shm")]
        extra["rozmiar bazy [MB]"] = round(sum(f.stat().st_size for f in files if f.exists()) / 1048576, 1)
    except OSError:
        pass
    out(f"## Status kolektora: {'DZIALA' if s['collector_alive'] else 'NIE DZIALA'}")
    kv({"ostatnia próbka": an.fmt_ts(smp.get("ts")), "wiek próbki [s]": s["sample_age_s"],
        "admin": s["admin"], "kolektor start": an.fmt_ts(s["collector_started"]),
        "kolektor stop": an.fmt_ts(s["collector_stopped"]), "liczniki": s["counts"] and {
            **s["counts"], "first_sample": an.fmt_ts(s["counts"]["first_sample"])}, **extra})
    if smp:
        keys = ["cpu_total", "cpu_temp", "cpu_clock_mhz", "gpu_load", "gpu_temp", "gpu_hotspot", "gpu_vram_mb",
                "ram_pct", "disk_lat_ms", "ui_ms", "dwm_ms", "fg_exe"]
        kv({k: smp.get(k) if not isinstance(smp.get(k), float) else round(smp[k], 1) for k in keys}, "Ostatnia próbka")
    if s["open_session"]:
        o = s["open_session"]
        out(f"### Trwa sesja gry: #{o['id']} {o['exe']} ({o['running_s'] // 60} min)")
    if not s["admin"]:
        out("_Uwaga: kolektor bez uprawnień admina - część czujników/PresentMon może być niedostępna._")


def cmd_health(con, a):
    h = an.health_overview(con, a.days)
    if a.json:
        return dump(h)
    out(f"## Zdrowie systemu - ostatnie {a.days} dni (od {h['since']})")
    if h["sample_coverage"]:
        kv(h["sample_coverage"], "Pokrycie danymi")
    findings(h["findings"])
    out("### Incydenty wg rodzaju")
    table([{"kind": k, "count": v} for k, v in h["incidents_by_kind"].items()])
    out("### Incydenty dziennie")
    kinds = sorted({k for v in h["incidents_per_day"].values() for k in v})
    table([{"day": d, **{k: v.get(k, 0) for k in kinds}} for d, v in h["incidents_per_day"].items()], ["day"] + kinds)
    out("### Nieoczekiwane wyłączenia (Kernel-Power 41 / 6008)")
    table([{"time": u["time"], "bugcheck": u.get("bugcheck_code"), "sleep_in_progress": u.get("sleep_in_progress"),
            "power_btn": u.get("power_button_ts"), "notes": "; ".join(u.get("interpretation", [])) or u.get("source"),
            "last_sample": (u.get("last_sample_before") or {}).get("time")} for u in h["unclean_shutdowns"]], maxw=200)
    out("### Powtarzające się crashe / zawieszenia aplikacji")
    table(h["recurring_crashes"], ["app", "kind", "n", "first", "last"])
    out("### Freezy zarejestrowane przez kolektor")
    table(h["freezes"])
    out("### Zmiany konfiguracji")
    table([{"time": c["time"], "title": c["title"]} for c in h["config_changes"]])


def cmd_sessions(con, a):
    rows = an.sessions(con, a.limit)
    if a.json:
        return dump(rows)
    out("## Sesje gier")
    for r in rows:
        r["start"], r["end"] = an.fmt_ts(r["start_ts"]), an.fmt_ts(r["end_ts"])
        r["min"] = round((r["duration_s"] or 0) / 60, 1)
    table(rows, ["id", "exe", "start", "min", "end_reason", "fps_avg", "fps_1low", "fps_01low", "stutters",
                 "gpu_driver", "game_version"])


def cmd_session(con, a):
    r = an.session_report(con, a.id)
    if a.json:
        return dump(r)
    if "error" in r:
        return out(r["error"])
    s = r["session"]
    out(f"## Sesja #{s['id']} - {s['exe']}  ({r['start']} -> {r['end']}, {r['duration_s'] // 60} min, koniec: {s['end_reason']})")
    kv({"fps_avg": s["fps_avg"], "fps_1low": s["fps_1low"], "fps_01low": s["fps_01low"], "ft_p99_ms": s["ft_p99"],
        "ft_max_ms": s["ft_max"], "sterownik": s.get("gpu_driver"), "wersja gry": s.get("game_version")}, "Podsumowanie")
    if r["bound_pct"]:
        kv({"GPU-bound %": r["bound_pct"].get("GPU"), "CPU-bound %": r["bound_pct"].get("CPU"),
            "limit %": r["bound_pct"].get("limit")}, "Wąskie gardło")
    if r["fps_timeline"]:
        kv(r["fps_timeline"], "FPS w czasie")
    st = r["stutters"]
    kv({"liczba": st["count"], "na minutę": st["per_min"], "najgorszy ms": st["worst_ft_ms"],
        "przyczyny": st["cause_counts"]}, "Stuttery")
    if st["worst"]:
        table([{"time": w["time"], "ft_ms": w["ft_ms"], "median_ms": w["median_ms"], "causes": ", ".join(w["causes"])}
               for w in st["worst"]])
    kv(r["hardware"], "Sprzęt (szczyty/średnie)")
    if r["incidents"]:
        out("### Incydenty w trakcie sesji")
        table([{"time": i["time"], "kind": i["kind"], "title": i["title"]} for i in r["incidents"]])
    if r["events"]:
        out("### Zdarzenia Windows w trakcie sesji")
        table(r["events"], ["time", "provider", "event_id", "level", "message"])
    findings(r["findings"])


def cmd_incidents(con, a):
    rows = an.incidents(con, a.since, a.kind)
    if a.json:
        return dump(rows)
    out(f"## Incydenty od {an.fmt_ts(an.parse_time(a.since))}" + (f" ({a.kind})" if a.kind else ""))
    for r in rows:
        r["dur_s"] = round(r["end_ts"] - r["ts"]) if r["end_ts"] else None
    table(rows, ["time", "kind", "severity", "dur_s", "title"])


def cmd_events(con, a):
    rows = an.events(con, a.since, a.level, a.grep, a.limit)
    if a.json:
        return dump(rows)
    out(f"## Zdarzenia Windows od {an.fmt_ts(an.parse_time(a.since))} (level<={a.level})")
    for r in rows:
        r["message"] = (r["message"] or "")[:300]
    table(rows, ["time", "log", "provider", "event_id", "level", "message"], maxw=300)


def cmd_range(con, a):
    r = an.range_report(con, an.parse_time(a.frm), an.parse_time(a.to))
    if a.json:
        return dump(r)
    out(f"## Zakres {r['from']} -> {r['to']} ({r['duration_s'] // 60} min, {r['sample_count']} próbek)")
    if r.get("data_gaps"):
        out("Największe luki w danych: " + "; ".join(f"po {g['after']} - {g['gap_s']} s" for g in r["data_gaps"]))
    out("### Metryki")
    table([{"metric": k, **v} for k, v in r["stats"].items()])
    if r.get("foreground"):
        out("### Okno na pierwszym planie")
        table(r["foreground"])
    out("### Incydenty")
    table([{"time": i["time"], "kind": i["kind"], "title": i["title"]} for i in r["incidents"]])
    out("### Zdarzenia (level<=2)")
    table(r["events"], ["time", "provider", "event_id", "message"])
    out("### Top procesy CPU")
    table(r["top_procs"]["cpu"])
    out("### Top procesy I/O")
    table(r["top_procs"]["io"])
    out("### Sesje gier w zakresie")
    table(r["sessions"], ["id", "exe", "fps_avg", "fps_1low", "end_reason"])


def cmd_at(con, a):
    r = an.moment(con, an.parse_time(a.time), a.window)
    if a.json:
        return dump(r)
    out(f"## Chwila {r['at']} (+-{a.window:g} s)")
    if r["sample_gap_note"]:
        out(r["sample_gap_note"])
        out(f"Najbliższa próbka przed: {r['nearest_sample_before']}, po: {r['nearest_sample_after']}")
    table(r["samples"], ["time", "cpu_total", "cpu_core_max", "cpu_temp", "gpu_load", "gpu_temp", "gpu_hotspot",
                         "gpu_vram_mb", "ram_pct", "hard_faults_s", "disk_lat_ms", "dpc_pct", "ui_ms", "dwm_ms",
                         "fg_exe", "fg_hung"])
    out("### Incydenty")
    table([{"time": i["time"], "kind": i["kind"], "title": i["title"]} for i in r["incidents"]])
    out("### Zdarzenia")
    table(r["events"], ["time", "provider", "event_id", "level", "message"], maxw=250)
    out("### Procesy (cpu/io/mem)")
    table([p for p in r["procs"] if p["kind"] != "mem" or (p["mem_mb"] or 0) > 1500],
          ["time", "kind", "name", "cpu_pct", "mem_mb", "io_mbs"])


def cmd_procs(con, a):
    t0, t1 = an.parse_time(a.frm), an.parse_time(a.to)
    if a.json:
        rows = an._rows(con, "SELECT * FROM procs WHERE kind=? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 2000", (a.kind, t0, t1))
        return dump(rows)
    out(f"## Procesy ({a.kind}) {an.fmt_ts(t0)} -> {an.fmt_ts(t1)}")
    col = {"cpu": "cpu_pct", "mem": "mem_mb", "io": "io_mbs"}[a.kind]
    table(an._rows(con, f"SELECT name, ROUND(AVG({col}),2) AS avg, ROUND(MAX({col}),1) AS max, COUNT(*) AS appearances "
                        f"FROM procs WHERE kind=? AND ts BETWEEN ? AND ? AND name NOT IN ('System Idle Process','Idle') GROUP BY name ORDER BY SUM({col}) DESC LIMIT 25",
                   (a.kind, t0, t1)))


def cmd_sysinfo(con, a):
    rows = an._rows(con, "SELECT ts, info FROM system_info ORDER BY ts DESC LIMIT 2")
    if not rows:
        return out("Brak migawek system_info (kolektor jeszcze jej nie zrobił).")
    cur = json.loads(rows[0]["info"])
    if a.diff:
        if len(rows) < 2:
            return out("Tylko jedna migawka - nie ma z czym porównać.")
        old = json.loads(rows[1]["info"])
        d = {k: {"before": old.get(k), "after": cur.get(k)} for k in set(cur) | set(old) if old.get(k) != cur.get(k)}
        if a.json:
            return dump(d)
        out(f"## Zmiany konfiguracji {an.fmt_ts(rows[1]['ts'])} -> {an.fmt_ts(rows[0]['ts'])}")
        return table([{"key": k, "before": v["before"], "after": v["after"]} for k, v in d.items()], maxw=300) if d else out("Brak zmian.")
    if a.json:
        return dump({"ts": rows[0]["ts"], "info": cur})
    out(f"## Konfiguracja systemu (migawka {an.fmt_ts(rows[0]['ts'])})")
    for k, v in cur.items():
        out(f"- **{k}**: {cell(v)[:600]}")


def cmd_schema(con, a):
    comments = {}
    cur_table = None
    for line in db.SCHEMA.splitlines():
        m = re.match(r"\s*CREATE TABLE IF NOT EXISTS (\w+)\s*\(\s*(?:--\s*(.*))?", line)
        if m:
            cur_table = m.group(1)
            comments[(cur_table, None)] = m.group(2) or ""
            continue
        m = re.match(r"\s*(\w+)\s+(?:REAL|TEXT|INTEGER)[^-]*?(?:--\s*(.*))?$", line)
        if m and cur_table and m.group(1) not in ("PRIMARY", "UNIQUE"):
            comments[(cur_table, m.group(1))] = m.group(2) or ""
    res = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        res[t] = [{"column": r[1], "type": r[2], "comment": comments.get((t, r[1]), "")}
                  for r in con.execute(f"PRAGMA table_info({t})")]
    if a.json:
        return dump(res)
    for t, cols in res.items():
        out(f"### {t}" + (f" - {comments.get((t, None))}" if comments.get((t, None)) else ""))
        for c in cols:
            out(f"- `{c['column']}` {c['type']}" + (f" - {c['comment']}" if c["comment"] else ""))


def cmd_sql(con, a):
    q = a.query.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", q, re.I) or ";" in q:
        sys.exit("Dozwolone tylko pojedyncze zapytania SELECT/WITH.")
    try:
        con.execute("PRAGMA query_only=1")
        cur = con.execute(q)
        rows = [dict(r) for r in cur.fetchmany(501)]
    except sqlite3.Error as e:
        sys.exit(f"SQL error: {e}")
    trunc = len(rows) > 500
    rows = rows[:500]
    if a.json:
        return dump({"rows": rows, "truncated": trunc})
    table(rows, maxw=200)
    out(f"_{len(rows)} wierszy{' (obcięto do 500)' if trunc else ''}_")


def cmd_compare(con, a):
    rows = an.compare(con, a.exe, a.by, a.since)
    if a.json:
        return dump(rows)
    out(f"## Porównanie FPS wg: {a.by} (od {a.since})")
    for r in rows:
        r["period"] = f"{r['from_time'][:16]} - {r['to_time'][5:16]}"
        r["drivers"] = ", ".join(r["drivers"])
        r["versions"] = ", ".join(r["game_versions"])
    table(rows, ["exe", "group", "period", "sessions", "minutes", "fps_avg", "fps_avg_delta_pct", "fps_1low",
                 "fps_1low_delta_pct", "stutters_per_min", "gpu_bound_pct", "cpu_bound_pct", "limit_pct", "drivers",
                 "versions"])


def cmd_markers(con, a):
    m = an.timeline_markers(con, a.exe, a.since)
    if a.json:
        return dump(m)
    out("## Znaczniki zmian (sterowniki, aktualizacje gier, OS, BIOS, konfiguracja)")
    table(m, ["time", "kind", "label", "session"])


def cmd_config(con, a):
    cfg = config.load()
    if a.json:
        return dump(cfg)
    out("## Efektywna konfiguracja")
    for k, v in cfg.items():
        out(f"- **{k}**: {cell(v) if not isinstance(v, list) else ', '.join(v)}")
    out(f"- **DB_PATH**: {config.DB_PATH}")


def cmd_collector(con, a):
    """Ask the running (elevated) collector to restart itself - no UAC needed."""
    flag = config.DATA_DIR / "reload.request"
    flag.touch()
    out(f"Zlecono przeładowanie kolektora ({flag}). Kolektor sprawdza flagę co 5 s; "
        "w trakcie sesji gry przeładowanie czeka na jej koniec. Sprawdź: pm status")


def cmd_sensors(con, a):
    """Have the elevated collector dump every LHM sensor (the CLI itself runs without admin rights)."""
    path, flag = config.DATA_DIR / "sensors_dump.json", config.DATA_DIR / "sensors_dump.request"
    t0 = time.time()
    flag.touch()
    while not (path.exists() and path.stat().st_mtime >= t0 - 1):
        if time.time() - t0 > 30:
            flag.unlink(missing_ok=True)
            sys.exit("Kolektor nie odpowiedział w 30 s (czy działa? pm status)")
        time.sleep(0.5)
    d = json.loads(path.read_text(encoding="utf-8"))
    if a.json:
        return dump(d)
    out(f"## Czujniki LHM ({len(d['sensors'])}, admin={d['admin']}, {an.fmt_ts(d['ts'])})")
    table(d["sensors"], ["hardware", "type", "name", "value", "min", "max"])


def cmd_freeze_check(con, a):
    from perfmon import freezecheck
    r = freezecheck.run(con)
    if a.json:
        return dump(r)
    out(f"## Freeze check {r['at']} ({r['took_s']} s)")
    out("### Werdykt")
    for v in r["verdict"]:
        out(f"- {v}")
    kv({"DwmFlush [ms]": "ZABLOKOWANY > 20 s" if r["dwm_blocked"] else r["dwm_flush_ms"],
        "explorer ping [ms]": round(r["shell_ping_ms"]) if r["shell_ping_ms"] is not None else r["shell_note"],
        "pierwszy plan": f"{r['foreground']['exe']} (hung={r['foreground']['hung']})",
        "GPU 3D wg adaptera [%]": r["gpu_3d_by_adapter"], "CPU %": r["cpu_pct"], "RAM %": r["ram_pct"],
        "dwm.exe": r["dwm_process"], "explorer.exe": r["explorer_process"],
        "kolektor": r.get("collector")}, "Pomiary")
    out("### Monitory")
    table(r["displays"])
    out("### Top procesy CPU (1 s)")
    table(r["top_procs"])
    if r.get("recent_incidents"):
        out("### Incydenty z ostatnich 10 min")
        table(r["recent_incidents"])
    if r.get("recent_events"):
        out("### Zdarzenia (level<=3) z ostatnich 10 min")
        table(r["recent_events"], ["time", "provider", "event_id", "level", "message"])


# ---------------- UV/OC ----------------
def _onoff(v):
    return {"on": True, "off": False}.get(v.lower(), v)


def _settings(a):
    """CLI flags -> (cpu, gpu) dicts for ocanalyze.validate; only keys the user passed."""
    if a.co is not None and a.co_all is not None:
        raise ValueError("Podaj albo --co, albo --co-all")
    cpu, pbo, gpu = {}, {}, {}
    if a.co is not None:
        try:
            cpu["co_per_core"] = [int(x) for x in a.co.split(",") if x.strip()]
        except ValueError:
            raise ValueError("--co: liczby całkowite oddzielone przecinkami, np. -10,-15,-10,-5,-10,-10,-15,-10")
    if a.co_all is not None:
        cpu["co_per_core"] = [a.co_all] * 8
    for flag, key in (("ppt", "ppt"), ("tdc", "tdc"), ("edc", "edc"), ("scalar", "scalar"), ("boost", "boost_override")):
        if getattr(a, flag) is not None:
            pbo[key] = getattr(a, flag)
    if pbo:
        cpu["pbo"] = pbo
    if a.expo is not None:
        cpu["expo"] = _onoff(a.expo)
    if a.soc_v is not None:
        cpu["soc_v"] = a.soc_v
    for flag, key in (("gpu_uv", "voltage_offset_mv"), ("gpu_pl", "power_limit_pct"), ("gpu_clk", "max_clock_offset"),
                      ("vram", "vram_mhz"), ("fan", "fan_curve")):
        if getattr(a, flag) is not None:
            gpu[key] = getattr(a, flag)
    if a.vram_fast is not None:
        gpu["vram_fast_timing"] = _onoff(a.vram_fast)
    return cpu or None, gpu or None


def _delta(v):
    return None if v is None else f"{v:+g}%"


def cmd_oc(con, a):
    act = a.action
    if act == "list":
        ps = oc.profiles(con)
        if a.json:
            return dump(ps)
        out("## Profile UV/OC")
        table([{"id": p["id"], "name": p["name"], "status": p["status"], "active": "✓" if p["active"] else "",
                "summary": oc.summarize_settings(p), "active_hours": p["active_hours"], "created": p["created"]}
               for p in ps], maxw=160)
    elif act == "show":
        p = oc.profile(con, a.id)
        if not p:
            raise ValueError(f"Nie ma profilu #{a.id}")
        p["periods"] = [{"from": an.fmt_ts(f), "to": an.fmt_ts(t)} for pid, f, t in oc.periods(con) if pid == p["id"]]
        p["summary"] = oc.summarize_settings(p)
        if a.json:
            return dump(p)
        out(f"## Profil #{p['id']} - {p['name']} ({p['status']})")
        kv({"ustawienia": p["summary"], "utworzono": p["created"], "notatki": p["notes"],
            "aktywny w": "; ".join(f"{x['from']} -> {x['to']}" for x in p["periods"]) or "nigdy"})
        kv(p["cpu"], "CPU")
        kv(p["gpu"], "GPU")
    elif act in ("add", "edit"):
        cpu, gpu = _settings(a)
        since = an.parse_time(a.since) if act == "add" and a.since else None
        if act == "add":
            pid = oc.create_profile(con, a.name, cpu, gpu, a.notes, a.status or "testing")
            if a.activate:
                oc.activate(con, pid, ts=since)
        else:
            pid = a.id
            oc.update_profile(con, pid, name=a.name, cpu=cpu, gpu=gpu, notes=a.notes, status=a.status)
        p = oc.profile(con, pid)
        if a.json:
            return dump(p)
        out(f"Profil #{pid} {'utworzony' if act == 'add' else 'zaktualizowany'}: **{p['name']}** ({p['status']})")
        out(f"Ustawienia: {oc.summarize_settings(p)}")
        if act == "add" and a.activate:
            out(f"Aktywny od {an.fmt_ts(since or time.time())}.")
    elif act == "activate":
        ts = oc.activate(con, a.id, ts=an.parse_time(a.since) if a.since else None)
        p = oc.profile(con, a.id)
        if a.json:
            return dump({"profile": p, "since": ts})
        out(f"Aktywny profil: **{p['name']}** (#{p['id']}) od {an.fmt_ts(ts)}.")
        out("Sesje rozpoczęte od tej chwili zostały ponownie przypisane do profilu." if a.since
            else "Nowe sesje będą przypisywane do tego profilu.")
    elif act == "compare":
        r = oc.compare_profiles(con, a.exe, a.since)
        if a.json:
            return dump(r)
        out(f"## Porównanie profili - {r['exe'] or 'brak gier'} (od {a.since})")
        out(f"Punkt odniesienia: {r['baseline'] or '-'}")
        rows = [{**x, "fps_avg_delta_pct": _delta(x.get("fps_avg_delta_pct")),
                 "fps_per_w_delta_pct": _delta(x.get("fps_per_w_delta_pct"))} for x in r["rows"]]
        table(rows, ["profile", "sessions", "minutes", "fps_avg", "fps_avg_delta_pct", "fps_1low", "fps_per_w",
                     "fps_per_w_delta_pct", "power_w", "cpu_temp", "gpu_hotspot", "errors_per_h", "game_crashes"])
        small = [x["profile"] for x in r["rows"] if x["small_sample"]]
        if small:
            out(f"_mała próba (<{oc.MIN_COMPARE_MIN} min): {', '.join(small)}_")
    elif act == "stability":
        r = oc.stability(con, a.profile)
        if a.json:
            return dump(r)
        if "error" in r:
            return out(r["error"])
        p = r["profile"]
        out(f"## Stabilność - {p['name']} (#{p['id']}, {p['status']}): {r['verdict']}")
        kv({"ustawienia": r["settings"], "aktywny od": r["active_since"], "zebrane dane [h]": r["hours_collected"],
            "błędy/h": r["errors_per_h"], "błędy": r["errors"], "resety przy niskim obciążeniu": r["low_load_resets"]})
        table([{"check": c["check"], "ok": "✓" if c["ok"] else "✗", "value": c["value"]} for c in r["checks"]])
        if r["resets"]:
            out("### Resety")
            table(r["resets"], ["time", "cpu_avg", "low_load"])
        if r["missing"]:
            out("Brakuje: " + "; ".join(r["missing"]))
    elif act == "vf":
        r = oc.vf_curve(con, a.profile, a.since)
        if a.json:
            return dump(r)
        out(f"## Krzywa V/F (od {a.since}, {r['rows']} próbek)")
        for key, title in (("gpu", "GPU: MHz -> V (obciążenie >= 90%)"), ("cpu_single", "CPU: jeden wątek"),
                           ("cpu_all", "CPU: wszystkie rdzenie")):
            out(f"### {title}")
            table(r[key], ["mhz", "v", "n"])
    elif act == "cores":
        r = oc.per_core(con, a.profile, a.since)
        if a.json:
            return dump(r)
        out(f"## Rdzenie CPU (od {a.since})")
        table([{**c, "suspect": "✓" if c["suspect"] else ""} for c in r["cores"]],
              ["core", "whea19", "whea18", "stretch_pct", "eff_loaded_avg", "eff_max", "loaded_min", "suspect"])
        if r["note"]:
            out(r["note"])
        if r["whea_uncore"]:
            kv({"WHEA poza rdzeniami": r["whea_uncore"]})


def cmd_stress(con, a):
    act = a.action
    if act in ("start", "idle"):
        name = a.name or "idle soak"
        sid = oc.stress_start(con, name, "manual" if act == "start" else "idle_soak")
        if a.json:
            return dump({"session": sid, "name": name})
        out(f"Rozpoczęto test #{sid}: **{name}**.")
        out("Zostaw PC w spoczynku na 1-2 h, potem uruchom `pm stress stop`." if act == "idle"
            else "Po zakończeniu testu uruchom `pm stress stop`.")
    elif act == "stop":
        ids = oc.stress_stop(con)
        if a.json:
            return dump({"ended": ids})
        out(f"Zakończono: {', '.join(f'#{i}' for i in ids)}." if ids else "Brak trwających testów.")
        if ids:
            out("Wynik: pm stress list")
    elif act == "list":
        rows = oc.stress_runs(con, a.limit)
        if a.json:
            return dump(rows)
        out("## Testy obciążeniowe i bezczynność")
        for r in rows:
            r["test"] = r["test"] or r["exe"]
            r["errors"] = sum((r["errors"] or {}).values()) if r["errors"] is not None else None
        table(rows, ["id", "time", "test", "kind", "minutes", "end_reason", "errors", "tctl_max", "gpu_hot_max",
                     "cpu_w_avg", "gpu_w_avg", "stretch_max_pct"])


def build():
    p = argparse.ArgumentParser(prog="pm", description="PerfMonitor CLI")
    p.add_argument("--json", action="store_true", help="output JSON instead of Markdown")
    sp = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        s = sp.add_parser(name, **kw)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        s.set_defaults(fn=fn)
        return s
    add("status", cmd_status)
    add("health", cmd_health).add_argument("--days", type=float, default=14)
    add("sessions", cmd_sessions).add_argument("--limit", type=int, default=20)
    add("session", cmd_session).add_argument("id", type=int)
    s = add("incidents", cmd_incidents)
    s.add_argument("--since", default="7d")
    s.add_argument("--kind")
    s = add("events", cmd_events)
    s.add_argument("--since", default="24h")
    s.add_argument("--level", type=int, default=2)
    s.add_argument("--grep")
    s.add_argument("--limit", type=int, default=100)
    s = add("range", cmd_range)
    s.add_argument("frm", metavar="FROM")
    s.add_argument("to", metavar="TO", nargs="?", default="now")
    s = add("at", cmd_at)
    s.add_argument("time")
    s.add_argument("--window", type=float, default=30)
    s = add("procs", cmd_procs)
    s.add_argument("frm", metavar="FROM")
    s.add_argument("to", metavar="TO", nargs="?", default="now")
    s.add_argument("--kind", default="cpu", choices=["cpu", "mem", "io"])
    add("sysinfo", cmd_sysinfo).add_argument("--diff", action="store_true")
    add("schema", cmd_schema)
    add("sql", cmd_sql).add_argument("query")
    s = add("compare", cmd_compare)
    s.add_argument("--exe")
    s.add_argument("--by", default="week", choices=["week", "day", "month", "driver", "game_version", "session"])
    s.add_argument("--since", default="90d")
    s = add("markers", cmd_markers)
    s.add_argument("--exe")
    s.add_argument("--since", default="180d")
    add("config", cmd_config)
    add("collector", cmd_collector, help="restart the running collector").add_argument("action", choices=["reload"])
    add("sensors", cmd_sensors, help="dump every hardware sensor").add_argument("action", choices=["dump"])
    add("freeze-check", cmd_freeze_check, help="live diagnosis while the desktop is frozen")

    s = add("oc", cmd_oc, help="UV/OC profiles: list|show|add|edit|activate|compare|stability|vf|cores")
    ocp = s.add_subparsers(dest="action", required=True)

    def oc_action(name, writes=False):
        x = ocp.add_parser(name)
        x.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        x.set_defaults(writes=writes)
        return x

    def settings_flags(x):
        x.add_argument("--co", help="Curve Optimizer per core, e.g. -10,-15,-10,-5,-10,-10,-15,-10")
        x.add_argument("--co-all", type=int, help="Curve Optimizer for all 8 cores")
        for f in ("ppt", "tdc", "edc", "scalar"):
            x.add_argument(f"--{f}", type=int)
        x.add_argument("--boost", type=int, help="boost override [MHz]")
        x.add_argument("--expo", help="on|off|text, e.g. '6000 CL30'")
        x.add_argument("--soc-v", type=float, help="VSoC [V]")
        x.add_argument("--gpu-uv", type=int, help="GPU voltage offset [mV]")
        x.add_argument("--gpu-pl", type=int, help="GPU power limit [%%]")
        x.add_argument("--gpu-clk", type=int, help="GPU max clock offset [MHz]")
        x.add_argument("--vram", type=int, help="VRAM [MHz]")
        x.add_argument("--vram-fast", help="on|off")
        x.add_argument("--fan", help="fan curve description")
        x.add_argument("--notes")
        x.add_argument("--status", choices=oc.STATUSES)

    oc_action("list")
    oc_action("show").add_argument("id", type=int)
    x = oc_action("add", True)
    x.add_argument("name")
    settings_flags(x)
    x.add_argument("--activate", action="store_true")
    x.add_argument("--since", help="back-date the activation (e.g. 90d, 2026-01-01)")
    x = oc_action("edit", True)
    x.add_argument("id", type=int)
    x.add_argument("--name")
    settings_flags(x)
    x = oc_action("activate", True)
    x.add_argument("id", type=int)
    x.add_argument("--since", help="back-date the activation (re-tags sessions since then)")
    x = oc_action("compare")
    x.add_argument("--exe")
    x.add_argument("--since", default="180d")
    oc_action("stability").add_argument("--profile", type=int)
    for n in ("vf", "cores"):
        x = oc_action(n)
        x.add_argument("--profile", type=int)
        x.add_argument("--since", default="30d")

    s = add("stress", cmd_stress, help="manual stress tests / idle soak: start|idle|stop|list")
    stp = s.add_subparsers(dest="action", required=True)

    def st_action(name, writes=False):
        x = stp.add_parser(name)
        x.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        x.set_defaults(writes=writes)
        return x
    st_action("start", True).add_argument("name")
    st_action("idle", True).add_argument("name", nargs="?")
    st_action("stop", True)
    st_action("list").add_argument("--limit", type=int, default=30)
    return p


NEG_VALUE_FLAGS = {"--co", "--co-all", "--gpu-uv", "--gpu-pl", "--gpu-clk", "--soc-v"}


def _glue_negatives(argv):
    """argparse takes '-10' / '-10,-15' after a flag for an option: rewrite to --flag=-10."""
    res, i = [], 0
    while i < len(argv):
        if argv[i] in NEG_VALUE_FLAGS and i + 1 < len(argv) and argv[i + 1].startswith("-"):
            res.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            res.append(argv[i])
            i += 1
    return res


def main(argv=None):
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    a = build().parse_args(_glue_negatives(sys.argv[1:] if argv is None else list(argv)))
    if not hasattr(a, "json"):
        a.json = False
    import os
    if os.environ.get("PERFMON_DB"):  # testing: point at another database file
        config.DB_PATH = Path(os.environ["PERFMON_DB"])
    try:
        con = db.connect(readonly=not getattr(a, "writes", False))
    except sqlite3.Error as e:
        sys.exit(f"Nie mogę otworzyć bazy {config.DB_PATH}: {e}")
    try:
        a.fn(con, a)
    except ValueError as e:
        sys.exit(str(e))
    finally:
        con.close()


if __name__ == "__main__":
    main()
