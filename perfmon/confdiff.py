"""Human-readable (Polish) diffs of configuration snapshots: system_info and game settings.

Nested dicts are flattened to dotted keys, lists of dicts are keyed by their name, lists of scalars are compared as
sets (reordering is not a change). A value that was null before means "was unreadable", never a change."""
import json
import re

# system_info keys that change on their own (or are measurements, not settings): never a config change
VOLATILE = {"last_boot", "volumes", "background_tools", "pagefile", "disk_health", "disk_map", "snapshot",
            "power_plan",   # legacy "guid  (name)" string; the PowerShell era wrote the name as mojibake
            "timer_resolution.current_ms", "defender.signature_age_days"}

LABELS = {
    "os": "Windows", "bios": "BIOS", "board": "płyta główna", "cpu": "CPU", "ram": "pamięć RAM", "ram_total_gb": "RAM (GB)",
    "gpus": "GPU", "disks": "dyski", "monitors": "monitory", "displays": "wyświetlacze", "startup_apps": "autostart",
    "power_plan_guid": "plan zasilania (GUID)", "power_plan_name": "plan zasilania",
    "game_mode": "tryb gry Windows", "instant_replay": "AMD Instant Replay", "amd_recording": "nagrywanie AMD",
    "amd_desktop_recording": "nagrywanie pulpitu AMD", "game_dvr": "Xbox Game DVR", "hags": "HAGS",
    "vbs_running": "VBS", "memory_integrity": "integralność pamięci (HVCI)", "pagefile_auto": "plik wymiany automatyczny",
    "fast_startup": "szybki rozruch", "rebar": "Resizable BAR (SAM)",
    "power": "plan zasilania", "power.name": "plan zasilania", "power.cpu_min_pct": "min. stan procesora (%)",
    "power.cpu_max_pct": "maks. stan procesora (%)", "power.core_parking_min_pct": "parkowanie rdzeni: min. rdzeni (%)",
    "power.boost_mode": "tryb boost procesora", "power.epp_pct": "EPP (preferencja energii)",
    "power.pcie_aspm": "PCIe Link State Power Management", "power.usb_selective_suspend": "selektywne wstrzymywanie USB",
    "monitor_state": "monitor", "directx": "ustawienia grafiki DirectX",
    "directx.vrr_optimization": "optymalizacja VRR", "directx.windowed_optimizations": "optymalizacje gier w oknie",
    "directx.auto_hdr": "Auto HDR", "game_exes": "ustawienia exe gry", "timer_resolution": "rozdzielczość timera",
    "timer_resolution.global_requests": "GlobalTimerResolutionRequests",
    "mmcss": "MMCSS", "mmcss.system_responsiveness": "MMCSS SystemResponsiveness",
    "mmcss.network_throttling": "MMCSS NetworkThrottlingIndex", "priority_separation": "Win32PrioritySeparation",
    "mouse": "mysz", "mouse.pointer_precision": "precyzja wskaźnika (akceleracja)", "mouse.speed": "szybkość wskaźnika",
    "defender": "Windows Defender", "defender.realtime": "Defender: ochrona w czasie rzeczywistym",
    "defender.games_excluded": "Defender: wykluczenie folderu gry", "drivers": "sterownik", "kbs": "aktualizacje Windows (KB)",
    "amd": "Adrenalin", "amd.anti_lag": "Anti-Lag", "amd.chill": "Radeon Chill", "amd.boost": "Radeon Boost",
    "amd.frtc": "FRTC (limit klatek)", "amd.frtc_target": "FRTC cel FPS", "amd.rsr": "Radeon Super Resolution",
    "amd.afmf": "AFMF (Fluid Motion Frames)", "amd.vsync": "Adrenalin: czekaj na synchronizację pionową",
    "amd.freesync": "FreeSync", "amd.freesync.gaming": "FreeSync w grach", "amd.freesync.state": "FreeSync (stan ADL)",
    "gpus.driver": "sterownik GPU", "gpus.driver_date": "data sterownika GPU", "gpus.refresh": "odświeżanie GPU (Hz)",
    "gpus.res": "rozdzielczość GPU", "gpus.status": "stan GPU", "gpus.anti_lag": "Anti-Lag (sterownik)",
    "gpus.rebar_mode": "tryb ReBar sterownika", "gpus.bar_mb": "BAR GPU (MB)", "gpus": "GPU",
    "disks.health": "stan dysku", "disks.size_gb": "rozmiar dysku (GB)", "disks.bus": "magistrala dysku",
    "displays.mode": "tryb wyświetlacza", "displays.adapter": "karta wyświetlacza", "displays.primary": "główny wyświetlacz",
    "monitor_state.refresh_hz": "odświeżanie monitora (Hz)", "monitor_state.hdr": "HDR monitora",
    "monitor_state.hdr_supported": "obsługa HDR monitora",
    "game_exes.gpu_preference": "preferencja GPU (Windows)", "game_exes.compat_flags": "flagi zgodności",
    "game_exes.fso_disabled": "wyłączone optymalizacje pełnego ekranu",
    "defender.games_excluded.excluded": "Defender: wykluczenie folderu gry", "defender.antivirus": "Defender: antywirus",
    "defender.tamper_protection": "Defender: ochrona przed naruszeniami",
    "mouse.thresholds": "progi akceleracji myszy", "timer_resolution.finest_ms": "najdokładniejszy timer (ms)",
    "drivers.chipset": "sterownik chipsetu", "drivers.chipset.version": "sterownik chipsetu",
    "drivers.chipset.date": "data sterownika chipsetu", "drivers.audio": "sterownik audio",
    "drivers.audio.version": "sterownik audio", "drivers.audio.date": "data sterownika audio",
    "drivers.network": "sterownik sieci", "drivers.network.version": "sterownik sieci",
    "drivers.network.date": "data sterownika sieci", "drivers.bluetooth": "sterownik Bluetooth",
    "drivers.bluetooth.version": "sterownik Bluetooth", "drivers.bluetooth.date": "data sterownika Bluetooth",
    "drivers.usb": "sterownik kontrolera USB", "drivers.usb.version": "sterownik kontrolera USB",
    "drivers.usb.date": "data sterownika kontrolera USB",
    # game settings (gamesettings.NORMAL)
    "resolution": "rozdzielczość", "render_scale": "skala renderowania", "upscaler": "upscaler",
    "upscaler_mode": "tryb upscalera", "frame_gen": "generator klatek", "fps_cap": "limit FPS", "vsync": "VSync",
    "display_mode": "tryb wyświetlania", "texture_quality": "jakość tekstur", "ray_tracing": "ray tracing",
    "preset": "preset grafiki", "low_latency": "niskie opóźnienie", "hdr": "HDR", "fov": "FOV",
}

NAME_KEYS = ("name", "monitor", "device", "path", "id")


def _fmt(v):
    if v is None:
        return "brak"
    if isinstance(v, bool):
        return "wł." if v else "wył."
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)[:120]
    return str(v)


def _item_key(d):
    for k in NAME_KEYS:
        if d.get(k) not in (None, ""):
            return str(d[k])
    return None


def flatten(v, prefix=""):
    """{dotted key: scalar or frozenset}. Lists of dicts -> key[name].field; lists of scalars -> frozenset."""
    out = {}
    if isinstance(v, dict):
        for k, x in v.items():
            out.update(flatten(x, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(v, list):
        if v and all(isinstance(x, dict) for x in v) and all(_item_key(x) for x in v):
            seen = {}
            for x in v:
                k = _item_key(x)
                seen[k] = seen.get(k, 0) + 1
                name = k if seen[k] == 1 else f"{k}#{seen[k]}"
                out.update(flatten({kk: vv for kk, vv in x.items()}, f"{prefix}[{name}]"))
        else:
            out[prefix] = frozenset(json.dumps(x, sort_keys=True, ensure_ascii=False) if isinstance(x, (dict, list))
                                    else x for x in v)
    else:
        out[prefix] = v
    return out


def _volatile(key, volatile):
    base = re.sub(r"\[[^\]]*\]", "", key)
    return any(base == v or base.startswith(v + ".") for v in volatile)


def label(key):
    """Polish label for a flattened key: the key without its [item] parts, else its longest labelled prefix;
    the items (GPU / driver / monitor name) follow the label."""
    items = re.findall(r"\[([^\]]*)\]", key)
    base = re.sub(r"\[[^\]]*\]", "", key)
    head = LABELS.get(base)
    if head is None:
        parts = base.split(".")
        head = base
        for i in range(len(parts) - 1, 0, -1):
            p = ".".join(parts[:i])
            if p in LABELS:
                head = f"{LABELS[p]}: {'.'.join(parts[i:])}"
                break
    return f"{head} {' '.join(items)}" if items else head


def diff(old, new, volatile=VOLATILE, null_is_unknown=True):
    """[{key, label, before, after, text}] for keys present in both snapshots whose value changed."""
    if not old or not new:
        return []
    fo, fn = flatten(old), flatten(new)
    out = []
    item = lambda k: k[:k.rindex("]") + 1] if "]" in k else None
    io, inew = {item(k) for k in fo} - {None}, {item(k) for k in fn} - {None}
    whole = set()
    for it in sorted(io ^ inew):   # a whole item (driver, monitor, GPU) appeared or disappeared: one line
        top = re.split(r"[.\[]", it, maxsplit=1)[0]
        parent = it[:it.rindex("[")]
        unread = (parent in fo and fo[parent] is None) or (parent in fn and fn[parent] is None)   # list was null
        if top not in old or top not in new or unread or _volatile(it, volatile) or any(it.startswith(w) for w in whole):
            continue
        whole.add(it)
        added = it in inew
        out.append({"key": it, "label": label(it), "before": None if added else "obecny", "after": "obecny" if added else None,
                    "text": f"{label(it)}: {'dodano' if added else 'usunięto'}"})
    for k in sorted(set(fo) | set(fn)):
        if _volatile(k, volatile) or any(k.startswith(w) for w in whole):
            continue
        a, b = fo.get(k), fn.get(k)
        if a == b:
            continue
        top = re.split(r"[.\[]", k, maxsplit=1)[0]
        if null_is_unknown and (a is None or b is None):
            continue   # a value that became (un)readable is not a change
        if top not in old or top not in new:
            continue   # key added by a newer PerfMonitor version: not a change of the PC
        if isinstance(a, frozenset) or isinstance(b, frozenset):
            a, b = a or frozenset(), b or frozenset()
            added, removed = sorted(map(str, b - a)), sorted(map(str, a - b))
            parts = ([f"dodano {', '.join(added)}"] if added else []) + ([f"usunięto {', '.join(removed)}"] if removed else [])
            out.append({"key": k, "label": label(k), "before": sorted(map(str, a)), "after": sorted(map(str, b)),
                        "text": f"{label(k)}: {'; '.join(parts)}"})
            continue
        out.append({"key": k, "label": label(k), "before": a, "after": b,
                    "text": f"{label(k)}: {_fmt(a)} → {_fmt(b)}"})
    return out


def details(changes):
    """incident details for config_change: {key: {before, after, label, text}} (the UI reads before/after)."""
    return {c["key"]: {"before": c["before"], "after": c["after"], "label": c["label"], "text": c["text"]} for c in changes}


def title(changes, prefix="Zmiana konfiguracji", n=3):
    if not changes:
        return prefix
    more = f" (+{len(changes) - n})" if len(changes) > n else ""
    return f"{prefix}: " + "; ".join(c["text"] for c in changes[:n]) + more
