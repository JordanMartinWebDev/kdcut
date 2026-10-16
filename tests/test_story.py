"""The story pass, footage measurements and fast-forwards."""

import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from kdcut import config, media, planner, signals, story, transcribe
from kdcut.blocks import Block
from kdcut.planner import Plan
from kdcut.transcribe import Word
from kdcut.vad import STEP, Voice

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def sentence(start, text, step=0.35):
    return [Word(start=start + i * step, end=start + i * step + 0.3, text=w)
            for i, w in enumerate(text.split())]


def frame(fill, panel=False):
    """A tiny 160x90 RGB frame; `panel` draws a flat GUI-gray box in the middle."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (signals.W, signals.H), fill)
    if panel:
        ImageDraw.Draw(im).rectangle((60, 25, 100, 65), fill=(198, 198, 198))
        ImageDraw.Draw(im).rectangle((66, 31, 94, 59), fill=(139, 139, 139))   # slots
    return im.tobytes()


class MenuTests(unittest.TestCase):
    def test_open_menu_over_dimmed_world(self):
        self.assertGreater(signals.menu_share(frame((20, 18, 15), panel=True)), signals.MENU_FLAT)

    def test_gameplay_is_not_a_menu(self):
        self.assertEqual(signals.menu_share(frame((20, 18, 15))), 0.0)
        # A light gray block in daylight: same colour, but the world isn't dimmed.
        self.assertEqual(signals.menu_share(frame((120, 170, 230), panel=True)), 0.0)


class DescribeTests(unittest.TestCase):
    def test_scores_and_quiet_lines(self):
        n = int(60 / STEP)
        levels = [-80.0] * n
        for i in range(n):
            t = i * STEP
            if 1 <= t < 5:
                levels[i] = -30.0                            # calm talk
            elif 20 <= t < 24:
                levels[i] = -12.0 + (6 if int(t * 5) % 2 else -6)   # loud, swinging: shouting
        voice = Voice(levels, -50.0)
        sig = signals.RecordingSignals(frames=[float(t) for t in range(0, 60, 4)],
                                       motion=[1.0] * 3 + [30.0] * 5 + [2.0] * 7,
                                       menu=[0.0] * 10 + [0.3] * 5,
                                       game=[(t / 2, -40.0) for t in range(120)])
        bl = [Block("B1", 0, 1.0, 5.0, "so today we are going to build a house"),
              Block("B2", 0, 20.0, 24.0, "no no no no creeper run run run"),
              Block("B3", 0, 50.0, 55.0, "okay sorting the chests now")]
        for k, b in enumerate([Block(f"X{k}", 0, 56 + k, 56.5 + k, "x") for k in range(3)]):
            bl.append(b)
        notes = signals.describe(bl, {0: sig}, {0: voice}, [60.0])
        self.assertGreater(notes.scores["B2"]["energy"], notes.scores["B1"]["energy"])
        self.assertIn("B2", notes.quiet)                   # 26 s of quiet after the shouting
        self.assertIn("quiet 26s after", notes.quiet["B2"])
        self.assertIn("menus", notes.quiet["B2"])
        self.assertIn("energy", notes.block_tags["B2"])


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class PictureTests(unittest.TestCase):
    def test_motion_tells_moving_from_still(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=8", "-g", "30",
               "-c:v", "libx264", "-preset", "ultrafast", str(tmp / "moving.mp4"))
            ff("-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=30:duration=8", "-g", "30",
               "-c:v", "libx264", "-preset", "ultrafast", str(tmp / "still.mp4"))
            t, moving, _ = signals.picture(tmp / "moving.mp4")
            _, still, _ = signals.picture(tmp / "still.mp4")
            self.assertGreaterEqual(len(t), 6)
            self.assertGreater(sum(moving) / len(moving), sum(still) / len(still) + 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StoryTests(unittest.TestCase):
    def blocks(self):
        return [Block(f"B{i}", 0, i * 5.0, i * 5.0 + 4, f"line {i}.") for i in range(1, 7)]

    def test_outline_keeps_only_real_ids_and_caches(self):
        calls = []

        def fake(cfg, system, user, tool, desc, schema):
            calls.append(user)
            return {"summary": "Builds a shield, then it saves him.", "goals": ["shield"],
                    "beats": [{"from": "B1", "to": "B3", "kind": "goal", "what": "needs iron", "matters": 3},
                              {"from": "B9", "to": "B9", "kind": "aside", "what": "?", "matters": 1}],
                    "threads": [{"name": "shield", "setup": ["B2", "B99"], "payoff": ["B5"]},
                                {"name": "ghost", "setup": ["B98"], "payoff": ["B5"]}]}
        tmp = Path(tempfile.mkdtemp())
        o = story.outline(config.DEFAULTS, {}, self.blocks(), fake, cache=tmp / "story.json")
        self.assertEqual(len(o.beats), 1)
        self.assertEqual(o.threads, [{"name": "shield", "setup": ["B2"], "payoff": ["B5"]}])
        story.outline(config.DEFAULTS, {}, self.blocks(), fake, cache=tmp / "story.json")
        self.assertEqual(len(calls), 1)                    # second time from the cache
        self.assertIn("B1 line 1.", calls[0])
        self.assertIn("shield: setup B2 -> payoff B5", o.text())
        shutil.rmtree(tmp, ignore_errors=True)

    def test_setup_stays_while_its_payoff_does(self):
        o = story.Outline(threads=[{"name": "shield", "setup": ["B2"], "payoff": ["B5"]}])
        plan = Plan(drop={"B2": "dull", "B4": "filler"})
        self.assertEqual(story.protect_threads(plan, o, self.blocks()), ["B2"])
        self.assertEqual(set(plan.drop), {"B4"})
        plan = Plan(drop={"B2": "dull", "B5": "cut"})       # payoff cut too: setup may go
        self.assertEqual(story.protect_threads(plan, o, self.blocks()), [])

    def test_editor_sees_beats_and_measurements(self):
        bl = self.blocks()
        o = story.Outline(beats=[{"from": "B1", "to": "B3", "kind": "goal", "what": "needs iron", "matters": 3}])
        text = planner.format_blocks(bl, [Path("rec.mkv")], measured={"B2": "{energy 4 · action 2}"},
                                     beats=story.beat_of(o, bl))
        self.assertIn("== goal (matters 3): needs iron", text)
        self.assertIn("B2 [0:10 4s] {energy 4 · action 2} line 2.", text)


class FastTests(unittest.TestCase):
    def test_clean_fast_clamps(self):
        f = planner.clean_fast({"id": "B1", "seconds": 600, "speed": 50}, {"B1"}, {"B1": 300})
        self.assertEqual(f["speed"], 12.0)
        self.assertEqual(f["seconds"], 240.0)              # 20 s on screen at 12x
        self.assertIsNone(planner.clean_fast({"id": "B1", "seconds": 2, "speed": 4}, {"B1"}, {}))
        self.assertIsNone(planner.clean_fast({"id": "B7", "seconds": 60, "speed": 4}, {"B1"}, {}))

    def test_span_never_runs_into_the_next_line(self):
        from kdcut import speedup
        a, b = Block("B1", 0, 0.0, 3.0, "x."), Block("B2", 0, 40.0, 43.0, "y.")
        start, end = speedup.source_span(a, b, 120.0, {"pad_after": 0.3}, 5.0, 100.0)
        self.assertAlmostEqual(start, 8.3)
        self.assertAlmostEqual(end, 39.5)
        self.assertIsNone(speedup.source_span(a, Block("B2", 0, 9.0, 10.0, "y."), 60, {}, 5.0, 100.0))


class FakeAI:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, system, user, tool_name, desc, schema):
        self.calls.append((tool_name, system, user))
        if tool_name == "submit_outline":
            return {"summary": "Walks to the village and trades.", "goals": ["find the village"],
                    "beats": [{"from": "B0001", "to": "B0002", "kind": "goal", "what": "sets off", "matters": 3},
                              {"from": "B0003", "to": "B0003", "kind": "payoff", "what": "finds it", "matters": 3}],
                    "threads": [{"name": "village", "setup": ["B0001"], "payoff": ["B0003"]}]}
        if tool_name == "submit_edit":
            return {"drop": [{"id": "B0001", "reason": "slow start"}],
                    "fast": [{"id": "B0002", "seconds": 24, "speed": 6, "reason": "walk to the village"}],
                    "chapters": [{"at": "B0001", "title": "Intro"}]}
        return {"titles": ["Village"], "description": "x", "tags": [], "thumbnail_text": [], "thumbnail_moments": []}


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class StoryEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from kdcut.pipeline import run
        cls.tmp = Path(tempfile.mkdtemp())
        proj = cls.tmp / "Ep"
        out = proj / "output"
        proj.mkdir()
        rec = proj / "rec.mkv"
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=45",
           "-f", "lavfi", "-i", "sine=frequency=220:duration=45", "-g", "60", "-c:v", "libx264", "-preset", "ultrafast",
           "-c:a", "aac", str(rec))
        words = (sentence(1.0, "we need to find a village to trade.") + sentence(5.0, "okay lets walk that way.")
                 + sentence(38.0, "there it is the village."))
        cfg = config._merge(config.DEFAULTS, {"assets": {"index_path": str(cls.tmp / "i.json"),
                                                         "cache_dir": str(cls.tmp / "c")},
                                              "thumbnails": {"enabled": False}, "music": {"enabled": False}})
        info = media.probe(rec)
        key = transcribe._cache_key(rec, media.pick_mic_track(info, "auto"), cfg["whisper"]["model"])
        transcribe.save_cache(out / ".work" / "rec.words.json", key, words)
        cls.ai = FakeAI()
        cls.res = run(proj, cfg, use_ai=True, caller=cls.ai, recordings=[rec], broll_files=[], out_dir=out,
                      video={"title": "Ep", "cold_open": False, "end_screen": False})
        cls.xml = ET.parse(cls.res["timeline"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_story_pass_first_and_outline_in_every_window(self):
        names = [c[0] for c in self.ai.calls]
        self.assertLess(names.index("submit_outline"), names.index("submit_edit"))
        edit_prompt = next(u for n, _, u in self.ai.calls if n == "submit_edit")
        self.assertIn("STORY OUTLINE", edit_prompt)
        self.assertIn("== goal (matters 3): sets off", edit_prompt)
        self.assertIn("[quiet ", edit_prompt)                # the walk is measured
        self.assertIn("{energy", edit_prompt)

    def test_setup_kept_for_its_payoff(self):
        self.assertNotIn("B0001", self.res["plan"].drop)
        self.assertTrue(any("set-up" in n for n in self.res["plan"].notes))

    def test_walk_is_fast_forwarded_between_the_lines(self):
        spine = list(next(self.xml.iter("spine")))
        names = [c.get("name") for c in spine]
        i = next(k for k, n in enumerate(names) if n.startswith("Fast 6x"))
        self.assertTrue(0 < i < len(names) - 1)
        dur = spine[i].get("duration").rstrip("s").split("/")
        secs = int(dur[0]) / int(dur[1])
        self.assertAlmostEqual(secs, (38.0 - 0.5 - 7.75 - 0.3) / 6, delta=1.0)
        self.assertEqual(self.res["placed"]["fast"], 1)
        review = self.res["review"].read_text(encoding="utf-8")
        self.assertIn("Fast-forward 6x", review)


if __name__ == "__main__":
    unittest.main()
