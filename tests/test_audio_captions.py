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
