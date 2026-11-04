import unittest

from perfmon import gameplay, stuttercause


def sample(ts, **kw):
    return {"ts": ts, "gpu_load": 97, "gpu_power_w": 250, "user_idle_s": 1, "cpu_total": 40, "fg_exe": "cod.exe", **kw}


def frame(ts, fps=150):
    return {"ts": ts, "fps": fps, "frames": fps, "ft_p99": 9}


class SelectTest(unittest.TestCase):
    def pick(self, bad_sample=None, bad_fps=None):
        frames = [frame(t) for t in range(0, 200)]
        samples = [sample(t) for t in range(0, 200)]
        for t in range(95, 106):   # longer than a gap inside gameplay
            if bad_sample:
                samples[t] = sample(t, **bad_sample)
            if bad_fps:
                frames[t] = frame(t, bad_fps)
        return [f["ts"] for f in gameplay.select(frames, samples, 0)]

    def test_skips_warmup(self):
        self.assertEqual(self.pick()[0], gameplay.WARMUP_S)

    def test_keeps_normal_seconds(self):
        self.assertIn(100, self.pick())

    def test_drops_menu_idle_and_loading(self):
        for bad in ({"gpu_load": 30}, {"gpu_power_w": 60}, {"user_idle_s": 45}, {"cpu_total": 95}, {"fg_exe": "brave.exe"}):
            self.assertNotIn(100, self.pick(bad), bad)

    def test_drops_uncapped_menu_fps(self):
        self.assertNotIn(100, self.pick(bad_fps=1600))

    def test_seconds_without_samples_are_dropped(self):
        frames = [frame(t) for t in range(0, 100)]
        samples = [sample(t) for t in range(0, 100) if not 50 <= t < 60]
        self.assertNotIn(55, [f["ts"] for f in gameplay.select(frames, samples, 0)])

    def test_short_dip_inside_gameplay_stays(self):
        frames = [frame(t) for t in range(0, 200)]
        samples = [sample(t) for t in range(0, 200)]
        samples[100] = sample(100, gpu_load=30)
        self.assertIn(100, [f["ts"] for f in gameplay.select(frames, samples, 0)])

    def test_cpu_limited_game_keeps_its_low_gpu_seconds(self):
        frames = [frame(t) for t in range(0, 200)]
        samples = [sample(t, gpu_load=45, gpu_power_w=120) for t in range(0, 200)]
        self.assertIn(100, [f["ts"] for f in gameplay.select(frames, samples, 0)])


class RankTest(unittest.TestCase):
    def verdict(self, stutter_tags, base_tags, tag):
        return next(r["verdict"] for r in stuttercause.rank(stutter_tags, base_tags) if r["tag"] == tag)

    def test_tag_as_common_as_in_normal_seconds_is_not_a_cause(self):
        self.assertEqual(self.verdict([["bg"]] * 50, [["bg"]] * 100, "bg"), "ambient")

    def test_tag_only_around_stutters_is_a_cause(self):
        self.assertEqual(self.verdict([["disk"]] * 10 + [[]] * 40, [[]] * 100, "disk"), "confirmed")

    def test_lift_needs_enough_stutters(self):
        self.assertEqual(self.verdict([["disk"]] * 2, [[]] * 100, "disk"), "weak")

    def test_rare_tag_stays_weak(self):
        self.assertEqual(self.verdict([["disk"]] * 3 + [[]] * 97, [[]] * 100, "disk"), "weak")

    def test_tag_rarer_around_stutters_is_noise(self):
        self.assertEqual(self.verdict([["bg"]] * 1 + [[]] * 99, [["bg"]] * 10 + [[]] * 90, "bg"), "noise")

    def test_baseline_avoids_stutters(self):
        base = stuttercause.baseline_ts(list(range(100)), [50])
        self.assertFalse(set(base) & set(range(47, 54)))

    def test_baseline_is_capped(self):
        self.assertLessEqual(len(stuttercause.baseline_ts(list(range(10000)), [])), 2 * stuttercause.BASELINE_MAX)


if __name__ == "__main__":
    unittest.main()
