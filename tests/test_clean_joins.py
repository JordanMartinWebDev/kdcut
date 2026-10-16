"""Cuts between lines said in one breath, and music changes that land softly."""

import shutil
import tempfile
import unittest
from pathlib import Path

from kdcut import config, music
from kdcut.blocks import Block
from kdcut.planner import Plan, protect_joins
from kdcut.timeline import kept_ranges
from kdcut.vad import STEP, Voice


def voice(talk: list[tuple[float, float]], total: float = 30.0) -> Voice:
    lv = [-80.0] * int(total / STEP)
    for a, b in talk:
        for i in range(int(a / STEP), min(int(b / STEP), len(lv))):
            lv[i] = -20.0
    return Voice(lv, -50.0)


class CutBetweenTests(unittest.TestCase):
    def test_cuts_in_the_pause(self):
        v = voice([(1.0, 4.0), (4.4, 6.0)])
        t, clean = v.cut_between(4.1, 4.3)            # Whisper's boundary is a bit off
        self.assertTrue(clean)
        self.assertTrue(4.0 <= t <= 4.4)

    def test_one_breath_has_no_clean_cut(self):
        v = voice([(4.7, 9.9)])                       # "that new world feeling let's go"
        self.assertFalse(v.cut_between(7.58, 7.98)[1])


class JoinTests(unittest.TestCase):
    def blocks(self):
        return [Block("B4", 0, 4.72, 7.58, "Oh, baby, that new world feeling."),
                Block("B5", 0, 7.98, 9.02, "Let's go."),
                Block("B6", 0, 15.74, 25.0, "So a lot of you guys are probably going to be asking.")]

    def test_short_bit_said_in_the_same_breath_is_kept(self):
        plan = Plan(drop={"B5": "stray fragment"})
        self.assertEqual(protect_joins(plan, self.blocks(), {0: voice([(4.7, 9.9), (15.7, 25.0)])}), ["B5"])
        self.assertEqual(plan.drop, {})

    def test_bit_with_a_pause_before_it_can_still_go(self):
        plan = Plan(drop={"B5": "stray fragment"})
        self.assertEqual(protect_joins(plan, self.blocks(), {0: voice([(4.7, 7.6), (8.0, 9.9), (15.7, 25.0)])}), [])
        self.assertIn("B5", plan.drop)

    def test_retake_in_one_breath_still_goes(self):
        bl = [Block("B1", 0, 1.0, 4.0, "Okay that should be plenty."),
              Block("B2", 0, 4.0, 6.0, "right i got myself a couple pieces of food"),
              Block("B3", 0, 6.0, 12.0, "all right i got myself a couple pieces of food now a cave.")]
        plan = Plan(drop={"B2": "false start"})
        self.assertEqual(protect_joins(plan, bl, {0: voice([(1.0, 12.0)])}), [])

    def test_cut_lands_in_the_pause_not_in_the_next_word(self):
        bl = [Block("B1", 0, 1.0, 4.1, "we got it."), Block("B2", 0, 4.3, 5.0, "um."),
              Block("B3", 0, 9.0, 12.0, "next.")]
        v = voice([(1.0, 4.0), (4.4, 5.0), (9.0, 12.0)])
        r = kept_ranges(bl, Plan(drop={"B2": "filler"}), [30.0], config.DEFAULTS["edit"], {0: v})[0]
        self.assertTrue(4.0 <= r.end <= 4.4, r.end)


class MusicTests(unittest.TestCase):
    def test_changes_need_room_and_land_in_pauses(self):
        speech = [(12, 20), (21, 30), (50, 54), (55, 80), (82, 108), (111, 200)]
        raw = [(12, ["a"], ""), (54.02, ["b"], ""), (115, ["c"], ""), (257, ["d"], ""), (303, ["e"], "")]
        out = music.tidy_cues(raw, speech, 12, 400)
        self.assertEqual([x[1] for x in out], [["a"], ["c"], ["d"]])
        self.assertAlmostEqual(out[1][0], 108.3)     # asked for mid-sentence: moved into the pause
        self.assertTrue(all(b[0] - a[0] >= music.MIN_SECTION for a, b in zip(out, out[1:])))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_songs_crossfade(self):
        import subprocess
        from kdcut import ducking, media
        tmp = Path(tempfile.mkdtemp())
        try:
            for name, f in (("a", 300), ("b", 600)):
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={f}:duration=30",
                                str(tmp / f"{name}.wav")], check=True)
            ta = music.Track(path=str(tmp / "a.wav"), title="a", artist="", tags=[], duration=30.0)
            tb = music.Track(path=str(tmp / "b.wav"), title="b", artist="", tags=[], duration=30.0)
            pieces = [music.Placement(ta, 0.0, 0.0, 10.0), music.Placement(tb, 10.0, 0.0, 10.0)]
            bed = ducking.render_bed(pieces, 20.0, [1.0] * 2001, tmp / "bed.m4a")
            # Right at the change both songs play: the bed never drops to silence there.
            lv = __import__("kdcut.vision", fromlist=["loudness"]).loudness(bed, 0, window=0.25)
            around = [v for t, v in lv if 9.0 <= t <= 11.0]
            self.assertGreater(min(around), -33.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
