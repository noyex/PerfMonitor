"""Paths and settings. Overridable via data/config.json."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PERFMON_DATA_DIR") or ROOT / "data")   # env override: tests
DB_PATH = DATA_DIR / "perf.db"
LOG_PATH = DATA_DIR / "collector.log"
CONFIG_PATH = DATA_DIR / "config.json"

_WINGET = Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"))

DEFAULTS = {
    "sample_interval_s": 1.0,
    "proc_interval_s": 5.0,
    "event_poll_s": 30.0,
    "retention_days": 90,
    "detail_retention_days": 30,        # hw_detail (UV/OC telemetry); per-session aggregates are kept forever
    "detail_interval_s": 10.0,          # hw_detail outside game/stress sessions (1 s inside them)
    "db_guard_gb": 2.0,                 # above this perf.db size: warning incident + detail retention cut to 14 days
    "apic_ids_per_core": 2,             # WHEA APIC ID -> core (7700X with SMT: 2 threads per core)
    "gpu_od_poll_s": 600,               # Adrenalin tuning readout via ADL; 0 = never touch ADL
    "ui_port": 8765,
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


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
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
