"""User marks: `pm mark ["text"]` or a global hotkey while playing -> `user_mark` incident with context.

The CLI runs without admin rights and only appends a request line to data/mark.request; the collector
turns it into an incident using the time from the request (not the time it was picked up)."""
import ctypes
import json
import logging
import threading
import time
from ctypes import wintypes

from . import analyze as an
from . import config

log = logging.getLogger("perfmon.marks")

MARK_FLAG = config.DATA_DIR / "mark.request"
WINDOW_S = 30   # report context around a mark: [-WINDOW_S, +WINDOW_S]


# ---------- request (CLI side) ----------
def request(text=None, ts=None, source="cli"):
    """Append a mark request for the collector; returns its timestamp."""
    ts = time.time() if ts is None else float(ts)
    config.ensure_dirs()
    with open(MARK_FLAG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "text": text or None, "source": source}, ensure_ascii=False) + "\n")
    return ts


def take_requests():
    """Pending requests from the flag file (the file is consumed)."""
    if not MARK_FLAG.exists():
        return []
    tmp = MARK_FLAG.with_suffix(".taken")
    try:
        MARK_FLAG.replace(tmp)   # atomic: a `pm mark` racing with us starts a new file
        lines = tmp.read_text(encoding="utf-8", errors="replace").splitlines()
        tmp.unlink(missing_ok=True)
    except OSError as e:
        log.warning("mark.request: %s", e)
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
            out.append({"ts": float(r["ts"]), "text": r.get("text"), "source": r.get("source") or "cli"})
        except Exception:
            continue
    return out


def context(con, ts, fg_exe=None, session=None):
    """Incident details at the moment of the mark: the last sample / frame second at or before ts."""
    s = an._one(con, "SELECT cpu_total, gpu_load, ram_pct, disk_lat_ms, dwm_ms, ui_ms, fg_exe, game_session "
                     "FROM samples WHERE ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ts + 1, ts - 10)) or {}
    session = session or s.get("game_session")
    fps = None
    if session:
        f = an._one(con, "SELECT fps FROM frames WHERE session=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1",
                    (session, ts + 1, ts - 5))
        fps = an._r(f["fps"]) if f else None
    return {"fg_exe": fg_exe or s.get("fg_exe"), "game_session": session,
            "last_sample": {"fps": fps, "cpu": an._r(s.get("cpu_total")), "gpu": an._r(s.get("gpu_load")),
                            "ram_pct": an._r(s.get("ram_pct")), "disk_lat_ms": an._r(s.get("disk_lat_ms")),
                            "dwm_ms": an._r(s.get("dwm_ms")), "ui_ms": an._r(s.get("ui_ms"))}}


def title(text):
    return f"Znacznik: {text}" if text else "Znacznik użytkownika"


# ---------- report side ----------
def report(con, t0, t1, game_exe=None):
    """Marks in [t0, t1] with what happened in the +-WINDOW_S around each of them."""
    out = []
    for m in an.incidents_between(con, t0, t1, kinds="user_mark"):
        d = m["details"] if isinstance(m["details"], dict) else {}
        a, b = m["ts"] - WINDOW_S, m["ts"] + WINDOW_S
        fr = an._one(con, "SELECT MIN(fps) AS fps_min, MAX(ft_max) AS ft_max FROM frames WHERE ts BETWEEN ? AND ?",
                     (a, b)) or {}
        sm = an._one(con, "SELECT MAX(dwm_ms) AS dwm, MAX(ui_ms) AS ui, MAX(disk_lat_ms) AS disk FROM samples "
                          "WHERE ts BETWEEN ? AND ?", (a, b)) or {}
        n_st = con.execute("SELECT COUNT(*) FROM stutters WHERE ts BETWEEN ? AND ?", (a, b)).fetchone()[0]
        skip = an.NOT_BACKGROUND | {(game_exe or d.get("fg_exe") or "").lower()}
        bg = [r for r in an._rows(con, "SELECT name, ROUND(MAX(cpu_pct),1) AS cpu_max FROM procs WHERE kind='cpu' "
                                       "AND ts BETWEEN ? AND ? GROUP BY name ORDER BY MAX(cpu_pct) DESC LIMIT 8", (a, b))
              if (r["name"] or "").lower() not in skip and (r["cpu_max"] or 0) >= 3][:3]
        ev = [f"{e['provider']} {e['event_id']}" for e in an._rows(
            con, "SELECT provider, event_id FROM events WHERE ts BETWEEN ? AND ? AND level<=3 ORDER BY ts LIMIT 5", (a, b))]
        out.append({"ts": m["ts"], "time": m["time"], "text": d.get("text"), "source": d.get("source"),
                    "context": {"fps_min": an._r(fr.get("fps_min")), "ft_max": an._r(fr.get("ft_max")),
                                "stutters": n_st, "dwm_ms_max": an._r(sm.get("dwm")), "ui_ms_max": an._r(sm.get("ui")),
                                "disk_lat_max": an._r(sm.get("disk")), "top_bg_procs": bg, "events": ev}})
    return out


# ---------- global hotkey (collector side) ----------
MOD = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
NAMED_VK = {"pause": 0x13, "scrolllock": 0x91, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
            "pagedown": 0x22, "space": 0x20, "printscreen": 0x2C}


def parse_hotkey(spec):
    """'ctrl+alt+l' -> (modifiers, vk); None if empty or not understood."""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        return None
    mods, key = 0, parts[-1]
    for p in parts[:-1]:
        if p not in MOD:
            return None
        mods |= MOD[p]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key[0] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in NAMED_VK:
        vk = NAMED_VK[key]
    else:
        return None
    return mods, vk


class Hotkey:
    """RegisterHotKey + GetMessage loop on its own thread (the hotkey belongs to the registering thread)."""

    def __init__(self, spec, on_press, beep=True):
        self.spec, self.on_press, self.beep = spec, on_press, beep
        self.thread_id = None
        self.ok = False
        parsed = parse_hotkey(spec)
        if not parsed:
            if spec:
                log.warning("hotkey_mark %r not understood (e.g. ctrl+alt+l)", spec)
            return
        self.mods, self.vk = parsed
        self.thread = threading.Thread(target=self._run, daemon=True, name="hotkey")
        self.thread.start()

    def _run(self):
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        self.thread_id = k32.GetCurrentThreadId()
        if not u32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk):
            log.warning("hotkey %s not registered (used by another program?), error %s",
                        self.spec, ctypes.GetLastError())
            return
        self.ok = True
        log.info("hotkey %s registered for user marks", self.spec)
        msg = wintypes.MSG()
        try:
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.on_press(time.time())
                        if self.beep:
                            import winsound
                            winsound.Beep(1200, 70)
                    except Exception:
                        log.exception("hotkey handler")
        finally:
            u32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
