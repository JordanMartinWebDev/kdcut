import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image
    from kdcut import config, thumbnails
except ImportError:  # Pillow missing
    Image = None


def shot(path: Path):
    im = Image.new("RGB", (1920, 1080), (60, 64, 72))
    for x in range(1100, 1900, 60):          # busy right half
        for y in range(0, 1080, 60):
            im.paste((90 + (x * y) % 120, 90, 100), (x, y, x + 50, y + 50))
    im.save(path)


@unittest.skipIf(Image is None, "Pillow not installed")
class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.shot = self.tmp / "shot.png"
        shot(self.shot)
        chars = self.tmp / "poses"
        chars.mkdir()
        Image.new("RGBA", (400, 800), (250, 200, 30, 255)).save(chars / "waving.png")
        self.logo = self.tmp / "logo.png"
        Image.new("RGBA", (800, 200), (255, 230, 0, 255)).save(self.logo)
        self.cfg = config._merge(config.DEFAULTS, {"thumbnails": {
            "characters_dir": str(chars), "themes_dir": str(self.tmp / "themes")}})

    def test_text_goes_on_the_quiet_side(self):
        with Image.open(self.shot) as im:
            self.assertEqual(thumbnails.busier_side(im), "right")

    def test_without_ai_uses_defaults_and_keeps_logo(self):
        path = thumbnails.theme_path(self.cfg, "Survival Kingdom")
        thumbnails.Theme(name="Survival Kingdom", logo=str(self.logo)).save(path)
        out = thumbnails.make(self.cfg, {"title": "Ep 3", "series": "Survival Kingdom", "episode": 3},
                              [self.shot], self.tmp, titles=["I Built a Castle"],
                              text_ideas=["CASTLE DONE", "FINALLY"], index=None, use_ai=False)
        self.assertEqual(len(out), 3)
        for p in out:
            im = Image.open(p)
            self.assertEqual(im.size, (1280, 720))
            # Bottom-right corner (YouTube timestamp area) is left as background.
            self.assertLess(max(im.crop((1150, 650, 1270, 710)).convert("L").getextrema()), 200)
        theme = thumbnails.Theme.load(path)
        self.assertEqual(theme.logo, str(self.logo))
        self.assertFalse(theme.designed)
        concepts = (self.tmp / "thumbnails" / "concepts.json").read_text()
        self.assertIn('"text_side": "left"', concepts)
        self.assertIn("waving", concepts)

    def test_new_ideas_never_overwrite(self):
        args = dict(titles=["T"], text_ideas=["ONE"], index=None, use_ai=False)
        first = thumbnails.make(self.cfg, {"title": "x"}, [self.shot], self.tmp, **args)
        second = thumbnails.make(self.cfg, {"title": "x"}, [self.shot], self.tmp, **args)
        self.assertFalse(set(first) & set(second))

    def test_ai_designs_theme_around_logo(self):
        path = thumbnails.theme_path(self.cfg, "Survival Kingdom")
        thumbnails.Theme(name="Survival Kingdom", logo=str(self.logo)).save(path)
        seen = {}

        def fake(cfg, system, content, tool, desc, schema):
            seen["content"] = content
            seen["schema"] = schema
            return {"theme": {"primary": "#F5C518", "accent": "#9BE22D", "font": "x"},
                    "concepts": [{"background": 1, "text": "WOW", "text_side": "left", "character": "waving"}]}

        thumbnails.make(self.cfg, {"title": "x", "series": "Survival Kingdom", "episode": 4}, [self.shot],
                        self.tmp, titles=["T"], text_ideas=[], index=None, use_ai=True, caller=fake)
        self.assertIn("theme", seen["schema"]["properties"])
        texts = " ".join(b.get("text", "") for b in seen["content"])
        self.assertIn("series logo", texts)
        theme = thumbnails.Theme.load(path)
        self.assertTrue(theme.designed)
        self.assertEqual(theme.accent, "#9BE22D")
        self.assertEqual(theme.logo, str(self.logo))      # kept


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(Image is None, "Pillow not installed")
class PoseLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.shot = self.tmp / "shot.png"
        shot(self.shot)
        self.chars = self.tmp / "poses"
        self.chars.mkdir()
        # Left half red, right half blue: tells us whether the pose was flipped.
        pose = Image.new("RGBA", (400, 800), (220, 30, 30, 255))
        pose.paste((30, 30, 220, 255), (200, 0, 400, 800))
        pose.save(self.chars / "shocked.png")
        Image.new("RGBA", (10, 10), (0, 0, 0, 255)).save(self.chars / "_preview.png")
        self.cfg = config._merge(config.DEFAULTS, {"thumbnails": {
            "characters_dir": str(self.chars), "themes_dir": str(self.tmp / "themes")}})

    def write_library(self, data):
        import json
        (self.chars / "poses.json").write_text(json.dumps(data), encoding="utf-8")

    def test_reads_moods_facing_and_glow(self):
        self.write_library({"_style": {"glow": "#FFC440"},
                            "shocked": {"mood": "hands by face; surprise", "faces": "right"},
                            "odd": {"faces": "sideways"}, "plain": "just a mood"})
        poses, style = thumbnails.pose_library(self.cfg)
        self.assertEqual(style["glow"], "#FFC440")
        self.assertEqual(poses["shocked"].faces, "right")
        self.assertEqual(poses["odd"].faces, "left")        # unknown value falls back
        self.assertEqual(poses["plain"].mood, "just a mood")

    def test_missing_or_broken_library_is_harmless(self):
        self.assertEqual(thumbnails.pose_library(self.cfg), ({}, {}))
        (self.chars / "poses.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(thumbnails.pose_library(self.cfg), ({}, {}))

    def test_underscore_files_are_not_poses(self):
        names = [p.stem for p in thumbnails.list_characters(self.cfg)]
        self.assertEqual(names, ["shocked"])

    def test_ai_sees_pose_moods(self):
        self.write_library({"shocked": {"mood": "hands by face; surprise"}})
        seen = {}

        def fake(cfg, system, content, tool, desc, schema):
            seen["content"] = content
            return {"theme": {"primary": "#F5C518", "accent": "#9BE22D", "font": "x"},
                    "concepts": [{"background": 1, "text": "WOW", "text_side": "left", "character": "shocked"}]}

        thumbnails.make(self.cfg, {"title": "x"}, [self.shot], self.tmp, titles=["T"], text_ideas=[],
                        index=None, use_ai=True, caller=fake)
        texts = " ".join(b.get("text", "") for b in seen["content"])
        self.assertIn("- shocked: hands by face; surprise", texts)

    def _render(self, faces, side, glow=""):
        poses = {"shocked": thumbnails.PoseInfo(faces=faces)}
        c = thumbnails.Concept(text="WOW", text_side=side, character="shocked")
        return thumbnails.render(c, self.shot, thumbnails.Theme(), characters={"shocked": self.chars / "shocked.png"},
                                 poses=poses, glow=glow)

    def _left_half_is_red(self, img, side):
        # Sample the middle of the character, which sits opposite the text.
        xs = (1080, 1180) if side == "left" else (100, 200)
        a, b = img.getpixel((xs[0], 500)), img.getpixel((xs[1], 500))
        return a[0] > a[2] and b[2] > b[0]

    def test_pose_is_turned_toward_the_words(self):
        # A pose looking left stays as is on the right side, and is flipped on the left side.
        self.assertTrue(self._left_half_is_red(self._render("left", "left"), "left"))
        self.assertFalse(self._left_half_is_red(self._render("left", "right"), "right"))
        # A pose looking right is the opposite.
        self.assertFalse(self._left_half_is_red(self._render("right", "left"), "left"))
        self.assertTrue(self._left_half_is_red(self._render("right", "right"), "right"))

    def test_glow_tints_around_the_character(self):
        plain = self._render("left", "left")
        glowing = self._render("left", "left", glow="#00FF00")
        # Just outside the character's left edge (past the white outline), the glow adds green.
        edge = next(x for x in range(640, 1280) if plain.getpixel((x, 500))[0] > 200 and plain.getpixel((x, 500))[2] < 80)
        x = edge - 30
        self.assertGreater(glowing.getpixel((x, 500))[1], plain.getpixel((x, 500))[1] + 20)
