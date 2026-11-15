import json
import time
import unittest

from perfmon import notes, uiviews
from tests.test_bottleneck import new_db

DAY = 86400
CTX = {"adrenalin": "26.10.1", "os_build": "26200.1", "steam": {"appid": "730", "name": "Counter-Strike® 2", "buildid": "100"},
       "game_settings": {"normalized": {"resolution": "2560x1440", "fps_cap": 141}}}


def add(con, start, fps=140.0, low=95.0, minutes=30, exe="cs2.exe", ctx=CTX, reason="exit", frames=True):
    """A finished session the way the collector stores it: whole-session numbers plus the gameplay-only ones."""
    gp = {"seconds": minutes * 60, "usable": True, "fps_avg": fps, "fps_1low": low, "stutters": minutes,
          "stutters_per_min": 1.0}
    summ = {"frames": minutes * 60 * 100, "fps_avg": fps, "fps_1low": low, "gameplay": gp} if frames else {}
    cur = con.execute("INSERT INTO sessions(exe, start_ts, end_ts, end_reason, summary, context) VALUES (?,?,?,?,?,?)",
                      (exe, start, start + minutes * 60, reason, json.dumps(summ), json.dumps(ctx)))
    return cur.lastrowid


def history(con, now, n=5, **kw):
    for i in range(n, 0, -1):
        add(con, now - (i + 1) * DAY, **kw)


class GamesTest(unittest.TestCase):
    def setUp(self):
        uiviews._assessed.clear()

    def test_tiles_last_played_first_with_status(self):
        con, now = new_db(), time.time()
        history(con, now)
        add(con, now - DAY, fps=141.0)
        history(con, now, exe="cod.exe", ctx={"adrenalin": "26.10.1"})
        add(con, now - 3600, fps=100.0, low=60.0, exe="sp26-cod.exe", ctx={"adrenalin": "26.10.2"})
        add(con, now - 600, exe="launcher.exe", ctx={}, frames=False)   # no frames, clean exit: not a game played
        cod, cs = uiviews.games(con)
        self.assertEqual((cod["title"], cod["appid"], cod["sessions"]), ("sp26-cod", None, 6))   # one game, two exes
        self.assertEqual(cod["status"], {"kind": "slower", "cause": "driver"})
        self.assertEqual(cod["last"]["fps"], 100.0)
        self.assertEqual((cs["title"], cs["appid"], cs["status"]), ("Counter-Strike 2", "730", {"kind": "ok"}))

    def test_status_priorities(self):
        con, now = new_db(), time.time()
        add(con, now - 2 * DAY)
        self.assertEqual(uiviews.games(con)[0]["status"], {"kind": "unknown"})   # nothing to compare with
        crashed = add(con, now - DAY, reason="crash")
        st = uiviews.games(con)[0]["status"]
        self.assertEqual((st["kind"], st["ts"]), ("crash", now - DAY + 1800))
        con.execute("INSERT INTO experiments(name, exe, start_ts, status) VALUES ('limit 141', 'cs2.exe', ?, 'open')", (now,))
        self.assertEqual(uiviews.games(con)[0]["status"], {"kind": "test"})
        self.assertEqual(uiviews.games(con)[0]["last"]["id"], crashed)

    def test_frame_generation_shows_the_games_own_frames(self):
        con, now = new_db(), time.time()
        sid = add(con, now - DAY)
        con.execute("UPDATE sessions SET summary=? WHERE id=?", (json.dumps(
            {"frames": 1, "fps_avg": 180.0, "fps_1low": 120.0, "app_fps_avg": 92.0, "app_fps_1low": 61.0,
             "frame_gen_pct": 49.0}), sid))
        last = uiviews.games(con)[0]["last"]
        self.assertEqual((last["fps"], last["fps_1low"]), (92.0, 61.0))

    def test_game_page(self):
        con, now = new_db(), time.time()
        history(con, now)
        ctx = dict(CTX, adrenalin="26.10.2", steam=dict(CTX["steam"], buildid="101"))
        sid = add(con, now - DAY, fps=143.0, ctx=ctx)
        con.execute("INSERT INTO stutters(ts, session, ft_ms, median_ms, causes) VALUES (?,?,40,7,'[]')", (now - DAY + 5, sid))
        notes.add(con, "Wszystko w normie", "cs2.exe")
        g = uiviews.game(con, "CS2.exe")
        self.assertEqual((g["vs_usual"], g["baseline_sessions"], g["target_fps"]), (3, 5, None))
        self.assertEqual(g["settings"], CTX["game_settings"]["normalized"])
        self.assertEqual([(v["key"], v["value"], v["since_ts"]) for v in g["versions"]],
                         [("game", "build 101", now - DAY), ("driver", "26.10.2", now - DAY), ("os", "26200.1", None)])
        self.assertEqual(len(g["recent"]), uiviews.SESSIONS)
        self.assertEqual((g["recent"][0]["id"], g["recent"][0]["stutters"], g["recent"][1]["stutters"]), (sid, 1, 0))
        self.assertEqual(g["agent"]["note"]["text"], "Wszystko w normie")
        self.assertIn("error", uiviews.game(con, "nope.exe"))

    def test_target_fps_is_per_game(self):
        con, now = new_db(), time.time()
        add(con, now - DAY, exe="sp26-cod.exe", ctx={})
        self.assertEqual(uiviews.set_target(con, {"exe": "cod.exe", "fps": "120"}), {"target_fps": 120})
        self.assertEqual(uiviews.game(con, "sp26-cod.exe")["target_fps"], 120)
        uiviews.set_target(con, {"exe": "cod.exe", "fps": None})
        self.assertIsNone(uiviews.game(con, "cod.exe")["target_fps"])
        for body in ({"fps": 60}, {"exe": "cod.exe", "fps": 5000}, {"exe": "cod.exe", "fps": "abc"}):
            with self.assertRaises(ValueError):
                uiviews.set_target(con, body)

    def test_home_compares_finished_sessions_and_carries_the_agent_card(self):
        con, now = new_db(), time.time()
        history(con, now)
        add(con, now - DAY, fps=130.0)
        notes.add(con, "Zasilacz do sprawdzenia")
        o = uiviews.home(con)
        self.assertEqual(o["current"]["vs_usual"], -10)
        self.assertIsNone(o["recent"][-1]["vs_usual"])   # the oldest has no baseline
        self.assertEqual(o["agent"]["note"]["text"], "Zasilacz do sprawdzenia")


class ProblemsTest(unittest.TestCase):
    def test_rows_in_plain_groups_with_the_game_and_repeats_folded(self):
        con, now = new_db(), time.time()
        sid = add(con, now - 3 * DAY, minutes=60)
        start = now - 3 * DAY
        con.executemany("INSERT INTO samples(ts, game_session) VALUES (?,?)",
                        [(start + 100, sid), (start + 1500, sid), (start + 3000, sid), (now - DAY, None)])
        inc = [(start + 101, None, "gpu_driver", "Display 4101"), (start + 1501, None, "gpu_driver", "Display 4101"),
               (start + 3001, start + 3005, "ui_freeze", "UI"), (start + 3001, start + 3004, "dwm_stall", "DWM"),
               (start + 3 * 3600, None, "unclean_shutdown", "Kernel-Power 41"),      # logged at the next boot
               (start + 3 * 3600 + 60, None, "unclean_shutdown", "EventLog 6008"),   # the same reset
               (now - DAY + 5, None, "app_crash", "Crash aplikacji: Discord.exe"),
               (now - 3600, None, "app_crash", "Crash aplikacji: cs2.exe"),          # after its session closed
               (now - 1800, None, "vram_full", "VRAM")]                              # not a stability problem
        con.executemany("INSERT INTO incidents(ts, end_ts, kind, severity, title) VALUES (?,?,?,'warning',?)", inc)
        notes.add(con, "Reset wygląda na zasilanie")
        notes.add(con, "o grze", "cs2.exe")
        d = uiviews.problems(con, 14)
        rows = [(r["group"], r["severity"], r["count"], r["game"], r["app"]) for r in d["items"]]
        self.assertEqual(rows, [("crash", "serious", 1, "Counter-Strike 2", None), ("crash", "serious", 1, None, "Discord.exe"),
                                ("reset", "critical", 1, "Counter-Strike 2", None),
                                ("freeze", "warning", 1, "Counter-Strike 2", None),
                                ("gpu_driver", "critical", 2, "Counter-Strike 2", None)])
        self.assertEqual(d["items"][3]["duration_s"], 4.0)
        self.assertEqual(d["agent"]["note"]["text"], "Reset wygląda na zasilanie")   # the note about the whole PC


if __name__ == "__main__":
    unittest.main()
