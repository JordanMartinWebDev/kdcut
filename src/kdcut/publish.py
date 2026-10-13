"""Upload a finished export to YouTube with everything KDcut prepared.

Because you may change the edit in Resolve, captions and chapter times are rebuilt from the
exported video itself (a quick Whisper pass on the final file), so they always line up.
"""

from __future__ import annotations

import json
from pathlib import Path

from kdcut import captions, config, media, metadata, transcribe, youtube
from kdcut.blocks import fmt_ts


def chapter_lines(chapters: list[tuple[float, str]]) -> str:
    return "\n".join(f"{fmt_ts(t)} {title}" for t, title in chapters)


def publish(out_dir: Path, export: Path, cfg: dict, *, title: str, description: str, tags: list[str],
            thumbnail: Path | None, privacy: str = "private", publish_at: str | None = None,
            resync: bool = True, log=print, progress=lambda f: None, cancelled=lambda: False) -> dict:
    out_dir, export = Path(out_dir), Path(export)
    kit = {}
    if (out_dir / "upload_kit.json").is_file():
        kit = json.loads((out_dir / "upload_kit.json").read_text(encoding="utf-8"))
    anchors = [(a[0], a[1], float(a[2])) for a in kit.get("chapter_anchors", [])]
    duration = media.probe(export).duration
    warnings: list[str] = []

    # 1. Captions + chapters that match the final export.
    srt = ""
    chapters = [(t, title_) for title_, _, t in anchors]
    if resync:
        log("Listening to the final export so captions and chapters line up")
        wcfg = config._merge(cfg, {"whisper": {"mic_track": "auto"}})
        words = transcribe.transcribe_file(export, wcfg, out_dir / ".work" / "export",
                                           on_progress=lambda f: progress(f * 0.3), log=log)
        tw = [captions.TWord(w.start, w.end, w.text) for w in words]
        srt = captions.to_srt(tw)
        if anchors:
            chapters = captions.retime_chapters(anchors, tw)
    elif (out_dir / "captions.srt").is_file():
        srt = (out_dir / "captions.srt").read_text(encoding="utf-8")
    chapters = metadata.youtube_chapters(chapters, duration)

    desc = description.strip()
    if chapters:
        desc += "\n\nChapters\n" + chapter_lines(chapters)

    # 2. The video itself.
    log("Uploading to YouTube")
    base = 0.3 if resync else 0.0
    vid = youtube.upload_video(export, youtube.VideoMeta(title=title, description=desc, tags=tags,
                                                         privacy=privacy, publish_at=publish_at),
                               progress=lambda f: progress(base + f * (0.95 - base)), cancelled=cancelled)
    log(f"Uploaded: https://youtu.be/{vid}")

    # 3. Thumbnail and captions (a failure here doesn't lose the upload).
    if thumbnail and Path(thumbnail).is_file():
        try:
            youtube.set_thumbnail(vid, Path(thumbnail))
            log("Thumbnail set")
        except youtube.YouTubeError as exc:
            warnings.append(f"Thumbnail: {exc}")
    if srt.strip():
        try:
            youtube.upload_captions(vid, srt)
            log("Captions added")
        except youtube.YouTubeError as exc:
            warnings.append(f"Captions: {exc}")
    progress(1.0)

    record = {"video_id": vid, "url": f"https://youtu.be/{vid}", "studio": youtube.studio_url(vid),
              "title": title, "privacy": "scheduled" if publish_at else privacy,
              "publish_at": publish_at, "chapters": chapter_lines(chapters), "warnings": warnings}
    (out_dir / "youtube.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record
