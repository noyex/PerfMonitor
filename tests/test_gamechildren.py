import unittest
from unittest import mock

from perfmon import collector
from perfmon.collector import Collector


class FakeCapture:
    def __init__(self):
        self.calls = []

    def retarget(self, names):
        self.calls.append(list(names))


class Proc:
    def __init__(self, pid, parents=()):
        self.pid, self._parents = pid, [type("P", (), {"pid": p}) for p in parents]

    def parents(self):
        return self._parents


class GameChildTest(unittest.TestCase):
    def setUp(self):
        self.c = object.__new__(Collector)
        self.cap = FakeCapture()
        self.c.games = {100: {"session": 5, "capture": self.cap, "pid": 100, "children": {}}}
        self.c.game_sampler = mock.Mock()

    def test_ancestor_lookup(self):
        self.assertEqual(self.c.game_ancestor(Proc(200, parents=(150, 100, 4))), 100)   # grandchild too
        self.assertIsNone(self.c.game_ancestor(Proc(300, parents=(4,))))

    def test_child_joins_parent_session_and_capture(self):
        self.c.attach_child(100, 200, "sp26-cod.exe")
        self.assertEqual(self.cap.calls[-1], ["sp26-cod.exe"])
        self.assertEqual(self.c.game_pids(), {100, 200})

    def test_sampling_follows_the_child(self):
        self.c.attach_child(100, 200, "sp26-cod.exe")
        self.c.shader_pid = 100
        self.c.gpu_procs = mock.Mock(read=lambda: {})
        self.c.game_sampler.sample.return_value = None
        self.c.sample_gpu(1.0, {}, [])
        self.assertEqual(self.c.game_sampler.sample.call_args[0][0], 200)


if __name__ == "__main__":
    unittest.main()
