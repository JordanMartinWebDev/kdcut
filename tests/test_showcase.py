import json
import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image, ImageDraw
    from kdcut import config, showcase, thumbnails
except ImportError:  # Pillow missing
    Image = None


def shot(path: Path, color, mark=None):
    im = Image.new("RGB", (1920, 1080), color)
    if mark:   # a bright square = "the subject"
        x, y = mark
        ImageDraw.Draw(im).rectangle((x - 60, y - 60, x + 60, y + 60), fill=(255, 0, 255))
    im.save(path)


@unittest.skipIf(Image is None, "Pillow not installed")
class ShowcaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.shots = []
        for i, c in enumerate([(60, 140, 200), (80, 160, 70), (150, 120, 90), (90, 90, 140), (200, 180, 120)]):
            p = self.tmp / f"s{i}.png"
            shot(p, c, mark=(1500, 700) if i == 0 else None)
            self.shots.append(p)
        self.chars = self.tmp / "poses"
        self.chars.mkdir()
        Image.new("RGBA", (300, 700), (230, 150, 40, 255)).save(self.chars / "point_side.png")
        self.logo = self.tmp / "logo.png"
        Image.new("RGBA", (800, 260), (255, 210, 40, 255)).save(self.logo)
        self.cfg = config._merge(config.DEFAULTS, {"thumbnails": {
            "characters_dir": str(self.chars), "themes_dir": str(self.tmp / "themes")}})
        self.theme = thumbnails.Theme(name="Survival Kingdom", logo=str(self.logo), badge="EP {n}")

    def test_showcase_is_the_default_style(self):
        self.assertEqual(config.DEFAULTS["thumbnails"]["style"], "showcase")

    def test_title_block_has_logo_and_episode_line(self):
        tb = showcase.title_block(self.theme, 7, None, 600, 300)
        self.assertLessEqual(tb.width, 600 + 60)     # + shadow padding
        # the episode line is drawn in the cyan label colour under the logo
        px = [p for p in tb.convert("RGB").getdata() if p[2] > 200 and p[0] < 120]
        self.assertTrue(px)
        self.assertIsNone(showcase.title_block(thumbnails.Theme(name="", badge=""), None, None, 600, 300))

    def test_vista_keeps_the_screenshot_bright(self):
        sc = showcase.Showcase("vista", [showcase.Shot(self.shots[1])])
        im = showcase.render(sc, self.theme, episode=1)
        self.assertEqual(im.size, (1280, 720))
        # bottom-right is the plain screenshot: not darkened by a gradient or vignette
        r, g, b = im.getpixel((1200, 650))
        self.assertGreater(g, 140)

    def test_focus_puts_the_subject_where_asked(self):
        s = showcase.Shot(self.shots[0], focus=(1500 / 1920, 700 / 1080), zoom=1.3)
        fr = showcase._frame(s, (1280, 720), at=(0.68, 0.58))
        self.assertEqual(fr.getpixel((int(1280 * 0.68), int(720 * 0.58))), (255, 0, 255))

    def test_hero_puts_character_opposite_the_subject(self):
        sc = showcase.Showcase("hero", [showcase.Shot(self.shots[0], focus=(0.78, 0.65))],
                               character="point_side", character_side="left")
        chars = {p.stem: p for p in thumbnails.list_characters(self.cfg)}
        im = showcase.render(sc, self.theme, characters=chars, episode=2)
        r, g, b = im.getpixel((150, 600))
        self.assertGreater(r, 200)                     # character on the left
        xs = [i % 1280 for i, p in enumerate(im.getdata()) if p[0] > 230 and p[1] < 40 and p[2] > 230]
        self.assertTrue(xs)                            # the subject is visible (not covered)
        self.assertGreater(sum(xs) / len(xs), 1280 * 0.6)    # ...on the open right side

    def test_collage_needs_three_and_falls_back(self):
        two = showcase.Showcase("collage", [showcase.Shot(p) for p in self.shots[:2]])
        self.assertEqual(showcase.render(two, self.theme).size, (1280, 720))
        five = showcase.Showcase("collage", [showcase.Shot(p) for p in self.shots])
        im = showcase.render(five, self.theme, episode=3)
        gutter = im.getpixel((int(1280 * 0.29) + 3, 100))
        self.assertLess(sum(gutter), 120)              # dark gutter between tiles

    def test_make_without_ai(self):
        out = thumbnails.make(self.cfg, {"title": "x", "series": "Survival Kingdom", "episode": 1},
                              self.shots[:3], self.tmp, titles=["T"], text_ideas=["WOW!!"], index=None,
                              use_ai=False)
        self.assertEqual(len(out), 3)
        layouts = [c["layout"] for c in json.loads((self.tmp / "thumbnails" / "concepts.json").read_text())]
        self.assertEqual(layouts, ["vista", "hero", "collage"])
        theme = json.loads(thumbnails.theme_path(self.cfg, "Survival Kingdom").read_text())
        self.assertEqual(theme["badge"], "EPISODE {n}")

    def test_make_with_ai_uses_its_choices_and_no_hype(self):
        seen = {}

        def fake(cfg, system, content, tool, desc, schema):
            seen["system"], seen["schema"] = system, schema
            return {"theme": {"accent": "#FFD23F"},
                    "concepts": [{"layout": "hero", "backgrounds": [1], "focus": {"x": 0.78, "y": 0.65},
                                  "zoom": 3.0, "character": "point_side", "character_side": "left",
                                  "text": "THE BIG CASTLE TOUR TODAY"},
                                 {"layout": "collage", "backgrounds": [3, 1, 2, 9]}]}

        out = thumbnails.make(self.cfg, {"title": "x", "series": "Survival Kingdom", "episode": 4},
                              self.shots[:3], self.tmp, titles=["T"], text_ideas=[], index=None,
                              use_ai=True, caller=fake)
        self.assertEqual(len(out), 2)
        self.assertIn("no hype words", seen["system"])
        self.assertNotIn("icons", json.dumps(seen["schema"]))
        cs = json.loads((self.tmp / "thumbnails" / "concepts.json").read_text())
        self.assertEqual(cs[0]["shots"][0]["zoom"], 1.6)                    # clamped
        self.assertEqual(cs[0]["text"], "THE BIG CASTLE")                  # at most 3 words
        self.assertEqual([s["path"] for s in cs[1]["shots"]],
                         [str(self.shots[2]), str(self.shots[0]), str(self.shots[1])])   # bad index dropped
