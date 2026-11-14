"""Updates of the installed build from GitHub Releases: check, download the installer, verify it, run it.

The release (packaging, .github/workflows/release.yml) carries PerfMonitor-Setup-<version>.exe and SHA256SUMS.txt.
The installer runs silently and elevated (one UAC prompt); it stops the collector and the UI, replaces the program
folder and starts both again. A source checkout is updated with git, so it never offers an update."""
import ctypes
import hashlib
import json
import logging
import re
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__, config

log = logging.getLogger(__name__)

REPO = "noyex/PerfMonitor"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY_S = 6 * 3600
SETUP_RE = re.compile(r"^PerfMonitor-Setup-[\d.]+\.exe$")
INSTALLER_ARGS = "/SILENT /SUPPRESSMSGBOXES /NORESTART"
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "PerfMonitor-update"

_lock = threading.Lock()
_cache = {"ts": 0.0, "release": None}
_job = {"state": "idle", "progress": 0.0, "error": None}   # idle | downloading | installing | error


def parse_version(v):
    """'v1.2.3' -> (1, 2, 3); anything unparsable sorts below every real version."""
    try:
        return tuple(int(x) for x in str(v).lstrip("vV").split("."))
    except ValueError:
        return ()


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": f"PerfMonitor/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest_release(force=False):
    """The newest published release ({"version", "setup", "sums"} asset URLs), cached for CHECK_EVERY_S.
    None when GitHub cannot be reached or the release has no installer."""
    with _lock:
        if not force and time.time() - _cache["ts"] < CHECK_EVERY_S:
            return _cache["release"]
    rel = None
    try:
        with _get(LATEST_URL) as r:
            data = json.load(r)
        assets = {a["name"]: a for a in data.get("assets") or []}
        setup = next((a for n, a in assets.items() if SETUP_RE.match(n)), None)
        if setup and "SHA256SUMS.txt" in assets:
            rel = {"version": data["tag_name"].lstrip("vV"), "url": data.get("html_url"),
                   "setup": setup["browser_download_url"], "setup_name": setup["name"], "size": setup.get("size"),
                   "sums": assets["SHA256SUMS.txt"]["browser_download_url"]}
    except Exception as e:
        log.info("update check failed: %s", e)
    with _lock:
        _cache.update(ts=time.time(), release=rel)
    return rel


def status(force=False):
    """GET /api/update: {"current", "available": newer version or None, "state", "progress", "error"}."""
    rel = latest_release(force) if config.FROZEN else None
    newer = rel and parse_version(rel["version"]) > parse_version(__version__)
    with _lock:
        return {"current": __version__, "available": rel["version"] if newer else None,
                "url": rel["url"] if newer else None, **_job}


def _set(**kw):
    with _lock:
        _job.update(kw)


def _expected_sha(sums_url, name):
    with _get(sums_url) as r:
        for line in r.read().decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                return parts[0].lower()
    raise ValueError(f"brak sumy kontrolnej dla {name}")


def download(rel, dest_dir=DOWNLOAD_DIR):
    """Downloads the installer to dest_dir and checks its SHA-256; returns its path."""
    expected = _expected_sha(rel["sums"], rel["setup_name"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / rel["setup_name"]
    part = path.with_suffix(".part")
    h = hashlib.sha256()
    with _get(rel["setup"], timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or rel.get("size") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total:
                _set(progress=min(done / total, 1.0))
    if h.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise ValueError("suma kontrolna pobranego instalatora się nie zgadza")
    part.replace(path)
    return path


def run_installer(path):
    """Starts the installer elevated (ShellExecute "runas": CreateProcess cannot raise a UAC prompt)."""
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(path), INSTALLER_ARGS, None, 1)
    if rc <= 32:
        raise OSError("instalator nie został uruchomiony (odmowa uprawnień administratora?)")


def _install():
    try:
        rel = latest_release(force=True)
        if not rel or parse_version(rel["version"]) <= parse_version(__version__):
            raise ValueError("brak nowszej wersji")
        path = download(rel)
        _set(state="installing", progress=1.0)
        run_installer(path)   # the installer closes this process and starts the new version
    except Exception as e:
        log.warning("update failed: %s", e)
        _set(state="error", error=str(e))


def start_install():
    """POST /api/update: download and run the installer in the background; the UI follows status()."""
    if not config.FROZEN:
        raise ValueError("wersja ze źródeł aktualizuje się przez git")
    with _lock:
        if _job["state"] in ("downloading", "installing"):
            return dict(_job)
        _job.update(state="downloading", progress=0.0, error=None)
    threading.Thread(target=_install, daemon=True).start()
    return status()
