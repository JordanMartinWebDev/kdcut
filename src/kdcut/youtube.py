"""YouTube upload (standard library only).

Sign-in uses Google's desktop-app OAuth flow: the browser opens, you approve, Google sends
the code back to a tiny server on 127.0.0.1. The refresh token is stored in
~/.kdcut/youtube_token.json and only ever sent to Google.

You need your own Google Cloud "Desktop app" OAuth client (free); see the README.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from kdcut.config import HOME_DIR

SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.force-ssl")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
CLIENT_FILE = HOME_DIR / "youtube_client.json"
TOKEN_FILE = HOME_DIR / "youtube_token.json"
GAMING = "20"


class YouTubeError(RuntimeError):
    pass


class NeedsSignIn(YouTubeError):
    pass


# --------------------------------------------------------------------------------------
# Client + tokens
# --------------------------------------------------------------------------------------

def save_client_file(src: Path) -> None:
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    if "installed" not in data and "web" not in data:
        raise YouTubeError("That file isn't a Google OAuth client. Download the JSON for a "
                           "'Desktop app' client from Google Cloud Console.")
    CLIENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, CLIENT_FILE)


def _client() -> tuple[str, str]:
    if not CLIENT_FILE.is_file():
        raise NeedsSignIn("Choose your Google OAuth client file in Settings first.")
    data = json.loads(CLIENT_FILE.read_text(encoding="utf-8"))
    c = data.get("installed") or data.get("web") or {}
    return c["client_id"], c.get("client_secret", "")


def _post_form(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if "invalid_grant" in body:
            raise NeedsSignIn("Your YouTube sign-in expired. Sign in again in Settings.") from exc
        raise YouTubeError(f"Google sign-in error {exc.code}: {body[:300]}") from exc


def _save_token(tok: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tok), encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def signed_in() -> bool:
    return TOKEN_FILE.is_file() and CLIENT_FILE.is_file()


def sign_out() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


def sign_in(open_browser=webbrowser.open, timeout: float = 300) -> None:
    """Desktop OAuth with PKCE and a loopback redirect."""
    client_id, client_secret = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                result.update({k: v[0] for k, v in q.items()})
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = ("KDcut is connected to YouTube. You can close this tab." if ok
                   else "Sign-in didn't complete. You can close this tab and try again in KDcut.")
            self.wfile.write(f"<html><body style='font-family:sans-serif;padding:40px'><h2>{msg}</h2>"
                             "</body></html>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent",
    })
    open_browser(url)
    deadline = time.time() + timeout
    try:
        while "code" not in result and "error" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in result:
        raise YouTubeError("Sign-in was cancelled or timed out." if "error" not in result
                           else f"Google said: {result['error']}")
    tok = _post_form(TOKEN_URL, {"code": result["code"], "client_id": client_id,
                                 "client_secret": client_secret, "redirect_uri": redirect,
                                 "grant_type": "authorization_code", "code_verifier": verifier})
    if "refresh_token" not in tok:
        raise YouTubeError("Google didn't return a long-term sign-in. Try Sign out, then Sign in again.")
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    _save_token(tok)


def access_token() -> str:
    if not TOKEN_FILE.is_file():
        raise NeedsSignIn("Sign in to YouTube in Settings first.")
    tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    client_id, client_secret = _client()
    new = _post_form(TOKEN_URL, {"refresh_token": tok["refresh_token"], "client_id": client_id,
                                 "client_secret": client_secret, "grant_type": "refresh_token"})
    tok.update(access_token=new["access_token"],
               expires_at=time.time() + int(new.get("expires_in", 3600)) - 60)
    _save_token(tok)
    return tok["access_token"]


# --------------------------------------------------------------------------------------
# API calls
# --------------------------------------------------------------------------------------

def _request(method: str, url: str, body: bytes | None = None, headers: dict | None = None,
             timeout: float = 120):
    h = {"Authorization": f"Bearer {access_token()}"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    return urllib.request.urlopen(req, timeout=timeout)


def _api_error(exc: urllib.error.HTTPError) -> YouTubeError:
    body = exc.read().decode("utf-8", "replace")
    try:
        err = json.loads(body)["error"]
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        msg = err.get("message", body)
    except (ValueError, KeyError, TypeError):
        reason, msg = "", body
    friendly = {
        "quotaExceeded": "YouTube's daily upload limit for your API project was reached. Try tomorrow.",
        "uploadLimitExceeded": "YouTube says you've hit your channel's upload limit for today.",
        "forbidden": "YouTube refused the request. If this is the thumbnail, your channel may need "
                     "phone verification for custom thumbnails (youtube.com/verify).",
    }.get(reason)
    return YouTubeError(friendly or f"YouTube error {exc.code}: {msg[:300]}")


def channel_title() -> str:
    try:
        with _request("GET", f"{API}/channels?part=snippet&mine=true") as r:
            items = json.load(r).get("items", [])
    except urllib.error.HTTPError as exc:
        raise _api_error(exc) from exc
    return items[0]["snippet"]["title"] if items else "(no channel)"


def clean_text(text: str, limit: int) -> str:
    """YouTube rejects < and > in titles/descriptions."""
    text = (text or "").replace("<", "‹").replace(">", "›").strip()
    return text[:limit]


@dataclass
class VideoMeta:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    privacy: str = "private"             # private | unlisted | public
    publish_at: str | None = None        # RFC 3339 UTC, schedules a private video
    category: str = GAMING


def _metadata_json(meta: VideoMeta) -> bytes:
    tags, total = [], 0
    for t in meta.tags:
        t = clean_text(t, 100).replace(",", " ")
        if t and total + len(t) + 2 <= 480:
            tags.append(t)
            total += len(t) + 2
    status = {"privacyStatus": "private" if meta.publish_at else meta.privacy,
              "selfDeclaredMadeForKids": False}
    if meta.publish_at:
        status["publishAt"] = meta.publish_at
    return json.dumps({
        "snippet": {"title": clean_text(meta.title, 100) or "Untitled", "description":
                    clean_text(meta.description, 5000), "tags": tags, "categoryId": meta.category},
        "status": status,
    }).encode("utf-8")


def upload_video(path: Path, meta: VideoMeta, progress=None, cancelled=lambda: False,
                 chunk: int = 16 * 1024 * 1024) -> str:
    """Resumable upload; survives dropped connections. Returns the video id."""
    path = Path(path)
    total = path.stat().st_size
    try:
        with _request("POST", f"{UPLOAD_API}/videos?uploadType=resumable&part=snippet,status",
                      _metadata_json(meta), {"Content-Type": "application/json; charset=UTF-8",
                                             "X-Upload-Content-Length": str(total),
                                             "X-Upload-Content-Type": "video/*"}) as r:
            session = r.headers["Location"]
    except urllib.error.HTTPError as exc:
        raise _api_error(exc) from exc

    sent, failures = 0, 0
    with path.open("rb") as fh:
        while True:
            if cancelled():
                raise YouTubeError("Upload cancelled.")
            fh.seek(sent)
            data = fh.read(chunk)
            end = sent + len(data) - 1
            try:
                with _request("PUT", session, data, {"Content-Range": f"bytes {sent}-{end}/{total}"},
                              timeout=600) as r:
                    result = json.load(r)          # 200/201: finished
                    if progress:
                        progress(1.0)
                    return result["id"]
            except urllib.error.HTTPError as exc:
                if exc.code == 308:                 # chunk accepted, keep going
                    rng = exc.headers.get("Range")
                    sent = int(rng.split("-")[1]) + 1 if rng else 0
                    failures = 0
                    if progress:
                        progress(sent / total)
                    continue
                if exc.code in (500, 502, 503, 504) and failures < 6:
                    failures += 1
                    time.sleep(2 ** failures)
                    sent = _resume_position(session, total)
                    continue
                raise _api_error(exc) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if failures >= 6:
                    raise YouTubeError(f"Upload kept failing: {exc}") from exc
                failures += 1
                time.sleep(2 ** failures)
                sent = _resume_position(session, total)


def _resume_position(session: str, total: int) -> int:
    try:
        with _request("PUT", session, b"", {"Content-Range": f"bytes */{total}"}):
            return total
    except urllib.error.HTTPError as exc:
        if exc.code == 308:
            rng = exc.headers.get("Range")
            return int(rng.split("-")[1]) + 1 if rng else 0
        raise _api_error(exc) from exc


def set_thumbnail(video_id: str, image: Path) -> None:
    data = Path(image).read_bytes()
    if len(data) > 2_000_000:
        raise YouTubeError("Thumbnail is over YouTube's 2 MB limit.")
    try:
        with _request("POST", f"{UPLOAD_API}/thumbnails/set?videoId={video_id}&uploadType=media",
                      data, {"Content-Type": "image/jpeg"}):
            pass
    except urllib.error.HTTPError as exc:
        raise _api_error(exc) from exc


def upload_captions(video_id: str, srt: str, language: str = "en", name: str = "English") -> None:
    boundary = uuid.uuid4().hex
    meta = json.dumps({"snippet": {"videoId": video_id, "language": language, "name": name,
                                   "isDraft": False}})
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8") \
        + srt.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode("utf-8")
    try:
        with _request("POST", f"{UPLOAD_API}/captions?part=snippet&uploadType=multipart", body,
                      {"Content-Type": f"multipart/related; boundary={boundary}"}):
            pass
    except urllib.error.HTTPError as exc:
        raise _api_error(exc) from exc


def studio_url(video_id: str) -> str:
    return f"https://studio.youtube.com/video/{video_id}/edit"
