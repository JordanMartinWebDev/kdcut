"""End to end: project folder of raw recordings -> Resolve timeline + review notes + upload kit.

Project folder layout (created by `kdcut init`):

    MyEpisode/
      recordings/   raw OBS sessions, in recording order (MKV or MP4)
      broll/        Flashback renders, timelapses... name them descriptively
      kdcut.toml    optional per-video settings (topic, series, style notes)
      output/       everything kdcut produces
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from kdcut import blocks as blocks_mod
from kdcut import fcpxml, media, metadata, planner, transcribe
from kdcut.assets import AssetIndex
from kdcut.blocks import fmt_ts
from kdcut.planner import Plan
from kdcut.timeline import Clip, Marker, _Lanes, build, nearest_kept


def _video_settings(project: Path) -> dict:
    f = project / "kdcut.toml"
    if f.is_file():
        with f.open("rb") as fh:
            return tomllib.load(fh).get("video", {})
    return {}


def find_recordings(project: Path) -> list[Path]:
    recs = media.list_media(project / "recordings", media.VIDEO_EXTS)
    if not recs:  # allow dropping files straight into the project folder
        recs = media.list_media(project, media.VIDEO_EXTS)
    return recs


def run(project: Path, cfg: dict, use_ai: bool = True, retranscribe: bool = False,
        replan: bool = False, caller=None) -> dict:
    project = Path(project).resolve()
    out = project / "output"
    out.mkdir(parents=True, exist_ok=True)
    work = out / ".work"
    video = _video_settings(project)
    call = caller or planner.llm.call_tool

    # 1. Recordings -----------------------------------------------------------------
    media.require_ffmpeg()
    recordings = find_recordings(project)
    if not recordings:
        raise SystemExit(f"No recordings found. Put your OBS files in {project / 'recordings'}")
    infos = [media.probe(r) for r in recordings]
    print(f"[1/5] {len(recordings)} recording(s), "
          f"{sum(i.duration for i in infos) / 60:.1f} minutes total")
    fps = media.parse_fps(cfg["timeline"].get("fps")) or media.normalize_fps(infos[0].fps)
    width, height = infos[0].width or 1920, infos[0].height or 1080

    # 2. Transcribe -----------------------------------------------------------------
    print("[2/5] Transcribing")
    words = [transcribe.transcribe_file(r, cfg, work, force=retranscribe) for r in recordings]
    ed = cfg["edit"]
    blocks = blocks_mod.build_blocks(words, gap=float(ed["block_gap"]),
                                     max_seconds=float(ed["max_block_seconds"]))
    cues = [b for b in blocks if b.cue]
    print(f"  {len(blocks)} speech blocks, {len(cues)} spoken cue(s)")
    (out / "transcript.txt").write_text(
        planner.format_blocks(blocks, recordings), encoding="utf-8")

    # 3. Plan -----------------------------------------------------------------------
    index = AssetIndex.load(Path(cfg["assets"]["index_path"]))
    broll_files = media.list_media(project / "broll", media.VIDEO_EXTS | media.IMAGE_EXTS)
    broll = [(p.name, media.probe(p).duration or 5.0) for p in broll_files]
    plan_file = out / "plan.json"
    if plan_file.is_file() and not replan and not retranscribe:
        print("[3/5] Reusing existing plan.json (use --replan for a fresh AI edit)")
        plan = Plan.load(plan_file)
    elif use_ai:
        print("[3/5] AI edit")
        vocab = {}
        if index:
            vocab = {"sound effect": index.vocabulary("sfx", 100),
                     "overlay": index.vocabulary("overlay", 100)}
        plan = planner.make_plan(blocks, recordings, cfg, video, broll, vocab, caller=call)
        plan.save(plan_file)
    else:
        print("[3/5] No-AI mode: applying spoken cues and trimming pauses only")
        plan = planner.plan_without_ai(blocks)
        plan.save(plan_file)

    # 4. Timeline -------------------------------------------------------------------
    print("[4/5] Building timeline")
    name = video.get("title") or project.name
    tl, starts, ends = build(name, recordings, blocks, plan, fps, width, height,
                             [i.duration for i in infos], ed)
    lanes = _Lanes()
    tcfg = cfg["timeline"]
    cache = Path(cfg["assets"]["cache_dir"]).expanduser() / "converted"
    review: list[str] = []
    placed = {"broll": 0, "overlay": 0, "sfx": 0}

    def anchor(bid: str, at_end: bool = False) -> int | None:
        k = nearest_kept(bid, blocks, starts)
        if k is None:
            return None
        return ends[k] if at_end else starts[k]

    for item in plan.broll:
        rec = anchor(item["at"])
        path = project / "broll" / item["file"]
        if rec is None or not path.exists():
            continue
        info = media.probe(path)
        max_len = info.duration if info.duration > 0 else float(item.get("duration", 5))
        dur = tl.frames(min(float(item.get("duration", 6)), max_len))
        dur = min(dur, tl.duration - rec)
        if dur <= 0:
            continue
        lane = lanes.place(int(tcfg["broll_lane"]), rec, dur)
        tl.connected.append(Clip(path=path, src_in=0, dur=dur, rec=rec, lane=lane,
                                 enable="video", role="broll", name=path.stem))
        placed["broll"] += 1
        review.append(f"{fmt_ts(tl.seconds(rec))}  B-roll  {path.name}"
                      + (f"  ({item['reason']})" if item.get("reason") else ""))

    if index:
        for n, item in enumerate(plan.overlays):
            rec = anchor(item["at"])
            asset = index.best(item["query"], "overlay", salt=f"o{n}") or \
                index.best(item["query"], "image", salt=f"o{n}")
            if rec is None or asset is None:
                review.append(f"  (no overlay matched '{item['query']}')")
                continue
            path, how = media.prepare_overlay(Path(asset.path), cache)
            info = media.probe(path)
            want = float(item.get("duration", 3.0))
            secs = min(want, info.duration) if info.duration > 0 and not info.is_image else want
            dur = min(tl.frames(secs), tl.duration - rec)
            if dur <= 0:
                continue
            lane = lanes.place(int(tcfg["overlay_lane"]), rec, dur)
            tl.connected.append(Clip(path=path, src_in=0, dur=dur, rec=rec, lane=lane,
                                     enable="video", role="overlay", name=asset.name,
                                     is_image=info.is_image))
            placed["overlay"] += 1
            review.append(f"{fmt_ts(tl.seconds(rec))}  Overlay '{item['query']}' -> {asset.name}"
                          + (f"  [{how}]" if how != "as-is" else ""))

        for n, item in enumerate(plan.sfx):
            rec = anchor(item["at"], at_end=item.get("when") == "end")
            asset = index.best(item["query"], "sfx", salt=f"s{n}")
            if rec is None or asset is None:
                review.append(f"  (no sound matched '{item['query']}')")
                continue
            info = media.probe(Path(asset.path))
            dur = min(tl.frames(min(info.duration or 1.0, 8.0)), tl.duration - rec)
            if dur <= 0:
                continue
            lane = lanes.place(int(tcfg["sfx_lane"]), rec, dur)
            tl.connected.append(Clip(path=Path(asset.path), src_in=0, dur=dur, rec=rec,
                                     lane=lane, enable="audio", role="sfx", name=asset.name))
            placed["sfx"] += 1
            review.append(f"{fmt_ts(tl.seconds(rec))}  SFX '{item['query']}' -> {asset.name}")
    elif plan.overlays or plan.sfx:
        review.append("  (asset index missing: run `kdcut index-assets` to get SFX and overlays)")

    chapter_times: list[tuple[float, str]] = []
    for c in plan.chapters:
        rec = anchor(c["at"])
        if rec is not None:
            tl.markers.append(Marker(rec=rec, name=c["title"], kind="chapter"))
            chapter_times.append((tl.seconds(rec), c["title"]))
    for b in blocks:
        if "highlight" in b.flags and b.id in starts:
            tl.markers.append(Marker(rec=starts[b.id], name="Highlight (you said 'clip that')", kind="note"))

    stem = _safe(name)
    xml_path = fcpxml.write(tl, out / f"{stem}.fcpxml", event=cfg["channel"].get("name") or "kdcut")

    # 5. Review notes + upload kit ----------------------------------------------------
    print("[5/5] Writing review notes")
    raw_total = sum(i.duration for i in infos)
    final = tl.seconds(tl.duration)
    by_id = {b.id: b for b in blocks}
    cut_lines = []
    for bid, reason in plan.drop.items():
        b = by_id.get(bid)
        if b:
            cut_lines.append(f"- rec {b.source + 1} @ {fmt_ts(b.start)}  [{reason}]  \"{_short(b.text)}\"")
    for b in blocks:
        if "forced_drop" in b.flags and not b.cue:
            cut_lines.append(f"- rec {b.source + 1} @ {fmt_ts(b.start)}  [you said 'cut that']  \"{_short(b.text)}\"")

    report = [
        f"# Review: {name}", "",
        f"- Raw footage: {fmt_ts(raw_total)}  ->  edit: {fmt_ts(final)} "
        f"({100 * final / raw_total:.0f}% kept)" if raw_total else "",
        f"- Clips on the main track: {len(tl.main)}",
        f"- Placed: {placed['broll']} B-roll, {placed['overlay']} overlays, {placed['sfx']} sound effects",
        f"- Timeline: `{xml_path.name}` ({float(fps):.2f} fps, {width}x{height})", "",
        "## How to open it",
        "DaVinci Resolve > File > Import > Timeline... > pick the .fcpxml. "
        "If Resolve asks, let it create the timeline with the project's settings.", "",
    ]
    if plan.notes:
        report += ["## Things the AI wants you to check", *[f"- {n}" for n in plan.notes], ""]
    if review:
        report += ["## What was added (timeline time)", *[f"- {r}" for r in review], ""]
    report += [f"## What was cut ({len(cut_lines)} pieces, besides silences)", *cut_lines, ""]
    (out / "review.md").write_text("\n".join(report), encoding="utf-8")

    yt_chapters = metadata.youtube_chapters(chapter_times, final)
    kit_frames: list[str] = []
    md = metadata.Metadata()
    if use_ai:
        kept_blocks = [b for b in blocks if b.id in starts]
        try:
            md = metadata.generate(cfg, video, kept_blocks, caller=call)
        except Exception as exc:  # metadata is a bonus; never lose the timeline over it
            print(f"  metadata skipped: {exc}")
        for n, bid in enumerate(md.thumbnail_moments[:5], 1):
            b = by_id.get(bid)
            if not b:
                continue
            frame = out / "thumbnail_frames" / f"frame_{n}.jpg"
            try:
                media.extract_frame(recordings[b.source], b.start + min(1.0, b.duration / 2), frame)
                kit_frames.append(f"thumbnail_frames/{frame.name}  ({_short(b.text, 60)})")
            except media.MediaError:
                pass
    (out / "upload_kit.md").write_text(metadata.to_markdown(md, yt_chapters, kit_frames), encoding="utf-8")

    print(f"\nDone. Raw {fmt_ts(raw_total)} -> edit {fmt_ts(final)}")
    print(f"  timeline:   {xml_path}")
    print(f"  review:     {out / 'review.md'}")
    print(f"  upload kit: {out / 'upload_kit.md'}")
    return {"timeline": xml_path, "final_seconds": final, "raw_seconds": raw_total,
            "placed": placed, "plan": plan, "clips": len(tl.main)}


def _safe(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()
    return keep or "timeline"


def _short(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"
