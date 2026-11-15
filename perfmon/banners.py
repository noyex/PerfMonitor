"""Steam banner (header.jpg, 460x215) of a game for its tile in the window.

Looked up once per game and kept in DATA_DIR/banners: first in the Steam client's own library cache on this PC, then
on Steam's public CDN (config "steam_banners": false turns the download off). A game without one gets a text tile."""
import glob
import os
import time
import urllib.error
import urllib.request

from perfmon import config

try:
    import winreg
except ImportError:   # not Windows: tests
    winreg = None

CDN = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
MIN_BYTES = 4000              # the CDN answers unknown ids with a tiny placeholder
RETRY_S = 7 * 86400           # a miss is asked again after a week
TIMEOUT_S = 5


def _steam_dirs():
    dirs = [r"C:\Program Files (x86)\Steam"]
    if winreg:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                dirs.insert(0, winreg.QueryValueEx(k, "SteamPath")[0])
        except OSError:
            pass
    return dirs


def _jpeg(data):
    return data if data and len(data) >= MIN_BYTES and data[:2] == b"\xff\xd8" else None


def _local(appid):
    """The 460x215 header from the Steam client's library cache. Its layout changed over the years: today
    <appid>/<hash>/library_header.jpg, before that <appid>/header.jpg and <appid>_header.jpg."""
    for root in _steam_dirs():
        cache = os.path.join(root, "appcache", "librarycache")
        for pat in (os.path.join(appid, "*", "library_header.jpg"), os.path.join(appid, "library_header.jpg"),
                    os.path.join(appid, "*", "header.jpg"), os.path.join(appid, "header.jpg"), f"{appid}_header.jpg"):
            for path in glob.glob(os.path.join(cache, pat)):
                try:
                    with open(path, "rb") as f:
                        data = _jpeg(f.read())
                except OSError:
                    continue
                if data:
                    return data
    return None


def _download(appid):
    """JPEG bytes; b"" when Steam has no banner for this id; None when it could not be asked (offline)."""
    try:
        req = urllib.request.Request(CDN.format(appid=appid), headers={"User-Agent": "NXFrame"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return _jpeg(r.read(2_000_000)) or b""
    except urllib.error.HTTPError as e:
        return b"" if e.code == 404 else None   # 429 / 5xx say nothing about the banner
    except (OSError, ValueError):
        return None


def get(appid):
    """JPEG bytes of the banner, None when the game has none (or it cannot be fetched right now)."""
    appid = str(appid or "")
    if not appid.isdigit():
        return None
    folder = config.DATA_DIR / "banners"
    path, miss = folder / f"{appid}.jpg", folder / f"{appid}.none"
    try:
        return path.read_bytes()
    except OSError:
        pass
    try:
        if time.time() - miss.stat().st_mtime < RETRY_S:
            return None
    except OSError:
        pass
    data = _local(appid)
    if data is None and config.load().get("steam_banners", True):
        data = _download(appid)
    if data is None:
        return None   # nothing local and Steam not asked or not reachable: try again next time
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (path if data else miss).write_bytes(data)
    except OSError:
        pass
    return data or None
