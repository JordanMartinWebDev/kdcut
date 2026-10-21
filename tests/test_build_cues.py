"""Build cues, time skips, silent clips, story-driven music, running gags."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kdcut import clues, media, music, speedup, story
from kdcut.blocks import Block
from kdcut.planner import Plan, format_blocks

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


class CueTests(unittest.TestCase):
    def test_build_cues(self):
        for t in ["All right, let's build this thing.", "Cue the music!", "let's queue up some music",
                  "But let's just queue a time lapse and see.", "so start a montage of mining some diamonds",
                  "so without further ado let's give it a go", "Let's get digging.", "time to build 16 of these",
                  "So let's continue the montage."]:
            self.assertTrue(clues.is_build_cue(t), t)
        for t in ["Let's go check on the wheat farm.", "I built this house last time.",
                  "the music in this biome is nice", "let's go"]:
            self.assertFalse(clues.is_build_cue(t), t)

    def test_time_skips(self):
        for t in ["It's nighttime.", "Okay guys, it's been two minutes, and this is crazy",
                  "a few hours later and we're back", "It's now morning."]:
            self.assertTrue(clues.is_time_skip(t), t)
        for t in ["I need to sleep before it's night", "we waited for like two minutes and then after all "
                  "of that I decided it's been two minutes too long"]:
            self.assertFalse(clues.is_time_skip(t), t)

    def test_wait_before_a_time_skip_is_cut(self):
        blocks = [Block("B1", 0, 0, 5, "now all we got to do is wait for it to be nighttime"),
                  Block("B2", 0, 60, 62, "It's nighttime."), Block("B3", 0, 70, 80, "let's go")]
        clues.mark(blocks)
        plan = Plan(fast=[{"id": "B1", "seconds": 50, "speed": 8}], extend={"B1": 10.0, "B2": 3.0})
        notes = clues.enforce(plan, blocks)
        self.assertEqual(plan.fast, [])
        self.assertEqual(plan.extend, {"B2": 3.0})
        self.assertTrue(notes)


class SilentClipTests(unittest.TestCase):
    def blocks(self):
        return [Block("B0022", 6, 23, 38, "So gear, nether, bone meal, tree farm. That's the play."),
                Block("B0023", 7, 6, 24, "so start a montage of mining some diamonds."),
                Block("B0024", 12, 2, 8, "hi there mr zomberts")]

    def test_clips_with_no_talking_belong_to_the_line_before(self):
        self.assertEqual(speedup.silent_clips(self.blocks(), 14), {"B0023": [8, 9, 10, 11], "B0024": [13]})

    def test_they_show_up_for_the_editor(self):
        recs = [Path(f"r{i}.mp4") for i in range(14)]
        bl = self.blocks()
        clues.mark(bl)
        text = format_blocks(bl, recs, durations=[30.0] * 14)
        self.assertIn("Recording 9: r8.mp4, 30s clip, NO TALKING", text)
        self.assertIn("B0023 [0:06 18s] [BUILD CUE]", text)

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_joined_into_one_file(self):
        d = Path(tempfile.mkdtemp())
        try:
            parts = []
            for i in range(3):
                p = d / f"c{i}.mp4"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=2",
                                "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-preset", "ultrafast",
                                "-c:a", "aac", "-shortest", str(p)], check=True)
                parts.append(p)
            out = speedup.join_clips(parts, d / "j" / "joined.mp4")
            self.assertAlmostEqual(media.probe(out).duration, 6.0, delta=0.3)
            self.assertEqual(speedup.join_clips(parts, d / "j" / "joined.mp4"), out)   # cached
        finally:
            shutil.rmtree(d, ignore_errors=True)


class MusicCueTests(unittest.TestCase):
    def test_build_cue_song_starts_even_soon_after_a_change(self):
        speech = [(0, 50), (55, 100)]
        raw = [(0.0, ["a"], ""), (60.0, ["b"], ""), (100.0, ["build"], "")]
        out = music.tidy_cues(raw, speech, 0.0, 600.0, firm={100.0})
        self.assertEqual([t for t, *_ in out], [0.0, 100.0])          # 60s change dropped, build kept
        self.assertEqual(out[-1][1], ["build"])

    def test_without_firm_the_minimum_section_holds(self):
        raw = [(0.0, ["a"], ""), (60.0, ["b"], "")]
        out = music.tidy_cues(raw, [], 0.0, 600.0)
        self.assertEqual(len(out), 1)


class RunningGagTests(unittest.TestCase):
    def test_every_check_in_is_kept(self):
        blocks = [Block(f"B{i}", 0, i * 10, i * 10 + 5, "x") for i in range(1, 6)]
        fix = story.id_fixer({b.id for b in blocks})
        o = story._read({"summary": "s", "beats": [{"from": "B1", "to": "B5", "kind": "goal", "what": "w",
                                                    "matters": 3}],
                         "callbacks": [{"name": "wheat farm check", "ids": ["B2", "B4", "B5"]}]}, fix)
        plan = Plan(drop={"B2": "filler", "B4": "filler", "B3": "x"})
        story.protect_threads(plan, o, blocks)
        self.assertEqual(set(plan.drop), {"B3"})
        self.assertIn("wheat farm check", o.text())


if __name__ == "__main__":
    unittest.main()
