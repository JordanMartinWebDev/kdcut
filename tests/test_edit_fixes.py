"""Fixes from the first full episode: retakes left in, loose cut tails, black teasers."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kdcut import graphics, media
from kdcut.blocks import Block
from kdcut.planner import Plan, is_restart, protect_sentences
from kdcut.vad import STEP, Voice

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def clip(path: Path, scale: float, seconds: int = 6):
    """Test pattern with its brightness scaled (1.0 = normal, 0.06 = a dark cave)."""
    ff("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
       "-f", "lavfi", "-i", f"sine=duration={seconds}",
       "-vf", f"lutyuv=y=16+(val-16)*{scale}", "-c:v", "libx264", "-preset", "ultrafast",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path))
    return path


class RetakeTests(unittest.TestCase):
    def test_restart_is_spotted(self):
        self.assertTrue(is_restart("right i got myself a couple pieces of food",
                                   "all right i got myself a couple pieces of food now i think "
                                   "it's time to go and find a cave"))
        self.assertTrue(is_restart("gotta say i'm really happy with this thing",
                                   "and i gotta say overall i am really happy with this thing"))

    def test_different_sentences_are_not_a_restart(self):
        self.assertFalse(is_restart("so we need to find some iron first",
                                    "and then we can go mining in that cave over there"))
        self.assertFalse(is_restart("Yeah, I think we're going to have to make our own lectern.",
                                    "just means we'll have to make our own lectern all"))
        self.assertFalse(is_restart("oh", "oh no oh no"))

    def test_a_cut_false_start_stays_cut(self):
        bl = [Block("B1", 0, 0.0, 5.0, "Okay that should be enough food."),
              Block("B2", 0, 5.5, 7.0, "right i got myself a couple pieces of food"),
              Block("B3", 0, 7.2, 14.0, "all right i got myself a couple pieces of food now let's find a cave.")]
        plan = Plan(drop={"B2": "false start"})
        self.assertEqual(protect_sentences(plan, bl), [])
        self.assertIn("B2", plan.drop)


class CutTailTests(unittest.TestCase):
    def voice(self, talk_until: float, total: float = 10.0) -> Voice:
        n = int(total / STEP)
        return Voice([-20.0 if i * STEP < talk_until else -80.0 for i in range(n)], -50.0)

    def test_whisper_word_end_in_silence_is_pulled_back(self):
        v = self.voice(4.0)
        self.assertAlmostEqual(v.speech_end(4.7), 4.0, delta=0.03)

    def test_word_still_going_is_extended(self):
        v = self.voice(4.4)
        self.assertAlmostEqual(v.speech_end(4.1), 4.4, delta=0.03)

    def test_no_voice_nearby_trusts_whisper(self):
        v = self.voice(1.0)
        self.assertEqual(v.speech_end(4.7), 4.7)

    def test_kept_range_ends_shortly_after_the_voice(self):
        from kdcut.timeline import kept_ranges
        bl = [Block("B1", 0, 1.0, 4.7, "we got the diamonds."), Block("B2", 0, 9.0, 9.8, "ok.")]
        rngs = kept_ranges(bl, Plan(drop={"B2": "x"}), [10.0],
                           {"pad_before": 0.15, "pad_after": 0.3, "keep_pauses_up_to": 0.6},
                           voices={0: self.voice(4.0)})
        self.assertAlmostEqual(rngs[0].end, 4.3, delta=0.05)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class DarkTeaserTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_brightness_tells_dark_from_bright(self):
        bright = media.brightness(clip(self.dir / "b.mp4", 1.0), 0.5, 4)
        dark = media.brightness(clip(self.dir / "d.mp4", 0.06), 0.5, 4)
        self.assertGreater(bright, graphics.BRIGHT)
        self.assertLess(dark, graphics.VIEWABLE)

    def test_look_for_each_brightness(self):
        self.assertEqual(graphics.teaser_look(180), ("bw", 1.0))
        look, gamma = graphics.teaser_look(45)
        self.assertEqual(look, "bw")
        self.assertGreater(gamma, 1.1)
        look, gamma = graphics.teaser_look(8)
        self.assertEqual(look, "color")
        self.assertGreater(gamma, 2.0)

    def test_dark_teaser_comes_out_visible(self):
        src = clip(self.dir / "cave.mp4", 0.06)
        before = media.brightness(src, 0.5, 4)
        out, note = graphics.render_teaser(src, 0.5, 120, 30, self.dir / "teaser.mp4", 640, 360)
        after = media.brightness(out, 0.2, 3)
        self.assertIn("colour", note)
        self.assertGreater(after, before * 2.5)
        self.assertGreater(after, graphics.VIEWABLE)

    def test_bright_teaser_keeps_the_usual_look(self):
        src = clip(self.dir / "day.mp4", 1.0)
        _, note = graphics.render_teaser(src, 0.5, 60, 30, self.dir / "teaser.mp4", 640, 360)
        self.assertEqual(note, "black and white")

    def test_dark_pick_falls_back_to_a_backup(self):
        from kdcut.metadata import Metadata
        from kdcut.pipeline import _pick_cold_open
        from kdcut.transcribe import Word
        recs = [clip(self.dir / "cave.mp4", 0.03, 12), clip(self.dir / "day.mp4", 1.0, 12)]
        by_id = {"B1": Block("B1", 0, 2.0, 6.0, "go go go we gotta be fast"),
                 "B2": Block("B2", 1, 2.0, 6.0, "oh my gosh diamonds")}
        words = [[Word(2.0, 2.4, "go"), Word(2.5, 2.9, "fast")],
                 [Word(2.0, 2.4, "oh"), Word(2.5, 2.9, "diamonds")]]
        md = Metadata(cold_open={"id": "B1", "start_words": "go", "end_words": "fast", "reason": "chase"},
                      cold_open_backups=[{"id": "B2", "start_words": "oh", "end_words": "diamonds",
                                          "reason": "diamonds"}])
        pick, rng, level, note = _pick_cold_open(md, by_id, ["B1", "B2"], recs, words, [12.0, 12.0], {},
                                                 log=lambda *_: None)
        self.assertEqual(pick["id"], "B2")
        self.assertEqual(rng.source, 1)
        self.assertIn("too dark", note)

    def test_all_black_means_no_teaser(self):
        from kdcut.metadata import Metadata
        from kdcut.pipeline import _pick_cold_open
        from kdcut.transcribe import Word
        recs = [clip(self.dir / "void.mp4", 0.005, 12)]
        by_id = {"B1": Block("B1", 0, 2.0, 6.0, "go go go")}
        md = Metadata(cold_open={"id": "B1", "start_words": "go", "end_words": "go", "reason": "chase"})
        pick, rng, _, note = _pick_cold_open(md, by_id, ["B1"], recs, [[Word(2.0, 2.4, "go")]], [12.0], {},
                                             log=lambda *_: None)
        self.assertIsNone(pick)
        self.assertIsNone(rng)
        self.assertIn("too dark", note)


if __name__ == "__main__":
    unittest.main()
