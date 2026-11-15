import unittest
from unittest import mock

from perfmon import cli, notes
from tests.test_bottleneck import new_db
from tests.test_uiviews import add


class NotesTest(unittest.TestCase):
    def setUp(self):
        notes._tests.clear()

    def test_latest_by_scope(self):
        con = new_db()
        notes.add(con, "Zasilacz do sprawdzenia", now=100)
        notes.add(con, "  W tle działa   AorusLcdService ", "COD.exe", now=200)
        notes.add(con, "CS2 bez uwag", "cs2.exe", now=300)
        self.assertEqual(notes.latest(con)["text"], "CS2 bez uwag")
        self.assertEqual(notes.latest(con, "cod.exe")["text"], "W tle działa AorusLcdService")
        self.assertEqual(notes.latest(con, "sp26-cod.exe")["exe"], "cod.exe")   # launcher variants are one game
        self.assertEqual(notes.latest(con, general=True)["text"], "Zasilacz do sprawdzenia")
        self.assertIsNone(notes.latest(con, "other.exe"))

    def test_rejects_empty_and_long_text(self):
        con = new_db()
        for text in ("  ", "x" * (notes.MAX_LEN + 1)):
            with self.assertRaises(ValueError):
                notes.add(con, text)

    def test_remove_one_or_all(self):
        con = new_db()
        first = notes.add(con, "a")
        notes.add(con, "b")
        notes.add(con, "c")
        self.assertEqual(notes.remove(con, first), 1)
        self.assertEqual([n["text"] for n in notes.list_all(con)], ["c", "b"])
        self.assertEqual(notes.remove(con), 2)

    def test_card_carries_the_open_test_of_that_game(self):
        con = new_db()
        for day in (1, 2, 3):
            add(con, 1.7e9 + day * 86400, exe="cod.exe")
        con.execute("INSERT INTO experiments(name, exe, start_ts, status) VALUES ('bez nakładki', 'cod.exe', ?, 'open')",
                    (1.7e9 + 3.5 * 86400,))
        add(con, 1.7e9 + 4 * 86400, exe="cod.exe")
        test = notes.card(con)["test"]
        self.assertEqual((test["name"], test["sessions"], test["need"]), ("bez nakładki", 1, 2))
        self.assertTrue(test["missing"])
        self.assertEqual(notes.card(con, "cod.exe")["test"]["id"], test["id"])
        self.assertIsNone(notes.card(con, "cs2.exe")["test"])
        self.assertIsNone(notes.card(con, general=True)["test"])
        with mock.patch.object(notes.experiments, "evaluate") as evaluate:   # polled every 2 s: kept for a while
            self.assertEqual(notes.open_test(con), test)
        evaluate.assert_not_called()

    def test_cli_parses_note_commands(self):
        a = cli.build().parse_args(["note", "add", "Przycięcia", "robi", "X", "--exe", "cod.exe"])
        self.assertEqual((a.text, a.exe, a.writes), (["Przycięcia", "robi", "X"], "cod.exe", True))
        self.assertFalse(cli.build().parse_args(["note", "list"]).writes)


if __name__ == "__main__":
    unittest.main()
