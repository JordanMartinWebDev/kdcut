"""Timelapses: silent clips spliced into the story as scenes of their own, with voiceovers."""

import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from kdcut import config, media, timelapse, transcribe
from kdcut.blocks import Block
from kdcut.planner import Plan
from kdcut.timeline import Clip, build
from kdcut.transcribe import Word

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def sentence(start, text, step=0.35):
    return [Word(start=start + i * step, end=start + i * step + 0.3, text=w)
            for i, w in enumerate(text.split())]


class PairingTests(unittest.TestCase):
    def test_split_and_pair_by_name(self):
        vids, auds = timelapse.split_files([Path("House.mp4"), Path("house voiceover.m4a"),
                                            Path("Farm_timelapse.mp4"), Path("talk.wav")])
        self.assertEqual([p.name for p in vids], ["House.mp4", "Farm_timelapse.mp4"])
        items = [timelapse.Timelapse(v, 10.0) for v in vids]
        left = timelapse.pair_by_name(items, auds)
        self.assertEqual(items[0].voiceover.name, "house voiceover.m4a")
        self.assertIsNone(items[1].voiceover)
        self.assertEqual([p.name for p in left], ["talk.wav"])

    def test_resolve_uses_ai_spots_and_matches_loose_voiceovers(self):
        items = [timelapse.Timelapse(Path("House_Start.mp4"), 20.0), timelapse.Timelapse(Path("Farm.mp4"), 9.0)]
        loose = [Path("take1.wav")]
        inserts, notes = timelapse.resolve(
            [{"file": "House_Start.mp4", "after": "B0004", "voiceover": "take1.wav", "reason": "starts the house"},
             {"file": "Farm.mp4", "after": "B9999", "reason": "?"}],
            items, loose, {"B0004", "B0005"})
        self.assertEqual([t.name for t in inserts["B0004"]], ["House_Start.mp4"])
        self.assertEqual(items[0].voiceover, Path("take1.wav"))
        self.assertEqual([t.name for t in inserts["__end__"]], ["Farm.mp4"])   # unknown block id
        self.assertEqual(notes, [])

    def test_unplaced_go_last_and_leftover_voiceovers_pair_in_order(self):
        items = [timelapse.Timelapse(Path("a.mp4"), 5.0), timelapse.Timelapse(Path("b.mp4"), 5.0)]
        inserts, notes = timelapse.place_without_ai(items, [Path("vo 2.wav"), Path("vo 1.wav")])
        self.assertEqual([t.name for t in inserts["__end__"]], ["a.mp4", "b.mp4"])
        self.assertEqual(items[0].voiceover.name, "vo 1.wav")
        self.assertEqual(items[1].voiceover.name, "vo 2.wav")
        self.assertTrue(any("end of the story" in n for n in notes))


class FitTests(unittest.TestCase):
    def test_no_voiceover_plays_as_recorded(self):
        self.assertEqual(timelapse.fit(30.0, 0.0), (1.0, 0.0, 30.0))

    def test_long_timelapse_keeps_its_speed(self):
        self.assertEqual(timelapse.fit(60.0, 20.0), (1.0, 0.0, 60.0))

    def test_short_timelapse_is_slowed_to_fit(self):
        speed, hold, total = timelapse.fit(15.0, 20.0)
        self.assertAlmostEqual(total, timelapse.LEAD + 20.0 + timelapse.TAIL)
        self.assertAlmostEqual(15.0 / speed, total, places=1)
        self.assertEqual(hold, 0.0)

    def test_much_shorter_timelapse_holds_its_last_frame(self):
        speed, hold, total = timelapse.fit(5.0, 20.0)
        self.assertEqual(speed, timelapse.MIN_SPEED)
        self.assertAlmostEqual(hold, total - 10.0, places=2)

    def test_voiceover_pauses_and_cut_that_are_removed(self):
        words = (sentence(0.5, "so here's the house going up.") + sentence(6.0, "I went with spruce")
                 + [Word(7.5, 7.8, "cut"), Word(7.85, 8.1, "that")]
                 + sentence(10.0, "I went with dark oak for the frame."))
        ranges = timelapse.voice_ranges(words, 14.0, config.DEFAULTS["edit"])
        kept = sum(b - a for a, b in ranges)
        self.assertLess(kept, 8.0)
        self.assertFalse(any(a < 7.0 < b for a, b in ranges))         # "I went with spruce" is gone
        self.assertTrue(any(a <= 10.0 < b for a, b in ranges))


class InsertTests(unittest.TestCase):
    def test_timelapse_plays_right_after_its_block(self):
        bl = [Block("B1", 0, 0.0, 3.0, "alright let's get started on this house."),
              Block("B2", 0, 3.3, 6.0, "and the walls are done."),
              Block("B3", 0, 6.2, 9.0, "now the roof.")]
        tl_clip = Clip(path=Path("t.mp4"), src_in=0, dur=300, rec=0, role="timelapse")
        tl, starts, ends = build("x", [Path("rec.mkv")], bl, Plan(), Fraction(30), 1920, 1080, [10.0],
                                 config.DEFAULTS["edit"], inserts={"B1": [tl_clip]})
        roles = [c.role for c in tl.main]
        self.assertEqual(roles, ["main", "timelapse", "main"])
        self.assertEqual(tl.main[1].rec, tl.main[0].rec_end)
        self.assertEqual(starts["B2"], tl.main[2].rec + (starts["B2"] - tl.main[2].rec))
        self.assertGreaterEqual(starts["B2"], tl.main[1].rec_end)

    def test_cut_block_moves_its_timelapse_to_the_kept_one_before(self):
        bl = [Block("B1", 0, 0.0, 3.0, "let's build."), Block("B2", 0, 3.3, 6.0, "um."),
              Block("B3", 0, 6.2, 9.0, "done.")]
        tl_clip = Clip(path=Path("t.mp4"), src_in=0, dur=90, rec=0, role="timelapse")
        tl, _, _ = build("x", [Path("rec.mkv")], bl, Plan(drop={"B2": "filler"}), Fraction(30), 1920, 1080,
                         [10.0], config.DEFAULTS["edit"], inserts={"B2": [tl_clip]})
        self.assertEqual([c.role for c in tl.main], ["main", "timelapse", "main"])

    def test_end_inserts_go_after_the_story(self):
        bl = [Block("B1", 0, 0.0, 3.0, "hi.")]
        tl_clip = Clip(path=Path("t.mp4"), src_in=0, dur=90, rec=0, role="timelapse")
        tl, _, _ = build("x", [Path("rec.mkv")], bl, Plan(), Fraction(30), 1920, 1080, [10.0],
                         config.DEFAULTS["edit"], inserts={"__end__": [tl_clip]})
        self.assertEqual([c.role for c in tl.main], ["main", "timelapse"])


class FakeAI:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, system, user, tool_name, desc, schema):
        self.calls.append((tool_name, user))
        if tool_name == "submit_edit":
            return {"drop": [], "chapters": [{"at": "B0001", "title": "Intro"}]}
        if tool_name == "place_timelapses":
            after = next(line.split()[0] for line in user.splitlines()
                         if line.startswith("B0") and "get started on this house" in line)
            return {"placements": [{"file": "House_Start.mp4", "after": after, "voiceover": "take 1.wav",
                                    "reason": "starts the house"}]}
        return {"titles": ["Starter House"], "description": "A house.", "tags": ["minecraft"],
                "thumbnail_text": ["HOUSE"], "thumbnail_moments": []}


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TimelapseEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from kdcut.pipeline import run
        cls.tmp = Path(tempfile.mkdtemp())
        proj = cls.tmp / "Ep"
        out = proj / "output"
        work = out / ".work"
        rec = proj / "rec.mkv"
        proj.mkdir()
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=30",
           "-f", "lavfi", "-i", "sine=frequency=220:duration=30", "-c:v", "libx264", "-preset", "ultrafast",
           "-c:a", "aac", str(rec))
        cls.tl_video = proj / "House_Start.mp4"           # silent, shorter than the voiceover
        ff("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24:duration=4", "-c:v", "libx264",
           "-preset", "ultrafast", str(cls.tl_video))
        cls.vo = proj / "take 1.wav"                      # loose: the AI matches it
        ff("-f", "lavfi", "-i", "sine=frequency=330:duration=8", str(cls.vo))
        words = {rec: sentence(1.0, "welcome back everybody.") + sentence(5.0, "alright let's get started on this house.")
                 + sentence(15.0, "and there it is, the house is done."),
                 cls.vo: sentence(0.5, "so here I'm laying out the floor.") + sentence(4.0, "then the walls go up fast.")}
        cfg = config._merge(config.DEFAULTS, {"assets": {"index_path": str(cls.tmp / "i.json"),
                                                         "cache_dir": str(cls.tmp / "c")},
                                              "thumbnails": {"enabled": False}, "music": {"enabled": False}})
        for path, ws in words.items():
            info = media.probe(path)
            key = transcribe._cache_key(path, media.pick_mic_track(info, "auto"), cfg["whisper"]["model"])
            transcribe.save_cache(work / f"{path.stem}.words.json", key, ws)
        cls.ai = FakeAI()
        cls.res = run(proj, cfg, use_ai=True, caller=cls.ai, recordings=[rec], broll_files=[],
                      timelapse_files=[cls.tl_video, cls.vo], out_dir=out,
                      video={"title": "Ep", "cold_open": False, "end_screen": False})
        cls.xml = ET.parse(cls.res["timeline"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def spine(self):
        return list(next(self.xml.iter("spine")))

    def test_timelapse_is_its_own_scene_after_the_line(self):
        names = [c.get("name") for c in self.spine()]
        self.assertIn("Timelapse - House_Start", names)
        i = names.index("Timelapse - House_Start")
        self.assertTrue(0 < i < len(names) - 1)          # gameplay before and after it
        self.assertEqual(self.res["placed"]["timelapse"], 1)

    def test_voiceover_plays_over_it_and_it_was_stretched_to_fit(self):
        clip = next(c for c in self.spine() if c.get("name") == "Timelapse - House_Start")
        vos = [c for c in clip if c.get("name", "").startswith("Voiceover")]
        self.assertTrue(vos)
        self.assertTrue(all(c.get("lane") == "-1" for c in vos))
        dur = float(Fraction(clip.get("duration").rstrip("s")))
        self.assertGreater(dur, 4.5)                       # 4 s timelapse, ~6 s of voiceover
        prepared = list((self.res["out_dir"] / ".work" / "timelapses").glob("*.mp4"))
        info = media.probe(prepared[0])
        self.assertEqual((info.width, info.height), (640, 360))
        self.assertFalse(info.has_audio)

    def test_captions_include_the_voiceover(self):
        srt = (self.res["out_dir"] / "captions.srt").read_text(encoding="utf-8")
        self.assertIn("laying out the floor", srt)
        self.assertIn("get started on this house", srt)

    def test_review_mentions_it(self):
        review = self.res["review"].read_text(encoding="utf-8")
        self.assertIn("Timelapse  House_Start.mp4", review)
        self.assertIn("take 1.wav", review)

    def test_ai_was_told_about_the_loose_voiceover(self):
        user = next(u for name, u in self.ai.calls if name == "place_timelapses")
        self.assertIn("take 1.wav", user)
        self.assertIn("laying out the floor", user)


if __name__ == "__main__":
    unittest.main()
