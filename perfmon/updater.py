"""Updates of the installed build from GitHub Releases: check, download the installer, verify it, run it.

The release (packaging, .github/workflows/release.yml) carries NXFrame-Setup-<version>.exe and SHA256SUMS.txt.
The installer runs silently and elevated (one UAC prompt); it stops the collector and the window, replaces the program
folder and starts both again. A source checkout is updated with git, so it never offers an update."""
import contextlib
import ctypes
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__, config

log = logging.getLogger(__name__)

REPO = "noyex/PerfMonitor"   # the repository kept its name when the app became NX Frame
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_PREFIX = f"https://github.com/{REPO}/releases/download/"   # assets are taken from this repository only
CHECK_EVERY_S = 6 * 3600
RETRY_S = 600          # after a failed check (offline at logon): sooner than CHECK_EVERY_S
FORCE_MIN_S = 60       # ?force=1 is open to every local page: GitHub allows 60 anonymous calls an hour
LAUNCH_WAIT_S = 300    # the UAC prompt waits for the user this long
INSTALL_WAIT_S = 90    # the installer closes this process within seconds; still alive after this = it failed
SETUP_RE = re.compile(r"NXFrame-Setup-\d+(\.\d+)*\.exe")
SHA_RE = re.compile(r"[0-9a-f]{64}")
INSTALLER_ARGS = "/SILENT /SUPPRESSMSGBOXES /NORESTART"
DOWNLOAD_PREFIX = "NXFrame-update-"   # folders in %TEMP%, one per download
RC_CHECKSUM, RC_NOT_STARTED = 2, 3    # exit codes of the launcher process (launch)
LAUNCH_ERRORS = {RC_CHECKSUM: "suma kontrolna pobranego instalatora się nie zgadza",
                 RC_NOT_STARTED: "instalator nie został uruchomiony (odmowa uprawnień administratora?)"}

_lock = threading.Lock()
_cache = {"ts": 0.0, "release": None, "ok": False}   # ok: the last check reached GitHub
_job = {"state": "idle", "progress": 0.0, "error": None}   # idle | downloading | installing | error


def parse_version(v):
    """'v1.2.3' -> (1, 2, 3); anything unparsable sorts below every real version."""
    try:
        return tuple(int(x) for x in str(v).lstrip("vV").split("."))
    except ValueError:
        return ()


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": f"NXFrame/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def _release(data):
    """{"version", "setup", "sums", ...} of a GitHub release, or None when it carries no installer of ours."""
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets") or []
              if str(a.get("browser_download_url")).startswith(ASSET_PREFIX)}
    setup = next((n for n in assets if SETUP_RE.fullmatch(n)), None)
    if not setup or "SHA256SUMS.txt" not in assets:
        return None
    size = next(a.get("size") for a in data["assets"] if a["name"] == setup)
    return {"version": data["tag_name"].lstrip("vV"), "url": data.get("html_url"), "setup": assets[setup],
            "setup_name": setup, "size": size, "sums": assets["SHA256SUMS.txt"]}


def latest_release(force=False):
    """The newest published release, cached for CHECK_EVERY_S (RETRY_S after a failed check, FORCE_MIN_S when
    forced). None when the release has no installer; a failed check keeps the release known so far."""
    now = time.time()
    with _lock:
        ttl = FORCE_MIN_S if force else CHECK_EVERY_S if _cache["ok"] else RETRY_S
        if now - _cache["ts"] < ttl:
            return _cache["release"]
        _cache["ts"] = now   # claimed before the request: parallel callers get the cached answer
    try:
        with _get(LATEST_URL) as r:
            rel = _release(json.load(r))
    except Exception as e:
        log.info("update check failed: %s", e)
        with _lock:
            _cache["ok"] = False
            return _cache["release"]
    with _lock:
        _cache.update(release=rel, ok=True)
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
            if len(parts) == 2 and parts[1].lstrip("*") == name and SHA_RE.fullmatch(parts[0].lower()):
                return parts[0].lower()
    raise ValueError(f"brak sumy kontrolnej dla {name}")


def _sweep():
    """Removes the folders of earlier downloads (rmtree does not follow a link planted under that name)."""
    for d in Path(tempfile.gettempdir()).glob(DOWNLOAD_PREFIX + "*"):
        shutil.rmtree(d, ignore_errors=True)


def download(rel, dest_dir=None):
    """Downloads the installer into a folder of its own (a new one with a random name, unless dest_dir is given);
    returns its path and the SHA-256 it must have (see open_verified)."""
    expected = _expected_sha(rel["sums"], rel["setup_name"])
    if dest_dir is None:
        _sweep()
        dest_dir = tempfile.mkdtemp(prefix=DOWNLOAD_PREFIX)
    path = Path(dest_dir) / rel["setup_name"]
    with _get(rel["setup"], timeout=60) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or rel.get("size") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            done += len(chunk)
            if total:
                _set(progress=min(done / total, 1.0))
    return path, expected


@contextlib.contextmanager
def open_verified(path, expected):
    """Holds the installer open, shared for reading only: nothing can modify, replace or rename it, nor a folder
    above it. Checks its SHA-256 through that handle and yields the path the handle really points at (a link
    planted on the way to `path` is resolved here, not again at launch). What runs elevated inside the block is
    exactly what was verified."""
    import win32file
    handle = win32file.CreateFile(str(path), win32file.GENERIC_READ, win32file.FILE_SHARE_READ, None,
                                  win32file.OPEN_EXISTING, 0, None)
    try:
        h = hashlib.sha256()
        while True:
            _, chunk = win32file.ReadFile(handle, 1 << 16)
            if not chunk:
                break
            h.update(chunk)
        ok = h.hexdigest() == expected
        if ok:
            real = win32file.GetFinalPathNameByHandle(handle, 0)   # \\?\C:\... (ShellExecute wants it plain)
            yield real[4:] if real.startswith("\\\\?\\") and not real.startswith("\\\\?\\UNC\\") else real
    finally:
        handle.Close()
    if not ok:
        Path(path).unlink(missing_ok=True)
        raise ValueError(LAUNCH_ERRORS[RC_CHECKSUM])


def run_installer(path):
    """Starts the installer elevated (ShellExecute "runas": CreateProcess cannot raise a UAC prompt). Returns once
    the user accepted the prompt and the installer runs."""
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(path), INSTALLER_ARGS, None, 1)
    if rc <= 32:
        raise OSError(LAUNCH_ERRORS[RC_NOT_STARTED])


def launch(path, expected):
    """`-m perfmon.updater launch <path> <sha256>`: verify the installer and start it; the exit code says how it
    went. A process of its own, see _launch_detached."""
    try:
        with open_verified(Path(path), expected) as real:
            run_installer(real)
    except ValueError:
        return RC_CHECKSUM
    except Exception:   # the file cannot be opened, UAC declined, ...
        return RC_NOT_STARTED
    return 0


def _launch_detached(path, expected):
    """Runs launch() in another process and returns its exit code. The window's API server lives in a job object
    that is killed with the window, and a process started from inside a job may land in that job too: an installer
    there would die the moment it closes the window it is about to replace. The launcher leaves the job first (the
    window's job allows it, src-tauri/src/lib.rs); a job that forbids it, or no job at all, runs it as it is."""
    argv = config.module_argv("perfmon.updater", "launch", path, expected)
    for flags in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            return subprocess.run(argv, creationflags=subprocess.CREATE_NO_WINDOW | flags,
                                  timeout=LAUNCH_WAIT_S).returncode
        except PermissionError:
            continue
        except subprocess.TimeoutExpired:
            break
    return RC_NOT_STARTED


def _install():
    try:
        rel = latest_release()
        if not rel or parse_version(rel["version"]) <= parse_version(__version__):
            raise ValueError("brak nowszej wersji")
        path, expected = download(rel)
        _set(state="installing", progress=1.0)
        rc = _launch_detached(path, expected)
        if rc:
            raise RuntimeError(LAUNCH_ERRORS.get(rc, f"instalator nie został uruchomiony (kod {rc})"))
        time.sleep(INSTALL_WAIT_S)   # the installer closes this process and starts the new version
        raise RuntimeError("instalacja nie powiodła się albo została przerwana")   # still here
    except Exception as e:
        log.warning("update failed: %s", e)
        _set(state="error", progress=0.0, error=str(e))


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


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "launch":
        sys.exit(launch(sys.argv[2], sys.argv[3]))
    sys.exit(1)
