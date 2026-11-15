"""`pm note`: what the user's AI agent concluded, in a sentence or two, for the window to show.

The window keeps only what a person acts on; the analysis is the agent's job. A note is about one game (`exe`) or
about the whole PC. The card under it carries the open `pm test`, so "what are we testing now" needs no note."""
import time

from perfmon import experiments
from perfmon.regression import game_key

MAX_LEN = 400     # a card, not a report
KEEP = 200


def _row(r):
    return {"id": r["id"], "ts": r["ts"], "exe": r["exe"], "text": r["text"]}


def add(con, text, exe=None, now=None):
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("Podaj treść notatki, np. pm note add \"Przycięcia robi X w tle\".")
    if len(text) > MAX_LEN:
        raise ValueError(f"Notatka ma {len(text)} znaków, limit to {MAX_LEN}: jedno, dwa zdania wniosku.")
    cur = con.execute("INSERT INTO agent_notes(ts, exe, text) VALUES (?,?,?)",
                      (now or time.time(), (exe or "").strip().lower() or None, text))
    con.execute("DELETE FROM agent_notes WHERE id <= ?", (cur.lastrowid - KEEP,))
    con.commit()
    return cur.lastrowid


def remove(con, nid=None):
    """Delete one note, or all of them. Returns how many went."""
    cur = (con.execute("DELETE FROM agent_notes WHERE id=?", (int(nid),)) if nid is not None
           else con.execute("DELETE FROM agent_notes"))
    con.commit()
    return cur.rowcount


def list_all(con, limit=20):
    return [_row(r) for r in con.execute("SELECT * FROM agent_notes ORDER BY id DESC LIMIT ?", (int(limit),))]


def latest(con, exe=None, general=False):
    """The newest note: of one game (`exe`), about the whole PC (`general`), or of any kind."""
    if general:
        r = con.execute("SELECT * FROM agent_notes WHERE exe IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return _row(r) if r else None
    for r in con.execute("SELECT * FROM agent_notes ORDER BY id DESC"):
        if exe is None or (r["exe"] and game_key(r["exe"]) == game_key(exe)):
            return _row(r)
    return None


def open_test(con, exe=None):
    """The running `pm test` (of `exe` when given): its name and how far the "after" side is."""
    o = experiments._open(con)
    if not o or (exe is not None and game_key(o["exe"]) != game_key(exe)):
        return None
    r = experiments.evaluate(con, o)
    return {"id": r["id"], "name": r["name"], "exe": r["exe"], "sessions": r["after"].get("sessions") or 0,
            "need": experiments.MIN_SESSIONS, "missing": r["missing"], "verdict": r["verdict"]}


def card(con, exe=None, general=False):
    """The window's agent card: {"note", "test"}, either may be None."""
    return {"note": latest(con, exe, general), "test": None if general else open_test(con, exe)}
