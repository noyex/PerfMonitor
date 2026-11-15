"""Paths and settings. Overridable via config.json in the data folder."""
import json
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))   # the installed build (PyInstaller); else a source checkout
ROOT = Path(__file__).resolve().parent.parent
# The installed build keeps data outside its own folder, so an update (which replaces that folder) never touches it
_DEFAULT_DATA = Path(os.environ.get("LOCALAPPDATA", ROOT)) / "NX Frame" if FROZEN else ROOT / "data"
DATA_DIR = Path(os.environ.get("PERFMON_DATA_DIR") or _DEFAULT_DATA)   # env override: tests
DB_PATH = DATA_DIR / "perf.db"
GPU_PCT_MAX = 100.0   # GPU load of one process / engine above this is a PDH artifact (seen: 3e13 %), never real
LOG_PATH = DATA_DIR / "collector.log"
CONFIG_PATH = DATA_DIR / "config.json"

def _program_files():
    """Program Files from the registry, not from %ProgramFiles%: a user can override environment variables for all
    their processes (HKCU\\Environment), the elevated collector included."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion") as key:
        return winreg.QueryValueEx(key, "ProgramFilesDir")[0]


# LibreHardwareMonitor and PresentMon from winget: per user for a source checkout. The installed build's collector
# runs elevated from Program Files, so it takes them from the machine-wide folder only (the installer puts them
# there), where nothing but an administrator can replace the code it loads; see CODE_PATHS
_WINGET = (Path(_program_files()) / "WinGet" / "Packages" if FROZEN
           else Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")))
CODE_PATHS = ("lhm_dir", "presentmon_exe")   # config.json (user-writable) cannot move these in the installed build

DEFAULTS = {
    "sample_interval_s": 1.0,
    "proc_interval_s": 5.0,
    "event_poll_s": 30.0,
    "retention_days": 90,
    "detail_retention_days": 30,        # hw_detail (UV/OC telemetry); per-session aggregates are kept forever
    "incident_retention_days": 365,     # incidents + system_info snapshots (sessions, profiles, tests: forever)
    "detail_interval_s": 10.0,          # hw_detail outside game/stress sessions (1 s inside them)
    "db_guard_gb": 2.0,                 # above this perf.db size: warning incident + detail retention cut to 14 days
    "apic_ids_per_core": 2,             # WHEA APIC ID -> core (7700X with SMT: 2 threads per core)
    "gpu_od_poll_s": 600,               # Adrenalin tuning readout via ADL; 0 = never touch ADL
    "ui_port": 8765,
    "steam_banners": True,      # fetch a missing game banner from Steam's public CDN (perfmon/banners.py)
    "hotkey_mark": "ctrl+alt+l",   # global shortcut for `pm mark` while playing; "" = off
    "hotkey_beep": True,           # short beep confirming the mark
    "gpu_proc_interval_s": 5.0,    # GPU load / VRAM per process and the game process sampler
    # Shader caches measured during a game (growth at a stutter = shader compilation); "{steam}" =
    # steamapps\shadercache\<appid> of the game. Same list as shadercache.DEFAULT_DIRS (importing it here is a cycle).
    "shader_cache_dirs": [r"%LOCALAPPDATA%\AMD\DxCache", r"%LOCALAPPDATA%\AMD\DxcCache", r"%LOCALAPPDATA%\AMD\VkCache",
                          r"%LOCALAPPDATA%\AMD\OglCache", r"%LOCALAPPDATA%\AMD\DX9Cache", "{steam}"],
    "shader_scan_interval_s": 10,
    # Session context (perfmon/sessionctx.py): game config files re-read when they change (mtime checked this often);
    # extra config files per exe {"game.exe": ["%LOCALAPPDATA%\\Game\\settings.ini"]}, parsed by extension
    "game_settings_poll_s": 30,
    "game_settings_paths": {},
    # DLLs in the game process (overlays/hooks): first scan once the game settled, then this often; False = never.
    # Off by default: the module snapshot opens the game with VM_READ, which kernel anti-cheats may log; opt in
    "game_modules": False,
    "game_modules_settle_s": 120,
    "game_modules_poll_s": 600,
    # ETW black box (perfmon/etwbox.py): kernel trace in memory, dumped on these triggers; mode game|always|off.
    # A dump (wpr -stop) stutters the game itself, so stutter bursts dump at most once per burst_cooldown_s
    "etw": {"mode": "game", "buffer_mb": 256, "dump_last_s": 60, "burst_cooldown_s": 1800,
            "triggers": {"dwm_stall": True, "ui_freeze": True, "user_mark": True, "stutter_burst": 5, "dpc_pct": 8},
            "analyze": True, "analyze_during_game": False, "retention_days": 14, "max_gb": 2,
            "analyze_idle_s": 300},   # older unanalysed captures (backlog) only after this long without user input
    # Process start/stop log: these are ignored outside game sessions (inside only conhost/dllhost/WmiPrvSE)
    "proc_events_ignore": ["conhost.exe", "cmd.exe", "powershell.exe", "svchost.exe", "RuntimeBroker.exe",
                           "backgroundTaskHost.exe", "dllhost.exe", "WmiPrvSE.exe", "taskhostw.exe"],
    # Background processes whose start during a game is reported as a likely stutter cause
    "background_culprits": ["MsMpEng.exe", "TiWorker.exe", "CompatTelRunner.exe", "SearchIndexer.exe", "DismHost.exe",
                            "OneDrive.exe", "MoUsoCoreWorker.exe", "wuauclt.exe", "SgrmBroker.exe",
                            "MicrosoftEdgeUpdate.exe", "GoogleUpdate.exe", "steamwebhelper.exe", "EpicWebHelper.exe",
                            "AorusLcdService.exe", "GCC.exe", "EasyTune.exe"],
    "lhm_dir": str(_WINGET / "LibreHardwareMonitor.LibreHardwareMonitor_Microsoft.Winget.Source_8wekyb3d8bbwe"),
    "presentmon_exe": str(_WINGET / "Intel.PresentMon.Console_Microsoft.Winget.Source_8wekyb3d8bbwe" / "presentmon.exe"),
    # Games detected by exe name (lowercase) – add your own in data/config.json
    "game_exes": [
        "cod.exe", "cod24-cod.exe", "cod25-cod.exe", "blackops6.exe", "modernwarfare.exe",
        "cs2.exe", "valorant-win64-shipping.exe", "fortniteclient-win64-shipping.exe",
        "r5apex.exe", "r5apex_dx12.exe", "eldenring.exe", "cyberpunk2077.exe",
        "rdr2.exe", "gta5.exe", "gta5_enhanced.exe", "leagueoflegends.exe", "league of legends.exe",
        "overwatch.exe", "dota2.exe", "rocketleague.exe", "pubg.exe", "tslgame.exe",
        "escapefromtarkov.exe", "rustclient.exe", "bf2042.exe", "bf6.exe", "marvel-win64-shipping.exe",
        "minecraft.exe", "javaw.exe_minecraft", "witcher3.exe", "starfield.exe", "helldivers2.exe",
    ],
    # Paths under which every exe is treated as a game (heuristic)
    "game_path_markers": [
        "\\steamapps\\common\\", "\\epic games\\", "\\riot games\\", "\\xboxgames\\",
        "\\call of duty\\", "\\ea games\\", "\\gog galaxy\\games\\", "\\ubisoft game launcher\\games\\",
        "\\battlestate games\\", "\\windowsapps\\", "\\games\\",
    ],
    # Exes that are NEVER a game (launchers, crash handlers, anticheats)
    "not_game_exes": [
        "steam.exe", "steamwebhelper.exe", "steamservice.exe", "epicgameslauncher.exe",
        "epicwebhelper.exe", "battle.net.exe", "agent.exe", "riotclientservices.exe",
        "riotclientux.exe", "riotclientuxrender.exe", "unitycrashhandler64.exe",
        "unitycrashhandler32.exe", "crashreportclient.exe", "crashpad_handler.exe",
        "easyanticheat.exe", "easyanticheat_eos.exe", "beservice.exe", "beservice_x64.exe",
        "vgc.exe", "vgtray.exe", "eadesktop.exe", "eabackgroundservice.exe", "galaxyclient.exe",
        "upc.exe", "ubisoftconnect.exe", "gamebar.exe", "gamingservices.exe", "xboxpcapp.exe",
        "msedgewebview2.exe", "cefsharp.browsersubprocess.exe", "uninstall.exe", "unins000.exe",
        "redistributable.exe", "vc_redist.x64.exe", "dxsetup.exe", "launcher.exe", "presentmon.exe",
        "wallpaper32.exe", "wallpaper64.exe", "wallpaperservice32.exe", "wallpaperui.exe", "webwallpaper32.exe",
        "steamerrorreporter.exe",
    ],
    # Stress tests / benchmarks (lowercase exe) -> sessions with kind='stress'. "3d": capture frames with PresentMon
    "stress_exes": {
        "occt.exe": {"name": "OCCT"}, "y-cruncher.exe": {"name": "y-cruncher"},
        "prime95.exe": {"name": "Prime95"}, "cinebench.exe": {"name": "Cinebench"},
        "cinebenchr23.exe": {"name": "Cinebench R23"}, "3dmark.exe": {"name": "3DMark", "3d": True},
        "3dmarksteelnomad.exe": {"name": "3DMark Steel Nomad", "3d": True},
        "3dmarktimespy.exe": {"name": "3DMark Time Spy", "3d": True},
        "furmark.exe": {"name": "FurMark", "3d": True}, "furmark_gui.exe": {"name": "FurMark", "3d": True},
        "msi-kombustor-x64.exe": {"name": "Kombustor", "3d": True},
    },
    # Matched by path substring, before stress_exes: y-cruncher ships per-CPU binaries ("13-ZN4 ~ Kizuna.exe"),
    # CoreCycler runs the Prime95 copy from its own folder
    "stress_path_markers": {"\\corecycler": "CoreCycler", "\\y-cruncher": "y-cruncher"},
}


AGENT_GUIDE = Path(__file__).resolve().parent / "agent" / "AGENT.md"   # what "Zapytaj agenta" points an AI agent at


def cli_path() -> Path:
    """The `pm` CLI as a user's AI agent should call it: pm.exe next to the installed build, else pm.cmd."""
    return Path(sys.executable).with_name("pm.exe") if FROZEN else ROOT / "pm.cmd"


def module_argv(module, *args, console=False):
    """argv running `python -m module args`: the installed build's exe dispatches "-m" itself (perfmon/launcher.py).
    console: python.exe rather than pythonw.exe in a source checkout (a child whose output is read)."""
    exe = sys.executable
    if console and not FROZEN and exe.lower().endswith("pythonw.exe") and os.path.exists(exe[:-5] + ".exe"):
        exe = exe[:-5] + ".exe"
    return [exe, "-m", module, *map(str, args)]


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if FROZEN and k in CODE_PATHS:
                    continue
                if k in ("game_exes", "not_game_exes", "game_path_markers") and isinstance(v, list):
                    cfg[k] = list(dict.fromkeys(cfg[k] + [x.lower() for x in v]))
                elif k in ("stress_exes", "stress_path_markers") and isinstance(v, dict):
                    cfg[k] = {**cfg[k], **{x.lower(): y for x, y in v.items()}}
                else:
                    cfg[k] = v
        except Exception:
            pass
    return cfg


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps({
            "game_exes": [],
            "not_game_exes": [],
            "retention_days": 90,
        }, indent=2), encoding="utf-8")
