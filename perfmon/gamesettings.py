r"""Game settings snapshot: find the game's config files, parse them, normalize the settings that move FPS/stutter.

Read-only: files are opened for reading only (the game may hold them open; a locked file is reported, not retried).
Files found on this PC (copies of them are the test fixtures):
- Call of Duty (cod.exe / sp26-cod.exe, Steam "Call of Duty HQ" and Battle.net): %LOCALAPPDATA%\Activision\Call of Duty\
  players\s.1.0.cod26.txt (graphics/display; Documents\Call of Duty\players on older installs) and
  players\<id>\g.cod26.1.0.l.txt (per-player: FOV). Lines "Name@a;b;c = value // range"; a name appears twice when
  the game migrated it (AMDFidelityFX@0 = FSR 3, AMDFidelityFX@1 = FSR 4): the later line is the live one.
- Battlefield 6 (bf6.exe): Documents\Battlefield 6\settings\[steam\]PROFSAVEbf6mp_profile, "GstRender.Key value" lines.
  Frostbite's enums (upscaler technique, vsync, fullscreen mode, quality levels) are stored as numbers whose
  labels are not documented: they stay numbers (a change is still a change), never a guessed label.
- Unreal Engine games (Bodycam): <exe>\..\..\..\ = project folder <P>; %LOCALAPPDATA%\<P>\Saved\Config\Windows
  [NoEditor|Client]\GameUserSettings.ini (+ Engine.ini overrides). UE's EWindowMode (0 fullscreen,
  1 windowed fullscreen, 2 windowed) and scalability levels (0 low .. 4 cinematic) are engine-wide enums.
- Anything else: config "game_settings_paths" {"game.exe": ["%LOCALAPPDATA%\\Game\\*.ini", ...]} parsed by extension
  (.ini sections, .json flattened, other text as key=value / key: value / key value).
"""
import glob
import hashlib
import json
import os
import re

NORMAL = ("resolution", "render_scale", "upscaler", "upscaler_mode", "frame_gen", "fps_cap", "vsync", "display_mode",
          "texture_quality", "ray_tracing", "preset", "low_latency", "hdr", "fov")
# keys that change without the player touching a setting (window position, detection results, caches, audio)
VOLATILE_RE = re.compile(
    r"(?i)(volume|(^|\.)window(pos)?[xy]$|(^|\.)window(width|height|maximized)$|lastuser|lastconfirmed|lastrecommended|"
    r"lastsaved|lastused|benchmark|splash|shaderbundleversion|culture|cachedclientid|telemetry|timestamp|(^|\.)detected|"
    r"essdi|(^|\.)hud-|seasonintro|seasonvideo|recommendedset|driverversion|gpuname|(^|\.)option.*last|lastseason)")
MAX_BYTES = 4 * 2**20
MAX_CHANGES = 80


def _num(v):
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return v
    return int(f) if f == int(f) else round(f, 3)


def _bool(v):
    s = str(v).strip().lower() if v is not None else None
    return True if s in ("true", "1", "on", "yes") else False if s in ("false", "0", "off", "no") else None


# ---------------------------------------------------------------- parsers (text -> flat {key: str})
def parse_cod(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.split("@", 1)[0].strip()] = v.split(" // ", 1)[0].strip()   # later duplicate = migrated, live value
    return out


def parse_frostbite(text):
    out = {}
    for line in text.splitlines():
        p = line.strip().split(None, 1)
        if len(p) == 2 and re.match(r"^[\w.\-]+$", p[0]):
            out[p[0]] = p[1].strip()
    return out


def parse_ini(text, prefix=""):
    out, sec = {}, ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = line[1:-1].strip().rsplit(".", 1)[-1].split()[0] if line[1:-1].strip() else ""
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[f"{prefix}{sec + '.' if sec else ''}{k.strip()}"] = v.strip()
    return out


def parse_kv(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//", ";", "[")):
            continue
        m = re.match(r"^([^=:\s]+)\s*(?:[=:]\s*|\s+)(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def parse_json(text):
    out = {}

    def walk(v, p):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{p}.{k}" if p else str(k))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{p}[{i}]")
        else:
            out[p] = "" if v is None else str(v)
    walk(json.loads(text), "")
    return out


PARSERS = {"cod": parse_cod, "frostbite": parse_frostbite, "ini": parse_ini, "kv": parse_kv, "json": parse_json}


def _by_ext(path):
    ext = os.path.splitext(path)[1].lower()
    return "ini" if ext == ".ini" else "json" if ext == ".json" else "kv"


# ---------------------------------------------------------------- normalizers (raw -> NORMAL)
def norm_cod(r):
    g = r.get
    fx = g("AMDFidelityFX")
    mode = g("AMDSuperResolution2Quality") if fx in ("FSR 3", "FSR 4") else g("AMDSuperResolutionQuality") if fx == "FSR 1" else None
    fg = [_bool(g(k)) for k in ("FSRFrameInterpolation", "DLSSFrameGeneration", "IntelXeFG") if g(k) is not None]
    cap = _bool(g("CapFps"))
    return {"resolution": g("Resolution"), "render_scale": _num(g("ResolutionMultiplier")), "upscaler": fx,
            "upscaler_mode": mode, "frame_gen": any(fg) if fg else None,
            "fps_cap": _num(g("MaxFpsInGame")) if cap else "off" if cap is False else None,
            "vsync": g("VSync"), "display_mode": g("DisplayMode"), "texture_quality": _num(g("TextureQuality")),
            "ray_tracing": g("DxrMode"), "preset": g("GraphicsQuality"), "low_latency": _bool(g("AmdAntilag2")),
            "hdr": g("HDR"), "fov": _num(g("player.Fov"))}


def norm_frostbite(r):
    g = lambda k: r.get("GstRender." + k)
    w, h = g("ResolutionWidth"), g("ResolutionHeight")
    scale = _num(g("ResolutionScale"))
    lim = _bool(g("FrameRateLimiterEnable"))
    return {"resolution": f"{w}x{h}" if w and h else None,
            "render_scale": round(scale * 100) if isinstance(scale, (int, float)) else None,
            "upscaler": _num(g("AMDIntelUpscalingTechnique")), "upscaler_mode": None,
            "frame_gen": _bool(g("AMDFrameGenerationEnabled")),
            "fps_cap": _num(g("FrameRateLimit")) if lim else "off" if lim is False else None,
            "vsync": _num(g("VSyncMode")), "display_mode": _num(g("FullscreenMode")),
            "texture_quality": _num(g("TextureQuality")), "ray_tracing": _bool(g("RaytracingAmbientOcclusion")),
            "preset": _num(g("OverallGraphicsQuality")), "low_latency": _num(g("AMDLowLatency")),
            "hdr": _num(g("HighDynamicRangeMode")), "fov": _num(g("FieldOfViewVertical"))}


UE_WINDOW = {"0": "fullscreen", "1": "windowed_fullscreen", "2": "windowed"}
UE_LEVEL = {0: "low", 1: "medium", 2: "high", 3: "epic", 4: "cinematic"}


def norm_ue(r):
    def g(name):
        for k, v in r.items():
            if k == name or k.endswith("." + name):
                return v
        return None
    x, y = g("ResolutionSizeX"), g("ResolutionSizeY")
    lim = _num(g("FrameRateLimit"))
    tex = _num(g("sg.TextureQuality"))
    levels = {_num(v) for k, v in r.items() if ".sg." in k and not k.endswith("ResolutionQuality")}
    wm = g("FullscreenMode")
    return {"resolution": f"{x}x{y}" if x and y else None, "render_scale": _num(g("sg.ResolutionQuality")),
            "upscaler": None, "upscaler_mode": None, "frame_gen": None,
            "fps_cap": None if lim is None else "off" if lim == 0 else lim,
            "vsync": _bool(g("bUseVSync")), "display_mode": UE_WINDOW.get(str(wm).strip(), wm) if wm is not None else None,
            "texture_quality": UE_LEVEL.get(tex, tex), "ray_tracing": None,
            "preset": UE_LEVEL.get(next(iter(levels)), None) if len(levels) == 1 else ("mixed" if levels else None),
            "low_latency": None, "hdr": _bool(g("bUseHDRDisplayOutput")), "fov": None}


def norm_generic(r):
    return norm_ue(r) if any(k.endswith("ResolutionSizeX") for k in r) else {k: None for k in NORMAL}


# ---------------------------------------------------------------- locating the files
def documents():
    try:
        from win32com.shell import shell, shellcon
        return shell.SHGetFolderPath(0, shellcon.CSIDL_PERSONAL, None, 0)
    except Exception:
        return os.path.join(os.path.expanduser("~"), "Documents")


def _local():
    return os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")


def _newest(patterns, exclude=None):
    files = [f for p in patterns for f in glob.glob(p) if os.path.isfile(f) and not (exclude and exclude.search(f))]
    return max(files, key=os.path.getmtime) if files else None


def cod_files(exe_path):
    roots = [os.path.join(_local(), "Activision", "Call of Duty", "players"),
             os.path.join(documents(), "Call of Duty", "players")]
    s = _newest([os.path.join(r, "s.*.txt") for r in roots])
    g = _newest([os.path.join(r, "*", "g.*.l.txt") for r in roots], re.compile(r"\.pm\.", re.I))
    return [(s, "cod", ""), (g, "cod", "player.")], [os.path.join(r, "s.*.txt") for r in roots]


def bf6_files(exe_path):
    base = os.path.join(documents(), "Battlefield 6", "settings")
    pats = [os.path.join(base, "PROFSAVE*_profile"), os.path.join(base, "*", "PROFSAVE*_profile")]
    return [(_newest(pats), "frostbite", "")], pats


def ue_files(exe_path):
    m = re.match(r"(.*)\\Binaries\\Win64\\[^\\]+$", exe_path or "", re.I)
    if not m:
        return [], []
    proj = m.group(1)
    name = os.path.basename(proj)
    dirs = [os.path.join(base, "Saved", "Config", plat) for base in (os.path.join(_local(), name), proj)
            for plat in ("Windows", "WindowsNoEditor", "WindowsClient")]
    gus = [os.path.join(d, "GameUserSettings.ini") for d in dirs]
    found = next((f for f in gus if os.path.isfile(f)), None)
    files = [(found, "ini", "")]
    if found:
        files.append((os.path.join(os.path.dirname(found), "Engine.ini"), "ini", "engine."))
    return files, gus


# (exe matcher, file locator, normalizer); first match wins, the UE locator is tried for every exe
GAMES = [
    (re.compile(r"^(cod|sp\d+-cod|cod\d+-cod|blackops\d*|modernwarfare)\.exe$", re.I), cod_files, norm_cod),
    (re.compile(r"^bf6\.exe$", re.I), bf6_files, norm_frostbite),
]


def _expand(p):
    return os.path.expandvars(p.replace("{documents}", documents()).replace("{localappdata}", _local()))


def locate(exe, exe_path, cfg=None):
    """([(path, parser, key prefix)], normalizer, searched patterns) or None when the game is not known at all."""
    extra = [(_newest([_expand(p)]), _by_ext(p), "") for p in ((cfg or {}).get("game_settings_paths") or {}).get(
        (exe or "").lower(), [])]
    for rx, loc, norm in GAMES:
        if rx.match(exe or ""):
            files, searched = loc(exe_path)
            return files + extra, norm, searched
    files, searched = ue_files(exe_path)
    if files:
        return files + extra, norm_ue, searched
    if extra:
        return extra, norm_generic, [p for p in cfg["game_settings_paths"][exe.lower()]]
    return None


def _read(path):
    with open(path, "rb") as f:
        data = f.read(MAX_BYTES)
    return data.decode("utf-8-sig", errors="replace")


def _hash(raw):
    stable = {k: v for k, v in raw.items() if not VOLATILE_RE.search(k)}
    return hashlib.sha1(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def snapshot(exe, exe_path, cfg=None):
    """{"files", "normalized", "hash", "raw"} | {"error", "searched"} | None (game without known config files)."""
    loc = locate(exe, exe_path, cfg)
    if loc is None:
        return None
    files, norm, searched = loc
    raw, used, errors = {}, [], []
    for path, parser, prefix in files:
        if not path or not os.path.isfile(path):
            continue
        try:
            text = _read(path)
            parsed = parse_ini(text, prefix) if parser == "ini" else PARSERS[parser](text)
            if parser != "ini" and prefix:
                parsed = {prefix + k: v for k, v in parsed.items()}
        except (OSError, ValueError) as e:
            errors.append(f"{os.path.basename(path)}: {e}")
            continue
        raw.update(parsed)
        used.append({"path": path, "mtime": os.path.getmtime(path)})
    if not used:
        return {"error": "; ".join(errors) or "nie znaleziono pliku ustawień gry", "searched": searched}
    try:
        normalized = norm(raw)
    except Exception as e:   # a parser bug must not lose the raw snapshot
        normalized, errors = {k: None for k in NORMAL}, errors + [f"normalize: {e}"]
    out = {"files": used, "normalized": normalized, "hash": _hash(raw), "raw": raw}
    if errors:
        out["errors"] = errors
    return out


def file_stamp(snap):
    """(path, mtime) of the files a snapshot came from: cheap change check between full re-reads."""
    out = []
    for f in (snap or {}).get("files") or []:
        try:
            out.append((f["path"], os.path.getmtime(f["path"])))
        except OSError:
            out.append((f["path"], None))
    return tuple(out)


def raw_changes(old, new):
    """{key: [before, after]} of non-volatile raw keys (missing on one side = null)."""
    old, new = old or {}, new or {}
    keys = sorted(k for k in set(old) | set(new) if not VOLATILE_RE.search(k) and old.get(k) != new.get(k))
    return {k: [old.get(k), new.get(k)] for k in keys[:MAX_CHANGES]}


# ---------------------------------------------------------------- persistence (game_settings table)
def store(con, exe, snap, ts):
    """Keep the raw settings of `exe` when they changed. Returns the context entry for the session:
    normalized + hash + files + raw keys changed vs the previously stored snapshot of the same game."""
    if not snap or "error" in snap:
        return snap
    prev = con.execute("SELECT ts, hash, raw, normalized FROM game_settings WHERE exe=? ORDER BY ts DESC LIMIT 1",
                       (exe.lower(),)).fetchone()
    ctx = {"hash": snap["hash"], "normalized": snap["normalized"], "files": snap["files"]}
    if snap.get("errors"):
        ctx["errors"] = snap["errors"]
    if prev and prev[1] == snap["hash"]:
        ctx["since_ts"] = prev[0]
        return ctx
    if prev:
        ctx["prev_hash"] = prev[1]
        ctx["changed"] = raw_changes(json.loads(prev[2] or "{}"), snap["raw"])
    con.execute("INSERT INTO game_settings(ts, exe, hash, normalized, raw) VALUES (?,?,?,?,?)",
                (ts, exe.lower(), snap["hash"], json.dumps(snap["normalized"], ensure_ascii=False),
                 json.dumps(snap["raw"], ensure_ascii=False)))
    ctx["since_ts"] = ts
    return ctx
