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
