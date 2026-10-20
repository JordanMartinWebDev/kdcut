"""B-roll goes on the words that name what it shows, uses just the part it needs, never stacks."""

import unittest
from fractions import Fraction
from pathlib import Path

from kdcut import broll
from kdcut.blocks import Block
from kdcut.planner import Plan, make_plan
from kdcut.timeline import Clip, Timeline
from kdcut.transcribe import Word

REC = Path("rec.mp4")


def words_of(text: str, start: float, step: float = 0.4):
    return [Word(start + i * step, start + i * step + 0.3, w) for i, w in enumerate(text.split())]


def timeline(cuts=()):
    """60s recording on a 30fps timeline; `cuts` are (start, end) seconds removed."""
    tl = Timeline("t", Fraction(30), 1920, 1080)
    t, rec = 0.0, 0
    for a, b in list(cuts) + [(60.0, 60.0)]:
        if a > t:
            d = tl.frames(a) - tl.frames(t)
            tl.main.append(Clip(path=REC, src_in=tl.frames(t), dur=d, rec=rec))
            rec += d
        t = b
    return tl


class PlaceTests(unittest.TestCase):
    def setUp(self):
        text = ("am so far away from home just for a little bit of bamboo but it's not the only "
                "thing i also went out and got some kelp")
        self.words = words_of(text, 10.0)
        self.block = Block("B5", 0, 10.0, self.words[-1].end, text)

    def test_starts_on_the_words_not_the_block(self):
        tl = timeline()
        item = {"file": "Getting_Kelp.mp4", "at": "B5", "start_words": "got some kelp",
                "clip_from": 8, "clip_to": 16}
        p = broll.plan_pick(item, Path("Getting_Kelp.mp4"), 40.0, self.block, self.words, REC, tl,
                            tl.frames(10.0))
        kelp = next(w for w in self.words if w.text == "got").start
        self.assertTrue(p.exact)
        self.assertAlmostEqual(tl.seconds(p.rec), kelp - broll.LEAD, delta=0.05)
        self.assertGreater(tl.seconds(p.rec), 17.0)          # well after the block start (10s)
        self.assertEqual(p.src_in, tl.frames(8))             # just the part asked for
        self.assertEqual(p.dur, tl.frames(8))

    def test_cut_before_the_words_moves_it_too(self):
        tl = timeline(cuts=[(2.0, 7.0)])                      # 5s removed earlier
        item = {"file": "k.mp4", "at": "B5", "start_words": "got some kelp", "clip_from": 0, "clip_to": 6}
        p = broll.plan_pick(item, Path("k.mp4"), 40.0, self.block, self.words, REC, tl, tl.frames(5.0))
        kelp = next(w for w in self.words if w.text == "got").start
        self.assertAlmostEqual(tl.seconds(p.rec), kelp - broll.LEAD - 5.0, delta=0.05)

    def test_unknown_words_fall_back_to_the_block(self):
        tl = timeline()
        item = {"file": "k.mp4", "at": "B5", "start_words": "a nether portal appears", "clip_from": 0, "clip_to": 6}
        p = broll.plan_pick(item, Path("k.mp4"), 40.0, self.block, self.words, REC, tl, tl.frames(10.0))
        self.assertFalse(p.exact)
        self.assertEqual(p.rec, tl.frames(10.0))

    def test_part_past_the_end_of_the_file_is_clamped(self):
        tl = timeline()
        item = {"file": "k.mp4", "at": "B5", "start_words": "got some kelp", "clip_from": 9, "clip_to": 30}
        p = broll.plan_pick(item, Path("k.mp4"), 12.0, self.block, self.words, REC, tl, 0)
        self.assertLessEqual(tl.seconds(p.src_in + p.dur), 12.0 + 0.04)
        self.assertGreaterEqual(tl.seconds(p.dur), broll.MIN_SECONDS - 0.04)


class StackingTests(unittest.TestCase):
    def pick(self, start, secs, exact=True):
        return broll.Pick("x", Path("x.mp4"), rec=start * 30, src_in=0, dur=secs * 30, exact=exact)

    def test_earlier_one_ends_where_the_next_starts(self):
        out = broll.no_stacking([self.pick(10, 8), self.pick(15, 6)], lambda s: int(s * 30))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].rec + out[0].dur, out[1].rec)

    def test_too_short_to_trim_gives_way(self):
        out = broll.no_stacking([self.pick(10, 8), self.pick(11, 6)], lambda s: int(s * 30))
        self.assertEqual([p.rec for p in out], [11 * 30])

    def test_one_on_its_words_beats_a_fallback(self):
        out = broll.no_stacking([self.pick(10, 8, exact=True), self.pick(11, 6, exact=False)],
                                lambda s: int(s * 30))
        self.assertEqual([p.rec for p in out], [10 * 30])


class LookTests(unittest.TestCase):
    def test_frame_times(self):
        self.assertEqual(len(broll.frame_times(6)), 3)
        self.assertEqual(len(broll.frame_times(300)), broll.FRAMES)
        self.assertTrue(all(0 < t < 30 for t in broll.frame_times(30)))

    def test_segments_are_clamped_and_described(self):
        segs = broll.clean_segments([{"from": 9, "to": 50, "shows": "breaking kelp underwater"},
                                     {"from": 0, "to": 9, "shows": "leaving the base"}], 40.0)
        self.assertEqual(broll.describe(segs), "0-9s leaving the base; 9-40s breaking kelp underwater")


class PlannerTests(unittest.TestCase):
    def cfg(self):
        return {"llm": {"window_blocks": 600}, "edit": {}, "channel": {}, "music": {}}

    def test_file_reused_for_another_part_but_not_the_same_part(self):
        blocks = [Block("B1", 0, 0, 5, "we went to the jungle"), Block("B2", 0, 6, 10, "and got kelp")]

        def caller(*a, **k):
            return {"broll": [
                {"file": "a.mp4", "at": "B1", "start_words": "the jungle", "clip_from": 0, "clip_to": 6},
                {"file": "a.mp4", "at": "B2", "start_words": "got kelp", "clip_from": 10, "clip_to": 16},
                {"file": "a.mp4", "at": "B2", "start_words": "got kelp", "clip_from": 3, "clip_to": 8},
            ]}
        import kdcut.planner as pl
        orig = pl.system_prompt
        pl.system_prompt = lambda cfg, video: ""
        try:
            plan = make_plan(blocks, [REC], self.cfg(), {}, [("a.mp4", 30.0, "0-30s jungle")], {},
                             caller=caller, log=lambda *_: None)
        finally:
            pl.system_prompt = orig
        self.assertEqual([(b["clip_from"], b["clip_to"]) for b in plan.broll], [(0, 6), (10, 16)])


if __name__ == "__main__":
    unittest.main()
