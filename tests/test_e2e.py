"""End-to-end run on synthetic footage with a fake transcript and a fake AI.

Needs ffmpeg. Whisper and the Claude API are not called.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from kdcut import audioprep, config, media, transcribe
from kdcut.assets import AssetIndex
from kdcut.pipeline import run
from kdcut.transcribe import Word

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def make_recording(path: Path, seconds: int):
    # Video + two audio tracks, like OBS with "mix" and "mic" tracks.
    ff("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=60:duration={seconds}",
       "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
       "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
       "-map", "0:v", "-map", "1:a", "-map", "2:a",
       "-metadata:s:a:1", "title=Mic",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path))


def sentence(start, text, step=0.35):
    return [Word(start=start + i * step, end=start + i * step + 0.3, text=w)
            for i, w in enumerate(text.split())]


class FakeAI:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, system, user, tool_name, desc, schema):
        self.calls.append((tool_name, user))
        if tool_name == "describe_frames":
            ids = [b["text"].split(":")[0] for b in user if b.get("type") == "text" and b["text"].startswith("F")]
            return {"frames": [{"id": i, "desc": "fighting a zombie in a cave", "action": 3} for i in ids]}
        if tool_name == "design_thumbnails":
            return {"theme": {"primary": "#E63946", "accent": "#FFD23F", "font": "not-a-font", "badge": "EP {n}"},
                    "concepts": [
                        {"background": 1, "text": "FINALLY DIAMONDS", "highlight": "DIAMONDS", "text_side": "left",
                         "icons": ["Diamond_Pickaxe", "made-up icon"], "character": "shocked",
                         "focus": {"x": 0.7, "y": 0.5}, "ring": True, "arrow": True},
                        {"background": 1, "text": "THE CASTLE", "text_side": "right", "character": None}]}
        if tool_name == "submit_edit":
            return {
                "drop": [{"id": "B0001", "reason": "mic check"}],
                "extend": [{"id": "B0005", "seconds": 4}],
                "broll": [{"file": "castle-walls-timelapse.mp4", "at": "B0005", "duration": 4,
                           "reason": "talks about finishing the walls"}],
                "overlays": [{"at": "B0006", "query": "diamond pickaxe", "duration": 2, "text": "Diamonds"}],
                "sfx": [{"at": "B0006", "query": "level up", "when": "end"},
                        {"at": "B0003", "query": "ender dragon roar"}],
                "chapters": [{"at": "B0002", "title": "Intro"},
                             {"at": "B0005", "title": "The castle walls"},
                             {"at": "B0008", "title": "Going mining"}],
                "notes": ["Check the overlay timing on the pickaxe."],
            }
        return {
            "titles": ["I Built a Castle and Found Diamonds"],
            "description": "Castle walls and diamonds.",
            "tags": ["minecraft", "castle"],
            "thumbnail_text": ["DIAMONDS!"],
            "thumbnail_moments": ["B0006"],
            "cold_open": {"id": "B0006", "start_words": "oh my gosh diamonds", "end_words": "gosh diamonds",
                          "lead_in": 0, "reason": "diamonds reaction"},
            "music": [{"at": "B0002", "track": "Calm Fields - Sleepy Fish", "why": "building"},
                      {"at": "B0008", "track": "", "why": "silence for the ending"}],
        }


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        proj = cls.tmp / "Episode 12"
        for sub in ("recordings", "broll"):
            (proj / sub).mkdir(parents=True)
        (proj / "kdcut.toml").write_text('[video]\ntopic = "Building a castle"\n')
        make_recording(proj / "recordings" / "2026-10-13 19-00-01.mkv", 40)
        make_recording(proj / "recordings" / "2026-10-13 19-30-12.mkv", 25)
        ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=8", "-c:v", "libx264",
           "-preset", "ultrafast", str(proj / "broll" / "castle-walls-timelapse.mp4"))

        # Fake editing pack, including a WebM overlay that needs converting for Resolve.
        pack = cls.tmp / "pack"
        (pack / "Sound Effects" / "Minecraft").mkdir(parents=True)
        (pack / "Item  Animations" / "Item Spin").mkdir(parents=True)
        ff("-f", "lavfi", "-i", "sine=frequency=880:duration=1", str(pack / "Sound Effects" / "Minecraft" / "Level Up.wav"))
        ff("-f", "lavfi", "-i", "color=c=red@0.5:size=200x200:rate=30:duration=3,format=yuva420p",
           "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
           str(pack / "Item  Animations" / "Item Spin" / "diamond_pickaxe_Item Spin.webm"))

        from PIL import Image
        (pack / "Assets For Thumbnails" / "Items").mkdir(parents=True)
        Image.new("RGBA", (16, 16), (90, 220, 220, 255)).save(pack / "Assets For Thumbnails" / "Items" / "Diamond_Pickaxe.png")

        home = cls.tmp / "home"
        cls.cfg = config._merge(config.DEFAULTS, {
            "assets": {"index_path": str(home / "index.json"), "cache_dir": str(home / "cache")},
        })
        idx = AssetIndex.build([pack])
        idx.save(Path(cls.cfg["assets"]["index_path"]))

        # Pre-seed the transcript cache so Whisper isn't needed.
        recs = sorted((proj / "recordings").iterdir())
        rec1 = (sentence(0.5, "test test is this on.")
                + sentence(4.0, "welcome back to KD plays.")
                + sentence(8.0, "today we finish the castle oops wrong way")
                + [Word(11.0, 11.3, "cut"), Word(11.35, 11.65, "that")]
                + sentence(14.0, "so I finished the castle walls.")
                + sentence(25.0, "oh my gosh diamonds!")
                + [Word(28.0, 28.3, "clip"), Word(28.35, 28.65, "that")])
        rec2 = (sentence(1.0, "back again, time to go mining.")
                + sentence(10.0, "that's it for today, see you next time."))
        work = proj / "output" / ".work"
        for rec, words in zip(recs, [rec1, rec2]):
            info = media.probe(rec)
            track = media.pick_mic_track(info, "auto")
            key = transcribe._cache_key(rec, track, cls.cfg["whisper"]["model"])
            transcribe.save_cache(work / f"{rec.stem}.words.json", key, words)
        cls.proj = proj
        # Thumbnail inputs: a screenshot and a character pose.
        shot = cls.tmp / "screenshot.png"
        ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080", "-frames:v", "1", str(shot))
        chars = cls.tmp / "poses"
        chars.mkdir()
        from PIL import Image
        Image.new("RGBA", (300, 600), (240, 200, 40, 255)).save(chars / "shocked.png")
        mdir = cls.tmp / "music" / "Calm"
        mdir.mkdir(parents=True)
        ff("-f", "lavfi", "-i", "sine=frequency=330:duration=25", str(mdir / "ES_Calm Fields - Sleepy Fish.mp3"))
        ff("-f", "lavfi", "-i", "sine=frequency=550:duration=30", str(mdir / "ES_Lazy Sunday - Ava Low.mp3"))
        ff("-f", "lavfi", "-i", "sine=frequency=990:duration=2", str(mdir / "ES_Sting.mp3"))   # too short
        import kdcut.music as music_mod
        music_mod.HISTORY_FILE = home / "music_history.json"
        cls.cfg = config._merge(cls.cfg, {"music": {"folders": [str(cls.tmp / "music")], "volume_db": -20},
                                          "edit": {"overlays": True}})
        cls.cfg = config._merge(cls.cfg, {"vision": {"enabled": True, "min_gap": 8},
                                          "thumbnails": {"style": "classic", "characters_dir": str(chars),
                                                         "themes_dir": str(home / "themes")}})
        cls.video = {"title": "Episode 12", "topic": "Building a castle", "series": "Hardcore",
                     "episode": 12, "screenshots": [str(shot)]}
        cls.ai = FakeAI()
        cls.result = run(proj, cls.cfg, use_ai=True, caller=cls.ai, video=cls.video)
        out = proj / "output"
        cls.kit = (out / "upload_kit.md").read_text(encoding="utf-8")
        cls.review = (out / "review.md").read_text(encoding="utf-8")
        cls.xml = (out / "Episode 12.fcpxml").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_missing_mic_track_falls_back(self):
        from kdcut.media import AudioStream, MediaInfo, pick_mic_track
        one = MediaInfo(path=Path("a.mp4"), duration=1, has_video=True, audio=[AudioStream(0, 2, "")])
        said = []
        self.assertEqual(pick_mic_track(one, "2", log=said.append), 0)
        self.assertIn("only has 1 track", said[0])
        two = MediaInfo(path=Path("b.mkv"), duration=1, has_video=True,
                        audio=[AudioStream(0, 2, "Game"), AudioStream(1, 1, "Mic")])
        self.assertEqual(pick_mic_track(two, "3"), 1)

    def test_mic_track_auto_picks_titled_track(self):
        info = media.probe(next((self.proj / "recordings").iterdir()))
        self.assertEqual(media.pick_mic_track(info, "auto"), 1)

    def test_ai_saw_cues_and_broll(self):
        prompt = next(c[1] for c in self.ai.calls if c[0] == "submit_edit")
        self.assertIn("[FORCED CUT]", prompt)
        self.assertIn("[HIGHLIGHT]", prompt)
        self.assertIn("castle-walls-timelapse.mp4", prompt)

    def test_outputs_exist(self):
        out = self.proj / "output"
        for f in ("Episode 12.fcpxml", "review.md", "upload_kit.md", "plan.json", "transcript.txt"):
            self.assertTrue((out / f).exists(), f)
        self.assertTrue((out / "thumbnails" / "thumbnail_1.jpg").exists())
        self.assertIn("I Built a Castle and Found Diamonds", self.kit)
        self.assertIn("DIAMONDS!", self.kit)
        self.assertIn("mic check", self.review)
        self.assertIn("no sound matched 'ender dragon roar'", self.review)
        self.assertIn("B-roll  castle-walls-timelapse.mp4", self.review)

    def test_spoken_cut_removed_the_mistake(self):
        self.assertIn("[you said 'cut that']", self.review)
        self.assertIn("today we finish the castle oops wrong way", self.review)
        plan = self.result["plan"]
        self.assertEqual(plan.extend, {"B0005": 4.0})

    def test_edit_is_shorter(self):
        story = self.result["final_seconds"] - self.result["end_screen_seconds"]
        self.assertLess(story, self.result["raw_seconds"] * 0.6)
        self.assertEqual(self.result["placed"], {"broll": 1, "overlay": 1, "sfx": 1, "music": 1, "timelapse": 0, "fast": 0})

    def test_fcpxml_is_valid_and_frame_accurate(self):
        root = ET.fromstring(self.xml.split("<!DOCTYPE fcpxml>", 1)[1])
        self.assertEqual(root.tag, "fcpxml")
        fmt = root.find("resources/format")
        self.assertEqual(fmt.get("frameDuration"), "1/60s")
        fd = Fraction(1, 60)

        def secs(v):
            return Fraction(v[:-1]) if v != "0s" else Fraction(0)

        spine = root.find(".//sequence/spine")
        clips = list(spine)
        self.assertGreaterEqual(len(clips), 3)
        expected = Fraction(0)
        for c in clips:
            off, dur = secs(c.get("offset")), secs(c.get("duration"))
            self.assertEqual(off, expected, "main clips must be back to back")
            self.assertEqual((dur / fd).denominator, 1, "durations must be whole frames")
            expected += dur
            for child in c:
                if child.tag in ("asset-clip", "video"):
                    start = secs(c.get("start"))
                    self.assertGreaterEqual(secs(child.get("offset")), start)
                    self.assertLess(secs(child.get("offset")), start + dur)
                    self.assertIn(child.get("lane"), {"1", "2", "3", "-1", "-2", "-3", "-4"})
        # Converted overlay is a ProRes MOV, not the original WebM.
        srcs = [m.get("src") for m in root.iter("media-rep")]
        self.assertTrue(any(s.endswith(".mov") for s in srcs))
        self.assertFalse(any(s.endswith(".webm") for s in srcs))
        markers = [m.get("value") for m in root.iter("marker")]
        self.assertIn("The castle walls", markers)
        self.assertTrue(any("Highlight" in m for m in markers))

    def test_green_screen_overlay_gets_keyed(self):
        src = self.tmp / "green_Item Spin.mp4"
        ff("-f", "lavfi", "-i", "color=c=0x00FF00:size=320x180:rate=30:duration=1",
           "-vf", "drawbox=x=140:y=70:w=40:h=40:color=red:t=fill",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src))
        out, how = media.prepare_overlay(src, self.tmp / "keycache")
        self.assertEqual(how, "keyed")
        self.assertTrue(media.probe(out).has_alpha)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgba", "-"], capture_output=True).stdout
        corner_alpha = raw[3]
        center = (90 * 320 + 160) * 4
        self.assertEqual(corner_alpha, 0)
        self.assertEqual(raw[center + 3], 255)
        self.assertGreater(raw[center], 200)  # still red

    def test_alpha_prores_is_used_as_is(self):
        src = self.tmp / "alpha.mov"
        ff("-f", "lavfi", "-i", "color=c=green@0.0:size=64x64:rate=30:duration=1,format=yuva444p10le",
           "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(src))
        out, how = media.prepare_overlay(src, self.tmp / "keycache")
        self.assertEqual((out, how), (src, "as-is"))

    def test_cold_open_is_first_and_retention_rules_sent(self):
        root = ET.fromstring(self.xml.split("<!DOCTYPE fcpxml>", 1)[1])
        first = root.find(".//sequence/spine")[0]
        self.assertTrue(first.get("name").startswith("Cold open"))
        secs = Fraction(first.get("duration")[:-1])
        self.assertTrue(3 <= secs <= 5, secs)                     # the words, not a fixed length
        # Rendered black-and-white "coming up" clip; its audio still comes from the diamonds block.
        self.assertEqual(first.get("start"), "0s")
        voice = next(c for c in first if c.get("lane") == "-1")
        self.assertAlmostEqual(float(Fraction(voice.get("start")[:-1])), 25.0, delta=1.0)
        teaser = self.proj / "output" / ".work" / "teaser.mp4"
        self.assertTrue(teaser.exists())
        self.assertAlmostEqual(media.probe(teaser).duration, float(secs), delta=0.15)
        sat = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", str(teaser), "-frames:v", "1",
                              "-vf", "signalstats,metadata=print:key=lavfi.signalstats.SATAVG:file=-",
                              "-f", "null", "-"], capture_output=True, text=True).stdout
        self.assertLess(float(sat.split("SATAVG=")[1].split()[0]), 2.0)     # black and white
        self.assertIn("Cold open: ", self.review)
        system_prompts = [c for c in self.ai.calls if c[0] == "submit_metadata"]
        self.assertIn("cold open", system_prompts[0][1])
        self.assertIn("[HIGHLIGHT]", system_prompts[0][1])

    def test_footage_check_feeds_the_planner(self):
        described = [c for c in self.ai.calls if c[0] == "describe_frames"]
        self.assertTrue(described, "frames were sent to the AI")
        plan_prompt = next(c[1] for c in self.ai.calls if c[0] == "submit_edit")
        self.assertIn("[on screen", plan_prompt)
        self.assertIn("fighting a zombie", plan_prompt)
        self.assertTrue(list((self.proj / "output" / ".work" / "frames").glob("F*.jpg")))

    def test_thumbnails_made_with_series_theme(self):
        thumbs = self.result["thumbnails"]
        self.assertEqual(len(thumbs), 2)
        from PIL import Image
        for t in thumbs:
            self.assertEqual(Image.open(t).size, (1280, 720))
            self.assertLess(t.stat().st_size, 2_000_000)
        theme = json.loads((Path(self.cfg["thumbnails"]["themes_dir"]) / "hardcore.json").read_text())
        self.assertEqual(theme["accent"], "#FFD23F")
        self.assertEqual(theme["font"], "")          # unknown font names are ignored
        concepts = json.loads((self.proj / "output" / "thumbnails" / "concepts.json").read_text())
        self.assertEqual(concepts[0]["icons"], ["Diamond_Pickaxe"])
        self.assertEqual(concepts[0]["character"], "shocked")
        prompt = next(c[1] for c in self.ai.calls if c[0] == "design_thumbnails")
        self.assertTrue(any(b.get("type") == "image" for b in prompt))
        self.assertIn("No style exists yet", prompt[0]["text"])

    def test_end_screen_closes_the_video(self):
        from PIL import Image
        root = ET.fromstring(self.xml.split("<!DOCTYPE fcpxml>", 1)[1])
        last = list(root.find(".//sequence/spine"))[-1]
        self.assertEqual(last.tag, "asset-clip")         # animated: a video clip, not a still
        self.assertEqual(last.get("name"), "End screen")
        ref = root.find(f".//asset[@id='{last.get('ref')}']/media-rep").get("src")
        self.assertTrue(ref.endswith(".mp4"))
        dur = Fraction(last.get("duration").rstrip("s"))
        self.assertAlmostEqual(float(dur), 20.0, places=1)
        self.assertEqual(self.result["end_screen_seconds"], 20.0)
        img = Image.open(self.result["end_screen"])
        self.assertEqual(img.size, (640, 360))          # matches the recordings
        review = (self.proj / "output" / "review.md").read_text()
        self.assertIn("## End screen", review)
        self.assertIn("Import from video", review)

    def test_voice_and_game_audio_on_their_own_tracks(self):
        root = ET.fromstring(self.xml.split("<!DOCTYPE fcpxml>", 1)[1])
        spine = list(root.find(".//sequence/spine"))
        for main in spine:
            if main.tag != "asset-clip" or main.get("name") == "End screen":
                continue
            self.assertEqual(main.get("srcEnable"), "video")
            kids = {c.get("lane"): c for c in main if c.tag == "asset-clip"}
            self.assertTrue(kids["-1"].get("name").startswith("Voice - "))
            self.assertTrue(kids["-2"].get("name").startswith("Game - "))
            for lane in ("-1", "-2"):
                self.assertEqual(kids[lane].get("offset"), main.get("start"))
                self.assertEqual(kids[lane].get("duration"), main.get("duration"))
        voice = sorted((self.proj / "output" / ".work" / "audio").glob("*.voice.m4a"))
        self.assertEqual(len(voice), 2)
        lufs, _ = audioprep.measure(voice[0])
        self.assertAlmostEqual(lufs, audioprep.VOICE_LUFS, delta=2.5)
        sfx = [e for e in root.iter("asset-clip") if e.get("lane") == "-4"]
        self.assertTrue(sfx)
        self.assertTrue(all(e.find("adjust-volume") is not None for e in sfx))

    def test_text_label_with_overlay(self):
        self.assertIn("Text 'Diamonds'", self.review)
        root = ET.fromstring(self.xml.split("<!DOCTYPE fcpxml>", 1)[1])
        self.assertTrue(any(e.get("name") == "Text - Diamonds" for e in root.iter("video")))

    def test_music_on_its_own_track(self):
        root = ET.fromstring(self.xml.split("<!DOCTYPE fcpxml>", 1)[1])
        music = [e for e in root.iter("asset-clip") if e.get("name", "").startswith("Music (dips")]
        self.assertEqual(len(music), 1)
        self.assertEqual(music[0].get("lane"), "-3")
        bed = self.proj / "output" / "music_bed.m4a"
        self.assertTrue(bed.exists())
        self.assertGreater(media.probe(bed).duration, 5)
        lufs, _ = audioprep.measure(bed)
        self.assertTrue(-38 < lufs < -20, lufs)          # audible under a -16 LUFS voice
        self.assertIn("Music  Calm Fields - Sleepy Fish", self.review)
        prompt = next(c[1] for c in self.ai.calls if c[0] == "submit_metadata")
        self.assertIn("Lazy Sunday - Ava Low [Calm]", prompt)
        self.assertNotIn("Sting", prompt)
        self.assertGreaterEqual(self.result["placed"]["music"], 1)

    def test_rerun_reuses_plan_without_ai(self):
        ai = FakeAI()
        run(self.proj, self.cfg, use_ai=False, caller=ai)
        self.assertEqual([c[0] for c in ai.calls], [])

    def test_no_ai_mode_honours_cues(self):
        (self.proj / "output" / "plan.json").rename(self.proj / "output" / "plan.bak")
        try:
            res = run(self.proj, self.cfg, use_ai=False)
            self.assertEqual(res["plan"].drop, {})
            self.assertEqual(res["placed"]["broll"], 0)
            self.assertLess(res["final_seconds"], res["raw_seconds"])
        finally:
            (self.proj / "output" / "plan.json").unlink(missing_ok=True)
            (self.proj / "output" / "plan.bak").rename(self.proj / "output" / "plan.json")


if __name__ == "__main__":
    unittest.main()
