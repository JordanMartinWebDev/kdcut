"""The channel's music feel: no funeral songs, scary/heavy music only where it's dangerous."""

import unittest

from kdcut import music


def track(title, mood="", energy="", genre="", path=None):
    return music.Track(path=path or f"/m/{title}.mp3", title=title, artist="X", tags=[], duration=200.0,
                       mood=mood, energy=energy, genre=genre)


GOOD_FATHER = track("A Good Father", "Hopeful, Laid Back", "Low", "Gospel, Praise & Worship")
HAPPY = track("A Little Bit of This", "Happy, Marching", "High", "Soul, Adventure")
SKY = track("The Way to the Sky", "Happy, Restless", "High", "Adventure, Modern Classical")
ELDERS = track("The Elders 2", "Sad, Sentimental", "Low", "Adventure, Beautiful")
SEA = track("Sea Adventures", "Epic, Heavy & Ponderous", "High", "Action, Adventure")
SHE = track("She Is Dead", "Mysterious; Weird", "Low")
CHILL = track("Easy Afternoon", "Laid Back, Happy", "Low", "Acoustic")
UNKNOWN = track("Lonely River")


class FeelTests(unittest.TestCase):
    def test_feel(self):
        self.assertEqual(music.feel(GOOD_FATHER), "somber")
        self.assertEqual(music.feel(ELDERS), "somber")
        self.assertEqual(music.feel(SEA), "scary")           # big moments only, not banned
        self.assertEqual(music.feel(SHE), "scary")
        self.assertEqual(music.feel(HAPPY), "bright")

    def test_upbeat_offers_no_funeral_and_no_low_energy(self):
        offered, special = music.for_vibe([GOOD_FATHER, HAPPY, SKY, ELDERS, SEA, SHE, CHILL, UNKNOWN])
        self.assertEqual({t.title for t in offered}, {"A Little Bit of This", "The Way to the Sky", "Lonely River"})
        self.assertEqual({t.title for t in special}, {"Sea Adventures", "She Is Dead"})

    def test_chill_allows_laid_back_but_never_sad(self):
        offered, _ = music.for_vibe([GOOD_FATHER, CHILL, ELDERS, HAPPY], "chill")
        self.assertEqual({t.title for t in offered}, {"Easy Afternoon", "A Little Bit of This"})

    def test_untagged_marked_and_listed_last(self):
        text = music.library_text([UNKNOWN, HAPPY], {})
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("- A Little Bit of This"))
        self.assertIn("[no mood tags]", lines[1])


class StartTests(unittest.TestCase):
    def test_music_starts_right_after_the_cold_open(self):
        raw = [(146.0, [HAPPY], "day one"), (302.0, [SKY], "cave")]
        out = music.from_the_start(raw, 8.7)
        self.assertEqual(out[0][0], 8.7)
        self.assertEqual(out[1][0], 302.0)

    def test_deliberate_silence_at_the_start_stays(self):
        raw = [(30.0, [], "let the intro breathe"), (90.0, [HAPPY], "")]
        self.assertEqual(music.from_the_start(raw, 8.7), raw)


class SpecialTests(unittest.TestCase):
    def test_scary_pick_outside_danger_is_swapped(self):
        cues = [(0.0, [HAPPY], "intro"), (200.0, [SHE, SEA], "looking for a cave"), (400.0, [SEA], "skeleton chase")]
        out, notes = music.guard_special(cues, [HAPPY, SKY], [SHE, SEA], lambda t: t >= 400, {})
        self.assertEqual(out[1][1][0].title, "The Way to the Sky")   # no everyday backup: closest everyday track
        self.assertEqual(out[2][1][0].title, "Sea Adventures")       # the chase keeps it
        self.assertEqual(len(notes), 1)
        self.assertIn("She Is Dead", notes[0])

    def test_everyday_backup_is_used_first(self):
        out, _ = music.guard_special([(10.0, [SHE, SKY], "")], [HAPPY, SKY], [SHE], lambda t: False, {})
        self.assertEqual(out[0][1], [SKY])

    def test_long_sections_continue_in_the_same_feel(self):
        lib = [HAPPY, SKY, SHE, SEA]
        cue = music.Cue(0.0, 700.0, [HAPPY])
        placed = music.fill([cue], lib, {}, seed=3)
        self.assertGreater(len(placed), 2)
        self.assertTrue(all(music.feel(p.track) == "bright" for p in placed))


if __name__ == "__main__":
    unittest.main()
