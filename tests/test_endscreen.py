import json
import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image
    from kdcut import config, endscreen, pipeline, thumbnails
except ImportError:  # Pillow missing
    Image = None


@unittest.skipIf(Image is None, "Pillow not installed")
class EndScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.frame = self.tmp / "frame.png"
        Image.new("RGB", (640, 360), (40, 160, 60)).save(self.frame)
        self.pose = self.tmp / "point_side.png"
        # left half red, right half blue: tells us whether it was mirrored
        im = Image.new("RGBA", (300, 600), (220, 30, 30, 255))
        im.paste((30, 30, 220, 255), (150, 0, 300, 600))
        im.save(self.pose)

    def render(self, **kw):
        return endscreen.render(self.tmp / "es.png", 1920, 1080, frame=self.frame,
                                theme=thumbnails.Theme(accent="#FFC440"), series="Survival Kingdom",
                                character=self.pose, **kw)

    def test_layout_and_labels(self):
        es = self.render()
        self.assertEqual(Image.open(es.image).size, (1920, 1080))
        self.assertEqual(es.labels, ["WATCH NEXT", "MORE SURVIVAL KINGDOM"])
        self.assertEqual(len(es.slots), 2)
        for x, y, w, h in es.slots:
            self.assertAlmostEqual(w / h, 16 / 9, places=2)      # YouTube video elements are 16:9

    def test_series_label_without_series(self):
        es = endscreen.render(self.tmp / "es.png", 1280, 720, labels=["WATCH NEXT", "MORE {series}"])
        self.assertEqual(es.labels, ["WATCH NEXT", "MORE VIDEOS"])
        self.assertEqual(Image.open(es.image).size, (1280, 720))
        self.assertEqual(es.slots[0], (66, 100, 469, 264))       # scaled with the frame

    def test_slots_are_empty_and_character_stays_off_the_circle(self):
        es = self.render()
        im = Image.open(es.image).convert("RGB")
        x, y, w, h = es.slots[0]
        self.assertLess(max(im.crop((x + 20, y + 20, x + w - 20, y + h - 20)).convert("L").getextrema()), 40)
        cx, cy, cw, ch = es.subscribe
        inner = im.crop((cx + 45, cy + 45, cx + cw - 45, cy + ch - 45)).convert("L")   # square inside the circle
        self.assertLess(max(inner.getextrema()), 40)              # nothing drawn over the circle

    def test_character_looks_toward_the_slots(self):
        def left_is_red(es):
            im = Image.open(es.image).convert("RGB")
            row = [im.getpixel((x, 700)) for x in range(1200, 1920)]
            reds = [i for i, p in enumerate(row) if p[0] > 180 and p[2] < 90]
            blues = [i for i, p in enumerate(row) if p[2] > 180 and p[0] < 90]
            return min(reds) < min(blues)
        self.assertTrue(left_is_red(self.render(faces="left")))
        self.assertFalse(left_is_red(self.render(faces="right")))

    def test_pose_choice_falls_back(self):
        self.assertEqual(pipeline.END_SCREEN_POSES[0], "point_side")
        self.assertEqual(config.DEFAULTS["edit"]["end_screen_seconds"], 20)
