"""Primary music folders: new music is used first, the rest of the library is the fallback."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kdcut import montage, music, shorts


def track(title, primary=False, mood="Happy", energy="High", genre="Pop", duration=200.0):
    return music.Track(path=f"/m/{title}.mp3", title=title, artist="X", tags=[], duration=duration,
                       mood=mood, energy=energy, genre=genre, primary=primary)


OLD_A, OLD_B = track("Old A"), track("Old B")
NEW_A, NEW_B = track("New A", True), track("New B", True)


class PrimaryTests(unittest.TestCase):
    def test_listing_puts_primary_first_under_its_own_heading(self):
        text = music.primary_text([OLD_A, NEW_A, OLD_B], {})
        self.assertTrue(text.startswith("PRIMARY"))
        self.assertLess(text.index("New A"), text.index("Rest of the library"))
        self.assertGreater(text.index("Old A"), text.index("Rest of the library"))

    def test_listing_without_primary_is_unchanged(self):
        self.assertEqual(music.primary_text([OLD_A, OLD_B], {}), music.library_text([OLD_A, OLD_B], {}))

    def test_long_section_continues_with_primary_music(self):
        cue = music.Cue(0, 1000, [NEW_A])
        pieces = music.fill([cue], [OLD_A, OLD_B, NEW_A, NEW_B], {})
        self.assertEqual([p.track.title for p in pieces[:2]], ["New A", "New B"])

    def test_falls_back_when_primary_runs_out(self):
        cue = music.Cue(0, 1000, [NEW_A])
        names = [p.track.title for p in music.fill([cue], [OLD_A, NEW_A], {})]
        self.assertIn("Old A", names)

    def test_montage_prefers_unused_primary_song(self):
        recent = {OLD_A.path: 0, NEW_A.path: time.time()}
        self.assertEqual(montage.pick_song([OLD_A, NEW_A], recent).title, "New A")
        self.assertEqual(montage.pick_song([OLD_A, NEW_A], {}, used={NEW_A.path}).title, "Old A")

    def test_short_prefers_primary(self):
        self.assertEqual(shorts.pick_track([OLD_A, OLD_B, NEW_A], 40, "x", {NEW_A.path: time.time()}).title, "New A")

    def test_scan_marks_primary_folder_tracks(self):
        with tempfile.TemporaryDirectory() as d:
            new, old = Path(d, "new"), Path(d, "old")
            new.mkdir(), old.mkdir()
            Path(new, "ES_Fresh - A.mp3").write_bytes(b"x")
            Path(old, "ES_Classic - B.mp3").write_bytes(b"x")
            Path(old, "ES_Fresh - A.mp3").write_bytes(b"x")          # same song in both
            info = mock.Mock(tags={}, duration=180.0)
            with mock.patch.object(music.media, "probe", return_value=info):
                lib = music.scan_library([old, Path(d, "missing")], cache_file=Path(d, "i.json"),
                                         log=lambda m: None, primary=[new, Path(d, "nope")])
            by = {t.title: t.primary for t in lib.music}
            self.assertEqual(by, {"Fresh": True, "Classic": False})
            # Cached re-scan without the primary folder: nothing primary any more.
            with mock.patch.object(music.media, "probe", return_value=info):
                lib = music.scan_library([old], cache_file=Path(d, "i.json"), log=lambda m: None)
            self.assertFalse(any(t.primary for t in lib.music))


if __name__ == "__main__":
    unittest.main()
