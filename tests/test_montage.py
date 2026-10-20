"""Beat-synced montages: hits on the beat, the song's beat under the cuts."""

import shutil
import subprocess
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

try:
    import numpy as np
except ImportError:      # the app bundles numpy; plain test runs may not have it
    np = None

from kdcut import config, ducking, media, music, planner

HAVE = shutil.which("ffmpeg") is not None and np is not None
SR = 44100


def write(path, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def song(path, bpm=120.0, first=0.23, seconds=60):
    """Kick on every beat, hi-hat noise on the off-beat, a quiet pad underneath."""
    t = np.arange(seconds * SR) / SR
    x = 0.1 * np.sin(2 * np.pi * 220 * t)
    period = 60.0 / bpm
    k = 0
    while first + k * period < seconds - 1:
        i = int((first + k * period) * SR)
        n = np.arange(4410) / SR
        x[i:i + 4410] += 0.8 * np.sin(2 * np.pi * 60 * n) * np.exp(-n * 30)
        j = int((first + (k + 0.5) * period) * SR)
        x[j:j + 2000] += 0.3 * np.random.default_rng(k).normal(0, 1, 2000) * np.exp(-np.arange(2000) / 300)
        k += 1
    write(path, x)


def game(path, hits, seconds=60):
    """Block sounds (loud bursts) at `hits`, footsteps (quiet ticks) every 0.45 s."""
    rng = np.random.default_rng(1)
    g = 0.003 * rng.normal(0, 1, seconds * SR)
    for t in hits:
        i = int(t * SR)
        g[i:i + 3000] += 0.6 * rng.normal(0, 1, 3000) * np.exp(-np.arange(3000) / 500)
    for t in np.arange(0.5, seconds - 1, 0.45):
        i = int(t * SR)
        g[i:i + 800] += 0.05 * rng.normal(0, 1, 800) * np.exp(-np.arange(800) / 150)
    write(path, g)


@unittest.skipUnless(HAVE, "needs ffmpeg and numpy")
class DetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from kdcut import montage
        cls.m = montage
        cls.dir = Path(tempfile.mkdtemp())
        song(cls.dir / "song.wav")
        cls.true = sorted(np.random.default_rng(5).uniform(1, 58, 30))
        game(cls.dir / "game.wav", cls.true)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_beat_grid_with_and_without_bpm_tag(self):
        env, fps = self.m.onset_envelope(self.m.decode(self.dir / "song.wav", 0, 60), low_only=True)
        for bpm in (120.0, None):
            period, phase = self.m.beat_grid(env, fps, bpm)
            self.assertAlmostEqual(period, 0.5, delta=0.002)
            self.assertAlmostEqual(phase, 0.23, delta=0.015)       # the kick, not the hi-hat

    def test_block_sounds_found_footsteps_ignored(self):
        env, fps = self.m.onset_envelope(self.m.decode(self.dir / "game.wav", 0, 60))
        found = [h[0] for h in self.m.find_hits(env, fps, 0.0)]
        near = [min(abs(f - t) for f in found) for t in self.true]
        self.assertGreaterEqual(sum(e < 0.015 for e in near), 27)
        self.assertLessEqual(len(found), 34)                         # not the ~130 footsteps

    def test_menus_are_skipped(self):
        env, fps = self.m.onset_envelope(self.m.decode(self.dir / "game.wav", 0, 60))
        found = [h[0] for h in self.m.find_hits(env, fps, 0.0, skip=[(0.0, 30.0)])]
        self.assertTrue(found and all(t > 30 for t in found))

    def test_slices_in_order_and_never_overlap(self):
        hits = [(float(t), 1.0) for t in self.true]
        picked = self.m.choose(hits, [0.5] * 20)
        self.assertEqual(picked, sorted(picked))
        self.assertTrue(all(b - a >= 0.5 for a, b in zip(picked, picked[1:])))
        self.assertIsNone(self.m.choose(hits[:5], [0.5] * 20))      # too few: caller falls back

    def test_fast_songs_cut_every_other_beat(self):
        hits = [(float(t), 1.0) for t in self.true]
        plan = self.m.lay_out(hits, Path("s.wav"), "S", 60 / 170, 0.1, 60.0)
        self.assertEqual(plan.step, 2)
        plan = self.m.lay_out(hits, Path("s.wav"), "S", 60 / 100, 0.1, 60.0)
        self.assertEqual(plan.step, 1)


@unittest.skipUnless(HAVE, "needs ffmpeg and numpy")
class RenderTests(unittest.TestCase):
    def test_hits_land_on_the_beat(self):
        from kdcut import montage
        d = Path(tempfile.mkdtemp())
        try:
            song(d / "song.wav")
            true = sorted(np.random.default_rng(5).uniform(1, 58, 30))
            game(d / "game.wav", true)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=60",
                            "-i", str(d / "game.wav"), "-f", "lavfi", "-i", "sine=frequency=300:duration=60",
                            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast",
                            "-g", "30", "-c:a", "aac", str(d / "rec.mkv")], check=True)
            track = music.Track(path=str(d / "song.wav"), title="Click Party", artist="T", tags=[], duration=60.0,
                                bpm="120", energy="High")
            res = montage.make(d / "rec.mkv", 0, 2.0, 59.0, track, Fraction(30), 320, 180, d / "m.mp4", -6.0,
                               log=lambda *_: None)
            self.assertIsNotNone(res)
            info = media.probe(res.file)
            self.assertAlmostEqual(info.duration, res.frames / 30, delta=0.1)
            x = montage.decode(res.file, 0, info.duration)
            env, fps = montage.onset_envelope(x)
            found = [h[0] for h in montage.find_hits(env, fps, 0.0)]
            err = [min(abs(f - b) for f in found) for b in res.plan.beats[:-1]]
            self.assertGreaterEqual(sum(e < 0.02 for e in err), len(err) - 1)
            self.assertEqual(res.plan.hits, sorted(res.plan.hits))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_too_few_hits_returns_none(self):
        from kdcut import montage
        d = Path(tempfile.mkdtemp())
        try:
            game(d / "game.wav", [5.0, 9.0, 14.0], seconds=20)
            song(d / "song.wav", seconds=30)
            track = music.Track(path=str(d / "song.wav"), title="S", artist="T", tags=[], duration=30.0, bpm="120")
            self.assertIsNone(montage.make(d / "game.wav", 0, 0.0, 20.0, track, Fraction(30), 320, 180,
                                           d / "m.mp4", -6.0, log=lambda *_: None))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class MixTests(unittest.TestCase):
    def test_music_bed_goes_quiet_under_a_montage(self):
        g = ducking.envelope([], 30.0, talk_db=-18, gap_db=-10, silent=[(10.0, 20.0)])
        r = ducking.RATE
        self.assertEqual(g[15 * r], 0.0)
        self.assertGreater(g[5 * r], 0.2)
        self.assertGreater(g[25 * r], 0.2)
        self.assertTrue(0 < g[int(9.8 * r)] < g[5 * r])            # a short fade, not a hard cut

    def test_planner_takes_at_most_two(self):
        from kdcut.blocks import Block
        bl = [Block(f"B{i}", 0, i * 100.0, i * 100.0 + 5, "building time.") for i in range(1, 6)]

        def fake(cfg, system, user, tool, desc, schema):
            self.assertIn("montage", schema["properties"])
            return {"drop": [], "chapters": [{"at": "B1", "title": "Intro"}],
                    "montage": [{"id": f"B{i}", "seconds": 90, "reason": "building"} for i in range(1, 5)]}
        plan = planner.make_plan(bl, [Path("r.mkv")], config.DEFAULTS, {}, [], {}, caller=fake,
                                 log=lambda *_: None, quiet_seconds={f"B{i}": 95.0 for i in range(1, 6)})
        self.assertEqual(len(plan.montage), 2)
        self.assertEqual(plan.montage[0]["seconds"], 90.0)


if __name__ == "__main__":
    unittest.main()


class FakeAI:
    def __call__(self, cfg, system, user, tool_name, desc, schema):
        if tool_name == "submit_outline":
            return {"summary": "Builds a house.", "beats": []}
        if tool_name == "submit_edit":
            return {"drop": [], "chapters": [{"at": "B0001", "title": "Intro"}],
                    "montage": [{"id": "B0001", "seconds": 75, "reason": "building the house"}]}
        return {"titles": ["House"], "description": "x", "tags": [], "thumbnail_text": [], "thumbnail_moments": []}


@unittest.skipUnless(HAVE, "needs ffmpeg and numpy")
class MontageEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import kdcut.music as music_mod
        from kdcut import transcribe
        from kdcut.pipeline import run
        from kdcut.transcribe import Word
        cls.tmp = Path(tempfile.mkdtemp())
        proj, out = cls.tmp / "Ep", cls.tmp / "Ep" / "output"
        proj.mkdir()
        hits = sorted(np.random.default_rng(9).uniform(8, 80, 45))
        game(cls.tmp / "game.wav", hits, seconds=90)
        rec = proj / "rec.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=90",
                        "-i", str(cls.tmp / "game.wav"), "-f", "lavfi", "-i", "sine=frequency=300:duration=90",
                        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-metadata:s:a:1", "title=Mic",
                        "-metadata:s:a:0", "title=Game", "-c:v", "libx264", "-preset", "ultrafast", "-g", "60",
                        "-c:a", "aac", str(rec)], check=True)
        mdir = cls.tmp / "music"
        mdir.mkdir()
        song(mdir / "ES_Click Party - Tester.wav", seconds=60)
        music_mod.HISTORY_FILE = cls.tmp / "history.json"
        cfg = config._merge(config.DEFAULTS, {
            "assets": {"index_path": str(cls.tmp / "i.json"), "cache_dir": str(cls.tmp / "c")},
            "thumbnails": {"enabled": False}, "music": {"folders": [str(mdir)]}})
        words = [Word(2.0 + 0.35 * i, 2.3 + 0.35 * i, w) for i, w in enumerate("alright lets build the house.".split())]
        words += [Word(84.0 + 0.35 * i, 84.3 + 0.35 * i, w) for i, w in enumerate("and there it is.".split())]
        info = media.probe(rec)
        key = transcribe._cache_key(rec, media.pick_mic_track(info, "auto"), cfg["whisper"]["model"])
        transcribe.save_cache(out / ".work" / "rec.words.json", key, words)
        cls.res = run(proj, cfg, use_ai=True, caller=FakeAI(), recordings=[rec], broll_files=[], out_dir=out,
                      video={"title": "Ep", "cold_open": False, "end_screen": False})
        cls.xml = ET.parse(cls.res["timeline"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_montage_between_the_lines_with_its_own_music(self):
        spine = list(next(self.xml.iter("spine")))
        names = [c.get("name") for c in spine]
        i = next(k for k, n in enumerate(names) if n.startswith("Montage - "))
        self.assertTrue(0 < i < len(names) - 1)
        self.assertEqual(self.res["placed"]["montage"], 1)
        self.assertEqual(self.res["placed"]["fast"], 0)
        review = self.res["review"].read_text(encoding="utf-8")
        self.assertIn("cuts on the beat of Click Party", review)
