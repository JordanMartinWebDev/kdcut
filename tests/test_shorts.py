"""Shorts: build timelapse + glamor shots + voiceover -> a finished vertical Short with captions."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kdcut import config, media, pipeline, shorts, transcribe
from kdcut.transcribe import Word

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
SCFG = config.DEFAULTS["shorts"]


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def sentence(start, text, step=0.4):
    return [Word(start=start + i * step, end=start + i * step + 0.35, text=w)
            for i, w in enumerate(text.split())]


class ClassifyTests(unittest.TestCase):
    def test_audio_is_voiceover_and_glamor_names_are_the_reveal(self):
        s = shorts.classify([Path("Farm Glamor 2.mp4"), Path("vo.m4a"), Path("Farm timelapse.mp4"),
                             Path("farm reveal 1.mp4"), Path("Cinematic flyover.mkv")])
        self.assertEqual([p.name for p in s.timelapses], ["Farm timelapse.mp4"])
        self.assertEqual([p.name for p in s.glamor], ["Cinematic flyover.mkv", "Farm Glamor 2.mp4",
                                                      "farm reveal 1.mp4"])
        self.assertEqual(s.voiceover, Path("vo.m4a"))
        self.assertFalse(s.empty())

    def test_folder_with_nothing_is_no_short(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(shorts.from_folder(Path(d)))
            self.assertIsNone(shorts.from_folder(Path(d) / "missing"))


class PlanTests(unittest.TestCase):
    TL = [(Path("tl.mp4"), 60.0)]
    GL = [(Path("g1.mp4"), 6.0), (Path("g2.mp4"), 5.0)]

    def test_voiceover_sets_the_length_and_glamor_shots_close_it(self):
        p = shorts.plan(self.TL, self.GL, 20.0, SCFG)
        self.assertAlmostEqual(p.total, shorts.LEAD + 20.0 + shorts.TAIL, places=2)
        self.assertEqual([pc.kind for pc in p.pieces], ["timelapse", "glamor", "glamor"])
        glam = [pc for pc in p.pieces if pc.kind == "glamor"]
        self.assertTrue(all(abs(pc.seconds - SCFG["glamor_seconds"]) < 0.01 for pc in glam))
        self.assertAlmostEqual(p.reveal_at, p.total - 2 * SCFG["glamor_seconds"], places=2)
        self.assertGreater(p.pieces[0].speed, 1.0)        # 60 s timelapse squeezed
        self.assertEqual(p.hold, 0.0)
        self.assertAlmostEqual(glam[0].start, (6.0 - SCFG["glamor_seconds"]) / 2, places=2)   # middle of the move

    def test_too_long_voiceover_is_cut_to_the_cap_with_a_note(self):
        p = shorts.plan(self.TL, self.GL, 90.0, SCFG)
        self.assertAlmostEqual(p.total, SCFG["max_seconds"], places=2)
        self.assertLess(p.voice_seconds, 90.0)
        self.assertTrue(any("voiceover" in n for n in p.notes))

    def test_no_voiceover_uses_a_set_timelapse_length(self):
        p = shorts.plan(self.TL, self.GL, 0.0, SCFG)
        self.assertAlmostEqual(p.total, SCFG["timelapse_seconds"] + 2 * SCFG["glamor_seconds"], places=2)

    def test_short_timelapse_is_slowed_then_the_reveal_takes_the_rest(self):
        p = shorts.plan([(Path("tl.mp4"), 4.0)], self.GL, 20.0, SCFG)
        self.assertEqual(p.pieces[0].speed, shorts.MIN_SPEED)
        self.assertTrue(any("Slowed" in n for n in p.notes))
        self.assertAlmostEqual(sum(pc.seconds for pc in p.pieces) + p.hold, p.total, places=2)
        self.assertGreater(p.hold, 0)          # 8 s + all 11 s of glamor still short of 21.6 s

    def test_glamor_only(self):
        p = shorts.plan([], self.GL, 0.0, SCFG)
        self.assertEqual(p.reveal_at, 0.0)
        self.assertGreaterEqual(p.total, SCFG["min_seconds"])

    def test_needs_something_to_show(self):
        with self.assertRaises(ValueError):
            shorts.plan([], [], 10.0, SCFG)


class CaptionTests(unittest.TestCase):
    def test_words_follow_the_kept_voiceover(self):
        words = sentence(0.0, "here's the farm") + sentence(10.0, "all done")
        placed = shorts.place_words(words, [(0.0, 1.5), (9.8, 11.0)], lead=0.4)
        self.assertEqual([w.text for w in placed], ["here's", "the", "farm", "all", "done"])
        self.assertAlmostEqual(placed[3].start, 0.4 + 1.5 + 0.2, places=2)

    def test_trim_ranges(self):
        self.assertEqual(shorts.trim_ranges([(0, 4), (10, 14)], 6.0), [(0, 4), (10, 12)])

    def test_groups_are_short_and_break_on_sentences(self):
        words = [shorts.CWord(w.start, w.end, w.text)
                 for w in sentence(0.0, "today we're building the starter farm. it's huge")]
        groups = shorts.chunk(words)
        self.assertTrue(all(len(g) <= 3 for g in groups))
        self.assertEqual(groups[-1][0].text, "it's")       # new sentence, new group

    def test_ass_highlights_the_spoken_word_and_shows_the_title(self):
        words = [shorts.CWord(1.0, 1.3, "hello"), shorts.CWord(1.4, 1.7, "there!")]
        ass = shorts.build_ass(shorts.chunk(words), "Starter farm build", "Arial Black", 10.0, SCFG)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn(shorts.ass_color("#FFC440"), ass)
        self.assertEqual(shorts.ass_color("#FFC440"), "&H0040C4FF")
        self.assertIn("STARTER FARM\\NBUILD", ass)
        self.assertEqual(ass.count("Style: Caption"), 1)
        self.assertEqual(ass.count(",Caption,"), 2)        # one event per word
        self.assertIn("HELLO", ass)


class FakeAI:
    def __init__(self):
        self.calls = 0

    def __call__(self, cfg, system, user, tool_name, desc, schema):
        self.calls += 1
        assert tool_name == "short_kit"
        return {"on_screen_title": "Farm Glow Up", "titles": ["This Farm Took 2 Days"],
                "description": "Starter farm done. Full episode on the channel! #minecraft #shorts"}


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.d = Path(cls.tmp.name)
        ff("-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=10", "-c:v", "libx264",
           "-preset", "ultrafast", str(d / "Farm timelapse.mp4"))
        ff("-f", "lavfi", "-i", "smptebars=size=320x180:rate=30:duration=4", "-c:v", "libx264",
           "-preset", "ultrafast", str(d / "Farm glamor.mp4"))
        ff("-f", "lavfi", "-i", "sine=f=220:d=2.5", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
           "-f", "lavfi", "-i", "sine=f=330:d=2.5", "-filter_complex",
           "[1]atrim=0:3[s];[0][s][2]concat=n=3:v=0:a=1,volume=0.5", "-c:a", "aac", str(d / "vo.m4a"))
        cls.cfg = config.load(user_config=d / "none.toml", settings_file=d / "none.json")
        cls.cfg["shorts"] = {**cls.cfg["shorts"], "min_seconds": 6, "fps": 30}
        cls.cfg["assets"]["index_path"] = str(d / "no_index.json")
        cls.out = d / "out"
        work = cls.out / ".work"
        work.mkdir(parents=True)
        vo = d / "vo.m4a"
        words = sentence(0.1, "here's the starter farm.") + sentence(5.6, "and it's done!")
        key = transcribe._cache_key(vo, media.pick_mic_track(media.probe(vo), "auto"),
                                    cls.cfg["whisper"]["model"])
        transcribe.save_cache(work / f"{vo.stem}.words.json", key, words)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_makes_a_vertical_short_with_captions_and_a_kit(self):
        files = shorts.classify(list(self.d.glob("*.mp4")) + [self.d / "vo.m4a"])
        ai = FakeAI()
        res = shorts.make(self.out, self.cfg, {"title": "Kingdom Ep 3"}, files, use_ai=True, caller=ai,
                          log=lambda m: None)
        info = media.probe(res["video"])
        self.assertEqual((info.width, info.height), (1080, 1920))
        self.assertTrue(info.audio)
        self.assertAlmostEqual(info.duration, res["seconds"], delta=0.15)
        self.assertEqual(res["video"].name, "Kingdom Ep 3 - Short.mp4")
        ass = (self.out / ".work" / "short" / "short.ass").read_text(encoding="utf-8")
        self.assertIn("FARM GLOW UP", ass)
        self.assertIn("STARTER", ass)
        kit = res["kit"].read_text(encoding="utf-8")
        self.assertIn("This Farm Took 2 Days", kit)
        self.assertIn("Related video", kit)
        # Re-running doesn't ask the AI again.
        shorts.make(self.out, self.cfg, {"title": "Kingdom Ep 3"}, files, use_ai=True, caller=ai,
                    log=lambda m: None)
        self.assertEqual(ai.calls, 1)

    def test_crop_layout_and_no_voiceover(self):
        cfg = {**self.cfg, "shorts": {**self.cfg["shorts"], "layout": "crop", "title": False}}
        files = shorts.ShortFiles(timelapses=[self.d / "Farm timelapse.mp4"], glamor=[self.d / "Farm glamor.mp4"])
        res = shorts.make(self.out / "crop", cfg, {"title": "Ep"}, files, use_ai=False, log=lambda m: None)
        info = media.probe(res["video"])
        self.assertEqual((info.width, info.height), (1080, 1920))
        self.assertAlmostEqual(info.duration, 10.0 + 3.5, delta=0.15)   # whole timelapse + one glamor shot

    def test_a_broken_short_never_breaks_the_edit(self):
        files = shorts.ShortFiles(timelapses=[self.d / "missing.mp4"])
        res = pipeline.make_short(self.out / "broken", self.cfg, {"title": "Ep"}, files, use_ai=False,
                                  log=lambda m: None)
        self.assertIn("error", res)
        self.assertIsNone(pipeline.make_short(self.out, self.cfg, {}, shorts.ShortFiles(), use_ai=False))


if __name__ == "__main__":
    unittest.main()
