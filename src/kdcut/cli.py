"""kdcut command line.

    kdcut setup                      create ~/.kdcut/config.toml
    kdcut init  <folder>             create a new episode folder
    kdcut index-assets [pack ...]    scan editing packs (run once, and after adding packs)
    kdcut build <folder>             recordings -> Resolve timeline + review + upload kit
    kdcut short <folder>             just the vertical Short from the folder's short/ files
    kdcut stats                      save a snapshot of your YouTube channel stats
    kdcut mcp                        channel stats server for the Claude desktop app (stdio)
    kdcut find  "level up" --sfx     test what the asset search picks
    kdcut doctor                     check ffmpeg, GPU, API key, packs
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from kdcut import __version__, config


def cmd_setup(args) -> int:
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    if config.USER_CONFIG.exists() and not args.force:
        print(f"Config already exists: {config.USER_CONFIG}")
    else:
        config.USER_CONFIG.write_text(config.USER_TEMPLATE, encoding="utf-8")
        print(f"Wrote {config.USER_CONFIG}. Add your editing pack folders under [assets] packs.")
    return 0


def cmd_init(args) -> int:
    p = Path(args.folder)
    for sub in ("recordings", "broll", "timelapses", "short", "output"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    toml = p / "kdcut.toml"
    if not toml.exists():
        toml.write_text(config.PROJECT_TEMPLATE, encoding="utf-8")
    print(f"Created {p.resolve()}")
    print("  1. Put your OBS recordings in recordings/ (they're edited in filename order)")
    print("  2. Optional: Flashback renders to show while you talk go in broll/; silent timelapses that")
    print("     play as their own scene go in timelapses/, with any voiceover audio for them")
    print("  3. Optional Short: a build timelapse, glamor shots (put 'glamor' in their names) and a")
    print("     voiceover go in short/")
    print("  4. Fill in the topic in kdcut.toml, then run:  kdcut build \"" + str(p) + "\"")
    return 0


def cmd_index(args) -> int:
    from kdcut.assets import AssetIndex
    cfg = config.load()
    packs = [Path(p) for p in (args.packs or cfg["assets"]["packs"])]
    if not packs:
        print("No packs given. Pass folders, or list them under [assets] packs in "
              f"{config.USER_CONFIG}")
        return 1
    index = AssetIndex.build(packs)
    index.save(Path(cfg["assets"]["index_path"]))
    print(f"Indexed {len(index.assets)} assets: {index.summary()}")
    print(f"Saved to {Path(cfg['assets']['index_path']).expanduser()}")
    return 0


def cmd_find(args) -> int:
    from kdcut.assets import AssetIndex
    cfg = config.load()
    index = AssetIndex.load(Path(cfg["assets"]["index_path"]))
    if not index:
        print("No asset index yet. Run: kdcut index-assets")
        return 1
    cat = "sfx" if args.sfx else "overlay" if args.overlay else args.category
    for score, a in index.search(args.query, cat, k=args.n):
        print(f"{score:6.2f}  [{a.category}] {a.path}")
    return 0


def cmd_build(args) -> int:
    from kdcut import pipeline
    project = Path(args.folder)
    cfg = config.load(project)
    pipeline.run(project, cfg, use_ai=not args.no_ai, retranscribe=args.retranscribe,
                 replan=args.replan)
    return 0


def cmd_short(args) -> int:
    from kdcut import pipeline, shorts
    project = Path(args.folder).resolve()
    cfg = config.load(project)
    short = shorts.from_folder(project / "short")
    if short is None:
        print(f"No Short files in {project / 'short'}. Put a build timelapse, glamor shots "
              "(with 'glamor' in the name) and a voiceover there.")
        return 1
    video = {"title": project.name, **pipeline._video_settings(project)}
    res = pipeline.make_short(project / "output", cfg, video, short, use_ai=not args.no_ai)
    if not res or res.get("error"):
        return 1
    for n in res["notes"]:
        print(f"  check: {n}")
    return 0


def cmd_stats(args) -> int:
    from kdcut import stats
    cfg = config.load()
    folder = Path(args.folder) if args.folder else stats.default_folder(cfg)
    stats.snapshot(folder)
    return 0


def cmd_mcp(args) -> int:
    from kdcut import mcp_server
    return mcp_server.main()


def cmd_doctor(args) -> int:
    cfg = config.load()
    ok = True

    def line(good: bool, msg: str):
        nonlocal ok
        ok &= good
        print(("  OK   " if good else "  !!   ") + msg)

    line(shutil.which("ffmpeg") is not None, "ffmpeg on PATH (winget install Gyan.FFmpeg)")
    try:
        import faster_whisper  # noqa: F401
        line(True, "faster-whisper installed")
    except ImportError:
        line(False, "faster-whisper installed (pip install faster-whisper)")
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        line(n > 0, f"NVIDIA GPU visible to Whisper ({n} device(s))")
    except Exception:
        line(False, "NVIDIA GPU visible to Whisper (pip install nvidia-cublas-cu12 nvidia-cudnn-cu12)")
    from kdcut.llm import api_key
    line(bool(api_key(cfg)), f"Claude API key in {cfg['llm']['api_key_env']}")
    line(config.USER_CONFIG.exists(), f"user config at {config.USER_CONFIG} (kdcut setup)")
    idx = Path(cfg["assets"]["index_path"]).expanduser()
    line(idx.exists(), f"asset index at {idx} (kdcut index-assets)")
    for p in cfg["assets"]["packs"]:
        line(Path(p).expanduser().is_dir(), f"pack folder exists: {p}")
    print("\nAll good." if ok else "\nFix the !! lines above (no-AI mode works without the API key).")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kdcut", description="AI-assisted Minecraft video editor.")
    ap.add_argument("--version", action="version", version=f"kdcut {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="create ~/.kdcut/config.toml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("init", help="create a new episode folder")
    s.add_argument("folder")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("index-assets", help="scan editing packs")
    s.add_argument("packs", nargs="*")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("find", help="search the asset index")
    s.add_argument("query")
    s.add_argument("--sfx", action="store_true")
    s.add_argument("--overlay", action="store_true")
    s.add_argument("--category")
    s.add_argument("-n", type=int, default=8)
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser("build", help="edit an episode folder")
    s.add_argument("folder")
    s.add_argument("--no-ai", action="store_true", help="only spoken cues + silence trimming")
    s.add_argument("--replan", action="store_true", help="ignore the saved plan.json and ask the AI again")
    s.add_argument("--retranscribe", action="store_true")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("short", help="make just the vertical Short from short/")
    s.add_argument("folder")
    s.add_argument("--no-ai", action="store_true", help="simple titles instead of AI ones")
    s.set_defaults(fn=cmd_short)

    s = sub.add_parser("stats", help="save a snapshot of your channel stats")
    s.add_argument("--folder", help="where to save it (default: Videos/KDcut/Channel stats)")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("mcp", help="channel stats server for the Claude desktop app")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("doctor", help="check your setup")
    s.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if os.environ.get("KDCUT_DEBUG"):
            raise
        print(f"\nError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
