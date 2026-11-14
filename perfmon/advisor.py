"""`pm advise`: a short ranked list of concrete changes, each backed by the user's own numbers.

An item exists only while its condition holds in the data, so it disappears once the fix is done. Settings we cannot
read go to `unknown` (with how to check), claims the data refutes go to `not_worth`. Expected effects are qualitative:
we never promise a percentage gain, only a before/after test can prove one."""
import re
import time
from collections import Counter, defaultdict

from . import analyze as an
from . import experiments, present, regression

MAIN_GAME_DAYS = 14
RECENT_SESSIONS = 6
CAUSE_MIN_SESSIONS = 2        # a cause must be confirmed in this many recent sessions
BG_GPU_MIN_PCT = 10           # average GPU % of a background process across recent sessions
COMPOSED_MIN_PCT = 50         # composed presentation share of frames
GPU_BOUND_MIN_PCT = 60        # GPU-bound share of gameplay for the upscaler advice
BG_CPU_NOT_WORTH_PCT = 5      # all background apps together below this: autostart cleanup is pointless
GPU_BOUND_NO_CPU_PCT = 90     # above this faster CPU/RAM cannot help
BAR_OFF_MB = 256              # biggest GPU memory BAR at or below this = Resizable BAR inactive
COMMIT_EPISODES_MIN = 2       # commit_full episodes in MAIN_GAME_DAYS before the pagefile volume's free space matters
UNCLEAN_MIN = 2               # hard resets in MAIN_GAME_DAYS that make finding their cause the first item
WIFI_WARN_MIN = 20            # Wi-Fi driver warnings in MAIN_GAME_DAYS
WIFI_LINK_PCT = 50            # share of network seconds on Wi-Fi for the driver to matter
WIFI_PROBLEMS_MIN = 3         # network incidents located at home (Wi-Fi / router) that the warnings could explain
OVERLAY_MIN_SESSIONS = 2      # sessions in which an overlay / capture hook / injector was loaded into the game
FRAME_GEN_ON_PCT = 5          # % generated frames: the session was played with frame generation
FRAME_GEN_BASE_FPS = 60       # application FPS below which AMD advises against frame generation (latency, artifacts)
BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"
ASPM_PL = {"off": "wyłączone", "moderate": "umiarkowane oszczędzanie", "maximum": "maksymalne oszczędzanie"}
GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# Wi-Fi driver event providers: Intel (Netwtw14, Netwbw02...), Realtek, MediaTek, Qualcomm
WIFI_PROVIDERS = ("Netwtw%", "Netwbw%", "Netwsw%", "rtwlan%", "RtlWlan%", "mtkwl%", "Qcamain%", "athw%")
NET_HOME_KINDS = ("net_down", "net_link_down", "packet_loss", "latency_spike", "wifi_weak")

TIER_STABILITY, TIER_NORMAL, TIER_LAST = 0, 1, 2
BG_GPU_IGNORE = {"dwm.exe", "system", "idle", "csrss.exe", "explorer.exe", "searchhost.exe"}
# not autostart apps: our own tools, Windows components, the GPU driver service (judged by the background_gpu rule)
OWN_PROCS = {"python.exe", "pythonw.exe", "powershell.exe", "presentmon.exe", "dwm.exe", "svchost.exe", "csrss.exe",
             "audiodg.exe", "lsass.exe", "services.exe", "winlogon.exe", "wmiprvse.exe", "msmpeng.exe", "sihost.exe",
             "taskhostw.exe", "ctfmon.exe", "searchhost.exe", "runtimebroker.exe", "fontdrvhost.exe", "gamingservices.exe",
             "amdrsserv.exe", "explorer.exe", "conhost.exe", "wudfhost.exe", "dllhost.exe", "smss.exe"}
GIGABYTE_RE = re.compile(r"easytune|aorus|^gcc|gigabyte", re.I)


def _game_key(exe):
    return regression.game_key(exe)   # sp26-cod.exe and cod.exe are one game


# ---------------------------------------------------------------- data gathering

def _main_game(con):
    """(key, [session rows newest first]) of the game with the most gameplay seconds in the last 14 days."""
    t0 = time.time() - MAIN_GAME_DAYS * 86400
    secs, rows = defaultdict(float), []
    for r in an._rows(con, f"SELECT id, exe, start_ts, end_ts, summary FROM sessions WHERE start_ts>=? AND {an.GAMES_ONLY} "
                           f"ORDER BY start_ts DESC", (t0,)):
        gp = (an._json(r["summary"]) or {}).get("gameplay") or {}
        secs[_game_key(r["exe"])] += gp.get("seconds") or 0
        r["gp"] = gp
        rows.append(r)
    if not secs or max(secs.values()) <= 0:
        return None, []
    key = max(secs, key=secs.get)
    mine = [r for r in rows if _game_key(r["exe"]) == key and r["gp"].get("usable")]
    return key, mine[:RECENT_SESSIONS]


def _bg_cpu(con, sessions, game_exes):
    """Average summed CPU % of background processes while the game ran: (avg total, top [(name, avg)])."""
    totals, per = [], defaultdict(float)
    skip = {n.lower() for n in game_exes} | an.NOT_BACKGROUND | OWN_PROCS
    for s in sessions:
        end = s["start_ts"] + (s["gp"].get("seconds") or 0) / (s["gp"].get("share_pct") or 100) * 100
        rows = con.execute("SELECT ts, name, cpu_pct FROM procs WHERE kind='cpu' AND ts BETWEEN ? AND ?",
                           (s["start_ts"], end)).fetchall()
        by_ts = defaultdict(float)
        for ts, name, cpu in rows:
            if (name or "").lower() in skip or not cpu:
                continue
            by_ts[ts] += cpu
            per[name] += cpu
        totals += list(by_ts.values())
    n = max(1, len(totals))
    top = sorted(((k, v / n) for k, v in per.items()), key=lambda kv: -kv[1])[:3]
    return (sum(totals) / n if totals else None), top


def _wifi(con, t0):
    """Wi-Fi driver warnings since t0 and what the network data says about the link (net_s 0 = no network data)."""
    like = " OR ".join("provider LIKE ?" for _ in WIFI_PROVIDERS)
    ev = con.execute(f"SELECT provider, event_id, COUNT(*), MAX(message) FROM events WHERE ts>=? AND level<=3 AND ({like}) "
                     "GROUP BY provider, event_id ORDER BY COUNT(*) DESC LIMIT 1", (t0, *WIFI_PROVIDERS)).fetchone()
    if not ev:
        return None
    links = Counter()
    for link, n in con.execute("SELECT link, COUNT(*) FROM net_samples WHERE ts>=? GROUP BY link", (t0,)):
        links[link] += n
    home = Counter(i["kind"] for i in an.incidents_between(con, t0, time.time(), kinds=NET_HOME_KINDS, limit=100000,
                                                           group=True)
                   if i["kind"] == "net_link_down" or (isinstance(i["details"], dict) and i["details"].get("where") == "home"))
    net_s = sum(n for link, n in links.items() if link)   # disconnected seconds (link NULL) are neither Wi-Fi nor cable
    return {"provider": ev[0], "event_id": ev[1], "n": ev[2], "adapter": (ev[3] or "").split("|")[-1].strip() or None,
            "net_s": net_s, "wifi_pct": round(100 * links["wifi"] / net_s) if net_s else None, "home": dict(home)}


def gather(con):
    row = con.execute("SELECT info FROM system_info ORDER BY ts DESC LIMIT 1").fetchone()
    info = an._json(row[0]) if row else None
    key, sessions = _main_game(con)
    reports = []
    for s in reversed(sessions):
        rep = an.session_report(con, s["id"])   # finished sessions come from the report cache
        if "error" not in rep:
            reports.append(rep)
    exes = {s["exe"] for s in sessions}
    cpu, top = _bg_cpu(con, sessions, exes) if sessions else (None, [])
    played = Counter()
    for s in sessions:
        played[s["exe"]] += s["gp"].get("seconds") or 0
    return {"now": time.time(), "info": info or {}, "has_info": bool(info), "game": sessions[0]["exe"] if sessions else None,
            "test_exe": played.most_common(1)[0][0] if played else None,
            "reports": reports, "health": an.health_overview(con, MAIN_GAME_DAYS), "bg_cpu": cpu, "bg_cpu_top": top,
            "wifi": _wifi(con, time.time() - MAIN_GAME_DAYS * 86400), "tests": experiments.by_key(con)}


# ---------------------------------------------------------------- helpers over the sessions

def _minutes(rep):
    gp = (rep.get("summary") or {}).get("gameplay") or {}
    return (gp.get("seconds") or 0) / 60


def _bottleneck(reports):
    """Minutes-weighted over the sessions: dominant kind, its share of minutes, total minutes, GPU-bound %."""
    mins, gpu_w, tot = defaultdict(float), 0.0, 0.0
    norm = {"gpu_power": "gpu", "cpu_thread": "cpu"}
    for r in reports:
        bn = (r.get("summary") or {}).get("bottleneck") or {}
        m = _minutes(r)
        if not bn.get("kind") or m <= 0:
            continue
        mins[norm.get(bn["kind"], bn["kind"])] += m
        gpu_w += (bn.get("gpu_bound_pct") or 0) * m
        tot += m
    if not tot:
        return {"kind": None, "share_pct": None, "minutes": 0, "gpu_bound_pct": None}
    kind = max(mins, key=mins.get)
    # for the GPU the real share of gameplay, not of sessions that happened to be mostly GPU-bound
    share = gpu_w / tot if kind == "gpu" else 100 * mins[kind] / tot
    return {"kind": kind, "share_pct": round(share), "minutes": round(tot, 1), "gpu_bound_pct": round(gpu_w / tot, 1)}


def _confirmed(reports):
    """tag -> [(report, ranking row)] for causes confirmed against normal gameplay."""
    out = defaultdict(list)
    for r in reports:
        for c in (r.get("stutters") or {}).get("cause_ranking") or []:
            if c["verdict"] == "confirmed":
                out[c["tag"]].append((r, c))
    return out


def _bg_gpu(reports):
    """exe -> (mean avg GPU % over all sessions, sessions seen)."""
    acc, seen = defaultdict(float), defaultdict(int)
    for r in reports:
        for b in r.get("gpu_background") or []:
            if b["name"].lower() in BG_GPU_IGNORE:
                continue
            acc[b["name"]] += b.get("avg_pct") or 0
            seen[b["name"]] += 1
    n = max(1, len(reports))
    return {k: (v / n, seen[k]) for k, v in acc.items()}


def _composed_pct(reports):
    vals = []
    for r in reports:
        sh = present.class_shares(((r.get("summary") or {}).get("present") or {}).get("modes"))
        if sh:
            vals.append(sh.get("composed", 0.0))
    return round(sum(vals) / len(vals), 1) if vals else None


def _pagefile(info):
    """{letter, free_gb, ram_gb} when the pagefile is auto-managed on a volume with less free space than RAM."""
    if info.get("pagefile_auto") is not True or not info.get("pagefile"):
        return None
    m = re.match(r"\s*([A-Za-z]):", str(info["pagefile"][0]))
    vol = next((v for v in info.get("volumes") or [] if m and v.get("letter", "").upper() == m.group(1).upper()), None)
    ram = info.get("ram_total_gb")
    if not vol or vol.get("free_gb") is None or not ram or vol["free_gb"] >= ram:
        return None
    return {"letter": vol["letter"], "free_gb": vol["free_gb"], "ram_gb": ram}


def _plural(n, one, few, many):
    if n == 1:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _plan_guid(info):
    g = info.get("power_plan_guid") or (GUID_RE.search(info.get("power_plan") or "") or [None])[0]
    return g.lower() if g else None


def _overlays(reports):
    """name -> (category, sessions) of hooks seen in the game process (game_modules scans, status ok only)."""
    seen = {}
    for r in reports:
        m = (r.get("config") or {}).get("modules") or {}
        if m.get("status") != "ok":
            continue
        for f in m.get("flagged") or []:
            if f.get("category") in ("overlay", "capture", "injector"):
                cat, n = seen.get(f["name"], (f["category"], 0))
                seen[f["name"]] = (cat, n + 1)
    return seen


def _frame_gen(reports):
    """(application FPS, minutes) over recent sessions played with frame generation, or None."""
    fps_w = mins = 0.0
    for r in reports:
        ft = r.get("fps_timeline") or {}
        if (ft.get("frame_gen_pct") or 0) < FRAME_GEN_ON_PCT or not ft.get("avg"):
            continue
        m = _minutes(r) or (r.get("duration_s") or 0) / 60
        fps_w, mins = fps_w + ft["avg"] * m, mins + m
    return (fps_w / mins, mins) if mins else None


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _item(key, title, why, expected, risk, undo, how, kind, tier=TIER_NORMAL, score=0.0):
    return {"key": key, "title": title, "why": why, "expected": expected, "risk": risk, "undo": undo, "how": how,
            "kind": kind, "_tier": tier, "_score": score}


# ---------------------------------------------------------------- cause rules

def _cause_item(tag, hits, n_sessions):
    reps = [h[0] for h in hits]
    rows = [h[1] for h in hits]
    share = _avg([r["stutter_pct"] for r in rows])
    lifts = [r["lift"] for r in rows if r.get("lift")]
    lift = f", {max(lifts):.1f}x częściej niż w zwykłej grze" if lifts else ""
    base = f"Potwierdzone jako przyczyna stutterów w {len(hits)} z {n_sessions} ostatnich sesji (średnio {share}% stutterów{lift})"
    score = 40 + (share or 0) / 2 + 5 * len(hits)
    name = tag.split(":", 1)[0]
    if name == "vram_shared":
        sh = max((r.get("summary") or {}).get("vram_shared_max_mb") or 0 for r in reps)
        return _item(tag, "Obniż jakość tekstur", f"{base}; VRAM wylewał się do pamięci współdzielonej (do {sh} MB).",
                     "mniej stutterów, jeśli to VRAM je powoduje", "niższa jakość tekstur", "ustaw tekstury z powrotem",
                     "Gra > Grafika > Jakość tekstur / Streaming tekstur: o stopień niżej; zamknij przeglądarkę i inne aplikacje "
                     "używające GPU przed grą.", "smoothness", score=score)
    if name == "ram":
        hw = [r.get("hardware") or {} for r in reps]
        pk, lim = max(h.get("commit_peak_gb") or 0 for h in hw), max(h.get("commit_limit_gb") or 0 for h in hw)
        return _item(tag, "Zwiększ plik stronicowania i zamknij aplikacje w tle",
                     f"{base}; commit sięgał {pk} GB przy limicie {lim} GB.",
                     "mniej stutterów przy braku pamięci, jeśli to ona je powoduje", "zajmuje miejsce na dysku systemowym",
                     "wróć do automatycznego rozmiaru pliku stronicowania",
                     "Ustawienia > System > Informacje > Zaawansowane ustawienia systemu > Wydajność > Ustawienia > Zaawansowane > "
                     "Pamięć wirtualna > Zmień: odznacz automatyczne, rozmiar niestandardowy 24576 MB (początkowy i maksymalny) "
                     "na dysku z wolnym miejscem; przed grą zamknij przeglądarkę i launchery.", "stability", score=score)
    if name == "disk":
        lat = max((r.get("hardware") or {}).get("disk_lat_max_ms") or 0 for r in reps)
        d = next((r["disk"] for r in reps if (r.get("disk") or {}).get("game_disk_bus")), {})
        if d.get("game_disk_bus") == "NVMe":   # already on the fast disk: moving the game cannot be the fix
            return _item(tag, "Sprawdź skoki opóźnienia dysku", f"{base}; opóźnienie do {lat} ms, a gra jest już na "
                         f"{d.get('game_disk_name') or 'NVMe'}.", "mniej stutterów, jeśli znajdziesz, co obciąża dysk",
                         "brak", "cofnij wykluczenie antywirusa",
                         "Pod stutterem zobacz `pm etw show ID` (proces z najwolniejszym I/O); wyklucz folder gry ze "
                         "skanowania Defendera; sprawdź firmware w Samsung Magician.", "smoothness", score=score)
        return _item(tag, "Przenieś grę na szybszy dysk (NVMe)", f"{base}; opóźnienie dysku do {lat} ms.",
                     "mniej stutterów od doczytywania, jeśli to dysk je powoduje", "brak (kopiowanie plików gry)",
                     "przenieś grę z powrotem",
                     "Sprawdź, gdzie jest gra (Steam/Battle.net > Właściwości > Zainstalowane pliki) i przenieś ją na Samsung 990 PRO; "
                     "wyklucz folder gry ze skanowania antywirusa.", "smoothness", score=score)
    if name == "background_start":
        exe = tag.split(":", 1)[1]
        if exe.lower() == "mousocoreworker.exe":
            return _item(tag, "Dokończ aktualizacje Windows przed grą",
                         f"{base}; start MoUsoCoreWorker.exe (Windows Update) zbiega się ze stutterami.",
                         "mniej stutterów, jeśli to aktualizacje je powodują", "brak",
                         "przywróć godziny aktywności",
                         "Ustawienia > Windows Update > Sprawdź dostępność aktualizacji i zainstaluj przed grą; "
                         "Zaawansowane > Godziny aktywności: ustaw godziny grania.", "smoothness", score=score)
        return _item(tag, f"Nie uruchamiaj {exe} w trakcie gry", f"{base}; start {exe} zbiega się ze stutterami.",
                     "mniej stutterów, jeśli to ten proces je powoduje", "brak", "włącz proces z powrotem",
                     f"Zakończ {exe} przed grą (Menedżer zadań) albo wyłącz jego autostart / zadanie w tle.", "smoothness",
                     score=score - 5)
    texts = {
        "dpc": ("Znajdź sterownik z wysokim DPC", "mniej stutterów, jeśli to sterownik je powoduje", "brak",
                "Uruchom LatencyMon podczas gry, zaktualizuj lub usuń sterownik z najwyższym czasem DPC (często LAN/Wi-Fi, audio, USB)."),
        "thermal": ("Obniż temperatury", "mniej stutterów od throttlingu, jeśli to on je powoduje", "brak",
                    "Oczyść radiator i filtry, popraw przepływ powietrza, ustaw bardziej agresywną krzywą wentylatorów (BIOS lub aplikacja)."),
        "vram": ("Obniż jakość tekstur", "mniej stutterów, jeśli to VRAM je powoduje", "niższa jakość tekstur",
                 "Gra > Grafika > Jakość tekstur: o stopień niżej; zamknij inne aplikacje używające GPU."),
        "pagefile": ("Zamknij aplikacje przed grą", "mniej stutterów od pliku stronicowania, jeśli to on je powoduje", "brak",
                     "Zamknij przeglądarkę i launchery; plik stronicowania trzymaj na dysku NVMe."),
        "disk_reset": ("Sprawdź kontroler dysku NVMe", "koniec zacięć przy resetach dysku", "brak",
                       "Zaktualizuj firmware (Samsung Magician), sprawdź osadzenie M.2 i wyłącz Link State Power Management."),
        "disk_hot": ("Schłodź dysk z grą (NVMe)", "mniej stutterów od throttlingu dysku, jeśli to on je powoduje", "brak",
                     "Załóż radiator M.2 (z płyty lub osobny) i zapewnij przepływ powietrza przy slocie pod kartą graficzną; "
                     "temperaturę dysku widać w raporcie sesji."),
        "background_scan": ("Przesuń skany Defendera poza granie", "mniej stutterów, jeśli to skany je powodują", "brak",
                            "Dodaj folder gry do wykluczeń: Zabezpieczenia Windows > Ochrona przed wirusami > Wykluczenia."),
        "shader_compile": ("Skompiluj shadery przed grą", "mniej stutterów w kolejnych sesjach", "dłuższy start gry",
                           "Uruchom wstępną kompilację shaderów w menu gry; nie czyść cache shaderów w Adrenalinie."),
        "cpu_thread": ("Odciąż główny wątek CPU", "mniej stutterów, jeśli główny wątek je powoduje", "niższa jakość",
                       "Gra > Grafika: obniż ustawienia zależne od CPU (zasięg widzenia, liczba postaci, cienie, śledzenie promieni); "
                       "zamknij procesy w tle."),
    }
    if name == "background":
        exe = tag.split(":", 1)[1]
        return _item(tag, f"Zamknij {exe} na czas gry", f"{base}; {exe} zabiera CPU w chwili stutterów.",
                     "mniej stutterów, jeśli to ten proces je powoduje", "brak", "uruchom proces ponownie",
                     f"Zakończ {exe} przed grą (Menedżer zadań) albo wyłącz jego autostart.", "smoothness", score=score)
    if name in texts:
        title, exp, risk, how = texts[name]
        return _item(tag, title, base + ".", exp, risk, "przywróć poprzednie ustawienia", how, "smoothness", score=score)
    return None


# ---------------------------------------------------------------- the rules

def build(d):
    """Pure function over gathered data (see gather); returns the advise() payload."""
    info, reports, health = d["info"], d["reports"], d["health"]
    n = len(reports)
    bn = _bottleneck(reports)
    items, not_worth, unknown = [], [], []

    # [unclean_shutdowns] repeated hard resets: finding their cause comes before any tuning
    unclean = health.get("unclean_shutdowns") or []
    if len(unclean) >= UNCLEAN_MIN:
        no_bsod = sum(1 for u in unclean if u.get("bugcheck_code") == 0)
        what = _plural(len(unclean), "nieoczekiwane wyłączenie", "nieoczekiwane wyłączenia", "nieoczekiwanych wyłączeń")
        items.append(_item(
            "unclean_shutdowns", "Znajdź przyczynę twardych resetów",
            f"{len(unclean)} {what} w ostatnich {health.get('days', 14):g} dniach"
            + (f", {no_bsod} bez blue screena (BugcheckCode=0)" if no_bsod else "")
            + f"; ostatnie {unclean[-1].get('time') or '?'}. Dopóki komputer się resetuje, pomiary wydajności są mniej warte.",
            "stabilny komputer i wiarygodne pomiary, jeśli znajdziesz przyczynę", "brak (to diagnoza, nie zmiana ustawień)",
            "nic do cofania",
            "Uruchom `pm health`: przy każdym resecie jest kod błędu, stan tuż przed (temperatury, obciążenie) i wnioski. "
            "Najpierw cofnij ostatnie zmiany UV/OC/EXPO (`pm oc stability`), potem sterowniki i BIOS.",
            "stability", TIER_STABILITY, 150 + len(unclean)))

    # [fast_startup] hard resets that happened during shutdown/sleep while Fast Startup is on
    during = [u for u in unclean if u.get("during_shutdown")]
    if info.get("fast_startup") == 1 and during:
        items.append(_item(
            "fast_startup", "Wyłącz szybki rozruch (Fast Startup)",
            f"{len(during)} z {len(unclean)} twardych resetów w ostatnich {health.get('days', 14):g} dniach wydarzyło się podczas "
            f"zamykania/uśpienia (SleepInProgress != 0), a Fast Startup jest włączony (HiberbootEnabled=1).",
            "mniej resetów przy zamykaniu, jeśli to hibernacja jądra je powoduje", "dłuższy start systemu o kilka sekund",
            "włącz z powrotem w tym samym miejscu",
            "Panel sterowania > Opcje zasilania > Wybierz działanie przycisków zasilania > Zmień ustawienia, które są obecnie "
            "niedostępne > odznacz „Włącz szybkie uruchamianie”.", "stability", TIER_STABILITY, 100 + len(during)))

    # [gigabyte_bloat] board software present or crashing
    tools = [t for t in info.get("background_tools") or [] if GIGABYTE_RE.search(t)]
    crashes = [c for c in health.get("recurring_crashes") or [] if GIGABYTE_RE.search(c.get("app", ""))]
    if tools or crashes:
        bits = []
        if tools:
            bits.append("działa: " + ", ".join(tools))
        for c in crashes:
            bits.append(f"{c['app']} crashował {c['n']}x")
        items.append(_item(
            "gigabyte_bloat", "Odinstaluj Gigabyte Control Center / EasyTune / Aorus LCD",
            "Oprogramowanie płyty Gigabyte ciągle działa w tle (" + "; ".join(bits) + "); to częste źródło crashy i opóźnień.",
            "mniej crashy i procesów w tle", "tracisz sterowanie RGB/LCD z poziomu systemu; wentylatory z BIOS-u działają dalej",
            "zainstaluj Gigabyte Control Center ze strony gigabyte.com",
            "Ustawienia > Aplikacje > Zainstalowane aplikacje: odinstaluj Gigabyte Control Center, EasyTune, AORUS LCD; "
            "sterowniki chipsetu zostają (AMD).", "stability", TIER_STABILITY, 90 + 5 * sum(c["n"] for c in crashes)))

    # [igpu_monitor] a monitor driven by the iGPU
    gpus = info.get("gpus") or []
    igpu_names = {g["name"] for g in gpus if g.get("igpu")}
    disp = info.get("displays") or []
    on_igpu = [m for m in disp if m.get("adapter") in igpu_names]
    if on_igpu:
        m = on_igpu[0]
        items.append(_item(
            "igpu_monitor", "Przepnij monitor z iGPU na kartę graficzną",
            f"Monitor {m.get('monitor') or '?'} ({m.get('mode')}) jest podpięty do iGPU ({m['adapter']}); przez to DWM składa obraz "
            f"na dwóch GPU.", "płynniejszy pulpit i mniej opóźnień, jeśli to iGPU je powoduje", "brak",
            "przepnij kabel z powrotem",
            "Wyłącz PC, przepnij kabel DisplayPort/HDMI monitora do wyjść karty RX 9070 XT (nie do płyty głównej); "
            "opcjonalnie wyłącz iGPU w BIOS: Settings > Advanced > Integrated Graphics > Disabled.", "smoothness",
            score=60))
    elif disp and igpu_names:
        not_worth.append({"key": "igpu_monitor", "title": "Przepinanie monitorów z iGPU na kartę",
                          "why": "Oba monitory są już na " + ", ".join(sorted({m["adapter"] for m in disp})) +
                                 " (EnumDisplayDevices); odczyt Win32_VideoController dla iGPU (rozdzielczość/Hz) jest nieaktualny."})

    # [background_gpu:<exe>] process eating the GPU while gaming
    for exe, (avg, seen) in sorted(_bg_gpu(reports).items(), key=lambda kv: -kv[1][0]):
        if avg < BG_GPU_MIN_PCT or seen < min(2, n):
            continue
        ranked = [c for r in reports for c in (r.get("stutters") or {}).get("cause_ranking") or []
                  if c["tag"] == f"background_gpu:{exe}"]
        verdicts = {c["verdict"] for c in ranked}
        base = f"{exe} zajmuje średnio {avg:.0f}% GPU w {seen} z {n} ostatnich sesji."
        if exe.lower() == "amdrsserv.exe":
            ir = info.get("instant_replay")
            state = (" Instant Replay jest włączony (InstantReplayEnabled=1)." if ir == 1 else
                     " Instant Replay jest wyłączony w rejestrze, więc to inna funkcja Adrenalin (np. nagrywanie pulpitu, overlay)."
                     if ir == 0 else "")
            honest = (" Ranking stutterów oznacza go jako stałe tło (występuje także w zwykłych sekundach), więc nie dowodzi, że "
                      "powoduje stuttery; tylko test przed/po da odpowiedź." if "ambient" in verdicts else "")
            items.append(_item(
                f"background_gpu:{exe}", "Wyłącz Instant Replay i nagrywanie w Adrenalin", base + state + honest,
                "mniejsze obciążenie GPU w tle; wpływ na stuttery do sprawdzenia testem przed/po", "brak klipów z Instant Replay",
                "włącz Instant Replay z powrotem",
                "AMD Software: Adrenalin > Nagrywanie i przesyłanie strumieniowe (ikona kamery) > wyłącz Instant Replay, "
                "nagrywanie pulpitu i overlay metryk; zagraj tę samą misję i porównaj `pm compare`.", "smoothness", score=45 + avg / 4))
        else:
            items.append(_item(
                f"background_gpu:{exe}", f"Zamknij {exe} na czas gry", base, "mniejsze obciążenie GPU w tle",
                "brak", "uruchom aplikację ponownie",
                f"Zakończ {exe} przed grą (Menedżer zadań) lub wyłącz jego autostart i akcelerację sprzętową.", "smoothness",
                score=40 + avg / 4))

    # [composed] game presented through DWM
    cp = _composed_pct(reports)
    if cp is not None and cp > COMPOSED_MIN_PCT:
        clicks = _avg([((r.get("summary") or {}).get("present") or {}).get("click_lat_avg") for r in reports])
        wall = " Wallpaper Engine działa w tle." if any("wallpaper" in t.lower() for t in info.get("background_tools") or []) else ""
        items.append(_item(
            "composed", "Graj w wyłącznym pełnym ekranie bez nakładek",
            f"{cp:.0f}% klatek przechodzi przez kompozycję DWM (Composed) w ostatnich sesjach"
            + (f", opóźnienie klik-do-ekranu średnio {clicks} ms" if clicks else "") + "." + wall,
            "mniejsze opóźnienie wejścia, jeśli kompozycja je dodaje", "Alt+Tab wolniejszy, inne okno może minimalizować grę",
            "wróć do trybu okno bez ramki",
            "Gra > Wyświetlanie > Tryb: Pełny ekran (nie „okno bez ramki”); wyłącz nakładki (Discord, Steam, GeForce/Adrenalin "
            "overlay, Xbox Game Bar); wstrzymaj Wallpaper Engine podczas gry (Ustawienia > Wydajność > Gra w pełnym ekranie: Wstrzymaj).",
            "latency", score=35 + cp / 5))

    # [cause:<tag>] causes confirmed in several recent sessions
    for tag, hits in _confirmed(reports).items():
        if len(hits) >= CAUSE_MIN_SESSIONS:
            it = _cause_item(tag, hits, n)
            if it:
                if tag.startswith("ram"):
                    it["_tier"] = TIER_STABILITY
                items.append(it)

    # [pagefile_space] auto-managed pagefile on a nearly full volume while commit hits the limit
    commits = (health.get("incidents_by_kind") or {}).get("commit_full") or 0
    pf = _pagefile(info)
    if pf and commits >= COMMIT_EPISODES_MIN and not any(i["key"].startswith("ram") for i in items):
        # another volume only when every disk is an SSD: a pagefile on a hard drive would make it worse
        ssd_only = bool(info.get("disks")) and all(x.get("media") == "SSD" for x in info["disks"])
        other = max((v for v in info.get("volumes") or [] if v.get("letter") != pf["letter"]),
                    key=lambda v: v.get("free_gb") or 0, default=None) if ssd_only else None
        items.append(_item(
            "pagefile_space", f"Zwolnij miejsce na {pf['letter']}: albo ustaw stały plik stronicowania",
            f"Pamięć zadeklarowana (commit) dochodziła do limitu w {commits} epizodach w ostatnich "
            f"{health.get('days', 14):g} dniach, a plik stronicowania jest zarządzany automatycznie na {pf['letter']}:, "
            f"gdzie zostało {pf['free_gb']:g} GB wolnego (mniej niż {pf['ram_gb']:g} GB RAM), więc nie ma gdzie urosnąć.",
            "mniej zacięć i crashy przy braku pamięci, jeśli to limit commit je powoduje", "zajmuje miejsce na dysku",
            "wróć do automatycznego rozmiaru pliku stronicowania",
            f"Zwolnij co najmniej {max(8, round(pf['ram_gb'] - pf['free_gb'])):g} GB na {pf['letter']}: (Ustawienia > System > "
            "Magazyn > Pliki tymczasowe) albo ustaw plik stronicowania ręcznie: Zaawansowane ustawienia systemu > Wydajność > "
            "Zaawansowane > Pamięć wirtualna > Zmień: odznacz automatyczne, rozmiar niestandardowy 16384-32768 MB"
            + (f" na {other['letter']}: ({other['free_gb']:g} GB wolne)" if other else "") + ".",
            "stability", TIER_STABILITY, 80 + commits))

    # [upscaler] GPU-bound: the fastest FPS gain
    gb = bn["gpu_bound_pct"]
    if gb is not None and gb >= GPU_BOUND_MIN_PCT:
        items.append(_item(
            "upscaler", "Włącz FSR (jakość) lub obniż ustawienia graficzne",
            f"Gra jest ograniczona przez GPU przez {gb:.0f}% czasu rozgrywki ({bn['minutes']:.0f} min w {n} sesjach); "
            f"CPU nie jest wąskim gardłem.", "więcej FPS, bo to GPU jest limitem", "nieco mniej ostry obraz",
            "przełącz z powrotem na natywną rozdzielczość",
            "Gra > Grafika > Skalowanie: AMD FSR (tryb Jakość) albo obniż cienie/oświetlenie/refleksje o stopień; "
            "porównaj FPS tej samej misji w `pm compare`.", "fps", score=30 + gb / 5))
    if gb is not None and gb >= GPU_BOUND_NO_CPU_PCT:
        not_worth.append({"key": "cpu_ram", "title": "Szybsza pamięć, OC procesora, wymiana CPU",
                          "why": f"Gra jest GPU-bound przez {gb:.0f}% czasu; CPU i RAM nie ograniczają FPS."})

    # [power_plan] Balanced: a cheap test, not a promised gain (Zen 4 boosts the same on Balanced most of the time)
    tested = (d.get("tests") or {}).get("power_plan") or {}
    if _plan_guid(info) == BALANCED_GUID:
        if tested.get("status") == "done" and tested.get("verdict") in ("bez zmian", "pogorszyło"):
            not_worth.append({"key": "power_plan", "title": "Plan zasilania Wysoka wydajność",
                              "why": f"Test #{tested['id']} ({tested['time']}): {tested['verdict']}."})
        else:
            pw = info.get("power") or {}
            det = [f"min. stan procesora {pw['cpu_min_pct']}%" if pw.get("cpu_min_pct") is not None else None,
                   f"parkowanie rdzeni: min. {pw['core_parking_min_pct']}% rdzeni" if pw.get("core_parking_min_pct") is not None else None,
                   f"PCIe Link State Power Management: {ASPM_PL.get(pw['pcie_aspm'], pw['pcie_aspm'])}" if pw.get("pcie_aspm") is not None else None]
            det = [x for x in det if x]
            items.append(_item(
                "power_plan", "Przetestuj plan zasilania Wysoka wydajność",
                "Aktywny jest plan Zrównoważony" + (f" ({', '.join(det)})" if det else "") + ". Na tym procesorze różnica "
                "bywa żadna albo niewielka (szybsze wybudzanie rdzeni), więc to test, nie pewny zysk.",
                "do sprawdzenia testem przed/po; zwykle mała różnica",
                "wyższy pobór prądu w spoczynku", "wróć do planu Zrównoważony",
                "Panel sterowania > Opcje zasilania > Wysoka wydajność, potem `pm test start \"Plan Wysoka wydajność\" "
                "--key power_plan`, zagraj kilka zwykłych sesji i `pm test stop`.", "fps", score=12))

    # [wifi_driver] driver warnings matter only when the PC plays over Wi-Fi and the network data shows home problems
    w = d.get("wifi")
    if w and w["n"] >= WIFI_WARN_MIN:
        name = f"{w['provider']} (zdarzenie {w['event_id']}, {w['n']}x" + (f", {w['adapter']}" if w["adapter"] else "") + ")"
        probs = sum(w["home"].values())
        if not w["net_s"]:
            unknown.append({"key": "wifi_driver", "title": f"Ostrzeżenia sterownika Wi-Fi: {name}",
                            "check": "Brak pomiarów sieci: uruchom kolektor z monitorem sieci, potem `pm net`."})
        elif w["wifi_pct"] < WIFI_LINK_PCT:
            not_worth.append({"key": "wifi_driver", "title": "Sterownik Wi-Fi",
                              "why": f"{name}, ale {100 - w['wifi_pct']}% czasu połączenie idzie kablem."})
        elif probs >= WIFI_PROBLEMS_MIN:
            items.append(_item(
                "wifi_driver", "Zaktualizuj sterownik Wi-Fi albo podłącz kabel",
                f"Sterownik Wi-Fi zgłosił ostrzeżenia {name} w ostatnich {MAIN_GAME_DAYS} dniach; komputer jest na Wi-Fi "
                f"przez {w['wifi_pct']}% czasu, a problemy sieci po stronie domu: "
                + ", ".join(f"{k} {v}x" for k, v in sorted(w["home"].items(), key=lambda kv: -kv[1])) + ".",
                "stabilniejszy ping i mniej utraty pakietów, jeśli to karta Wi-Fi je powoduje", "brak",
                "przywróć poprzedni sterownik (Menedżer urządzeń > Wycofaj sterownik)",
                "Zainstaluj najnowszy sterownik karty (dla Intel: Intel Driver & Support Assistant); w Menedżerze urządzeń > "
                "karta sieciowa > Zarządzanie energią odznacz wyłączanie urządzenia; najlepiej graj po kablu Ethernet.",
                "latency", score=20 + min(30, 3 * probs)))
        else:
            not_worth.append({"key": "wifi_driver", "title": "Sterownik Wi-Fi",
                              "why": f"{name}, ale pomiary sieci nie pokazują problemów po stronie domu (ping i utrata "
                                     "pakietów do routera w normie)."})

    # [overlays] hooks loaded into the game process (only with game_modules on: the scan reads the game's module list)
    for name, (cat, k) in sorted(_overlays(reports).items(), key=lambda kv: -kv[1][1]):
        if k < min(OVERLAY_MIN_SESSIONS, n):
            continue
        what = {"overlay": "nakładka", "capture": "przechwytywanie obrazu", "injector": "wstrzyknięta biblioteka"}[cat]
        items.append(_item(
            f"overlay:{name}", f"Wyłącz {name} w grze",
            f"{name} ({what}) był załadowany do procesu gry w {k} z {n} ostatnich sesji. Każdy taki hook dokłada pracę do "
            "każdej klatki i bywa źródłem stutterów oraz crashy sterownika.",
            "mniej opóźnień i stutterów, jeśli ta nakładka je powoduje; do sprawdzenia testem przed/po",
            "brak nakładki w grze", "włącz nakładkę z powrotem",
            f"Wyłącz nakładkę {name} w jej ustawieniach (dla tej gry albo globalnie), potem `pm test start \"Bez {name}\" "
            f"--key overlay:{name}` i kilka zwykłych sesji.", "latency", score=22 + 3 * k))

    # [frame_gen_base] frame generation on top of a low application frame rate
    fg = _frame_gen(reports)
    if fg and fg[0] < FRAME_GEN_BASE_FPS:
        items.append(_item(
            "frame_gen_base", "Obniż ustawienia albo wyłącz generowanie klatek",
            f"W sesjach z generowaniem klatek gra renderuje średnio {fg[0]:.0f} własnych klatek/s ({fg[1]:.0f} min). "
            f"Poniżej {FRAME_GEN_BASE_FPS} FPS generator dodaje odczuwalne opóźnienie i artefakty, a licznik w overlayu "
            "pokazuje liczbę z generowanymi klatkami.",
            "mniejsze opóźnienie wejścia i mniej artefaktów", "niższy licznik FPS w overlayu albo niższa jakość",
            "włącz generowanie klatek z powrotem",
            "Gra > Grafika: obniż preset albo włącz upscaler (FSR Jakość) tak, by bez generatora było co najmniej "
            f"{FRAME_GEN_BASE_FPS} FPS; albo wyłącz generowanie klatek (FSR Frame Generation / AFMF w Adrenalinie).",
            "latency", score=30))

    # [crash_dump] dumps cannot be written while the PC crashes: nothing will explain the next blue screen
    cd = info.get("crash_dump") or {}
    if cd.get("problems") and (unclean or any(c.get("kind") == "bsod" for c in health.get("recurring_crashes") or [])):
        items.append(_item(
            "crash_dump", "Napraw zapis zrzutów pamięci",
            "Windows nie zapisze zrzutu przy blue screenie: " + "; ".join(cd["problems"]) + ". Przy resetach bez blue "
            "screena (BugcheckCode=0) zrzutu i tak nie ma, ale następny blue screen zostałby bez przyczyny.",
            "kod błędu i sterownik winowajcy przy następnym blue screenie", "zajmuje miejsce na dysku systemowym",
            "przywróć poprzednie ustawienia zrzutu",
            "Zaawansowane ustawienia systemu > Uruchamianie i odzyskiwanie > Ustawienia: Zapisywanie informacji o "
            "debugowaniu: Automatyczny zrzut pamięci; plik stronicowania na C: zarządzany przez system.",
            "stability", TIER_STABILITY, 70))

    # [game_dvr]
    if info.get("game_dvr") == 1:
        items.append(_item("game_dvr", "Wyłącz nagrywanie Xbox Game DVR",
                           "Xbox Game DVR jest włączony (GameDVR_Enabled=1).", "drobna poprawa płynności, jeśli nagrywa w tle",
                           "brak klipów z Xbox Game Bar", "włącz z powrotem",
                           "Ustawienia > Gry > Przechwytywanie > wyłącz „Nagrywaj w tle, gdy gram”.", "smoothness", score=5))

    # [rebar]
    bars = [g.get("bar_mb") for g in gpus if not g.get("igpu") and g.get("bar_mb")]
    if info.get("rebar") is False:
        items.append(_item(
            "rebar", "Włącz Resizable BAR (SAM)",
            f"Okno pamięci karty ma {max(bars) if bars else BAR_OFF_MB} MB; przy aktywnym SAM obejmuje cały VRAM. "
            "Potwierdź w GPU-Z (pole Resizable BAR).",
            "więcej FPS, zwykle najwięcej na kartach AMD", "rzadko: niestabilność na starym BIOS-ie", "wyłącz opcję w BIOS",
            "BIOS: Settings > IO Ports > Above 4G Decoding = Enabled, potem Re-Size BAR Support = Auto/Enabled. Wymaga "
            "dysku systemowego w trybie UEFI (CSM wyłączone).", "fps", score=100))

    # [memory_integrity] last, small
    if info.get("memory_integrity") == 1:
        items.append(_item(
            "memory_integrity", "Wyłącz integralność pamięci (HVCI)",
            "Integralność pamięci jest włączona (HypervisorEnforcedCodeIntegrity=1).",
            "najwyżej niewielka różnica w FPS",
            "słabsza ochrona przed złośliwymi sterownikami; anti-cheat (FACEIT, Riot Vanguard) może wymagać włączonej",
            "włącz z powrotem i zrestartuj", "Zabezpieczenia Windows > Zabezpieczenia urządzenia > Izolacja rdzenia > "
            "Integralność pamięci: wyłącz i uruchom ponownie.", "fps", TIER_LAST, 0))

    # not worth: autostart apps
    cpu = d.get("bg_cpu")
    if cpu is not None and cpu < BG_CPU_NOT_WORTH_PCT:
        top = ", ".join(f"{k} {v:.1f}%" for k, v in d.get("bg_cpu_top") or [])
        not_worth.append({"key": "autostart", "title": "Wyłączanie programów z autostartu",
                          "why": f"Aplikacje w tle zużywają razem średnio {cpu:.1f}% CPU podczas gry (próg {BG_CPU_NOT_WORTH_PCT}%)"
                                 + (f"; najwięcej: {top}" if top else "") + ", więc to nie podniesie FPS."})

    # unknown: settings we could not read
    if not d.get("has_info"):
        unknown.append({"key": "system_info", "title": "Brak migawki konfiguracji",
                        "check": "Uruchom kolektor (`pm collector`); migawka powstaje raz dziennie."})
    else:
        if info.get("hags") is None:
            unknown.append({"key": "hags", "title": "Planowanie GPU z akceleracją sprzętową (HAGS)",
                            "check": "Ustawienia > System > Ekran > Grafika > Domyślne ustawienia grafiki."})
        if info.get("rebar") is None:
            unknown.append({"key": "rebar", "title": "Resizable BAR / SAM",
                            "check": "AMD Software: Adrenalin > Wydajność > Dostrajanie (Smart Access Memory) lub GPU-Z (Resizable BAR)."})

    items.sort(key=lambda i: (i["_tier"], -i["_score"]))
    for p, i in enumerate(items, 1):
        i["priority"] = p
        del i["_tier"], i["_score"]
    return {"generated": an.fmt_ts(d.get("now") or time.time()), "game": d.get("game"), "test_exe": d.get("test_exe"),
            "bottleneck": {k: bn[k] for k in ("kind", "share_pct", "minutes")}, "sessions": n,
            "items": items, "not_worth": not_worth, "unknown": unknown}


def advise(con):
    d = gather(con)
    rep = build(d)
    tests = d["tests"]
    for i in rep["items"]:
        i["test"] = tests.get(i["key"])
    return rep
