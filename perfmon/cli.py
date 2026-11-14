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
from perfmon import bottleneck, confdiff, config, db, present  # noqa: E402
from perfmon import experiments as exps  # noqa: E402
from perfmon import ocanalyze as oc  # noqa: E402
from perfmon import ocassist  # noqa: E402

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


def cmd_advise(con, a):
    from perfmon import advisor
    r = advisor.advise(con)
    if a.json:
        return dump(r)
    b = r["bottleneck"]
    out(f"## Co zmienić - {r['game'] or 'brak danych o grze'} ({r['generated']})")
    if b["kind"]:
        out(f"Wąskie gardło: {b['kind']} przez {b['share_pct']}% z {b['minutes']} min grania ({r['sessions']} ostatnich sesji).")
    for i in r["items"]:
        out(f"### {i['priority']}. {i['title']} [{i['kind']}]")
        for k, label in (("why", "Dlaczego"), ("expected", "Oczekiwany efekt"), ("risk", "Ryzyko"), ("undo", "Cofnięcie"),
                         ("how", "Jak")):
            out(f"- {label}: {i[k]}")
    if not r["items"]:
        out("Brak rekomendacji - dane nie wskazują nic do zmiany.")
    if r["not_worth"]:
        out("### Nie warto")
        for n in r["not_worth"]:
            out(f"- {n['title']}: {n['why']}")
    if r["unknown"]:
        out("### Nieznane (sprawdź ręcznie)")
        for u in r["unknown"]:
            out(f"- {u['title']}: {u['check']}")


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
    table(h["recurring_crashes"], ["app", "kind", "n", "first", "last", "module", "code", "modules", "verdict"], maxw=160)
    out("### Freezy zarejestrowane przez kolektor")
    table(h["freezes"])
    out("### Zmiany konfiguracji")
    table([{"time": c["time"], "title": c["title"]} for c in h["config_changes"]])
    if h.get("diag_incidents"):
        out("### Znaczniki, dyski, skany, wycieki (ostatnie 30)")
        table(h["diag_incidents"][:30], ["time", "kind", "severity", "title"], maxw=160)


def cmd_sessions(con, a):
    rows = an.sessions(con, a.limit)
    if a.json:
        return dump(rows)
    out("## Sesje gier")
    for r in rows:
        r["start"], r["end"] = an.fmt_ts(r["start_ts"]), an.fmt_ts(r["end_ts"])
        r["min"] = round((r["duration_s"] or 0) / 60, 1)
    table(rows, ["id", "exe", "start", "min", "end_reason", "fps_avg", "fps_1low", "fps_01low", "stutters",
                 "present", "bottleneck", "mem_growth", "gpu_driver", "game_version"])


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
    if r.get("bottleneck"):
        out(f"Wąskie gardło: {bottleneck.describe(r['bottleneck'])}")
    if r["fps_timeline"]:
        kv(r["fps_timeline"], "FPS w czasie")
    if r.get("present"):
        out(f"Prezentacja: {present.describe(r['present'])}")
    sh = r.get("shader") or {}
    if sh.get("cache_mb") is not None or sh.get("first_session_after_change") not in (None, "none"):
        out(f"Cache shaderów: {cell(sh.get('cache_mb'))} MB (+{cell(sh.get('cache_growth_mb'))} MB w sesji); "
            f"pierwsza sesja po zmianie: {sh.get('first_session_after_change') or '-'}")
    st = r["stutters"]
    kv({"liczba": st["count"], "na minutę": st["per_min"], "najgorszy ms": st["worst_ft_ms"],
        "przyczyny": st["cause_counts"]}, "Stuttery")
    if st.get("cause_ranking"):
        out("Przyczyny względem zwykłej rozgrywki (potwierdzona = przy stutterach częściej niż normalnie):")
        table([{"przyczyna": r["tag"], "przy stutterach %": r["stutter_pct"], "zwykle %": r["base_pct"],
                "ocena": {"confirmed": "potwierdzona", "weak": "możliwa, mało danych", "ambient": "stała, bez związku",
                          "noise": "szum"}[r["verdict"]]}
               for r in st["cause_ranking"] if r["tag"] != "shader_or_unknown"][:8])
    if st["worst"]:
        table([{"time": w["time"], "ft_ms": w["ft_ms"], "median_ms": w["median_ms"], "causes": ", ".join(w["causes"])
                + (f" (pm etw show {w['etw_capture']})" if w.get("etw_capture") else "")} for w in st["worst"]])
    if r.get("crash"):
        c = r["crash"]
        it = c.get("interpretation") or {}
        kv({"moduł": c.get("module") and f"{c['module']} {c.get('module_version') or ''}".strip(),
            "kod wyjątku": c.get("exception_code") and f"{c['exception_code']} {c.get('exception_name') or ''}".strip(),
            "offset": c.get("offset"), "wersja gry": c.get("app_version"), "raport WER": c.get("report_path"),
            "zrzut": c.get("dump_path"), "interpretacja": f"{it.get('verdict')} [pewność: {it.get('confidence')}]",
            "kroki": "; ".join(it.get("next_steps") or [])}, "Crash")
    kv(r["hardware"], "Sprzęt (szczyty/średnie)")
    gp = r.get("game_proc")
    if gp:
        mg = r.get("mem_growth") or {}
        if mg:
            out(f"Pamięć gry: {mg['private_start_mb']} -> {mg['private_end_mb']} MB, trend {mg['mem_growth_mb_h']} MB/h "
                f"(R² {mg['mem_growth_r2']}){' - możliwy wyciek' if mg['leak'] else ''}")
        out(f"VRAM gry: dedicated max {cell(gp['vram_dedicated_max_mb'])} MB, shared max {cell(gp['vram_shared_max_mb'])} MB; "
            f"najbardziej obciążony wątek p95 {cell(gp['thread_max_p95'])}%, pamięć prywatna max {cell(gp['private_max_mb'])} MB")
    d = r.get("disk") or {}
    if d.get("per_disk"):
        out(f"### Dyski (gra na {d.get('game_drive') or '?'}: {d.get('game_disk_name') or d.get('game_disk') or '?'}"
            f"{', ' + d['game_disk_bus'] if d.get('game_disk_bus') else ''}; odczyt gry śr. "
            f"{cell((d.get('game_read') or {}).get('avg_mbs'))} / max {cell((d.get('game_read') or {}).get('max_mbs'))} MB/s)")
        table(d["per_disk"])
    pe = r.get("proc_events") or {}
    if pe.get("culprits") or pe.get("defender_scans"):
        out("### Procesy w tle startujące w trakcie sesji")
        table(pe.get("culprits") or [], ["name", "starts", "first_time", "parent_name", "heavy_pct"])
        for s in pe.get("defender_scans") or []:
            out(f"- Skan Windows Defender {s['start_time']} -> {s['end_time'] or 'trwa'}"
                + (f" ({s['duration_s']} s)" if s.get("duration_s") else ""))
    if r.get("gpu_background"):
        out("### Procesy w tle na GPU")
        table(r["gpu_background"])
    if r.get("net", {}).get("seconds"):
        print_net(r["net"], "Sieć w trakcie sesji")
    if r["incidents"]:
        out("### Incydenty w trakcie sesji")
        table([{"time": i["time"], "kind": i["kind"], "title": i["title"]} for i in r["incidents"]])
    if r["events"]:
        out("### Zdarzenia Windows w trakcie sesji")
        table(r["events"], ["time", "provider", "event_id", "level", "message"])
    print_marks(r.get("marks"))
    print_config(r.get("config"))
    findings(r["findings"])


def print_config(c):
    """Settings in effect for the session and what changed vs the previous session of the game."""
    if not c:
        return
    out("### Ustawienia w tej sesji")
    gs = c.get("game_settings")
    if gs:
        out("Gra: " + ", ".join(f"{k}={cell(v)}" for k, v in gs.items() if v is not None))
    elif c.get("game_settings_error"):
        out(f"Gra: {c['game_settings_error']}")
    amd = c.get("amd") or {}
    if amd and "error" not in amd:
        fs = ", ".join(f"{f['name']} {'wł.' if f.get('gaming') else 'wył.'}" for f in amd.get("freesync") or [])
        out("Adrenalin (globalnie): " + ", ".join(f"{k}={cell(amd.get(k))}" for k in
            ("anti_lag", "chill", "boost", "afmf", "rsr", "frtc", "vsync")) + (f"; FreeSync {fs}" if fs else ""))
    for m in ((c.get("modules") or {}).get("procs") or {}).items():
        name, scan = m
        if scan.get("status") == "ok":
            out(f"Moduły {name}: {scan['count']} DLL; oznaczone: " + (", ".join(f["name"] for f in scan["flagged"]) or "brak"))
        else:
            out(f"Moduły {name}: nieznane ({scan.get('reason')})")
    ch = c.get("changes") or {}
    texts = (ch.get("context") or []) + (ch.get("game") or []) + (ch.get("system") or [])
    if ch.get("game_raw_changed") and not ch.get("game"):
        texts.append(f"plik ustawień gry: {ch['game_raw_changed']} zmienionych kluczy")
    if c.get("previous_session"):
        out(f"Zmiany od sesji #{c['previous_session']}: " + ("; ".join(texts) if texts else "brak wykrytych"))
    for e in c.get("mid_session") or []:
        out(f"- {an.fmt_ts(e['ts'])} zmiana ustawień gry: " + ("; ".join(e.get("changes") or []) or f"{e.get('raw_changed')} kluczy"))
    for e in c.get("modules_changes") or []:
        out(f"- {an.fmt_ts(e['ts'])} moduły: " + "; ".join(e["changes"]))


def print_marks(ms):
    if not ms:
        return
    out("### Znaczniki użytkownika (kontekst +-30 s)")
    table([{"time": m["time"], "text": m["text"], "fps_min": m["context"]["fps_min"], "ft_max": m["context"]["ft_max"],
            "stutters": m["context"]["stutters"], "dwm_ms_max": m["context"]["dwm_ms_max"],
            "disk_lat_max": m["context"]["disk_lat_max"],
            "bg_procs": ", ".join(f"{p['name']} {p['cpu_max']}%" for p in m["context"]["top_bg_procs"]),
            "events": ", ".join(m["context"]["events"])} for m in ms], maxw=200)


def cmd_mark(con, a):
    """Mark 'something was wrong just now' - the collector stores it with the system state."""
    from perfmon import marks
    ts = marks.request(" ".join(a.text) if a.text else None)
    s = an.latest(con)
    out(f"Zapisano znacznik na {time.strftime('%H:%M:%S', time.localtime(ts))}"
        + ("" if s["collector_alive"] else " (UWAGA: kolektor nie działa, znacznik zostanie dodany po jego starcie)"))
    out(f"Podgląd: pm at {time.strftime('%H:%M:%S', time.localtime(ts))} --window 30")


def print_net(n, title="Sieć"):
    """Compact network summary (shared by pm net / session / range)."""
    out(f"### {title}")
    if not n or not n.get("seconds"):
        return out("_(brak danych sieciowych)_")
    rows = []
    for k, label in (("gw", "router"), ("isp", "operator"), ("inet", "internet"), ("game", "serwer gry")):
        t = (n.get("targets") or {}).get(k) or {}
        if t.get("probes"):
            rows.append({"cel": label, "avg": t["avg"], "p50": t["p50"], "p95": t["p95"], "p99": t["p99"], "max": t["max"],
                         "jitter": t["jitter"], "loss_%": t["loss_pct"], "próbki": t["probes"]})
    table(rows)
    d = n.get("dns") or {}
    extra = {"DNS avg/p95 ms": f"{d['avg']}/{d['p95']}" if d.get("avg") is not None else None,
             "błędy DNS": d.get("failures") or None, "brak łącza [s]": n.get("disconnected_s") or None,
             "łącze [s]": n.get("link_s"), "błędy karty": n.get("nic_errors")}
    kv(extra)
    w = n.get("wifi")
    if w:
        kv({"sygnał min/śr %": f"{w['signal_min']}/{w['signal_avg']}", "RSSI dBm": f"{w['rssi_min']}..{w['rssi_max']}",
            "SSID": w["ssids"], "BSSID": w["bssids"], "kanały": w["channels"],
            "rozłączenia (nieoczekiwane)": f"{w['disconnects']} ({w['unexpected_disconnects']})" if w["disconnects"] else None},
           "Wi-Fi")
        if w["disconnect_events"]:
            table([{"time": e["time"], "reason_code": e["reason_code"], "reason": e["reason"],
                    "user": e["user_initiated"]} for e in w["disconnect_events"][:15]])
    cor = n.get("correlation")
    if cor and cor.get("stutters"):
        out(f"- Stuttery blisko problemu sieci (+-{cor['window_s']:g} s): {cor['stutters_near_net_problem']}/{cor['stutters']} "
            f"({cor['stutters_near_net_problem_pct']}%; losowo spodziewane ~{cor['chance_pct']}%)")
    if n.get("incidents"):
        out("#### Incydenty sieci")
        table([{"time": i["time"], "kind": i["kind"], "where": (i["details"] or {}).get("where") if isinstance(i["details"], dict) else None,
                "dur_s": round(i["end_ts"] - i["ts"]) if i["end_ts"] else None, "title": i["title"]} for i in n["incidents"]])
    out("#### Werdykt")
    for v in n.get("verdict") or []:
        out(f"- {v}")


def cmd_net(con, a):
    t0 = an.parse_time(a.frm) if a.frm else an.parse_time(a.since)
    t1 = an.parse_time(a.to)
    n = an.net_summary(con, t0, t1)
    if a.json:
        return dump(n)
    out(f"## Sieć {n['from']} -> {n['to']} ({n['seconds']} próbek)")
    print_net(n, "Jakość połączenia")


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
    print_marks(r.get("marks"))
    out("### Top procesy CPU")
    table(r["top_procs"]["cpu"])
    out("### Top procesy I/O")
    table(r["top_procs"]["io"])
    out("### Sesje gier w zakresie")
    table(r["sessions"], ["id", "exe", "fps_avg", "fps_1low", "end_reason"])
    if r.get("net", {}).get("seconds"):
        print_net(r["net"])


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
    nt = r["net"]
    if nt["samples"]:
        out("### Sieć (ms; loss w %; link/Wi-Fi)")
        table([{"time": x["time"], "gw": x["gw_ms"], "isp": x["isp_ms"], "inet": x["inet_ms"], "game": x["game_ms"],
                "loss_inet": None if x["inet_loss"] is None else round(100 * x["inet_loss"]),
                "loss_gw": None if x["gw_loss"] is None else round(100 * x["gw_loss"]),
                "link": x["link"] or "BRAK", "wifi_%": x["wifi_signal"], "dns": x["dns_ms"]}
               for x in nt["samples"] if abs(x["t_rel"]) <= 15 or x["inet_loss"] or x["gw_loss"] or x["link"] is None])
        out("Werdykt: " + "; ".join(nt["summary"]["verdict"]))
    out("### Incydenty")
    table([{"time": i["time"], "kind": i["kind"], "title": i["title"]} for i in r["incidents"]])
    for i in r["incidents"]:
        if i["kind"] == "user_mark" and isinstance(i["details"], dict):
            out(f"- Znacznik {i['time']}: {i['details'].get('text') or '(bez opisu)'} - stan: "
                f"{cell(i['details'].get('last_sample'))}")
    out("### Zdarzenia")
    table(r["events"], ["time", "provider", "event_id", "level", "message"], maxw=250)
    out("### Procesy (cpu/io/mem)")
    table([p for p in r["procs"] if p["kind"] != "mem" or (p["mem_mb"] or 0) > 1500],
          ["time", "kind", "name", "cpu_pct", "mem_mb", "io_mbs"])


def cmd_procs(con, a):
    t0, t1 = an.parse_time(a.frm), an.parse_time(a.to)
    if a.events:
        from perfmon import procwatch
        rows = procwatch.events_list(con, t0, t1, 1000)
        if a.json:
            return dump(rows)
        out(f"## Starty/zakończenia procesów {an.fmt_ts(t0)} -> {an.fmt_ts(t1)}")
        return table(rows, ["time", "action", "name", "pid", "parent_name", "session"])
    if a.json:
        rows = an._rows(con, "SELECT * FROM procs WHERE kind=? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 2000", (a.kind, t0, t1))
        return dump(rows)
    out(f"## Procesy ({a.kind}) {an.fmt_ts(t0)} -> {an.fmt_ts(t1)}")
    col = {"cpu": "cpu_pct", "mem": "mem_mb", "io": "io_mbs", "gpu": "gpu_pct"}[a.kind]
    table(an._rows(con, f"SELECT name, ROUND(AVG({col}),2) AS avg, ROUND(MAX({col}),1) AS max, COUNT(*) AS appearances "
                        f"FROM procs WHERE kind=? AND ts BETWEEN ? AND ? AND name NOT IN ('System Idle Process','Idle') AND ({col}<=? OR kind!='gpu') GROUP BY name ORDER BY SUM({col}) DESC LIMIT 25",
                   (a.kind, t0, t1, config.GPU_PCT_MAX)))


def cmd_sysinfo(con, a):
    rows = an._rows(con, "SELECT ts, info FROM system_info ORDER BY ts DESC LIMIT 2")
    if not rows:
        return out("Brak migawek system_info (kolektor jeszcze jej nie zrobił).")
    cur = json.loads(rows[0]["info"])
    if a.diff:
        if len(rows) < 2:
            return out("Tylko jedna migawka - nie ma z czym porównać.")
        d = confdiff.diff(json.loads(rows[1]["info"]), cur)   # same rules as config_change incidents
        if a.json:
            return dump(d)
        out(f"## Zmiany konfiguracji {an.fmt_ts(rows[1]['ts'])} -> {an.fmt_ts(rows[0]['ts'])}")
        return table([{"change": c["text"]} for c in d], maxw=300) if d else out("Brak zmian.")
    if a.json:
        return dump({"ts": rows[0]["ts"], "info": cur})
    out(f"## Konfiguracja systemu (migawka {an.fmt_ts(rows[0]['ts'])})")
    for k, v in cur.items():
        if k != "disk_health":
            out(f"- **{k}**: {cell(v)[:600]}")
    from perfmon import diskinfo
    if diskinfo.health_rows(cur):
        out("### Zdrowie dysków (Get-StorageReliabilityCounter)")
        table(diskinfo.health_rows(cur))
        for w in diskinfo.health_warnings(cur):
            out(f"- UWAGA: {w}")


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
    rows = an.compare(con, a.exe, a.by, a.since, a.before, a.after)
    if a.json:
        return dump(rows)
    if a.before:
        out(f"## Porównanie przed {a.before} / po {a.after or a.before}")
    else:
        out(f"## Porównanie FPS wg: {a.by} (od {a.since})")
    for r in rows:
        r["period"] = f"{r['from_time'][:16]} - {r['to_time'][5:16]}"
        r["drivers"] = ", ".join(r["drivers"])
        r["versions"] = ", ".join(r["game_versions"])
        r["bottleneck"] = ", ".join(f"{k} {v}%" for k, v in r["bottleneck"].items())
    table(rows, ["exe", "group", "period", "sessions", "minutes", "fps_avg", "fps_avg_delta_pct", "fps_1low",
                 "fps_1low_delta_pct", "stutters_per_min", "stutters_per_min_delta_pct", "gpu_bound_pct",
                 "cpu_bound_pct", "limit_pct", "bottleneck", "drivers", "versions"])
    for r in rows:
        if r.get("changes"):
            out(f"- {r['exe']} {r['group']}: zmiany względem poprzedniej grupy: " + "; ".join(r["changes"]))
        if r.get("settings_mixed"):
            out(f"- {r['exe']} {r['group']}: grupa łączy sesje z różnymi ustawieniami gry")
    small = sorted({r["exe"] for r in rows if r.get("small_sample")})
    if small:
        out(f"UWAGA (small_sample): za mało danych dla {', '.join(small)} - grupa ma < {an.SMALL_SAMPLE_MIN} min "
            f"lub < {an.SMALL_SAMPLE_SESSIONS} sesje; różnice mogą być przypadkowe.")


def cmd_markers(con, a):
    m = an.timeline_markers(con, a.exe, a.since)
    if a.json:
        return dump(m)
    out("## Znaczniki zmian (sterowniki, aktualizacje gier, OS, BIOS, konfiguracja)")
    table(m, ["n", "time", "kind", "label", "session"])
    if m:
        out("_Przed/po: pm compare --exe EXE --before m<n> (np. m3)_")


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


def cmd_dumps(con, a):
    """WER LocalDumps (user-mode minidumps of crashing games): the elevated collector changes HKLM on request."""
    from perfmon import crashinfo
    if a.action == "status":
        st = crashinfo.local_dumps_status()
        if a.json:
            return dump(st)
        out("Minidumpy aplikacji (WER LocalDumps): " + ("WŁĄCZONE" if st["enabled"] else "wyłączone"))
        return kv({k: v for k, v in st.items() if k != "enabled"})
    flag = config.DATA_DIR / "dumps.request"
    flag.write_text(a.action, encoding="utf-8")
    want = a.action == "enable"
    t0 = time.time()
    while time.time() - t0 < 15:
        if not flag.exists() and crashinfo.local_dumps_status()["enabled"] == want:
            return out(f"Minidumpy {'włączone' if want else 'wyłączone'}"
                       + (f" - zapis do {crashinfo.CRASH_DUMPS} (max 5 na aplikację)" if want else ""))
        time.sleep(0.5)
    out("Kolektor nie wykonał zmiany w 15 s (czy działa jako admin? pm status, data/collector.log)")


def cmd_etw(con, a):
    """ETW black box: captures list/details, analysis, manual start/stop (via the elevated collector)."""
    from perfmon import etwbox
    s = etwbox.settings(config.load())
    if a.action in ("start", "stop"):
        etwbox.request(a.action)
        return out(f"Wysłano do kolektora: {a.action} (stan: data/collector.log, 'ETW black box ...')")
    if a.action in ("show", "analyze") and a.id is None:
        sys.exit(f"pm etw {a.action} ID")
    if a.action == "analyze":
        rw = db.connect()
        try:
            summ = etwbox.analyze(rw, a.id)
        finally:
            rw.close()
        out(f"Zrzut {a.id}: analiza {summ['analysis_s']} s (tracerpt {summ['convert_s']} s, CSV {summ['csv_mb']} MB)")
        a.action = "show"
    if a.action == "show":
        r = an._one(con, "SELECT * FROM etw_captures WHERE id=?", (a.id,))
        if not r:
            sys.exit(f"Brak zrzutu {a.id}")
        r["summary"] = an._json(r["summary"]) or {}
        if a.json:
            return dump(r)
        sm = r["summary"]
        out(f"## Zrzut ETW #{r['id']} - {an.fmt_ts(r['ts'])}, wyzwalacz {r['trigger']}, sesja {r['session'] or '-'}, "
            f"{r['status']}")
        kv({"plik": r["path"] or "(usunięty)", "MB": r["size_mb"], "okno s": sm.get("window_s"),
            "zdarzenia": sm.get("events"), "utracone": sm.get("events_lost"), "analiza s": sm.get("analysis_s"),
            "błąd": sm.get("error")})
        for key, title in (("dpc_top", "DPC wg sterownika"), ("isr_top", "ISR wg sterownika"),
                           ("disk_top", "Dysk wg procesu (MB)"), ("disk_slowest", "Najwolniejsze I/O wg procesu")):
            if sm.get(key):
                out(f"### {title}")
                table(sm[key])
        return
    rows = an._rows(con, "SELECT id, ts, trigger, session, size_mb, status, summary FROM etw_captures "
                         "ORDER BY ts DESC LIMIT ?", (a.limit,))
    for r in rows:
        r["time"] = an.fmt_ts(r["ts"])
        sm = an._json(r.pop("summary")) or {}
        r["result"] = etwbox.headline(sm)[1] if r["status"] == "analyzed" else sm.get("error") or ""
    if a.json:
        return dump(rows)
    t = etwbox.tools_available()
    out(f"## Czarna skrzynka ETW (tryb {s['mode']}, bufor {s['buffer_mb']} MB, analiza ostatnich {s['dump_last_s']} s; "
        f"wpr {'jest' if t['wpr'] else 'BRAK'}, tracerpt {'jest' if t['tracerpt'] else 'BRAK'})")
    table(rows, ["id", "time", "trigger", "session", "size_mb", "status", "result"], maxw=160)


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


def _oc_next(r):
    cur = r["current"]
    out("## Następny krok UV/OC" + (f" - aktywny profil: {cur['profile']} ({cur['settings']})" if cur else ""))
    if r["blocked"]:
        out("**Strojenie poczeka.**" + (f" Odblokuje się {r['until']}." if r.get("until") else ""))
        for b in r["blockers"]:
            out(f"- {b['what']}: {b['detail']}" + (f" (do {b['until']})" if b.get("until") else ""))
    for b in r.get("warnings") or []:
        out(f"Uwaga: {b['what']}: {b['detail']} - zmiany ustawień poczekają do {b['until']}.")
    s = r["step"]
    if s:
        out(f"### {s['title']}")
        kv({"gdzie": s["where"], "wartość": s["value"], "komenda": s["command"], "po awarii": s.get("reject_command"),
            "uwaga": s.get("note"), "co się nie zgadza": s.get("failed")})
        for x in s.get("skipped") or []:
            out(f"_{x}_")
    t = r["test"]
    if t:
        out(f"### Test: {t['what']}")
        for c in t["criteria"]:
            out(f"- [{'x' if c['ok'] else ' '}] {c['check']}" + (f" ({c['value']})" if c.get("value") is not None else ""))
    if r["next_if_ok"]:
        out(f"Jeśli OK: {r['next_if_ok']}")
    if r["rollback_if_bad"]:
        out(f"Jeśli źle: {r['rollback_if_bad']}")
    out("_PerfMonitor tylko doradza - niczego nie zmienia w BIOS ani Adrenalin i sam nie oznacza profilu jako stabilny._")


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
    elif act == "next":
        r = ocassist.next_step(con)
        if a.json:
            return dump(r)
        _oc_next(r)
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


def _print_test(r):
    out(f"## Test #{r['id']}: {r['name']} ({r.get('exe')}, {r.get('status')})")
    if r.get("metrics"):
        table([{"metryka": m["label"], "przed": m["before"], "po": m["after"], "zmiana %": m["change_pct"],
                "istotne": "tak" if m["significant"] else "nie"} for m in r["metrics"]],
              ["metryka", "przed", "po", "zmiana %", "istotne"])
    b, af = r.get("before") or {}, r.get("after") or {}
    if b:
        out(f"Przed: {b['sessions']} sesji, {b['minutes']} min gry. Po: {af['sessions']} sesji, {af['minutes']} min gry.")
    out(f"**Werdykt: {r['verdict']}**" + (f" ({'; '.join(r['missing'])})" if r.get("missing") else ""))
    for w in r.get("warnings") or []:
        out(f"- Uwaga: {w}")


def cmd_test(con, a):
    act = a.action
    if act == "start":
        tid = exps.start(con, a.name, a.exe, a.key)
        if a.json:
            return dump({"id": tid})
        out(f"Rozpoczęto test #{tid}: **{a.name}**. Zmień JEDNO ustawienie i graj dalej, potem `pm test show` / `pm test stop`.")
    elif act in ("stop", "show"):
        r = exps.stop(con, a.id) if act == "stop" else exps.show(con, a.id)
        if a.json:
            return dump(r)
        _print_test(r if r.get("name") else dict(r, name="?"))
    elif act == "cancel":
        tid = exps.cancel(con, a.id)
        out(json.dumps({"cancelled": tid}) if a.json else f"Anulowano test #{tid}.")
    else:
        rows = exps.list_all(con)
        if a.json:
            return dump(rows)
        out("## Testy zmian")
        table(rows, ["id", "time", "name", "exe", "status", "verdict"])


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
    add("mark", cmd_mark, help="mark a lag/problem right now").add_argument("text", nargs="*")
    add("advise", cmd_advise, help="ranked concrete changes backed by your own data")
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
    s = add("net", cmd_net, help="network quality summary (ping, loss, Wi-Fi, incidents)")
    s.add_argument("frm", metavar="FROM", nargs="?", help="range start (default: --since)")
    s.add_argument("to", metavar="TO", nargs="?", default="now")
    s.add_argument("--since", default="24h")
    s = add("at", cmd_at)
    s.add_argument("time")
    s.add_argument("--window", type=float, default=30)
    s = add("procs", cmd_procs)
    s.add_argument("frm", metavar="FROM")
    s.add_argument("to", metavar="TO", nargs="?", default="now")
    s.add_argument("--kind", default="cpu", choices=["cpu", "mem", "io", "gpu"])
    s.add_argument("--events", action="store_true", help="process starts/stops instead of top processes")
    add("sysinfo", cmd_sysinfo).add_argument("--diff", action="store_true")
    add("schema", cmd_schema)
    add("sql", cmd_sql).add_argument("query")
    s = add("compare", cmd_compare)
    s.add_argument("--exe")
    s.add_argument("--by", default="week", choices=["week", "day", "month", "driver", "game_version", "session", "present_mode"])
    s.add_argument("--since", default="90d")
    s.add_argument("--before", help="split point: time or change marker m<n> from pm markers --exe EXE")
    s.add_argument("--after", help="start of the 'after' group (default: --before); sessions in between are skipped")
    s = add("markers", cmd_markers)
    s.add_argument("--exe")
    s.add_argument("--since", default="180d")
    add("config", cmd_config)
    add("collector", cmd_collector, help="restart the running collector").add_argument("action", choices=["reload"])
    add("sensors", cmd_sensors, help="dump every hardware sensor").add_argument("action", choices=["dump"])
    add("freeze-check", cmd_freeze_check, help="live diagnosis while the desktop is frozen")
    s = add("etw", cmd_etw, help="ETW black box: kernel trace dumps on freezes / marks / DPC storms")
    s.add_argument("action", nargs="?", default="list", choices=["list", "show", "analyze", "start", "stop"])
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--limit", type=int, default=30)
    add("dumps", cmd_dumps, help="WER minidumps of crashing apps (HKLM, via the collector)").add_argument(
        "action", choices=["status", "enable", "disable"])

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
    oc_action("next")
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

    s = add("test", cmd_test, help="one-setting test with before/after verdict: start|stop|cancel|list|show")
    tp = s.add_subparsers(dest="action", required=True)

    def t_action(name, writes=False):
        x = tp.add_parser(name)
        x.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        x.set_defaults(writes=writes)
        return x
    x = t_action("start", True)
    x.add_argument("name")
    x.add_argument("--exe")
    x.add_argument("--key")
    for n in ("stop", "cancel"):
        t_action(n, True).add_argument("id", nargs="?", type=int)
    t_action("list")
    t_action("show").add_argument("id", nargs="?", type=int)
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
