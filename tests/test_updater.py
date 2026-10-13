import unittest

from kdcut import updater


def release(name, asset=True):
    return {"name": name, "tag_name": "app-latest", "body": "notes",
            "assets": [{"name": "KDcut-Setup.exe", "size": 123,
                        "browser_download_url": "https://example.invalid/KDcut-Setup.exe"}] if asset else []}


class UpdaterTests(unittest.TestCase):
    def test_version_compare(self):
        self.assertTrue(updater.is_newer("0.4.12", "0.4.9"))
        self.assertTrue(updater.is_newer("0.5", "0.4.99"))
        self.assertFalse(updater.is_newer("0.4.9", "0.4.9"))
        self.assertFalse(updater.is_newer("0.4.8", "0.4.9"))
        self.assertFalse(updater.is_newer("garbage", "0.4.9"))

    def test_release_parsing(self):
        upd = updater.from_release(release("KDcut 0.4.12"), "0.4.9")
        self.assertEqual(upd.version, "0.4.12")
        self.assertEqual(upd.size, 123)
        self.assertIsNone(updater.from_release(release("KDcut 0.4.9"), "0.4.9"))
        self.assertIsNone(updater.from_release(release("KDcut 0.4.12", asset=False), "0.4.9"))


if __name__ == "__main__":
    unittest.main()
