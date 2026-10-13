"""Checks run against the *installed* app (used by the Windows build on GitHub).

    KDcut.exe --selftest result.json [--whisper]

Builds the window off-screen, checks ffmpeg and the speech-recognition libraries are
bundled, then edits a tiny synthetic recording end to end without the AI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


def run(out_file: Path) -> int:
    results: dict[str, str] = {}

    def check(name, fn):
        try:
            value = fn()
            results[name] = "ok" + (f": {value}" if value not in (None, True) else "")
        except Exception:
            results[name] = "FAIL: " + traceback.format_exc()[-1500:]

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    tmp = Path(tempfile.mkdtemp(prefix="kdcut-selftest-"))

    from kdcut import appcore, config, media, pipeline, transcribe
    from kdcut.transcribe import Word

    check("ffmpeg bundled", lambda: appcore.ffmpeg_ok() or (_ for _ in ()).throw(RuntimeError("ffmpeg not found")))

    def imports():
        import ctranslate2
        import faster_whisper  # noqa: F401
        return f"ctranslate2 {ctranslate2.__version__}, cuda devices: {ctranslate2.get_cuda_device_count()}"
    check("speech libraries", imports)

    def window():
        from kdcut import gui
        app = gui.make_app(sys.argv[:1])
        w = gui.MainWindow()
        w.show()
        d = gui.SettingsDialog(w)
        d.collect()
        app.processEvents()
        w.close()
        d.close()
        return "window and settings built"
    check("window", window)

    def ff(*args):
        subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def edit():
        rec = tmp / "session 1.mkv"
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=60:duration=12",
           "-f", "lavfi", "-i", "sine=frequency=300:duration=12",
           "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(rec))
        green = tmp / "pack" / "Item Animations" / "diamond_Item Spin.mp4"
        green.parent.mkdir(parents=True)
        ff("-f", "lavfi", "-i", "color=c=0x00FF00:size=320x180:rate=30:duration=1",
           "-vf", "drawbox=x=140:y=70:w=40:h=40:color=cyan:t=fill", "-pix_fmt", "yuv420p", str(green))
        out, how = media.prepare_overlay(green, tmp / "cache")
        assert how == "keyed" and media.probe(out).has_alpha, how

        cfg = config._merge(config.DEFAULTS, {"assets": {"index_path": str(tmp / "index.json"),
                                                          "cache_dir": str(tmp / "cache")}})
        outdir = tmp / "Test video"
        words = [Word(0.5 + i * 0.4, 0.8 + i * 0.4, w) for i, w in enumerate("hello and welcome back".split())]
        words += [Word(3.0, 3.3, "oops"), Word(4.0, 4.3, "cut"), Word(4.35, 4.65, "that.")]
        words += [Word(7.0 + i * 0.4, 7.3 + i * 0.4, w) for i, w in enumerate("today we build a castle.".split())]
        info = media.probe(rec)
        key = transcribe._cache_key(rec, media.pick_mic_track(info, "auto"), cfg["whisper"]["model"])
        transcribe.save_cache(outdir / ".work" / f"{rec.stem}.words.json", key, words)
        res = pipeline.run(outdir, cfg, use_ai=False, recordings=[rec], broll_files=[],
                           video={"title": "Test video"}, out_dir=outdir)
        assert res["timeline"].exists()
        assert 0 < res["final_seconds"] < 12
        return f"{res['clips']} clips, {res['final_seconds']:.1f}s"
    check("edit pipeline", edit)

    def thumb():
        from PIL import Image

        from kdcut import thumbnails
        shot = tmp / "shot.png"
        Image.new("RGB", (1920, 1080), (70, 80, 90)).save(shot)
        cfg = config._merge(config.DEFAULTS, {"thumbnails": {"themes_dir": str(tmp / "themes")}})
        out = thumbnails.make(cfg, {"title": "x", "episode": 1}, [shot], tmp, titles=["Test"],
                              text_ideas=["IT WORKS"], index=None, use_ai=False, log=lambda m: None)
        assert out and Image.open(out[0]).size == (1280, 720)
        return f"{len(out)} thumbnails"
    check("thumbnails", thumb)

    if "--whisper" in sys.argv:
        def whisper():
            cfg = config._merge(config.DEFAULTS, {"whisper": {"model": "tiny", "device": "cpu"}})
            speech = tmp / "speech.wav"
            ff("-f", "lavfi", "-i", "sine=frequency=200:duration=4", str(speech))
            words = transcribe.transcribe_file(speech, cfg, tmp / "w", force=True)
            return f"transcribed ({len(words)} words from a test tone)"
        check("whisper", whisper)

    out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(v.startswith("ok") for v in results.values()) else 1
