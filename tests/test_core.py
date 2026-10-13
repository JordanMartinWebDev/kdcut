import unittest
from fractions import Fraction
from pathlib import Path
import tempfile

from kdcut.assets import AssetIndex, scan_pack
from kdcut.blocks import build_blocks, find_cues
from kdcut.metadata import youtube_chapters
from kdcut.planner import Plan
from kdcut.timeline import kept_ranges, build
from kdcut.transcribe import Word


def words_from(spec):
    """spec: list of (start, text) with each word lasting 0.3s."""
    return [Word(start=s, end=s + 0.3, text=t) for s, t in spec]


def sentence(start, text, step=0.35):
    return [(start + i * step, w) for i, w in enumerate(text.split())]


class CueTests(unittest.TestCase):
    def test_isolated_cut_cue_drops_previous_block(self):
        spec = (sentence(0, "welcome back to the channel.")
                + sentence(3, "so today we uh mess this up")
                + [(6.0, "cut"), (6.35, "that")]
                + sentence(8, "so today we are building a castle."))
        blocks = build_blocks([words_from(spec)])
        texts = {b.text: b for b in blocks}
        self.assertIn("forced_drop", texts["so today we uh mess this up"].flags)
        self.assertEqual(texts["cut that"].cue, "cut")
        self.assertNotIn("forced_drop", texts["welcome back to the channel."].flags)
        self.assertNotIn("forced_drop", texts["so today we are building a castle."].flags)

    def test_cut_inside_sentence_is_not_a_cue(self):
        w = words_from(sentence(0, "I need to cut that tree down"))
        self.assertEqual(find_cues(w), [])

    def test_clip_that_highlights_and_chapter_cue(self):
        spec = (sentence(0, "oh my gosh diamonds!") + [(3.0, "clip"), (3.35, "that")]
                + [(6.0, "new"), (6.35, "chapter")] + sentence(9, "now we go to the nether."))
        blocks = build_blocks([words_from(spec)])
        by_text = {b.text: b for b in blocks}
        self.assertIn("highlight", by_text["oh my gosh diamonds!"].flags)
        self.assertIn("chapter", by_text["now we go to the nether."].flags)
        self.assertEqual(sum(1 for b in blocks if b.cue), 2)


class RangeTests(unittest.TestCase):
    def setUp(self):
        spec = (sentence(0, "one two three.") + sentence(1.6, "four five six.")
                + sentence(10, "seven eight.") + sentence(13, "nine ten."))
        self.blocks = build_blocks([words_from(spec)])

    def test_short_pauses_merge_long_pauses_split(self):
        r = kept_ranges(self.blocks, Plan(), [20.0], {"keep_pauses_up_to": 2.5})
        self.assertEqual(len(r), 2)
        self.assertAlmostEqual(r[0].start, 0.0)

    def test_dropped_block_is_not_bridged(self):
        mid = next(b for b in self.blocks if b.text.startswith("four"))
        plan = Plan(drop={mid.id: "test"})
        r = kept_ranges(self.blocks, plan, [20.0], {"keep_pauses_up_to": 2.5})
        for rng in r:
            self.assertFalse(rng.start < mid.end and rng.end > mid.start, "range overlaps dropped speech")

    def test_extend_keeps_gameplay(self):
        last = self.blocks[-1]
        r = kept_ranges(self.blocks, Plan(extend={last.id: 5}), [30.0], {})
        self.assertGreater(r[-1].end, last.end + 4.9)

    def test_timeline_frames_are_contiguous(self):
        tl, starts, ends = build("t", [Path("a.mkv")], self.blocks, Plan(), Fraction(60), 1920, 1080, [20.0], {})
        rec = 0
        for c in tl.main:
            self.assertEqual(c.rec, rec)
            rec += c.dur
        self.assertEqual(set(starts), {b.id for b in self.blocks})


class AssetTests(unittest.TestCase):
    def test_index_and_search(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            files = [
                "Sound Effects/Minecraft/random/levelup.wav",
                "Sound Effects/Minecraft/random/orb.wav",
                "Sound Effects/Minecraft/block/netherite/break1.wav",
                "Sound Effects/Minecraft/block/netherite/break2.wav",
                "Sound Effects/Extra/Bigger Whoosh.wav",
                "Editing Assets/Item  Animations/Item Spin/diamond_pickaxe_Item Spin.mp4",
                "Editing Assets/Item  Animations/Item Spin/golden_apple_Item Spin.mp4",
                "Transitions/Minecraft Items & Mobs/Sword Wipe.mp4",
                "Assets For Thumbnails And Videos/Blocks/Diamond_Ore.png",
                "__MACOSX/._junk.wav",
                "Editing Assets/._diamond.mp4",
                "Fonts/Minecraft.ttf",
            ]
            for f in files:
                p = root / f
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
            assets = scan_pack(root)
            self.assertEqual(len(assets), 10)
            idx = AssetIndex(assets)
            cats = {Path(a.path).name: a.category for a in assets}
            self.assertEqual(cats["levelup.wav"], "sfx")
            self.assertEqual(cats["diamond_pickaxe_Item Spin.mp4"], "overlay")
            self.assertEqual(cats["Sword Wipe.mp4"], "transition")
            self.assertEqual(cats["Diamond_Ore.png"], "image")
            self.assertEqual(cats["Minecraft.ttf"], "font")
            self.assertEqual(Path(idx.best("level up", "sfx").path).name, "levelup.wav")
            self.assertEqual(Path(idx.best("diamond pickaxe spin", "overlay").path).name,
                             "diamond_pickaxe_Item Spin.mp4")
            self.assertEqual(Path(idx.best("whoosh", "sfx").path).name, "Bigger Whoosh.wav")
            self.assertIsNone(idx.best("ender dragon roar", "sfx"))
            picks = {Path(idx.best("netherite break", "sfx", salt=s).path).name for s in "abcdefgh"}
            self.assertEqual(picks, {"break1.wav", "break2.wav"})


class MusicTests(unittest.TestCase):
    def test_epidemic_names(self):
        from kdcut.music import parse_name
        self.assertEqual(parse_name("ES_Flying Over Hills - Hallman"), ("Flying Over Hills", "Hallman"))
        self.assertEqual(parse_name("my_song"), ("my song", ""))

    def test_fill_continues_with_other_tracks_and_respects_silence(self):
        from kdcut.music import Cue, Track, fill
        a = Track("a", "Calm A", "x", ["Calm"], 60)
        b = Track("b", "Calm B", "x", ["Calm"], 60)
        c = Track("c", "Tense C", "x", ["Tense"], 60)
        cues = [Cue(0, 150, [a]), Cue(150, 200, []), Cue(200, 260, [c])]
        out = fill(cues, [a, b, c], {})
        self.assertEqual(out[0].track.path, "a")
        self.assertNotEqual(out[1].track.path, "a")           # no back-to-back repeat
        self.assertAlmostEqual(sum(p.length for p in out if p.start < 150), 150)
        self.assertFalse(any(150 <= p.start < 200 for p in out))  # silence kept
        self.assertEqual(out[-1].track.path, "c")

    def test_single_track_library_loops_instead_of_hanging(self):
        from kdcut.music import Cue, Track, fill
        a = Track("a", "Only", "", [], 30)
        out = fill([Cue(0, 95, [a])], [a], {})
        self.assertEqual(len(out), 3)


class ChapterTests(unittest.TestCase):
    def test_youtube_rules(self):
        ch = youtube_chapters([(3, "Intro"), (5, "Too close"), (60, "Mining"), (300, "Nether")], 600)
        self.assertEqual(ch, [(0.0, "Intro"), (60, "Mining"), (300, "Nether")])
        self.assertEqual(youtube_chapters([(0, "a"), (100, "b")], 600), [])


if __name__ == "__main__":
    unittest.main()
