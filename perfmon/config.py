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
    "ui_port": 8765,
    "hotkey_mark": "ctrl+alt+l",   # global shortcut for `pm mark` while playing; "" = off
    "hotkey_beep": True,           # short beep confirming the mark
    "gpu_proc_interval_s": 5.0,    # GPU load / VRAM per process and the game process sampler
    # Shader caches measured during a game (growth at a stutter = shader compilation); "{steam}" =
    # steamapps\shadercache\<appid> of the game. Same list as shadercache.DEFAULT_DIRS (importing it here is a cycle).
    "shader_cache_dirs": [r"%LOCALAPPDATA%\AMD\DxCache", r"%LOCALAPPDATA%\AMD\DxcCache", r"%LOCALAPPDATA%\AMD\VkCache",
                          r"%LOCALAPPDATA%\AMD\OglCache", r"%LOCALAPPDATA%\AMD\DX9Cache", "{steam}"],
    "shader_scan_interval_s": 10,
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
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k in ("game_exes", "not_game_exes", "game_path_markers") and isinstance(v, list):
                    cfg[k] = list(dict.fromkeys(cfg[k] + [x.lower() for x in v]))
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
