"""`pm advise`: a short ranked list of concrete changes, each backed by the user's own numbers.

An item exists only while its condition holds in the data, so it disappears once the fix is done. Settings we cannot
read go to `unknown` (with how to check), claims the data refutes go to `not_worth`. Expected effects are qualitative:
we never promise a percentage gain, only a before/after test can prove one."""
import re
import time
from collections import Counter, defaultdict

from . import analyze as an
from . import experiments, present

MAIN_GAME_DAYS = 14
RECENT_SESSIONS = 6
CAUSE_MIN_SESSIONS = 2        # a cause must be confirmed in this many recent sessions
BG_GPU_MIN_PCT = 10           # average GPU % of a background process across recent sessions
COMPOSED_MIN_PCT = 50         # composed presentation share of frames
GPU_BOUND_MIN_PCT = 60        # GPU-bound share of gameplay for the upscaler advice
BG_CPU_NOT_WORTH_PCT = 5      # all background apps together below this: autostart cleanup is pointless
GPU_BOUND_NO_CPU_PCT = 90     # above this faster CPU/RAM cannot help
BAR_OFF_MB = 256              # biggest GPU memory BAR at or below this = Resizable BAR inactive

TIER_STABILITY, TIER_NORMAL, TIER_LAST = 0, 1, 2
GAME_ALIAS = re.compile(r"^(sp|mp|zm)\d*-", re.I)    # sp26-cod.exe and cod.exe are one game
BG_GPU_IGNORE = {"dwm.exe", "system", "idle", "csrss.exe", "explorer.exe", "searchhost.exe"}
# not autostart apps: our own tools, Windows components, the GPU driver service (judged by the background_gpu rule)
OWN_PROCS = {"python.exe", "pythonw.exe", "powershell.exe", "presentmon.exe", "dwm.exe", "svchost.exe", "csrss.exe",
             "audiodg.exe", "lsass.exe", "services.exe", "winlogon.exe", "wmiprvse.exe", "msmpeng.exe", "sihost.exe",
             "taskhostw.exe", "ctfmon.exe", "searchhost.exe", "runtimebroker.exe", "fontdrvhost.exe", "gamingservices.exe",
             "amdrsserv.exe", "explorer.exe", "conhost.exe", "wudfhost.exe", "dllhost.exe", "smss.exe"}
GIGABYTE_RE = re.compile(r"easytune|aorus|^gcc|gigabyte", re.I)


def _game_key(exe):
    return GAME_ALIAS.sub("", (exe or "").lower())


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
            "reports": reports, "health": an.health_overview(con, MAIN_GAME_DAYS), "bg_cpu": cpu, "bg_cpu_top": top}


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

    # [fast_startup] hard resets that happened during shutdown/sleep while Fast Startup is on
    unclean = health.get("unclean_shutdowns") or []
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
    rep = build(gather(con))
    tests = experiments.by_key(con)
    for i in rep["items"]:
        i["test"] = tests.get(i["key"])
    return rep
