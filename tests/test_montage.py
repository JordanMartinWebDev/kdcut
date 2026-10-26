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


def block_sound():
    """One fixed 'block' sound: the game plays the same file every time."""
    r = np.random.default_rng(42)
    n = np.arange(3000)
    return r.normal(0, 1, 3000) * np.exp(-n / 500)


def eat_sound():
    """A different crunchy sound (eating), just as loud and sharp."""
    r = np.random.default_rng(7)
    n = np.arange(2500)
    return r.normal(0, 1, 2500) * np.exp(-n / 300) * (1 + 0.5 * np.sin(n / 9))


def game(path, hits, seconds=60, eats=()):
    """Block sounds at `hits`, eating at `eats`, footsteps (quiet ticks) every 0.45 s."""
    rng = np.random.default_rng(1)
    g = 0.003 * rng.normal(0, 1, seconds * SR)
    b, e = block_sound(), eat_sound()
    for k, t in enumerate(hits):
        i = int(t * SR)
        g[i:i + len(b)] += (0.12 + 0.06 * (k % 3)) * b[: len(g) - i]
    for t in eats:
        i = int(t * SR)
        g[i:i + len(e)] += 0.2 * e[: len(g) - i]
    for t in np.arange(0.5, seconds - 1, 0.45):
        i = int(t * SR)
        g[i:i + 800] += 0.05 * rng.normal(0, 1, 800) * np.exp(-np.arange(800) / 150)
    write(path, g)


def fake_assets(root: Path) -> Path:
    """A Minecraft assets folder holding just our block sound, stored so that playing it at
    the game's 0.8 break pitch gives the sound heard in the 'recording'."""
    import json
    assets = root / "assets"
    (assets / "indexes").mkdir(parents=True)
    h = "ab" + "0" * 38
    (assets / "objects" / "ab").mkdir(parents=True)
    pcm = (np.clip(block_sound() * 0.2, -1, 1) * 32767).astype("<i2")
    with wave.open(str(assets / "objects" / "ab" / h), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(SR / 0.8))
        w.writeframes(pcm.tobytes())
    (assets / "indexes" / "1.json").write_text(json.dumps(
        {"objects": {"minecraft/sounds/dig/stone1.ogg": {"hash": h, "size": 1}}}))
    return assets


def matcher_for(root: Path):
    from kdcut import mcsounds
    return mcsounds.Matcher.load(fake_assets(root), cache_dir=root)


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

    def test_only_block_sounds_count_not_eating(self):
        d = Path(tempfile.mkdtemp())
        try:
            blocks = [3.0, 7.5, 12.1, 20.4, 33.3, 41.0]
            eats = [5.0, 5.25, 5.5, 5.75, 26.0, 26.2, 26.4, 50.0]
            game(d / "g.wav", blocks, eats=eats)
            m = matcher_for(d)
            x = self.m.decode(d / "g.wav", 0, 60)
            got = [t for t, _ in self.m.block_hits(x, 0.0, 60.0, m)]
            for t in blocks:
                self.assertTrue(any(abs(t - g) < 0.03 for g in got), t)
            for t in eats:
                self.assertFalse(any(abs(t - g) < 0.06 for g in got), t)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_busiest_run_wins(self):
        lone = [(5.0, 1.0), (27.0, 1.0)]
        run = [(60.0 + 0.4 * k, 1.0) for k in range(60)]
        plan = self.m.lay_out(lone + run, Path("s.wav"), "S", 0.5, 0.1, 120.0)
        self.assertGreaterEqual(min(plan.hits), 60.0)

    def test_menus_are_skipped(self):
        env, fps = self.m.onset_envelope(self.m.decode(self.dir / "game.wav", 0, 60))
        found = [h[0] for h in self.m.find_hits(env, fps, 0.0, skip=[(0.0, 30.0)])]
        self.assertTrue(found and all(t > 30 for t in found))

    def run_of(self, n=120, gap=0.4, at=60.0):
        return [(at + gap * k, 1.0) for k in range(n)]

    def test_slices_in_order_and_never_overlap(self):
        plan = self.m.lay_out(self.run_of(), Path("s.wav"), "S", 0.5, 0.1, 120.0)
        for a, b in zip(plan.segments, plan.segments[1:]):
            self.assertGreaterEqual(b.src + 1e-6, a.src + a.dur * a.speed)     # no footage twice
        self.assertGreater(plan.ramps, 0)                                     # fast-forward shots mixed in
        self.assertEqual({round(g.dur, 3) for g in plan.segments if g.hit}, {0.25, 0.5})   # beat and half-beat cuts

    def test_cuts_land_on_the_song_grid_from_a_downbeat(self):
        plan = self.m.lay_out(self.run_of(), Path("s.wav"), "S", 0.5, 0.1, 120.0, bar_offset=3)
        first = plan.song_start + self.m.PRE
        self.assertAlmostEqual(((first - 0.1) / 0.5) % 4, 3.0, places=6)    # bar starts on the accented beat
        for b in plan.beats:
            self.assertAlmostEqual(((b - self.m.PRE) / 0.25) % 1, 0.0, places=6)  # every hit on a (half) beat

    def test_game_sound_stops_before_the_next_dig(self):
        hits = self.run_of()
        plan = self.m.lay_out(hits, Path("s.wav"), "S", 0.5, 0.1, 120.0)
        times = [t for t, _ in hits]
        for g in plan.segments:
            if g.hit is None:
                continue
            nxt = min(t for t in times if t > g.hit + 0.03)
            self.assertLess(g.src + g.audio, nxt)
            self.assertLessEqual(g.audio, g.dur + 1e-9)

    def test_long_build_shows_progress_start_to_finish(self):
        import random
        r = random.Random(1)
        hits, t, bursts = [(3.0, 1.0)], 10.0, []        # a lone sound, then 10 minutes of bursts
        while t < 600:
            bursts.append(t)
            for _ in range(r.randint(10, 40)):
                hits.append((round(t, 2), 1.0))
                t += r.uniform(0.3, 0.8)
            t += r.uniform(10, 40)                       # eating, walking, chests: no block sounds
        plan = self.m.lay_out(hits, Path("s.wav"), "S", 60 / 130, 0.1, 136.0)
        self.assertNotIn(3.0, plan.hits)                 # the lone sound isn't building
        self.assertLess(plan.hits[0], 60)                # starts near the start of the build...
        self.assertGreater(plan.hits[-1], 540)           # ...and ends near the end
        firsts = [g.hit for g in plan.segments if g.hit][::5]
        self.assertEqual(firsts, sorted(firsts))
        for a, b in zip(plan.segments, plan.segments[1:]):
            self.assertGreaterEqual(b.src + 1e-6, a.src + a.dur * a.speed)

    def test_fast_songs_still_cut_on_every_beat(self):
        hits = [(10.0 + i * 0.12, 0.9) for i in range(400)]       # plenty of digging
        period = 60 / 170
        plan = self.m.lay_out(hits, Path("s.wav"), "S", period, 0.1, 120.0)
        self.assertAlmostEqual(min(g.dur for g in plan.segments if g.hit), period / 2, places=6)  # half beats
        self.assertAlmostEqual(max(g.dur for g in plan.segments if g.hit), period, places=6)     # never 2 beats
        for b in plan.beats:                                      # every cut on the song's own grid
            k = (b - self.m.PRE) / (period / 2)
            self.assertAlmostEqual(k, round(k), places=6)
        plan = self.m.lay_out(hits, Path("s.wav"), "S", 60 / 100, 0.1, 120.0)
        self.assertAlmostEqual(min(g.dur for g in plan.segments), 0.3, places=6)

    def test_steady_points_skip_menus(self):
        pts = self.m.steady_points(100.0, 110.0, [(103.0, 105.0)])
        times = [t for t, _ in pts]
        self.assertTrue(all(not 103.0 <= t <= 105.0 for t in times))
        self.assertAlmostEqual(times[1] - times[0], self.m.STEADY, places=6)
        plan = self.m.lay_out(pts, Path("s.wav"), "S", 0.5, 0.1, 120.0, target=6.0)
        self.assertIsNotNone(plan)                               # enough to cut on the beat


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
                               matcher=matcher_for(d), log=lambda *_: None)
            self.assertIsNotNone(res)
            info = media.probe(res.file)
            self.assertAlmostEqual(info.duration, res.frames / 30, delta=0.1)
            x = montage.decode(res.file, 0, info.duration)
            env, fps = montage.onset_envelope(x)
            found = [h[0] for h in montage.find_hits(env, fps, 0.0)]
            err = [min(abs(f - b) for f in found) for b in res.plan.beats[1:]]   # the first is too close to t=0 to measure
            self.assertGreaterEqual(sum(e < 0.02 for e in err), len(err) - 1)
            self.assertEqual(res.plan.hits, sorted(res.plan.hits))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_too_few_hits_still_makes_a_beat_cut_montage(self):
        from kdcut import montage
        d = Path(tempfile.mkdtemp())
        try:
            game(d / "game.wav", [5.0, 9.0, 14.0], seconds=20)
            song(d / "song.wav", seconds=30)

            def video(name, wav, seconds):
                out = d / name
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                                f"testsrc2=size=320x180:rate=30:duration={seconds}", "-i", str(wav),
                                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
                                "-g", "30", "-c:a", "aac", "-shortest", str(out)], check=True)
                return out
            track = music.Track(path=str(d / "song.wav"), title="S", artist="T", tags=[], duration=30.0, bpm="120")
            res = montage.make(video("rec.mkv", d / "game.wav", 20), 0, 0.0, 20.0, track, Fraction(30), 320, 180,
                               d / "m.mp4", -6.0, matcher=matcher_for(d), log=lambda *_: None)
            self.assertIsNotNone(res)                            # a montage, not a fast-forward
            self.assertFalse(res.plan.sounds)
            self.assertIn("beat cuts only", res.note)
            # Without Minecraft's sound files there's no telling blocks from eating: still cut on the beat.
            game(d / "busy.wav", list(np.arange(1, 19, 0.6)), seconds=20)
            res = montage.make(video("busy.mkv", d / "busy.wav", 20), 0, 0.0, 20.0, track, Fraction(30), 320, 180,
                               d / "m2.mp4", -6.0, matcher=None, log=lambda *_: None)
            self.assertIsNotNone(res)
            self.assertFalse(res.plan.sounds)
            # Too short to fill two bars: then it really can't be a montage.
            game(d / "tiny.wav", [0.5], seconds=2)
            self.assertIsNone(montage.make(video("tiny.mkv", d / "tiny.wav", 2), 0, 0.0, 2.0, track, Fraction(30), 320, 180,
                                           d / "m3.mp4", -6.0, matcher=None, log=lambda *_: None))
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

    def test_planner_takes_up_to_the_limit(self):
        from kdcut.blocks import Block
        bl = [Block(f"B{i}", 0, i * 100.0, i * 100.0 + 5, "building time.") for i in range(1, 6)]

        def fake(cfg, system, user, tool, desc, schema):
            self.assertIn("montage", schema["properties"])
            return {"drop": [], "chapters": [{"at": "B1", "title": "Intro"}],
                    "montage": [{"id": f"B{i}", "seconds": 90, "reason": "building"} for i in range(1, 6)]}
        plan = planner.make_plan(bl, [Path("r.mkv")], config.DEFAULTS, {}, [], {}, caller=fake,
                                 log=lambda *_: None, quiet_seconds={f"B{i}": 95.0 for i in range(1, 6)})
        self.assertEqual(len(plan.montage), 4)
        self.assertEqual(plan.montage[0]["seconds"], 90.0)
        two = config._merge(config.DEFAULTS, {"edit": {"max_montages": 2}})
        plan = planner.make_plan(bl, [Path("r.mkv")], two, {}, [], {}, caller=fake,
                                 log=lambda *_: None, quiet_seconds={f"B{i}": 95.0 for i in range(1, 6)})
        self.assertEqual(len(plan.montage), 2)

    def test_episode3_speed_effects_are_thinned_out(self):
        """Episode 3's real plan: a 30 s 'montage' (it came out 5 s long) and 4 fast-forwards,
        two of them in the first two minutes and two right next to the montage."""
        from kdcut.blocks import Block
        blocks = [Block(f"B{i:04d}", 0, i * 10.0, i * 10.0 + 5, "x") for i in range(1, 200)]
        plan = planner.Plan(
            montage=[{"id": "B0080", "seconds": 30.0, "reason": "mining end stone for bridges"},
                     {"id": "B0146", "seconds": 40.0, "reason": "mining mud"},
                     {"id": "B0169", "seconds": 120.0, "reason": "building the mob farm"}],
            fast=[{"id": "B0008", "seconds": 90.0, "speed": 8.0, "reason": "15 minute search for stronghold"},
                  {"id": "B0012", "seconds": 40.0, "speed": 6.0, "reason": "exploring stronghold corridors"},
                  {"id": "B0081", "seconds": 40.0, "speed": 6.0, "reason": "walking toward end city"},
                  {"id": "B0085", "seconds": 40.0, "speed": 6.0, "reason": "walking among chorus trees"}])
        notes = planner.space_out(plan, blocks, config.DEFAULTS)
        self.assertEqual(len(plan.montage), 3)                        # montages stay (min length is checked at render)
        self.assertEqual([f["id"] for f in plan.fast], ["B0008"])     # the 15-minute search he talks about
        self.assertEqual(len(notes), 3)
        self.assertTrue(any("B0012" in n and "too close" in n for n in notes))
        self.assertTrue(any("B0081" in n for n in notes))             # walking right after a montage: cut
        short = planner.Plan(montage=[{"id": "B0050", "seconds": 20.0, "reason": "placing a few blocks"}])
        planner.space_out(short, blocks, config.DEFAULTS)
        self.assertEqual(short.montage, [])                           # 20 s of footage is no montage

    def test_fast_forwards_are_capped(self):
        from kdcut.blocks import Block
        blocks = [Block(f"B{i}", 0, i * 10.0, i * 10.0 + 5, "x") for i in range(100)]
        plan = planner.Plan(fast=[{"id": f"B{i}", "seconds": 20.0 + i, "speed": 6.0, "reason": "walk"}
                                  for i in range(0, 100, 10)])
        planner.space_out(plan, blocks, config.DEFAULTS)
        self.assertEqual([f["id"] for f in plan.fast], ["B70", "B80", "B90"])   # the 3 longest, in order

    def test_montage_is_never_a_blip(self):
        from kdcut import montage
        self.m = montage
        hits = [(10.0 + i * 0.5, 0.9) for i in range(12)]              # 6 s of digging: not enough
        self.assertIsNone(self.m.lay_out(hits, Path("s.wav"), "S", 0.5, 0.1, 120.0, min_seconds=10.0))
        plan = self.m.lay_out([(10.0 + i * 0.4, 0.9) for i in range(150)], Path("s.wav"), "S", 0.5, 0.1,
                              120.0, min_seconds=10.0)
        self.assertGreaterEqual(plan.seconds, 10.0)

    def test_hands_on_fast_forwards_become_montages(self):
        # Episode 2's real plan: the editor picked fast-forwards for everything quiet.
        plan = planner.Plan(montage=[{"id": "B0023", "seconds": 78.0, "reason": "mining diamonds in caves"}],
                            fast=[{"id": "B0012", "seconds": 40.7, "speed": 6.0, "reason": "ride back home"},
                                  {"id": "B0042", "seconds": 120.0, "speed": 8.0, "reason": "mining through the nether"},
                                  {"id": "B0130", "seconds": 60.0, "speed": 6.0, "reason": "fortress corridors"},
                                  {"id": "B0153", "seconds": 30.0, "speed": 5.0, "reason": "walking warped forest"},
                                  {"id": "B0155", "seconds": 90.0, "speed": 6.0, "reason": "hunting endermen"},
                                  {"id": "B0170", "seconds": 50.0, "speed": 6.0, "reason": "building the bone meal farm"},
                                  {"id": "B0180", "seconds": 12.0, "speed": 6.0, "reason": "placing a few blocks"}])
        notes = planner.promote_montages(plan, config.DEFAULTS)
        self.assertEqual([m["id"] for m in plan.montage], ["B0023", "B0042", "B0170"])
        self.assertEqual(len(notes), 2)
        self.assertEqual({f["id"] for f in plan.fast}, {"B0012", "B0130", "B0153", "B0155", "B0180"})
        off = config._merge(config.DEFAULTS, {"edit": {"montages": False}})
        self.assertEqual(planner.promote_montages(planner.Plan(fast=[{"id": "B1", "seconds": 60, "reason": "mining"}]), off), [])


if __name__ == "__main__":
    unittest.main()


class FakeAI:
    def __call__(self, cfg, system, user, tool_name, desc, schema):
        if tool_name == "place_timelapses":
            return {"placements": [{"file": "House.mp4", "after": "B0001", "reason": "the house going up"}]}
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
            "thumbnails": {"enabled": False}, "music": {"folders": [str(mdir)]},
            "edit": {"minecraft_assets": str(fake_assets(cls.tmp / "mc"))}})
        words = [Word(2.0 + 0.35 * i, 2.3 + 0.35 * i, w) for i, w in enumerate("alright lets build the house.".split())]
        words += [Word(84.0 + 0.35 * i, 84.3 + 0.35 * i, w) for i, w in enumerate("and there it is.".split())]
        info = media.probe(rec)
        key = transcribe._cache_key(rec, media.pick_mic_track(info, "auto"), cfg["whisper"]["model"])
        transcribe.save_cache(out / ".work" / "rec.words.json", key, words)
        # A timelapse of the same build, played right after the montage.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=6",
                        "-c:v", "libx264", "-preset", "ultrafast", str(cls.tmp / "House.mp4")], check=True)
        cls.res = run(proj, cfg, use_ai=True, caller=FakeAI(), recordings=[rec], broll_files=[], out_dir=out,
                      video={"title": "Ep", "cold_open": False, "end_screen": False},
                      timelapse_files=[cls.tmp / "House.mp4"])
        cls.xml = ET.parse(cls.res["timeline"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_song_carries_on_under_the_timelapse_after_it(self):
        spine = list(next(self.xml.iter("spine")))
        names = [c.get("name") for c in spine]
        i = next(k for k, n in enumerate(names) if n.startswith("Montage - "))
        self.assertTrue(names[i + 1].startswith("Timelapse - House"))
        carry = [c for c in self.xml.iter() if (c.get("name") or "").startswith("Montage song continues")]
        self.assertEqual(len(carry), 1)
        review = self.res["review"].read_text(encoding="utf-8")
        self.assertIn("carries on from the montage under House.mp4", review)

    def test_montage_between_the_lines_with_its_own_music(self):
        spine = list(next(self.xml.iter("spine")))
        names = [c.get("name") for c in spine]
        i = next(k for k, n in enumerate(names) if n.startswith("Montage - "))
        self.assertTrue(0 < i < len(names) - 1)
        self.assertEqual(self.res["placed"]["montage"], 1)
        self.assertEqual(self.res["placed"]["fast"], 0)
        review = self.res["review"].read_text(encoding="utf-8")
        self.assertIn("on the beat of Click Party", review)
