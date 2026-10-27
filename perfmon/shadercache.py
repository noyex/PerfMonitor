"""Driver / Steam shader cache size during a game session.

Growth of the cache while a stutter happens = shader compilation. ShaderWatch re-measures the cache
directories every few seconds on a background thread; a scan that exceeds its time budget yields NULL."""
import logging
import os
import threading
import time

from perfmon import gamectx

log = logging.getLogger("perfmon.shadercache")

DEFAULT_DIRS = [
    r"%LOCALAPPDATA%\AMD\DxCache",
    r"%LOCALAPPDATA%\AMD\DxcCache",
    r"%LOCALAPPDATA%\AMD\VkCache",
    r"%LOCALAPPDATA%\AMD\OglCache",
    r"%LOCALAPPDATA%\AMD\DX9Cache",
    "{steam}",   # steamapps\shadercache\<appid> of the current game
]
_COMMON = "\\steamapps\\common\\"


def _install_root(exe_path):
    """(steamapps dir, install root of the game) or None when the exe is not in a Steam library."""
    p = os.path.normpath(exe_path or "")
    i = p.lower().find(_COMMON)
    if i < 0:
        return None
    rest = p[i + len(_COMMON):].split("\\")[0]
    if not rest:
        return None
    return p[: i + len("\\steamapps")], p[: i + len(_COMMON)] + rest


def steam_appid(exe_path):
    """Steam appid string: steam_appid.txt next to the exe (or up to the install root), else appmanifest."""
    if not exe_path:
        return None
    d = os.path.dirname(os.path.normpath(exe_path))
    root = _install_root(exe_path)
    stop = root[1].lower() if root else d.lower()
    while True:
        try:
            with open(os.path.join(d, "steam_appid.txt"), encoding="utf-8", errors="replace") as f:
                v = f.read().strip()
            if v.isdigit():
                return v
        except OSError:
            pass
        parent = os.path.dirname(d)
        if d.lower() == stop or parent == d:
            break
        d = parent
    info = gamectx.steam_info(exe_path)
    return (info or {}).get("appid") or None


def steam_shader_dir(exe_path):
    """steamapps\\shadercache\\<appid> of a Steam-library game, only if it exists."""
    root = _install_root(exe_path)
    if not root:
        return None
    appid = steam_appid(exe_path)
    if not appid:
        return None
    d = os.path.join(root[0], "shadercache", appid)
    return d if os.path.isdir(d) else None


def resolve_dirs(patterns, exe_path=None):
    """Expand env vars and {steam}; keep existing directories, deduplicated."""
    out, seen = [], set()
    for pat in patterns or []:
        if "{steam}" in pat:
            d = steam_shader_dir(exe_path)
            if not d:
                continue
            pat = pat.replace("{steam}", d)
        d = os.path.normpath(os.path.expandvars(pat))
        key = os.path.normcase(d)
        if key in seen or not os.path.isdir(d):
            continue
        seen.add(key)
        out.append(d)
    return out


def dir_size(paths, budget_s=0.2):
    """(bytes, files) summed recursively over paths; (None, None) when budget_s of wall-clock time ran out.

    Symlinks and junctions are not followed; unreadable entries are skipped."""
    deadline = time.monotonic() + budget_s
    total = files = 0
    stack = list(paths)
    while stack:
        if time.monotonic() >= deadline:
            return None, None
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            while True:
                try:
                    e = next(it)
                except StopIteration:
                    break
                except OSError:
                    break
                try:
                    if e.is_symlink() or _is_junction(e):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    continue
        if time.monotonic() >= deadline and stack:
            return None, None
    return total, files


def _is_junction(entry):
    try:
        return entry.is_junction()   # Python 3.12+
    except (OSError, AttributeError):
        return False


class ShaderWatch:
    """Background thread: while a game is set, measures its cache dirs every interval_s (first scan at once)."""

    def __init__(self, patterns, interval_s=10, budget_s=0.2):
        self.patterns, self.interval_s, self.budget_s = list(patterns), interval_s, budget_s
        self._lock = threading.Lock()
        self._wake = threading.Event()    # set on set_game/stop: re-evaluate state now
        self._stop = threading.Event()
        self._dirs, self._gen, self._warned = [], 0, False
        self._latest = (None, None)
        self._t = threading.Thread(target=self._run, name="shadercache", daemon=True)
        self._t.start()

    def set_game(self, exe_path):
        """Start scanning the cache dirs of this game; None stops scanning (the thread stays)."""
        dirs = resolve_dirs(self.patterns, exe_path) if exe_path else []
        with self._lock:
            self._dirs, self._warned = dirs, False
            self._gen += 1
            self._latest = (None, None)
        self._wake.set()

    def latest(self):
        """(ts, bytes) of the last scan; bytes is None if that scan hit the time budget; (None, None) before any."""
        with self._lock:
            return self._latest

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._t is not threading.current_thread():
            self._t.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                dirs, gen = list(self._dirs), self._gen
            if not dirs:
                self._wake.wait()
                self._wake.clear()
                continue
            size, _ = dir_size(dirs, self.budget_s)
            with self._lock:
                if gen == self._gen:   # the game changed mid-scan: drop the stale result
                    self._latest = (time.time(), size)
                    warn = size is None and not self._warned
                    self._warned = self._warned or warn
                else:
                    warn = False
            if warn:
                log.warning("shader cache scan exceeded %.2fs budget (%d dirs); recording NULL", self.budget_s, len(dirs))
            self._wake.wait(self.interval_s)
            self._wake.clear()
