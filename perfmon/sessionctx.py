"""Configuration context of game sessions, driven by the collector: system snapshot (daily + quick at each session
start), the game's own settings (at start and when its files change) and the DLLs loaded into the game.

Everything here observes; nothing changes a setting. Results land in sessions.context (what was in effect at the
start), sessions.summary (modules, mid-session settings changes), system_info, game_settings and incidents."""
import json
import logging
import threading
import time

from . import confdiff, diskinfo, gamemods, gamesettings, sysinfo, winq

log = logging.getLogger("perfmon.sessionctx")
GPU_MODE_KEYS = ("refresh", "res")   # Win32_VideoController mode: the starting game may have switched it


def quick_merge(old, new):
    """Session-start snapshot on top of the stored one: daily-only sections (disk health, drivers, displays) and
    values a provider could not read carry over (unread is not changed); GPU modes stay the desktop's."""
    out = {k: v for k, v in old.items() if k not in ("error", "snapshot") and (k not in new or new[k] is None)}
    out.update({k: v for k, v in new.items() if v is not None})
    desk = {g.get("name"): g for g in old.get("gpus") or [] if isinstance(g, dict)}
    for g in out.get("gpus") or []:
        if isinstance(g, dict) and g.get("name") in desk:
            d = desk[g["name"]]
            g.update({k: d.get(k) for k in GPU_MODE_KEYS if k in d})
            if g.get("bar_mb") is None and "bar_mb" in d:   # not read by the quick snapshot
                g["bar_mb"] = d.get("bar_mb")
    return out


class SessionContext:
    def __init__(self, col):
        self.col = col
        self.cfg = col.cfg
        self.snap_lock = threading.Lock()   # daily and session-start snapshots never diff against each other mid-way

    # ---------- system snapshot ----------
    def game_paths(self):
        rows = self.col.exec("SELECT path FROM sessions WHERE COALESCE(kind,'game')='game' AND path IS NOT NULL "
                             "GROUP BY lower(path) ORDER BY MAX(start_ts) DESC LIMIT 20").fetchall()
        return [r[0] for r in rows]

    def latest_snapshot_ts(self):
        r = self.col.exec("SELECT MAX(ts) FROM system_info").fetchone()
        return r[0] if r else None

    def sysinfo(self, quick=False, session=None, extra_paths=()):
        """Take a snapshot, store it when it is the daily one or something changed, raise config_change incidents.
        session: also point that session's context at the snapshot in effect."""
        with self.snap_lock:
            paths = list(dict.fromkeys([*extra_paths, *self.game_paths()]))
            gpu = self.col.hw.gpu.Name if getattr(self.col.hw, "gpu", None) is not None else None
            info = sysinfo.collect(quick=quick, game_paths=paths, gpu_name=gpu)
            prev = self.col.exec("SELECT ts, info FROM system_info ORDER BY ts DESC LIMIT 1").fetchone()
            old = json.loads(prev[1]) if prev else None
            if quick and old:
                info = quick_merge(old, info)
            changes = confdiff.diff(old, info) if old else []
            now = time.time()
            ts = prev[0] if prev else None
            if not quick or not old or changes or set(info) - set(old):
                self.col.exec("INSERT INTO system_info VALUES (?, ?)", (now, json.dumps(info, ensure_ascii=False)))
                ts = now
            if changes:
                self.col.incident("config_change", "info", confdiff.title(changes), confdiff.details(changes), ts=now)
            if old:
                for kind, sev, title, det in diskinfo.health_changes(old, info):
                    self.col.incident(kind, sev, title, det)
            if session is not None and ts is not None:
                self.col.merge_context(session, {"system_snapshot_ts": ts})
            log.info("system snapshot %s in %.2f s, %d changes", "quick" if quick else "full",
                     (info.get("snapshot") or {}).get("seconds") or 0, len(changes))
            return info

    # ---------- session start ----------
    def start_context(self, exe, path, now):
        """Context entries known synchronously at session start (game settings, snapshot currently in effect)."""
        out = {"system_snapshot_ts": self.latest_snapshot_ts()}
        try:
            snap = gamesettings.snapshot(exe, path, self.cfg)
            if snap is not None:
                with self.col.lock:
                    out["game_settings"] = gamesettings.store(self.col.con, exe, snap, now)
                    self.col.con.commit()
        except Exception as e:
            log.exception("game settings for %s", exe)
            out["game_settings"] = {"error": str(e)}
        return out

    def started(self, g, ctx):
        gs = ctx.get("game_settings") or {}
        g["settings"] = {"hash": gs.get("hash"), "normalized": gs.get("normalized"),
                         "stamp": gamesettings.file_stamp(gs), "next": g["start"] + self.cfg.get("game_settings_poll_s", 30),
                         "events": []}
        g["mods"] = {"next": g["start"] + self.cfg.get("game_modules_settle_s", 120), "last": {}, "events": [], "pids": None}
        threading.Thread(target=self._quick_snapshot, args=(g["session"], g["path"]), daemon=True).start()

    def _quick_snapshot(self, sid, path):
        try:
            self.sysinfo(quick=True, session=sid, extra_paths=[path] if path else ())
        except Exception:
            log.exception("session-start snapshot for session %s", sid)
        finally:
            winq.release()

    # ---------- while the game runs ----------
    def tick(self, now):
        for g in list(self.col.games.values()):
            if g.get("stress") or "settings" not in g:
                continue
            try:
                self._settings_tick(g, now)
                self._modules_tick(g, now)
            except Exception:
                log.exception("session context tick for session %s", g["session"])

    def _settings_tick(self, g, now):
        st = g["settings"]
        if now < st["next"]:
            return
        st["next"] = now + self.cfg.get("game_settings_poll_s", 30)
        if st["hash"] and gamesettings.file_stamp({"files": [{"path": p} for p, _ in st["stamp"]]}) == st["stamp"]:
            return   # files untouched: no re-read
        snap = gamesettings.snapshot(g["exe"], g["path"], self.cfg)
        if not snap or "error" in snap:
            st["next"] = now + 600   # no settings file (yet): look again rarely, this runs on the main loop
            return
        st["stamp"] = gamesettings.file_stamp(snap)
        if snap["hash"] == st["hash"]:
            return
        with self.col.lock:
            entry = gamesettings.store(self.col.con, g["exe"], snap, now)
            self.col.con.commit()
        changes = confdiff.diff(st["normalized"], snap["normalized"], volatile=set()) if st["normalized"] else []
        texts = [c["text"] for c in changes]
        raw = entry.get("changed") or {}
        if st["hash"] is not None and (texts or raw):   # first readable snapshot of the session = baseline
            st["events"].append({"ts": now, "changes": texts, "raw_changed": len(raw)})
            self.col.merge_summary(g["session"], {"settings_changes": st["events"][-20:]})
            title = confdiff.title(changes, f"Zmiana ustawień gry {g['exe']}") if changes else \
                f"Zmiana ustawień gry {g['exe']} ({len(raw)} kluczy w pliku)"
            self.col.incident("game_settings_change", "info", title,
                              {"session": g["session"], "exe": g["exe"], "changes": texts, "raw_changed": raw}, ts=now)
        st["hash"], st["normalized"] = snap["hash"], snap["normalized"]

    def _modules_tick(self, g, now):
        if not self.cfg.get("game_modules", True):
            return
        m = g["mods"]
        pids = {g["pid"]: g["exe"], **g["children"]}
        if m["pids"] is not None and set(pids) != set(m["pids"]) and now >= g["start"] + self.cfg.get("game_modules_settle_s", 120):
            m["next"] = min(m["next"], now + 30)   # a new render child (CoD sp26-cod.exe): scan once it loaded
        if now < m["next"]:
            return
        m["next"] = now + self.cfg.get("game_modules_poll_s", 600)
        m["pids"] = pids
        scans = {name: gamemods.scan(pid) for pid, name in pids.items()}
        texts = [f"{name}: {t}" for name, s in scans.items() for t in gamemods.changed(m["last"].get(name), s)]
        first = not m["last"]
        m["last"] = scans
        summ = {"modules": {"ts": now, "procs": scans}}
        if texts and not first:
            m["events"].append({"ts": now, "changes": texts})
            summ["modules_changes"] = m["events"][-20:]
            log.info("game modules changed session=%s: %s", g["session"], "; ".join(texts))
        self.col.merge_summary(g["session"], summ)
