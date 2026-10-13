import email.message
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from kdcut import youtube


def http_error(code, headers=None, body=b""):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://x", code, "err", msg, io.BytesIO(body))


class FakeResp(io.BytesIO):
    def __init__(self, body=b"{}", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class MetadataTests(unittest.TestCase):
    def test_scheduled_videos_are_private_with_publish_time(self):
        meta = youtube.VideoMeta(title="Hi <3", description="a > b", tags=["minecraft"] * 200,
                                 privacy="public", publish_at="2026-10-20T17:00:00Z")
        body = json.loads(youtube._metadata_json(meta))
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(body["status"]["publishAt"], "2026-10-20T17:00:00Z")
        self.assertFalse(body["status"]["selfDeclaredMadeForKids"])
        self.assertNotIn("<", body["snippet"]["title"])
        self.assertNotIn(">", body["snippet"]["description"])
        self.assertLessEqual(sum(len(t) + 2 for t in body["snippet"]["tags"]), 480)
        self.assertEqual(body["snippet"]["categoryId"], "20")


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.video = self.tmp / "v.mp4"
        self.video.write_bytes(b"x" * 2500)

    def test_resumable_upload_with_a_dropped_connection(self):
        calls = []

        def fake(method, url, body=None, headers=None, timeout=120):
            calls.append((method, (headers or {}).get("Content-Range")))
            if method == "POST":
                return FakeResp(headers={"Location": "https://upload/session"})
            rng = headers["Content-Range"]
            if rng == "bytes 0-999/2500":
                raise http_error(308, {"Range": "bytes=0-999"})
            if rng == "bytes 1000-1999/2500" and not getattr(fake, "dropped", False):
                fake.dropped = True
                raise urllib.error.URLError("connection reset")
            if rng == "bytes */2500":
                raise http_error(308, {"Range": "bytes=0-999"})
            if rng == "bytes 1000-1999/2500":
                raise http_error(308, {"Range": "bytes=0-1999"})
            if rng == "bytes 2000-2499/2500":
                return FakeResp(json.dumps({"id": "abc123"}).encode())
            raise AssertionError(rng)

        seen = []
        with mock.patch.object(youtube, "_request", fake), mock.patch.object(youtube.time, "sleep"):
            vid = youtube.upload_video(self.video, youtube.VideoMeta("t", "d"), progress=seen.append, chunk=1000)
        self.assertEqual(vid, "abc123")
        self.assertIn(("PUT", "bytes */2500"), calls)          # asked where to resume
        self.assertEqual(seen[-1], 1.0)

    def test_quota_error_is_explained(self):
        body = json.dumps({"error": {"message": "x", "errors": [{"reason": "quotaExceeded"}]}}).encode()

        def fake(*a, **k):
            raise http_error(403, body=body)

        with mock.patch.object(youtube, "_request", fake):
            with self.assertRaises(youtube.YouTubeError) as ctx:
                youtube.upload_video(self.video, youtube.VideoMeta("t", "d"))
        self.assertIn("daily upload limit", str(ctx.exception))

    def test_captions_are_sent_as_multipart(self):
        sent = {}

        def fake(method, url, body=None, headers=None, timeout=120):
            sent.update(url=url, body=body, headers=headers)
            return FakeResp()

        with mock.patch.object(youtube, "_request", fake):
            youtube.upload_captions("vid1", "1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        self.assertIn("uploadType=multipart", sent["url"])
        self.assertIn(b'"videoId": "vid1"', sent["body"])
        self.assertIn(b"00:00:00,000 --> 00:00:01,000", sent["body"])


class SignInTests(unittest.TestCase):
    def test_loopback_flow_with_pkce(self):
        tmp = Path(tempfile.mkdtemp())
        client = tmp / "client.json"
        client.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "sec"}}))
        with mock.patch.object(youtube, "CLIENT_FILE", tmp / "c.json"), \
                mock.patch.object(youtube, "TOKEN_FILE", tmp / "t.json"):
            youtube.save_client_file(client)

            def browser(url):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                self.assertEqual(q["code_challenge_method"], ["S256"])
                self.assertIn("youtube.upload", q["scope"][0])
                cb = f"{q['redirect_uri'][0]}/?code=CODE&state={q['state'][0]}"
                direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                threading.Thread(target=lambda: direct.open(cb, timeout=10).read()).start()

            posted = {}

            def token(url, fields):
                posted.update(fields)
                return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

            with mock.patch.object(youtube, "_post_form", token):
                youtube.sign_in(open_browser=browser, timeout=20)
            self.assertEqual(posted["code"], "CODE")
            self.assertTrue(posted["code_verifier"])
            self.assertEqual(youtube.access_token(), "AT")
            self.assertTrue(youtube.signed_in())

    def test_bad_client_file_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "x.json"
        f.write_text("{}")
        with self.assertRaises(youtube.YouTubeError):
            youtube.save_client_file(f)


class PublishTests(unittest.TestCase):
    def test_chapters_and_captions_follow_the_final_export(self):
        import shutil
        import subprocess
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg missing")
        from kdcut import config, publish
        from kdcut.transcribe import Word
        out = Path(tempfile.mkdtemp())
        export = out / "final.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=90",
                        "-f", "lavfi", "-i", "sine=duration=90", "-c:v", "libx264", "-preset", "ultrafast",
                        "-shortest", str(export)], check=True)
        (out / "upload_kit.json").write_text(json.dumps({"chapter_anchors": [
            ["Intro", "hey guys welcome back", 0.0],
            ["The castle", "today we build the castle", 40.0],
            ["Diamonds", "now we go mining for diamonds", 80.0]]}))
        spoken = "hey guys welcome back".split() + ["filler"] * 20 + "today we build the castle".split() \
            + ["filler"] * 40 + "now we go mining for diamonds".split()
        words = [Word(i * 1.0, i * 1.0 + 0.5, w) for i, w in enumerate(spoken)]
        thumb = out / "t.jpg"
        thumb.write_bytes(b"jpg")
        calls = {}
        with mock.patch("kdcut.transcribe.transcribe_file", return_value=words), \
                mock.patch.object(youtube, "upload_video", side_effect=lambda p, meta, **k: calls.setdefault("meta", meta) and "VID"), \
                mock.patch.object(youtube, "set_thumbnail", side_effect=lambda v, p: calls.setdefault("thumb", v)), \
                mock.patch.object(youtube, "upload_captions", side_effect=lambda v, srt: calls.setdefault("srt", srt)):
            rec = publish.publish(out, export, config.DEFAULTS, title="My video", description="Desc.",
                                  tags=["minecraft"], thumbnail=thumb, log=lambda m: None)
        self.assertEqual(rec["video_id"], "VID")
        desc = calls["meta"].description
        self.assertIn("0:00 Intro", desc)
        self.assertIn("0:24 The castle", desc)      # moved from 0:40 to where it is in the export
        self.assertIn("1:09 Diamonds", desc)
        self.assertIn("hey guys welcome back", calls["srt"])
        self.assertTrue((out / "youtube.json").exists())


if __name__ == "__main__":
    unittest.main()
