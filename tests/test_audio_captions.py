import shutil
import tempfile
import unittest
from unittest import mock
from fractions import Fraction
from pathlib import Path

from kdcut import captions, ducking
from kdcut.timeline import Clip, Timeline
from kdcut.transcribe import Word


class DuckingTests(unittest.TestCase):
    def test_envelope_dips_under_speech_and_recovers(self):
        g = ducking.envelope([(5, 10)], 20, talk_db=-24, gap_db=-14)
        at = lambda s: g[int(s * ducking.RATE)]
        self.assertAlmostEqual(at(2), 10 ** (-14 / 20), places=4)
        self.assertAlmostEqual(at(7), 10 ** (-24 / 20), places=4)
        self.assertLess(at(4.9), at(4.5))            # already ramping down before the first word
        self.assertAlmostEqual(at(15), 10 ** (-14 / 20), places=4)

    def test_short_gaps_stay_ducked(self):
        merged = ducking.merge([(0, 2), (2.5, 4), (10, 12)])
        self.assertEqual(merged, [(0, 4), (10, 12)])


class CaptionTests(unittest.TestCase):
    def test_words_follow_the_edit(self):
        tl = Timeline("t", Fraction(60), 1920, 1080)
        rec = Path("a.mkv")
        # Keep source 10-20s, placed at timeline 0.
        tl.main.append(Clip(path=rec, src_in=600, dur=600, rec=0))
        words = [[Word(5, 5.3, "cut"), Word(12, 12.4, "hello"), Word(12.5, 12.9, "there.")]]
        tw = captions.words_on_timeline(words, [rec], tl)
        self.assertEqual([w.text for w in tw], ["hello", "there."])
        self.assertAlmostEqual(tw[0].start, 2.0)
        srt = captions.to_srt(tw)
        self.assertIn("00:00:02,000 --> ", srt)
        self.assertIn("hello there.", srt)

    def test_chapters_retimed_against_export(self):
        export = [captions.TWord(i * 0.5, i * 0.5 + 0.4, w) for i, w in enumerate(
            "hey guys welcome back today we build the castle walls okay now we go mining for diamonds".split())]
        out = captions.retime_chapters([("Intro", "hey guys welcome back", 0.0),
                                        ("Castle", "today we build the castle", 30.0),
                                        ("Mining", "now we go mining for diamonds", 90.0),
                                        ("Gone", "this text was cut in resolve", 120.0)], export)
        self.assertEqual(out[0], (0.0, "Intro"))
        self.assertEqual(out[1], (2.0, "Castle"))
        self.assertEqual(out[2], (5.5, "Mining"))
        self.assertEqual(out[3], (120.0, "Gone"))



@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class StemTests(unittest.TestCase):
    def test_game_ducks_under_the_voice(self):
        """Elytra wind in Episode 3 sat only ~11 dB under the voice while he talked. Game audio
        now dips while the mic hears him and comes back up in the gaps, and loud moments are
        compressed with a safety limiter on top."""
        import subprocess
        from kdcut import audioprep, media
        d = Path(tempfile.mkdtemp())
        try:
            rec = d / "rec.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-y",
                            # voice: talking from 0.5 to 3 s, silent after
                            "-f", "lavfi", "-i", "aevalsrc='if(between(t,0.5,3),0.3*sin(2*PI*220*t),0)':d=8",
                            # game: steady wind-like noise the whole time, one bang at 6 s
                            "-f", "lavfi", "-i",
                            "aevalsrc='0.02*sin(2*PI*700*t)*sin(2*PI*3*t)+if(between(t,6,6.2),0.5*sin(2*PI*90*t),0)':d=8",
                            "-map", "0:a", "-map", "1:a", "-c:a", "aac", "-b:a", "256k", str(rec)], check=True)
            info = media.probe(rec)
            with mock.patch.object(audioprep, "LOUDNESS_CACHE", d / "loud.json"):
                stems = audioprep.split_tracks(rec, info, 0, d / "audio", log=lambda *_: None)
            game = next(s for s in stems if s.kind == "game")
            self.assertEqual(game.path.suffix, ".wav")

            def level(a, b):
                out = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(a), "-t", str(b - a), "-i", str(game.path),
                                      "-af", "astats=measure_perchannel=none", "-f", "null", "-"],
                                     capture_output=True, text=True).stderr
                return float(out.split("RMS level dB:")[1].split()[0])
            talking, quiet = level(1.2, 2.6), level(4.2, 5.6)
            self.assertGreater(quiet - talking, 6.0)                       # ducked while he talks
            _, peak = audioprep.measure(game.path)
            self.assertLess(peak, audioprep.GAME_CEILING + 1.5)            # the bang is held down
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_game_audio_is_wav_and_voice_is_aac(self):
        """Game tracks go digitally silent when the game is paused, which can freeze ffmpeg's AAC
        encoder: they're written as WAV straight away."""
        import subprocess
        from kdcut import audioprep, media
        d = Path(tempfile.mkdtemp())
        try:
            rec = d / "rec.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                            "-f", "lavfi", "-i", "aevalsrc=0:d=6",          # a silent game track
                            "-map", "0:a", "-map", "1:a", "-c:a", "aac", str(rec)], check=True)
            info = media.probe(rec)
            with mock.patch.object(audioprep, "LOUDNESS_CACHE", d / "loud.json"):
                stems = audioprep.split_tracks(rec, info, 0, d / "audio", log=lambda *_: None)
            kinds = {s.kind: s.path.suffix for s in stems}
            self.assertEqual(kinds["voice"], ".m4a")
            self.assertEqual(kinds.get("game"), ".wav")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
