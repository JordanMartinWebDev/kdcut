"""Channel stats from YouTube Studio: what's working, read straight from YouTube.

Uses the same sign-in as uploads (read-only analytics permission on top):

- YouTube Analytics API: views, watch time, average view duration and percentage, subscribers
  gained, likes, traffic sources, retention, Shorts vs long-form.
- YouTube Reporting API: thumbnail impressions and click-through rate. YouTube only offers these
  as daily bulk reports, so KDcut asks for the report once and reads each day's file as it
  arrives (the first ones show up a day or two after setup).

Claude reads these through `kdcut.mcp_server` (live, while the desktop app is open) and through
the daily snapshot in `Videos/KDcut/Channel stats` (written each morning by a scheduled task).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import urllib.error
import urllib.parse
from pathlib import Path

from kdcut import youtube
from kdcut.config import HOME_DIR

ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
REPORTING = "https://youtubereporting.googleapis.com/v1"
REACH_REPORT = "channel_reach_basic_a1"
STATS_DIR = HOME_DIR / "stats"
REACH_FILE = STATS_DIR / "reach.json"
SHORT_MAX_SECONDS = 180

VIDEO_METRICS = ["views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
                 "subscribersGained", "likes", "comments", "shares"]


class StatsError(youtube.YouTubeError):
    pass


def _today() -> dt.date:
    return dt.date.today()


def _get(url: str) -> dict:
    try:
        with youtube._request("GET", url, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        raise _error(exc) from exc


def _error(exc: urllib.error.HTTPError) -> StatsError:
    body = exc.read().decode("utf-8", "replace")
    if exc.code == 403 and ("insufficient" in body.lower() or "scope" in body.lower()):
        return StatsError("KDcut isn't allowed to read channel stats yet. In KDcut: Settings → YouTube → "
                          "Allow channel stats (and enable the YouTube Analytics API and YouTube "
                          "Reporting API in your Google Cloud project).")
    if exc.code == 403 and ("has not been used" in body or "disabled" in body):
        api = "YouTube Reporting API" if "reporting" in body.lower() else "YouTube Analytics API"
        return StatsError(f"The {api} is turned off in your Google Cloud project. Enable it at "
                          "console.cloud.google.com → APIs & Services → Library, then try again.")
    try:
        msg = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        msg = body[:300]
    return StatsError(f"YouTube stats error {exc.code}: {msg}")


# --------------------------------------------------------------------------------------
# YouTube Analytics API
# --------------------------------------------------------------------------------------

def report(metrics: list[str], start: dt.date, end: dt.date, dimensions: list[str] | None = None,
           filters: str = "", sort: str = "", max_results: int = 0) -> list[dict]:
    """One Analytics API query, as a list of {column: value} rows."""
    q = {"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
         "metrics": ",".join(metrics)}
    if dimensions:
        q["dimensions"] = ",".join(dimensions)
    if filters:
        q["filters"] = filters
    if sort:
        q["sort"] = sort
    if max_results:
        q["maxResults"] = str(max_results)
    data = _get(ANALYTICS + "?" + urllib.parse.urlencode(q))
    cols = [h["name"] for h in data.get("columnHeaders", [])]
    return [dict(zip(cols, row)) for row in data.get("rows") or []]


def _round(v):
    return round(v, 2) if isinstance(v, float) else v


def overview(days: int = 28) -> dict:
    """Channel totals for the last `days` days, the period before it, and Shorts vs long-form."""
    end = _today() - dt.timedelta(days=1)            # today's numbers are still filling in
    start = end - dt.timedelta(days=days - 1)
    m = ["views", "estimatedMinutesWatched", "averageViewDuration", "subscribersGained",
         "subscribersLost", "likes"]
    now = (report(m, start, end) or [{}])[0]
    before = (report(m, start - dt.timedelta(days=days), start - dt.timedelta(days=1)) or [{}])[0]
    out = {"period": f"{start} to {end}", "totals": {k: _round(v) for k, v in now.items()},
           "previous_period": {k: _round(v) for k, v in before.items()}}
    try:
        split = report(["views", "estimatedMinutesWatched", "subscribersGained"], start, end,
                       ["creatorContentType"])
        out["by_format"] = {r["creatorContentType"]: {k: v for k, v in r.items() if k != "creatorContentType"}
                            for r in split}
    except StatsError as exc:
        out["by_format"] = f"not available ({exc})"
    return out


def traffic_sources(days: int = 28, video_id: str = "") -> list[dict]:
    end = _today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    rows = report(["views", "estimatedMinutesWatched"], start, end, ["insightTrafficSourceType"],
                  filters=f"video=={video_id}" if video_id else "", sort="-views")
    total = sum(r["views"] for r in rows) or 1
    return [{"source": r["insightTrafficSourceType"], "views": r["views"],
             "share": f"{100 * r['views'] / total:.0f}%"} for r in rows]


def retention(video_id: str, points: int = 10) -> list[dict]:
    """How many viewers are still watching at each point (1.0 = everyone who started)."""
    end = _today()
    rows = report(["audienceWatchRatio", "relativeRetentionPerformance"], dt.date(2005, 2, 14), end,
                  ["elapsedVideoTimeRatio"], filters=f"video=={video_id}")
    if not rows:
        return []
    step = max(len(rows) // points, 1)
    keep = rows[::step]
    if keep[-1] is not rows[-1]:
        keep.append(rows[-1])
    return [{"at": f"{100 * r['elapsedVideoTimeRatio']:.0f}%", "still_watching": round(r["audienceWatchRatio"], 2),
             "vs_similar_videos": round(r["relativeRetentionPerformance"], 2)} for r in keep]


def daily_views(video_id: str, days: int = 14) -> list[dict]:
    end = _today()
    start = end - dt.timedelta(days=days - 1)
    rows = report(["views", "subscribersGained"], start, end, ["day"], filters=f"video=={video_id}",
                  sort="day")
    return [{"day": r["day"], "views": r["views"], "subs": r["subscribersGained"]} for r in rows]


# --------------------------------------------------------------------------------------
# Videos (Data API) joined with their analytics
# --------------------------------------------------------------------------------------

def _seconds(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def uploads(limit: int = 15) -> list[dict]:
    """Your latest uploads (public, unlisted and scheduled), newest first."""
    ch = _get(f"{youtube.API}/channels?part=contentDetails&mine=true").get("items") or []
    if not ch:
        raise StatsError("No YouTube channel on this account.")
    playlist = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    items = _get(f"{youtube.API}/playlistItems?part=contentDetails&maxResults={min(limit, 50)}"
                 f"&playlistId={playlist}").get("items") or []
    ids = [i["contentDetails"]["videoId"] for i in items]
    if not ids:
        return []
    vids = _get(f"{youtube.API}/videos?part=snippet,contentDetails,statistics,status&id={','.join(ids)}"
                ).get("items") or []
    by_id = {v["id"]: v for v in vids}
    out = []
    for vid in ids:
        v = by_id.get(vid)
        if not v:
            continue
        secs = _seconds(v["contentDetails"].get("duration", ""))
        out.append({"id": vid, "title": v["snippet"]["title"],
                    "published": v["snippet"].get("publishedAt", "")[:10],
                    "status": v["status"].get("privacyStatus", ""),
                    "length": f"{secs // 60}:{secs % 60:02d}",
                    "format": "short" if secs <= SHORT_MAX_SECONDS and secs else "long",
                    "lifetime_views": int(v["statistics"].get("viewCount", 0))})
    return out


def video_table(limit: int = 15, kind: str = "all") -> list[dict]:
    """Latest uploads with their numbers since publishing (views, watch time, retention,
    subs, impressions and click-through rate)."""
    vids = [v for v in uploads(max(limit, 15)) if kind == "all" or v["format"] == kind][:limit]
    public = [v for v in vids if v["status"] == "public"]
    if public:
        start = min(dt.date.fromisoformat(v["published"]) for v in public)
        rows = report(VIDEO_METRICS, start, _today(), ["video"],
                      filters="video==" + ",".join(v["id"] for v in public), sort="-views",
                      max_results=200)
        by_id = {r["video"]: r for r in rows}
        reach = reach_by_video()
        for v in public:
            r = by_id.get(v["id"], {})
            v.update({k: _round(r.get(k)) for k in VIDEO_METRICS if k in r})
            if r.get("averageViewDuration") is not None:
                s = int(r["averageViewDuration"])
                v["averageViewDuration"] = f"{s // 60}:{s % 60:02d}"
            if v["id"] in reach:
                v.update(reach[v["id"]])
    return vids


def find_video(query: str) -> dict:
    """A video by id, URL or words from its title (latest 50 uploads)."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", query)
    if m or re.fullmatch(r"[\w-]{11}", query.strip()):
        vid = m.group(1) if m else query.strip()
        return next((v for v in uploads(50) if v["id"] == vid), {"id": vid, "title": vid, "published": ""})
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    best, score = None, 0.0
    for v in uploads(50):
        t = set(re.findall(r"[a-z0-9]+", v["title"].lower()))
        s = len(words & t) / max(len(words), 1)
        if s > score:
            best, score = v, s
    if best is None or score < 0.5:
        raise StatsError(f"No recent upload matches '{query}'.")
    return best


def video_details(query: str) -> dict:
    v = find_video(query)
    out = {"video": v}
    start = dt.date.fromisoformat(v["published"]) if v.get("published") else _today() - dt.timedelta(days=365)
    rows = report(VIDEO_METRICS, start, _today(), filters=f"video=={v['id']}")
    out["since_publish"] = {k: _round(val) for k, val in (rows[0] if rows else {}).items()}
    out["daily"] = daily_views(v["id"], 14)
    out["traffic_sources"] = traffic_sources(max((_today() - start).days + 1, 1), v["id"])
    out["retention"] = retention(v["id"])
    reach = reach_by_video().get(v["id"])
    out["impressions"] = reach or "no impression data yet (YouTube sends it daily, 1-2 days behind)"
    return out


# --------------------------------------------------------------------------------------
# YouTube Reporting API: impressions and click-through rate
# --------------------------------------------------------------------------------------

def _load_reach() -> dict:
    try:
        return json.loads(REACH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"job": "", "done": [], "videos": {}}


def _save_reach(data: dict) -> None:
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REACH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(REACH_FILE)


def ensure_reach_job() -> str:
    """Ask YouTube (once) to produce the daily impressions report. Returns the job id."""
    data = _load_reach()
    jobs = _get(f"{REPORTING}/jobs").get("jobs") or []
    job = next((j for j in jobs if j.get("reportTypeId") == REACH_REPORT), None)
    if job is None:
        body = json.dumps({"reportTypeId": REACH_REPORT, "name": "KDcut impressions"}).encode()
        try:
            with youtube._request("POST", f"{REPORTING}/jobs", body,
                                  {"Content-Type": "application/json"}, timeout=60) as r:
                job = json.load(r)
        except urllib.error.HTTPError as exc:
            raise _error(exc) from exc
    if data.get("job") != job["id"]:
        data["job"] = job["id"]
        _save_reach(data)
    return job["id"]


def parse_reach_csv(text: str) -> dict[str, dict[str, list[float]]]:
    """video id -> date -> [impressions, clicks] from one channel_reach_basic_a1 file."""
    out: dict[str, dict[str, list[float]]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        vid = row.get("video_id", "")
        if not vid:
            continue
        imp = float(row.get("video_thumbnail_impressions") or 0)
        ctr = float(row.get("video_thumbnail_impressions_ctr") or 0)
        day = row.get("date", "")
        cell = out.setdefault(vid, {}).setdefault(day, [0.0, 0.0])
        cell[0] += imp
        cell[1] += imp * ctr
    return out


def sync_reach(log=lambda m: None) -> int:
    """Download any new daily impression files. Returns how many were added."""
    job = ensure_reach_job()
    data = _load_reach()
    done = set(data.get("done", []))
    added = 0
    url = f"{REPORTING}/jobs/{job}/reports"
    while url:
        page = _get(url)
        for rep in page.get("reports") or []:
            if rep["id"] in done or not rep.get("downloadUrl"):
                continue
            try:
                with youtube._request("GET", rep["downloadUrl"], timeout=120) as r:
                    text = r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                raise _error(exc) from exc
            for vid, days in parse_reach_csv(text).items():
                vdays = data.setdefault("videos", {}).setdefault(vid, {})
                vdays.update(days)          # a re-issued day replaces the old one
            done.add(rep["id"])
            added += 1
        token = page.get("nextPageToken")
        url = f"{REPORTING}/jobs/{job}/reports?pageToken={token}" if token else ""
    data["done"] = sorted(done)
    _save_reach(data)
    if added:
        log(f"Read {added} day(s) of impressions from YouTube")
    return added


def reach_by_video() -> dict[str, dict]:
    out = {}
    for vid, days in _load_reach().get("videos", {}).items():
        imp = sum(d[0] for d in days.values())
        clicks = sum(d[1] for d in days.values())
        if imp:
            out[vid] = {"impressions": int(imp), "ctr": f"{100 * clicks / imp:.1f}%",
                        "impressions_through": max(days)}
    return out


# --------------------------------------------------------------------------------------
# Daily snapshot (scheduled task) for when the PC is asleep
# --------------------------------------------------------------------------------------

def snapshot(folder: Path, log=print) -> Path:
    """Write the latest numbers to `folder/latest.json` (+ a dated copy and a readable .md)."""
    try:
        sync_reach(log)
    except Exception as exc:
        log(f"Impressions skipped: {exc}")
    snap = {"taken": dt.datetime.now().isoformat(timespec="minutes"),
            "overview_28_days": overview(28), "latest_videos": video_table(15),
            "traffic_sources_28_days": traffic_sources(28)}
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snap, indent=2, ensure_ascii=False)
    (folder / "latest.json").write_text(text, encoding="utf-8")
    (folder / f"{_today().isoformat()}.json").write_text(text, encoding="utf-8")
    (folder / "latest.md").write_text(to_markdown(snap), encoding="utf-8")
    log(f"Channel stats saved to {folder}")
    return folder / "latest.json"


def to_markdown(snap: dict) -> str:
    o = snap["overview_28_days"]
    t = o.get("totals", {})
    lines = [f"# Channel stats ({snap['taken']})", "", f"Last 28 days ({o['period']}):",
             f"- Views {t.get('views', 0):,}, watch time {int(t.get('estimatedMinutesWatched', 0)) // 60:,} h, "
             f"subscribers +{t.get('subscribersGained', 0)} / -{t.get('subscribersLost', 0)}", "",
             "| Video | Format | Published | Views | Avg view | Avg % | Subs | Impressions | CTR |",
             "|---|---|---|---|---|---|---|---|---|"]
    for v in snap["latest_videos"]:
        lines.append(f"| {v['title'][:50]} | {v['format']} | {v['published']} | {v.get('views', '')} | "
                     f"{v.get('averageViewDuration', '')} | {v.get('averageViewPercentage', '')} | "
                     f"{v.get('subscribersGained', '')} | {v.get('impressions', '')} | {v.get('ctr', '')} |")
    lines += ["", "Traffic sources (28 days):",
              *[f"- {s['source']}: {s['views']} ({s['share']})" for s in snap["traffic_sources_28_days"]]]
    return "\n".join(lines) + "\n"


def default_folder(cfg: dict) -> Path:
    return Path(cfg["app"]["projects_dir"]).expanduser() / "Channel stats"
