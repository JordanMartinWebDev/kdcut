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
from kdcut import audioprep, broll as broll_mod, captions, clues, continuity, graphics, vad, ducking, fcpxml, media, metadata, music, planner, transcribe, vision
from kdcut import montage as montage_mod, signals as signals_mod, speedup, story
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
    n_build, n_skip = clues.mark(blocks)
    if n_build or n_skip:
        r.log(f"Heard {n_build} build cue(s) and {n_skip} time skip(s)")
    silent = speedup.silent_clips(blocks, len(recordings))       # block id -> clips with no talking after it
    (out / "transcript.txt").write_text(
        planner.format_blocks(blocks, recordings, durations=[i.duration for i in infos]), encoding="utf-8")

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
    # Where the voice really starts and stops, so cuts don't clip words.
    voices = {}
    for src, (rec_path, info) in enumerate(zip(recordings, infos)):
        if info.audio:
            mic = media.pick_mic_track(info, cfg["whisper"].get("mic_track", "auto"))
            got = vad.analyse(Path(rec_path), mic, work, log=r.log)
            if got:
                voices[src] = got
        r.check()
    durations = [i.duration for i in infos]
    if reuse_plan:
        r.step(2, "AI edit (reusing saved plan)")
        plan = Plan.load(plan_file)
    elif use_ai:
        r.step(2, "AI edit")
        vocab = {}
        if index:
            vocab = {"sound effect": index.vocabulary("sfx", 100),
                     "overlay": index.vocabulary("overlay", 100)}
        # How lively each part is (free, measured locally) and the story of the whole episode.
        measured = signals_mod.Notes()
        try:
            sigs = {}
            for src, (rec_path, info) in enumerate(zip(recordings, infos)):
                r.check()
                sigs[src] = signals_mod.measure(Path(rec_path), info, cfg["whisper"].get("mic_track", "auto"),
                                                work, log=r.log)
            measured = signals_mod.describe(blocks, sigs, voices, durations)
            for bid, srcs in silent.items():
                # Clips with no talking after this line are part of its quiet stretch.
                extra = sum(durations[q] for q in srcs)
                measured.quiet_seconds[bid] = measured.quiet_seconds.get(bid, 0.0) + extra
                if bid in measured.quiet:
                    measured.quiet[bid] = (measured.quiet[bid].rstrip("]")
                                           + f" · plus {len(srcs)} clip(s) with no talking, {extra:.0f}s]")
                else:
                    measured.quiet[bid] = f"[quiet after: {len(srcs)} clip(s) with no talking, {extra:.0f}s]"
        except Cancelled:
            raise
        except Exception as exc:     # the numbers help; the edit works without them
            r.log(f"Footage measurements skipped: {exc}")
        outline = None
        try:
            outline = story.outline(cfg, video, blocks, call, cache=out / "story.json", log=r.log, force=replan,
                                    notes=notes, durations=durations)
        except Cancelled:
            raise
        except Exception as exc:
            r.log(f"Story outline skipped: {exc}")
        plan_notes = {k: list(v) for k, v in notes.items()}
        for k, v in signals_mod.as_notes(measured).items():
            plan_notes.setdefault(k, []).extend(v)
        if broll:
            # What's in each B-roll clip, so the editor can match it and use just the right part.
            try:
                shows = broll_mod.look(broll_by_name, cfg, work, caller=call, log=r.log)
                broll = [(n, d, broll_mod.describe(shows.get(n, []))) for n, d in broll]
            except Exception as exc:
                r.log(f"B-roll not looked at (file names only): {exc}")
        plan = planner.make_plan(blocks, recordings, cfg, video, broll, vocab, caller=call,
                                 log=r.log, notes=plan_notes, measured=measured.block_tags,
                                 outline=outline, quiet_seconds=measured.quiet_seconds,
                                 durations=durations)
        notes = plan_notes
        plan.save(plan_file)
    else:
        r.step(2, "Quick edit (spoken cues and pauses only)")
        plan = planner.plan_without_ai(blocks)
        plan.blocks_key = planner.blocks_key(blocks)
        plan.save(plan_file)

    # Short cut bits said in one breath with kept talk can't be cut cleanly: keep them.
    # (Applied on every run, so saved plans benefit too.)
    planner.protect_sentences(plan, blocks)         # incl. sentences carried across clips
    glued = planner.protect_joins(plan, blocks, voices)
    plan.notes += clues.enforce(plan, blocks)       # waits he skips ("it's nighttime") are cut
    if glued:
        r.log(f"Kept {len(glued)} short bit(s) that couldn't be cut cleanly")

    # Packaging (titles, description, cold open) needs the whole kept story, so it is
    # decided now, before the timeline is built. Cached alongside plan.json.
    by_id = {b.id: b for b in blocks}
    kept_ids = [bid for rng in kept_ranges(blocks, plan, durations, ed, voices) for bid in rng.block_ids]

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


    # Watch the edit as a viewer: bring back lines the story needs, move misplaced timelapses.
    view_notes: list[str] = []
    if use_ai and ed.get("viewer_pass", True):
        cont_file = out / "continuity.json"
        tl_where = {t.name: after for after, ts in tl_inserts.items() for t in ts}
        cont_key = continuity.cache_key(plan, tl_where)
        saved = json.loads(cont_file.read_text(encoding="utf-8")) if cont_file.is_file() else {}
        if saved.get("key") == cont_key:
            fixes = continuity.Fixes(**saved["fixes"])
        else:
            r.log("Watching the edit as a viewer")
            inserts_text: dict[str, list[str]] = {}
            for after, ts in tl_inserts.items():
                for t in ts:
                    inserts_text.setdefault(after, []).append(f"TIMELAPSE {t.name} ({t.duration:.0f}s) plays here")
            for f in plan.fast:
                inserts_text.setdefault(f["id"], []).append(f"fast-forward: {f.get('reason', '')}")
            for m in plan.montage:
                inserts_text.setdefault(m["id"], []).append(f"montage: {m.get('reason', '')}")
            for bid, secs in plan.extend.items():
                inserts_text.setdefault(bid, []).append(f"keeps {secs:.0f}s of gameplay after this line")
            outline_now = story.cached(out / "story.json", blocks)
            try:
                fixes = continuity.check(cfg, video, blocks, dict(plan.drop), durations, call, notes=notes,
                                         inserts=inserts_text, outline_text=outline_now.text() if outline_now else "",
                                         timelapse_files=[t.name for t in timelapses],
                                         window=max(int(cfg["llm"].get("window_blocks", 600)), 50), log=r.log)
                cont_file.write_text(json.dumps({"key": cont_key, "fixes": fixes.__dict__}, indent=2,
                                                ensure_ascii=False), encoding="utf-8")
            except Cancelled:
                raise
            except Exception as exc:
                r.log(f"Viewer pass skipped: {exc}")
                fixes = continuity.Fixes()
        back = continuity.apply(plan, fixes, blocks)
        if back:
            planner.protect_sentences(plan, blocks)
            planner.protect_joins(plan, blocks, voices)
            r.log(f"Brought back {len(back)} line(s) the story needs")
            for x in fixes.restore:
                if x["id"] in back:
                    view_notes.append(f"Kept {x['id']}: {x['why']}")
        if fixes.moves and timelapses:
            by_name = {t.name: t for t in timelapses}
            for mv in fixes.moves:
                t = by_name.get(mv["file"])
                if t is None:
                    continue
                for after in list(tl_inserts):
                    if t in tl_inserts[after]:
                        tl_inserts[after].remove(t)
                        if not tl_inserts[after]:
                            del tl_inserts[after]
                tl_inserts.setdefault(mv["after"], []).append(t)
                view_notes.append(f"Moved timelapse {t.name} after {mv['after']}: {mv['why']}")
        view_notes += [f"Check: {p}" for p in fixes.problems]
        plan.notes += view_notes
        kept_ids = [bid for rng in kept_ranges(blocks, plan, durations, ed, voices) for bid in rng.block_ids]
        # A timelapse placed after a line that's still cut goes after the next kept line.
        for after in list(tl_inserts):
            if after != "__end__" and after not in kept_ids:
                k = nearest_kept(after, blocks, {i: 0 for i in kept_ids})
                moved = tl_inserts.pop(after)
                tl_inserts.setdefault(k or "__end__", []).extend(moved)

    # Packaging needs to know where timelapses play (they get upbeat music).
    for after, ts in tl_inserts.items():
        for t in ts:
            notes = {**notes, after: list(notes.get(after, [])) + [f"[TIMELAPSE plays here: {t.name}, {t.duration:.0f}s]"]}
    want_cold_open = bool(video.get("cold_open", ed.get("cold_open", True)))
    history = music.load_history()
    # The channel's music feel decides what the AI may pick from: nothing sad ever, and in the
    # default "upbeat" feel nothing low-energy either. Scary tracks only for scary moments.
    vibe = str(mcfg.get("vibe", "upbeat"))
    offered, scary = music.for_vibe(library, vibe)
    library_text = music.library_text(offered, history) if offered else ""
    if scary and library_text:
        library_text += ("\n\nScary or big moments only (the deep dark, being hunted, very low health, "
                         "big fights and chases):\n"
                         + music.library_text(scary, history))
    if library and len(offered) < len(library):
        r.log(f"Music feel '{vibe}': {len(offered)} of {len(library)} tracks fit"
              + (f", {len(scary)} kept for scary moments" if scary else ""))
    library = offered + scary
    md_file = out / "metadata.json"
    md = metadata.Metadata()
    if use_ai:
        cached = json.loads(md_file.read_text(encoding="utf-8")) if md_file.is_file() else {}
        if (cached and cached.pop("_cold_open_requested", None) == want_cold_open
                and cached.pop("_music_requested", False) == bool(library)
                and cached.pop("_rules", 0) == metadata.RULES_VERSION
                and cached.pop("_vibe", "upbeat") == vibe
                and md_file.stat().st_mtime >= plan_file.stat().st_mtime):
            md = metadata.Metadata(**cached)
        else:
            r.log("Writing titles and description" + (", picking a cold open" if want_cold_open else ""))
            try:
                md = metadata.generate(cfg, video, [by_id[i] for i in kept_ids], caller=call,
                                       cold_open=want_cold_open, notes=notes, music_library=library_text)
                md_file.write_text(json.dumps({**md.__dict__, "_cold_open_requested": want_cold_open,
                                               "_music_requested": bool(library),
                                               "_rules": metadata.RULES_VERSION, "_vibe": vibe},
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

    # 4. Timeline -------------------------------------------------------------------
    r.check()
    r.step(3, "Building timeline")
    name = video.get("title") or project.name
    prepared: dict[int, timelapse_mod.Prepared] = {}     # id(clip) -> prepared timelapse
    insert_clips: dict[str, list[Clip]] = {}
    fast_info: dict[int, dict] = {}                      # id(clip) -> the plan's fast item
    next_block = {}
    for a, b in zip(blocks, blocks[1:]):
        if a.source == b.source:
            next_block[a.id] = b
    def stretch(b, seconds: float):
        """(recording, info, start, end) of the quiet stretch after a block. When clips with no
        talking follow it, that's where the work is: they're joined into one file."""
        span = speedup.source_span(b, next_block.get(b.id), seconds, ed, plan.extend.get(b.id, 0.0),
                                   durations[b.source], voices.get(b.source))
        srcs = silent.get(b.id)
        if srcs:
            # The quiet end of this clip, then the clips with no talking, as one stretch. A short
            # clip is joined whole (so its timing stays exact); a long one only contributes the
            # clips after it.
            own = span is not None and durations[b.source] <= 300.0
            try:
                paths = ([Path(recordings[b.source])] if own else []) + [Path(recordings[q]) for q in srcs]
                joined = paths[0] if len(paths) == 1 else speedup.join_clips(
                    paths, work / "joined" / f"after_{b.id}.mp4", log=r.log)
                info = media.probe(joined)
                s0 = span[0] if own else 0.0
                s1 = min(info.duration, s0 + max(seconds, 4.0))
                if s1 - s0 > 4.0:
                    return joined, info, s0, s1
            except (media.MediaError, OSError) as exc:
                r.log(f"Couldn't join the clips after {b.id} ({exc})")
        if span is None:
            return None
        return Path(recordings[b.source]), infos[b.source], span[0], span[1]

    # Montages: building/mining cut to the beat of a song. Falls back to a fast-forward when
    # the stretch hasn't enough clear game sounds, there's no music, or numpy is missing.
    fast_items = list(plan.fast)
    montage_info: dict[int, montage_mod.Result] = {}
    montage_songs: set[str] = set()
    matcher = None
    if plan.montage and bool(ed.get("montages", True)):
        try:
            from kdcut import mcsounds
            matcher = mcsounds.Matcher.load(mcsounds.find_assets(ed.get("minecraft_assets") or None), log=r.log)
            if matcher is None:
                tl_notes.append("Montages became fast-forwards: Minecraft's sound files weren't found (set "
                                "edit.minecraft_assets to your launcher's assets folder)")
        except ImportError:
            matcher = None
        for n, item in enumerate(plan.montage[:int(ed.get("max_montages", 2))]):
            b = by_id.get(item.get("id"))
            if b is None:
                continue
            got = stretch(b, float(item["seconds"]))
            if got is None:
                continue
            rec_path, info, s0, s1 = got
            span = (s0, s1)
            r.check()
            res = None
            song = montage_mod.pick_song(offered, history, seed=len(blocks) + n, used=montage_songs)
            if song is not None:
                try:
                    sig = signals_mod.measure(Path(rec_path), info, cfg["whisper"].get("mic_track", "auto"),
                                              work, log=r.log)
                    r.log(f"Cutting {span[1] - span[0]:.0f}s after {b.id} into a montage to {song.name}")
                    vol = float(mcfg.get("volume_db", -20))
                    res = montage_mod.make(Path(rec_path), vision.game_audio_track(info, cfg["whisper"].get("mic_track", "auto")),
                                           span[0], span[1], song, fps, width, height,
                                           work / "montage" / f"montage_{b.id}.mp4",
                                           music_db=audioprep.music_gain(Path(song.path)) + max(vol + 16.0, -8.0),
                                           sig=sig, target=float(ed.get("montage_seconds", 15)),
                                           matcher=matcher, log=r.log,
                                           punch=float(ed.get("montage_punch", 0.06) or 0.0))
                except ImportError:
                    r.log("Montage skipped: numpy isn't available")
                except media.MediaError as exc:
                    r.log(f"Montage after {b.id} skipped: {exc}")
            if res is None:            # show the stretch as a fast-forward instead
                real = span[1] - span[0]
                fast_items.append({"id": b.id, "seconds": real, "speed": round(min(max(real / 12.0, 3.0), 12.0), 1),
                                   "reason": (item.get("reason") or "") + " (montage fallback)"})
                continue
            montage_songs.add(song.path)
            clip = Clip(path=res.file, src_in=0, dur=res.frames, rec=0, role="montage",
                        name=f"Montage - {song.title}")
            montage_info[id(clip)] = res
            insert_clips.setdefault(b.id, []).append(clip)
    done_ids = {bid for bid, clips in insert_clips.items() if any(c.role == "montage" for c in clips)}
    fast_items = [f for f in fast_items if f.get("id") not in done_ids]   # one treatment per stretch
    for item in fast_items:
        b = by_id.get(item.get("id"))
        if b is None:
            continue
        got = stretch(b, float(item["seconds"]))
        if got is None:
            continue
        src_path, _, s0, s1 = got
        span = (s0, s1)
        r.check()
        try:
            r.log(f"Speeding up {span[1] - span[0]:.0f}s after {b.id} ({item['speed']:g}x)")
            path, frames = speedup.render(src_path, span[0], span[1], float(item["speed"]),
                                          work / "fast" / f"fast_{b.id}.mp4", fps, width, height,
                                          badge=bool(ed.get("speedup_badge", True)), log=r.log)
        except media.MediaError as exc:
            r.log(f"Speed-up after {b.id} skipped: {exc}")
            continue
        clip = Clip(path=path, src_in=0, dur=frames, rec=0, role="fast",
                    name=f"Fast {item['speed']:g}x - {recordings[b.source].stem}")
        fast_info[id(clip)] = {**item, "real": span[1] - span[0]}
        insert_clips.setdefault(b.id, []).append(clip)
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
    placed = {"broll": 0, "overlay": 0, "sfx": 0, "music": 0, "timelapse": 0, "fast": 0, "montage": 0}
    montage_spans: list[tuple[float, float]] = []
    for clip in tl.main:
        m = montage_info.get(id(clip))
        if m is None:
            continue
        placed["montage"] += 1
        montage_spans.append((tl.seconds(clip.rec), tl.seconds(clip.rec_end)))
        tl.markers.append(Marker(rec=clip.rec, name=f"Montage: {m.plan.song_name}", kind="note"))
        review.append(f"{fmt_ts(tl.seconds(clip.rec))}  Montage: {m.real_seconds:.0f}s of gameplay as {m.note}, "
                      f"{tl.seconds(clip.dur):.0f}s")
    for clip in tl.main:
        f = fast_info.get(id(clip))
        if f is None:
            continue
        placed["fast"] += 1
        tl.markers.append(Marker(rec=clip.rec, name=f"Sped up {f['speed']:g}x", kind="note"))
        review.append(f"{fmt_ts(tl.seconds(clip.rec))}  Fast-forward {f['speed']:g}x: {f['real']:.0f}s of gameplay "
                      f"in {tl.seconds(clip.dur):.0f}s" + (f"  ({f['reason']})" if f.get("reason") else ""))
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

    picks = []
    for item in plan.broll:
        path = broll_by_name.get(item.get("file", ""))
        if path is None or not path.exists() or item.get("at") not in by_id:
            continue
        b = by_id[item["at"]]
        info = media.probe(path)
        length = 0.0 if path.suffix.lower() in media.IMAGE_EXTS else info.duration
        pick = broll_mod.plan_pick(item, path, length, b, words[b.source], recordings[b.source],
                                   tl, anchor(item["at"]))
        if pick:
            picks.append(pick)
    for pick in broll_mod.no_stacking(picks, tl.frames):
        lane = lanes.place(int(tcfg["broll_lane"]), pick.rec, pick.dur)
        tl.connected.append(Clip(path=pick.path, src_in=pick.src_in, dur=pick.dur, rec=pick.rec, lane=lane,
                                 enable="video", role="broll", name=pick.path.stem,
                                 is_image=pick.path.suffix.lower() in media.IMAGE_EXTS))
        placed["broll"] += 1
        part = f"{tl.seconds(pick.src_in):.0f}-{tl.seconds(pick.src_in + pick.dur):.0f}s of "
        review.append(f"{fmt_ts(tl.seconds(pick.rec))}  B-roll  {part}{pick.name}"
                      + ("" if pick.exact else "  [words not found: placed at the block]")
                      + (f"  ({pick.reason})" if pick.reason else ""))

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
        firm: set[float] = set()
        for c in md.music:
            at = c.get("at", "")
            b_at = by_id.get(at)
            # A song picked for a build cue (or for the line a timelapse/montage follows)
            # starts where the work starts: the end of that line.
            work_starts = b_at is not None and ("build_cue" in b_at.flags or at in insert_clips)
            rec = anchor(at, at_end=work_starts)
            if rec is None:
                continue
            tracks = [t for t in (find(c.get("track", "")), find(c.get("backup", ""))) if t]
            t0 = max(tl.seconds(rec), music_start)
            if work_starts:
                firm.add(t0)
            raw.append((t0, tracks, c.get("why", "")))
        if not raw:   # no AI picks: one fresh track per chapter
            cue_starts = sorted({max(t, music_start) for t, _ in chapter_times} | {music_start})
            rng_tracks = sorted(offered or library, key=lambda t: history.get(t.path, 0))
            raw = [(s, [rng_tracks[i % len(rng_tracks)]], "") for i, s in enumerate(cue_starts)]
        raw.sort(key=lambda x: x[0])
        # Talking on the finished timeline (also used for ducking below).
        speech = [(tl.seconds(starts[b]), tl.seconds(ends[b])) for b in starts] + vo_spans
        raw = music.tidy_cues(music.from_the_start(raw, music_start), speech, music_start, total, firm=firm)
        if scary:
            # Scary/heavy music only where the story outline says it's dangerous.
            outline = story.cached(out / "story.json", blocks)
            beat_at = story.beat_of(outline, blocks) if outline else {}
            timeline_blocks = sorted((tl.seconds(starts[b]), b) for b in starts)

            def danger_at(t: float) -> bool:
                import bisect
                i = bisect.bisect_right([s for s, _ in timeline_blocks], t + 1.0) - 1
                near = [b for _, b in timeline_blocks[max(i, 0):i + 6]]
                return any(beat_at.get(b, {}).get("kind") in music.SPECIAL_BEATS for b in near)
            raw, swaps = music.guard_special(raw, offered, scary, danger_at, history)
            for note in swaps:
                r.log(note)
                review.append(f"  ({note})")
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
            gains = ducking.envelope(speech, total, talk_db=talk_db, gap_db=gap_db, silent=montage_spans)
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
        music.record_use([pc.track.path for pc in pieces] + sorted(montage_songs))

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
        f"- Placed: {placed['timelapse']} timelapses, {placed['montage']} montages, {placed['fast']} fast-forwards, {placed['broll']} B-roll, {placed['overlay']} overlays, "
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
    # The episode's very last frame, so the animated end screen grows out of it seamlessly.
    # (A timelapse ends on its dip to black: take a frame from before the dip instead.)
    ends_dark = last.role == "timelapse" and ed.get("timelapse_transition", "dip") == "dip"
    back = 0.5 if ends_dark else 2 / float(tl.fps)
    try:
        media.extract_frame(last.path, max(tl.seconds(last.src_in + last.dur) - back, 0.0), frame)
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
    clip_path, still = es.image, True
    if bool(ed.get("end_screen_motion", True)) and es.layers is not None:
        import hashlib
        from PIL import Image
        if ends_dark:
            es.layers.sharp = Image.new("RGB", (width, height), (0, 0, 0))
        sig = hashlib.sha1(es.image.read_bytes() + f"|{secs}|{tl.fps}|{ends_dark}|m1".encode()).hexdigest()[:12]
        video_file = work / f"end_screen_{sig}.mp4"
        try:
            if not video_file.is_file():
                log("Animating the end screen")
                endscreen.animate(es.layers, video_file, secs, tl.fps, log=log)
            clip_path, still, es.video = video_file, False, video_file
        except Exception as exc:          # the still version is a fine fallback
            log(f"Kept the end screen still (couldn't animate it: {exc})")
    tl.main.append(Clip(path=clip_path, src_in=0, dur=tl.frames(secs), rec=tl.duration,
                        role="end_screen", name="End screen", is_image=still))
    log(f"Added a {secs:.0f}s {'still ' if still else ''}end screen" + (f" with the '{pose}' pose" if pose else ""))
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
