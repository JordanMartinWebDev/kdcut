"""End to end: raw recordings -> Resolve timeline + review notes + upload kit.

Used by both the desktop app (explicit file lists, progress callbacks) and the command line.

Command-line project folder layout (created by `kdcut init`):

    MyEpisode/
      recordings/   raw OBS sessions, in recording order (MKV or MP4)
      broll/        Flashback renders shown over the gameplay while you talk
      timelapses/   silent timelapses that play as their own scene, plus voiceover audio
      kdcut.toml    optional per-video settings (topic, series, style notes)
      output/       everything kdcut produces
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from kdcut import blocks as blocks_mod
from kdcut import audioprep, captions, graphics, vad, ducking, fcpxml, media, metadata, music, planner, transcribe, vision
from kdcut import timelapse as timelapse_mod
from kdcut.assets import AssetIndex
from kdcut.blocks import fmt_ts
from kdcut.planner import Plan
from kdcut.timeline import Clip, Marker, _Lanes, build, kept_ranges, nearest_kept, teaser_range


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


class Cancelled(Exception):
    pass


class Reporter:
    """Progress sink. The command line prints; the desktop app overrides these."""

    STEPS = ["Reading recordings", "Transcribing", "AI edit", "Building timeline", "Writing notes"]

    def step(self, index: int, label: str) -> None:
        print(f"[{index + 1}/{len(self.STEPS)}] {label}")

    def progress(self, fraction: float) -> None:
        pass

    def log(self, message: str) -> None:
        print(f"  {message}")

    def cancelled(self) -> bool:
        return False

    def check(self) -> None:
        if self.cancelled():
            raise Cancelled()


def run(project: Path, cfg: dict, use_ai: bool = True, retranscribe: bool = False,
        replan: bool = False, caller=None, *, recordings: list[Path] | None = None,
        broll_files: list[Path] | None = None, video: dict | None = None,
        out_dir: Path | None = None, reporter: Reporter | None = None,
        timelapse_files: list[Path] | None = None) -> dict:
    """Edit one video.

    Command line: pass just `project` (folder with recordings/ and broll/).
    Desktop app: pass explicit `recordings`, `broll_files`, `timelapse_files`, `video` and `out_dir`.
    `timelapse_files` mixes timelapse videos and voiceover audio.
    """
    r = reporter or Reporter()
    project = Path(project).resolve()
    out = Path(out_dir) if out_dir else project / "output"
    out.mkdir(parents=True, exist_ok=True)
    work = out / ".work"
    media.debug_log = out / "kdcut.log"
    media.debug(f"===== KDcut build of {out.name} =====")
    _say = r.log
    r.log = lambda m: (media.debug(m), _say(m))
    video = dict(video) if video is not None else _video_settings(project)
    call = caller or planner.llm.call_tool

    # 1. Recordings -----------------------------------------------------------------
    r.step(0, "Reading recordings")
    media.require_ffmpeg()
    recordings = [Path(p) for p in recordings] if recordings else find_recordings(project)
    if not recordings:
        raise RuntimeError(f"No recordings found. Put your OBS files in {project / 'recordings'}")
    infos = [media.probe(p) for p in recordings]
    r.log(f"{len(recordings)} recording(s), {sum(i.duration for i in infos) / 60:.1f} minutes total")
    fps = media.parse_fps(cfg["timeline"].get("fps")) or media.normalize_fps(infos[0].fps)
    width, height = infos[0].width or 1920, infos[0].height or 1080

    # 2. Transcribe -----------------------------------------------------------------
    r.check()
    r.step(1, "Transcribing")
    total_secs = sum(i.duration for i in infos) or 1.0
    done_secs = 0.0
    words = []
    for rec, info in zip(recordings, infos):
        base = done_secs

        def on_progress(f, base=base, dur=info.duration):
            r.progress(min((base + f * dur) / total_secs, 1.0))
            r.check()

        words.append(transcribe.transcribe_file(rec, cfg, work, force=retranscribe,
                                                on_progress=on_progress, log=r.log))
        done_secs += info.duration
    # Timelapses and their voiceovers.
    if timelapse_files is None:
        timelapse_files = media.list_media(project / "timelapses", media.VIDEO_EXTS | media.AUDIO_EXTS)
    tl_videos, tl_audio = timelapse_mod.split_files([Path(p) for p in timelapse_files if Path(p).exists()])
    timelapses = [timelapse_mod.Timelapse(p, media.probe(p).duration or 5.0) for p in tl_videos]
    loose_vo = timelapse_mod.pair_by_name(timelapses, tl_audio)
    vo_words: dict[str, list] = {}
    for vo in [t.voiceover for t in timelapses if t.voiceover] + loose_vo:
        r.check()
        vo_words[str(vo)] = transcribe.transcribe_file(vo, cfg, work, force=retranscribe, log=r.log)
    for t in timelapses:
        if t.voiceover:
            t.vo_words = vo_words[str(t.voiceover)]
    if timelapses:
        r.log(f"{len(timelapses)} timelapse(s), {len(tl_audio)} voiceover(s)")
    ed = cfg["edit"]
    blocks = blocks_mod.build_blocks(words, gap=float(ed["block_gap"]),
                                     max_seconds=float(ed["max_block_seconds"]))
    cues = [b for b in blocks if b.cue]
    r.log(f"{len(blocks)} speech blocks, {len(cues)} spoken cue(s)")
    (out / "transcript.txt").write_text(
        planner.format_blocks(blocks, recordings), encoding="utf-8")

    # 3. Plan -----------------------------------------------------------------------
    index = AssetIndex.load(Path(cfg["assets"]["index_path"]))
    # Music folders: background music, plus any sound effects that live in there too.
    mcfg = cfg.get("music", {})
    library: list[music.Track] = []
    if mcfg.get("folders"):
        lib = music.scan_library(mcfg["folders"], log=r.log)
        if mcfg.get("enabled", True) and video.get("music", True):
            library = lib.music
            r.log(f"Music library: {len(library)} track(s)")
        if lib.sfx:
            index = AssetIndex((index.assets if index else []) + music.sfx_assets(lib.sfx))
            r.log(f"{len(lib.sfx)} sound effect(s) from the music folders added to the SFX choices")
    r.check()
    if broll_files is None:
        broll_files = media.list_media(project / "broll", media.VIDEO_EXTS | media.IMAGE_EXTS)
    broll_by_name: dict[str, Path] = {}
    for p in map(Path, broll_files):
        name = p.name
        n = 2
        while name in broll_by_name:          # same filename from two folders
            name = f"{p.stem} ({n}){p.suffix}"
            n += 1
        broll_by_name[name] = p
    broll = [(n, media.probe(p).duration or 5.0) for n, p in broll_by_name.items()]
    plan_file = out / "plan.json"
    reuse_plan = plan_file.is_file() and not replan and not retranscribe
    if reuse_plan and Plan.load(plan_file).blocks_key != planner.blocks_key(blocks):
        r.log("The transcript or the editing rules changed since the last edit, so the AI edits it again")
        reuse_plan = False
    use_vision = use_ai and bool(video.get("vision", cfg.get("vision", {}).get("enabled", False)))
    notes: dict[str, list[str]] = {}
    if use_vision and not reuse_plan:
        r.step(2, "Looking at the footage")
        try:
            notes = vision.analyse(recordings, infos, blocks, cfg, work, caller=call, log=r.log,
                                   check=r.check)
            (out / "screen_notes.json").write_text(json.dumps(notes, indent=1), encoding="utf-8")
        except Cancelled:
            raise
        except Exception as exc:  # never lose the edit over the optional footage check
            r.log(f"Footage check skipped: {exc}")
    elif (out / "screen_notes.json").is_file() and use_vision:
        notes = json.loads((out / "screen_notes.json").read_text(encoding="utf-8"))
    if reuse_plan:
        r.step(2, "AI edit (reusing saved plan)")
        plan = Plan.load(plan_file)
    elif use_ai:
        r.step(2, "AI edit")
        vocab = {}
        if index:
            vocab = {"sound effect": index.vocabulary("sfx", 100),
                     "overlay": index.vocabulary("overlay", 100)}
        plan = planner.make_plan(blocks, recordings, cfg, video, broll, vocab, caller=call,
                                 log=r.log, notes=notes)
        plan.save(plan_file)
    else:
        r.step(2, "Quick edit (spoken cues and pauses only)")
        plan = planner.plan_without_ai(blocks)
        plan.blocks_key = planner.blocks_key(blocks)
        plan.save(plan_file)

    # Packaging (titles, description, cold open) needs the whole kept story, so it is
    # decided now, before the timeline is built. Cached alongside plan.json.
    by_id = {b.id: b for b in blocks}
    durations = [i.duration for i in infos]
    # Where the voice really starts and stops, so cuts don't clip words.
    voices = {}
    for src, (rec_path, info) in enumerate(zip(recordings, infos)):
        if info.audio:
            mic = media.pick_mic_track(info, cfg["whisper"].get("mic_track", "auto"))
            got = vad.analyse(Path(rec_path), mic, work, log=r.log)
            if got:
                voices[src] = got
        r.check()
    kept_ids = [bid for rng in kept_ranges(blocks, plan, durations, ed, voices) for bid in rng.block_ids]
    want_cold_open = bool(video.get("cold_open", ed.get("cold_open", True)))
    history = music.load_history()
    library_text = music.library_text(library, history) if library else ""
    md_file = out / "metadata.json"
    md = metadata.Metadata()
    if use_ai:
        cached = json.loads(md_file.read_text(encoding="utf-8")) if md_file.is_file() else {}
        if (cached and cached.pop("_cold_open_requested", None) == want_cold_open
                and cached.pop("_music_requested", False) == bool(library)
                and cached.pop("_rules", 0) == metadata.RULES_VERSION
                and md_file.stat().st_mtime >= plan_file.stat().st_mtime):
            md = metadata.Metadata(**cached)
        else:
            r.log("Writing titles and description" + (", picking a cold open" if want_cold_open else ""))
            try:
                md = metadata.generate(cfg, video, [by_id[i] for i in kept_ids], caller=call,
                                       cold_open=want_cold_open, notes=notes, music_library=library_text)
                md_file.write_text(json.dumps({**md.__dict__, "_cold_open_requested": want_cold_open,
                                               "_music_requested": bool(library),
                                               "_rules": metadata.RULES_VERSION},
                                              indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as exc:  # packaging is a bonus; never lose the timeline over it
                r.log(f"titles/description skipped: {exc}")
    if not want_cold_open:
        md.cold_open = None
    elif md.cold_open is None:
        # No AI pick: fall back to the first "clip that" moment, if there is one.
        hl = next((by_id[i] for i in kept_ids if "highlight" in by_id[i].flags), None)
        if hl is not None:
            md.cold_open = {"id": hl.id, "seconds": 6, "reason": "you said 'clip that'"}
    prefix, teaser_level, teaser_note = None, None, ""
    if md.cold_open:
        md.cold_open, prefix, teaser_level, teaser_note = _pick_cold_open(
            md, by_id, kept_ids, recordings, words, durations, voices, log=r.log)

    # Where the timelapses go (decided on the finished story, cached like the packaging).
    tl_inserts: dict[str, list] = {}
    tl_notes: list[str] = []
    if timelapses:
        kept_blocks = [by_id[i] for i in kept_ids]
        loose_text = [(p.name, " ".join(w.text.strip() for w in vo_words[str(p)])) for p in loose_vo]
        if use_ai:
            tl_file = out / "timelapses.json"
            key = timelapse_mod.cache_key(kept_blocks, timelapses, loose_text)
            saved = json.loads(tl_file.read_text(encoding="utf-8")) if tl_file.is_file() else {}
            placements = saved.get("placements") if saved.get("key") == key else None
            if placements is None:
                r.log("Finding the spot for each timelapse")
                try:
                    placements = timelapse_mod.place(cfg, video, kept_blocks, timelapses, loose_text, call)
                    tl_file.write_text(json.dumps({"key": key, "placements": placements}, indent=2,
                                                  ensure_ascii=False), encoding="utf-8")
                except Exception as exc:
                    r.log(f"Couldn't place the timelapses with the AI ({exc}); they go at the end")
                    placements = []
            tl_inserts, tl_notes = timelapse_mod.resolve(placements, timelapses, loose_vo, set(kept_ids))
        else:
            tl_inserts, tl_notes = timelapse_mod.place_without_ai(timelapses, loose_vo)
        for t in timelapses:          # voiceovers the AI (or the order) matched
            if t.voiceover is not None and not t.vo_words:
                t.vo_words = vo_words.get(str(t.voiceover), [])

    # 4. Timeline -------------------------------------------------------------------
    r.check()
    r.step(3, "Building timeline")
    name = video.get("title") or project.name
    prepared: dict[int, timelapse_mod.Prepared] = {}     # id(clip) -> prepared timelapse
    insert_clips: dict[str, list[Clip]] = {}
    n_tl = 0
    for after, items in tl_inserts.items():
        for item in items:
            n_tl += 1
            r.check()
            try:
                prep = timelapse_mod.prepare(item, n_tl, fps, width, height, ed, work, log=r.log)
            except media.MediaError as exc:
                r.log(f"Timelapse {item.name} skipped: {exc}")
                tl_notes.append(f"{item.name} couldn't be prepared ({exc})")
                continue
            clip = Clip(path=prep.file, src_in=0, dur=prep.frames, rec=0, role="timelapse",
                        name=f"Timelapse - {item.path.stem}")
            prepared[id(clip)] = prep
            insert_clips.setdefault(after, []).append(clip)
    tl, starts, ends = build(name, recordings, blocks, plan, fps, width, height,
                             durations, ed, prefix=prefix, voices=voices, inserts=insert_clips)
    lanes = _Lanes()
    tcfg = cfg["timeline"]

    # Every audio track of the recordings, as its own levelled track under the video.
    # (Resolve only imports a recording's first audio track, which loses a mic on track 2.)
    stems: dict[str, list[audioprep.Stem]] = {}
    for rec_path, info in zip(recordings, infos):
        if not info.audio:
            continue
        try:
            mic = media.pick_mic_track(info, cfg["whisper"].get("mic_track", "auto"))
            stems[str(rec_path)] = audioprep.split_tracks(Path(rec_path), info, mic, work / "audio", log=r.log)
        except media.MediaError as exc:
            r.log(f"Kept the recording's own audio for {Path(rec_path).name}: {exc}")
        r.check()
    audio_lanes = 0
    n_tracks = {str(p): len(i.audio) for p, i in zip(recordings, infos)}
    for clip in tl.main:
        got = stems.get(str(clip.path))
        if not got:
            continue
        if len(got) == n_tracks[str(clip.path)]:
            clip.enable = "video"       # every track is underneath; otherwise keep track 1 here
        for j, stem in enumerate(got):
            lanes.busy.setdefault(-(j + 1), []).append((clip.rec, clip.rec_end))
            tl.connected.append(Clip(path=stem.path, src_in=clip.src_in, dur=clip.dur, rec=clip.rec,
                                     lane=-(j + 1), enable="audio", role=stem.kind,
                                     name=f"{stem.label} - {clip.path.stem}"))
        audio_lanes = max(audio_lanes, len(got))
    # Voiceovers on the voice track, under their timelapse.
    vo_spans: list[tuple[float, float]] = []
    vo_caption_words: dict[str, list] = {}
    for clip in tl.main:
        prep = prepared.get(id(clip))
        if prep is None or prep.voice_file is None:
            continue
        for src_in, dur, offset in prep.pieces:
            rec = clip.rec + offset
            tl.connected.append(Clip(path=prep.voice_file, src_in=src_in, dur=dur, rec=rec,
                                     lane=lanes.place(-1, rec, dur), enable="audio", role="voiceover",
                                     name=f"Voiceover - {prep.item.voiceover.stem}"))
            vo_spans.append((tl.seconds(rec), tl.seconds(rec + dur)))
        vo_caption_words[str(prep.voice_file)] = prep.item.vo_words
        audio_lanes = max(audio_lanes, 1)
    music_lane = -(audio_lanes + 1)
    sfx_lane = min(int(tcfg["sfx_lane"]), music_lane - 1)
    cache = Path(cfg["assets"]["cache_dir"]).expanduser() / "converted"
    review: list[str] = []
    placed = {"broll": 0, "overlay": 0, "sfx": 0, "music": 0, "timelapse": 0}
    for clip in tl.main:
        prep = prepared.get(id(clip))
        if prep is None:
            continue
        placed["timelapse"] += 1
        tl.markers.append(Marker(rec=clip.rec, name=f"Timelapse: {prep.item.name}", kind="note"))
        review.append(timelapse_mod.review_line(tl.seconds(clip.rec), tl.seconds(clip.dur), prep,
                                                prep.item.reason))
    review += [f"  ({n})" for n in tl_notes]
    teaser = tl.main[0] if tl.main and tl.main[0].role == "cold_open" else None
    if teaser and video.get("teaser_style", True):
        # Black and white with "COMING UP", so viewers know it's from later in the episode.
        try:
            r.check()
            styled, look = graphics.render_teaser(teaser.path, tl.seconds(teaser.src_in), teaser.dur, tl.fps,
                                                  work / "teaser.mp4", width, height, log=r.log,
                                                  level=teaser_level)
            teaser_note = "; ".join(x for x in (teaser_note, look if look != "black and white" else "") if x)
            had_stems = bool(stems.get(str(teaser.path)))
            teaser.path, teaser.src_in, teaser.name = styled, 0, "Cold open - coming up"
            teaser.enable = "video" if had_stems else "all"
        except media.MediaError as exc:
            r.log(f"Kept the teaser plain (couldn't render the 'Coming up' look: {exc})")
    if not teaser and teaser_note:
        review.append(f"0:00  No cold open: {teaser_note}")
    if teaser:
        cb = by_id[md.cold_open["id"]]
        tl.markers.append(Marker(rec=0, name="Cold open (teaser)", kind="note"))
        review.append(f"0:00  Cold open: {tl.seconds(teaser.dur):.0f}s teaser from rec {cb.source + 1} @ "
                      f"{fmt_ts(cb.start)}  \"{_short(cb.text, 60)}\""
                      + (f"  ({md.cold_open['reason']})" if md.cold_open.get("reason") else ""))
        if teaser_note:
            review.append(f"      Teaser: {teaser_note}")
        if index:
            whoosh = index.best("whoosh", "sfx") or index.best("swoosh", "sfx")
            if whoosh:
                wdur = tl.frames(min(media.probe(Path(whoosh.path)).duration or 1.0, 3.0))
                rec_w = max(teaser.rec_end - wdur // 2, 0)
                tl.connected.append(Clip(path=Path(whoosh.path), src_in=0, dur=wdur, rec=rec_w,
                                         lane=lanes.place(sfx_lane, rec_w, wdur), volume_db=audioprep.sfx_gain(Path(whoosh.path)),
                                         enable="audio", role="sfx", name=whoosh.name))
                placed["sfx"] += 1

    def anchor(bid: str, at_end: bool = False) -> int | None:
        k = nearest_kept(bid, blocks, starts)
        if k is None:
            return None
        return ends[k] if at_end else starts[k]

    for item in plan.broll:
        rec = anchor(item["at"])
        path = broll_by_name.get(item["file"])
        if rec is None or path is None or not path.exists():
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

    for n, item in enumerate(plan.overlays if ed.get("overlays", False) else []):
        rec = anchor(item["at"])
        if rec is None:
            continue
        label = str(item.get("text") or "").strip()
        asset = None
        if index:
            asset = index.best(item["query"], "overlay", salt=f"o{n}") or \
                index.best(item["query"], "image", salt=f"o{n}")
        if asset is None and not label:
            review.append(f"  (no overlay matched '{item['query']}')")
            continue
        r.check()
        want = float(item.get("duration", 3.0))
        dur = min(tl.frames(want), tl.duration - rec)
        if asset is not None:
            path, how = media.prepare_overlay(Path(asset.path), cache)
            info = media.probe(path)
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
        if label and dur > 0:
            try:
                png = graphics.label_png(label, work / "labels" / f"label_{n + 1}.png", width, height)
            except Exception as exc:  # a missing label never stops the build
                r.log(f"Text label '{label}' skipped: {exc}")
                continue
            lane = lanes.place(int(tcfg["overlay_lane"]) + 1, rec, dur)
            tl.connected.append(Clip(path=png, src_in=0, dur=dur, rec=rec, lane=lane, enable="video",
                                     role="overlay", name=f"Text - {label}", is_image=True))
            review.append(f"{fmt_ts(tl.seconds(rec))}  Text '{label}'")

    if index:

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
            lane = lanes.place(sfx_lane, rec, dur)
            tl.connected.append(Clip(path=Path(asset.path), src_in=0, dur=dur, rec=rec,
                                     lane=lane, enable="audio", role="sfx", name=asset.name,
                                     volume_db=audioprep.sfx_gain(Path(asset.path))))
            placed["sfx"] += 1
            review.append(f"{fmt_ts(tl.seconds(rec))}  SFX '{item['query']}' -> {asset.name}")
    elif plan.overlays or plan.sfx:
        review.append("  (no editing packs indexed yet, so SFX and overlays were skipped)")

    chapter_times: list[tuple[float, str]] = []
    chapter_anchors: dict[str, str] = {}      # title -> words it starts with (to re-time later)
    for c in plan.chapters:
        rec = anchor(c["at"])
        if rec is not None:
            tl.markers.append(Marker(rec=rec, name=c["title"], kind="chapter"))
            chapter_times.append((tl.seconds(rec), c["title"]))
            k = nearest_kept(c["at"], blocks, starts)
            chapter_anchors[c["title"]] = by_id[k].text if k else ""
    for b in blocks:
        if "highlight" in b.flags and b.id in starts:
            tl.markers.append(Marker(rec=starts[b.id], name="Highlight (you said 'clip that')", kind="note"))

    # End screen: the last seconds, laid out for YouTube's end screen elements. Added before
    # the music so the music bed plays under it.
    end_screen, es_secs = None, 0.0
    if bool(video.get("end_screen", ed.get("end_screen", True))) and tl.main:
        try:
            end_screen, es_secs = _add_end_screen(tl, cfg, video, out, work, width, height, index, log=r.log)
            rec = tl.main[-1].rec
            tl.markers.append(Marker(rec=rec, name="End screen (add the elements in YouTube Studio)", kind="note"))
            review.append(f"{fmt_ts(tl.seconds(rec))}  End screen ({es_secs:.0f}s): "
                          + ", ".join(end_screen.labels) + " + subscribe")
        except Exception as exc:  # a bonus; never lose the edit over it
            r.log(f"End screen skipped: {exc}")

    # Background music on its own track (A3), after the teaser.
    if library:
        music_start = tl.seconds(teaser.rec_end) if teaser else 0.0
        total = tl.seconds(tl.duration)
        by_name = {t.name.lower(): t for t in library}

        def find(name: str):
            n = (name or "").strip().lower()
            if not n:
                return None
            return by_name.get(n) or next((t for t in library if n in t.name.lower() or t.title.lower() in n), None)

        raw = []
        for c in md.music:
            rec = anchor(c.get("at", ""))
            if rec is None:
                continue
            tracks = [t for t in (find(c.get("track", "")), find(c.get("backup", ""))) if t]
            raw.append((max(tl.seconds(rec), music_start), tracks, c.get("why", "")))
        if not raw:   # no AI picks: one fresh track per chapter
            cue_starts = sorted({max(t, music_start) for t, _ in chapter_times} | {music_start})
            rng_tracks = sorted(library, key=lambda t: history.get(t.path, 0))
            raw = [(s, [rng_tracks[i % len(rng_tracks)]], "") for i, s in enumerate(cue_starts)]
        raw.sort(key=lambda x: x[0])
        cues = [music.Cue(s, raw[i + 1][0] if i + 1 < len(raw) else total, tr, why)
                for i, (s, tr, why) in enumerate(raw)]
        pieces = [pc for pc in music.fill(cues, library, history, seed=len(blocks)) if pc.length >= 2]
        vol = float(mcfg.get("volume_db", -20))
        # Every track is levelled first (Epidemic and other sources differ by 10 dB or more),
        # then sits about 16 dB under your voice while you talk (setting "Normal") and comes
        # up 8 dB in the gaps.
        levels = [audioprep.music_gain(Path(pc.track.path)) for pc in pieces]
        talk_db, gap_db = vol + 2.0, vol + 10.0
        for pc in pieces:
            review.append(f"{fmt_ts(pc.start)}  Music  {pc.track.name}")
        bed = None
        if pieces and mcfg.get("duck", True):
            # One pre-mixed music bed that dips under your voice.
            speech = [(tl.seconds(starts[b]), tl.seconds(ends[b])) for b in starts] + vo_spans
            gains = ducking.envelope(speech, total, talk_db=talk_db, gap_db=gap_db)
            try:
                r.log("Mixing the music so it dips while you talk")
                bed = ducking.render_bed(pieces, total, gains, out / "music_bed.m4a", start=music_start,
                                         levels=levels)
            except Exception as exc:
                r.log(f"Music ducking skipped ({exc}); placing tracks at a steady level")
        if bed is not None:
            rec = tl.frames(music_start)
            dur = tl.duration - rec
            tl.connected.append(Clip(path=bed, src_in=0, dur=dur, rec=rec, lane=lanes.place(music_lane, rec, dur),
                                     enable="audio", role="music", name="Music (dips under voice)"))
        else:
            for pc, lvl in zip(pieces, levels):
                rec = tl.frames(pc.start)
                dur = min(tl.frames(pc.length), tl.duration - rec)
                if dur <= tl.frames(2):
                    continue
                tl.connected.append(Clip(path=Path(pc.track.path), src_in=tl.frames(pc.offset), dur=dur,
                                         rec=rec, lane=lanes.place(music_lane, rec, dur), enable="audio",
                                         role="music", name=pc.track.name,
                                         volume_db=round(vol + 4.0 + lvl, 1)))
        placed["music"] = len(pieces)
        music.record_use([pc.track.path for pc in pieces])

    stem = _safe(name)
    xml_path = fcpxml.write(tl, out / f"{stem}.fcpxml", event=cfg["channel"].get("name") or "kdcut")
    (out / "captions.srt").write_text(
        captions.to_srt(captions.words_on_timeline(words, recordings, tl, extra=vo_caption_words)),
        encoding="utf-8")

    # 5. Review notes + upload kit ----------------------------------------------------
    r.step(4, "Writing notes")
    raw_total = sum(i.duration for i in infos)
    final = tl.seconds(tl.duration)
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
        f"- Placed: {placed['timelapse']} timelapses, {placed['broll']} B-roll, {placed['overlay']} overlays, "
        f"{placed['sfx']} sound effects",
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
    if end_screen is not None:
        from kdcut import endscreen
        report += ["## End screen", *[f"- {s}" for s in endscreen.studio_steps(end_screen, es_secs)], ""]
    (out / "review.md").write_text("\n".join(report), encoding="utf-8")

    yt_chapters = metadata.youtube_chapters(chapter_times, final)
    kit_frames: list[str] = []
    shots = [Path(p) for p in video.get("screenshots") or [] if Path(p).exists()]
    if use_ai and not shots:
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
    chapters_text = "\n".join(f"{fmt_ts(t)} {title}" for t, title in yt_chapters)
    (out / "upload_kit.json").write_text(json.dumps({
        "titles": md.titles, "description": md.description, "chapters": chapters_text,
        "tags": md.tags, "thumbnail_text": md.thumbnail_text,
        "chapter_anchors": [[title, chapter_anchors.get(title, ""), t] for t, title in yt_chapters],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    r.log(f"Done. Raw {fmt_ts(raw_total)} -> edit {fmt_ts(final)}")
    r.log(f"timeline:   {xml_path}")
    thumbs: list[Path] = []
    if cfg.get("thumbnails", {}).get("enabled", True):
        if not shots:   # no screenshot given: use the frames the AI picked from the video
            shots = sorted((out / "thumbnail_frames").glob("frame_*.jpg"))[:5]
        try:
            thumbs = make_thumbnails(out, cfg, video, shots, use_ai, caller=call, log=r.log, index=index)
        except Exception as exc:  # thumbnails are a bonus; never lose the edit over them
            r.log(f"Thumbnails skipped: {exc}")

    return {"timeline": xml_path, "final_seconds": final, "raw_seconds": raw_total,
            "end_screen": end_screen.image if end_screen else None, "end_screen_seconds": es_secs,
            "placed": placed, "plan": plan, "clips": len(tl.main), "out_dir": out,
            "review": out / "review.md", "upload_kit": out / "upload_kit.md",
            "thumbnails": thumbs}


END_SCREEN_POSES = ("point_side", "presenting", "point_viewer", "wave")


def _pick_cold_open(md, by_id: dict, kept_ids: list[str], recordings, words, durations, voices,
                    log=print):
    """The AI's teaser pick, unless it's too dark to see: then its backups, then any 'clip
    that' moment, whichever is brightest enough. Returns (pick, range, brightness, note);
    pick is None when every candidate is too dark (no teaser beats a black screen)."""
    picks = [md.cold_open, *(md.cold_open_backups or [])]
    picks += [{"id": i, "seconds": 6, "reason": "you said 'clip that'"}
              for i in kept_ids if "highlight" in by_id[i].flags]
    seen, tried = set(), []
    for pick in picks:
        if not isinstance(pick, dict) or pick.get("id") not in by_id or pick["id"] in seen:
            continue
        seen.add(pick["id"])
        cb = by_id[pick["id"]]
        rng = teaser_range(cb, pick, words[cb.source], durations[cb.source], voices.get(cb.source))
        level = media.brightness(Path(recordings[rng.source]), rng.start, rng.end - rng.start)
        if level is None or level >= graphics.VIEWABLE:
            note = "" if not tried else (
                f"first pick ({tried[0][0]['id']}) was too dark to see, used {pick['id']} instead")
            if tried:
                log(f"Cold open: {note}")
            return pick, rng, level, note
        tried.append((pick, rng, level))
        if len(tried) >= 4:
            break
    if not tried:
        return None, None, None, ""
    pick, rng, level = max(tried, key=lambda t: t[2])
    if level >= 6:
        note = f"every pick was dark; used the brightest ({pick['id']})" if len(tried) > 1 else ""
        return pick, rng, level, note
    note = (f"skipped, the teaser moment{'s' if len(tried) > 1 else ''} "
            f"({', '.join(t[0]['id'] for t in tried)}) {'were' if len(tried) > 1 else 'was'} too dark to see")
    log(f"Cold open {note}")
    return None, None, level, note


def _add_end_screen(tl, cfg: dict, video: dict, out: Path, work: Path, width: int, height: int,
                    index: AssetIndex | None, log=print):
    """Render the end screen and append it to the main track. Returns (EndScreen, seconds)."""
    from kdcut import endscreen, thumbnails
    ed = cfg["edit"]
    secs = min(max(float(ed.get("end_screen_seconds", 20)), 5.0), 20.0)   # YouTube allows 5-20 s
    last = tl.main[-1]
    frame: Path | None = work / "end_screen_frame.jpg"
    try:
        media.extract_frame(last.path, max(tl.seconds(last.src_in + last.dur) - 0.5, 0.0), frame)
    except media.MediaError:
        frame = None
    series = video.get("series") or ""
    theme = thumbnails.Theme.load(thumbnails.theme_path(cfg, series)) or thumbnails.Theme()
    chars = {p.stem: p for p in thumbnails.list_characters(cfg)}
    poses, style = thumbnails.pose_library(cfg)
    want = str(ed.get("end_screen_pose") or "")
    pose = want if want in chars else next((n for n in END_SCREEN_POSES if n in chars), next(iter(chars), None))
    fonts = thumbnails.font_assets(index)
    es = endscreen.render(out / "end_screen.png", width, height, frame=frame, theme=theme, series=series,
                          character=chars.get(pose) if pose else None,
                          faces=poses.get(pose, thumbnails.PoseInfo()).faces if pose else "left",
                          glow=str(style.get("glow") or ""), labels=ed.get("end_screen_labels"),
                          font_path=fonts.get(theme.font))
    tl.main.append(Clip(path=es.image, src_in=0, dur=tl.frames(secs), rec=tl.duration,
                        role="end_screen", name="End screen", is_image=True))
    log(f"Added a {secs:.0f}s end screen" + (f" with the '{pose}' pose" if pose else ""))
    return es, secs


def make_thumbnails(out: Path, cfg: dict, video: dict, screenshots: list[Path], use_ai: bool,
                    caller=None, log=print, index: AssetIndex | None = None) -> list[Path]:
    """(Re)make thumbnails for an edited video. Also used by the app's "New ideas" button."""
    from kdcut import thumbnails
    if not screenshots:
        log("No screenshot for thumbnails (drag one into the Thumbnail box)")
        return []
    if index is None:
        index = AssetIndex.load(Path(cfg["assets"]["index_path"]))
    kit = {}
    if (out / "upload_kit.json").is_file():
        kit = json.loads((out / "upload_kit.json").read_text(encoding="utf-8"))
    log("Making thumbnails")
    return thumbnails.make(cfg, video, screenshots, out, titles=kit.get("titles") or [video.get("title", "")],
                           text_ideas=kit.get("thumbnail_text") or [], index=index, use_ai=use_ai,
                           caller=caller or planner.llm.call_tool, log=log)


def _safe(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()
    return keep or "timeline"


def _short(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"
