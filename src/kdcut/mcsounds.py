"""Which game sounds are blocks breaking or being placed: searched for in the game audio using
Minecraft's own sound files.

The game plays the exact same files every time, just quieter or louder, panned, and for
block break and place at 0.8 pitch. So each sharp sound in a stretch is compared, waveform
against waveform (normalised cross-correlation, a "matched filter"), with every block
break/place sound pitched like the game does. A real block sound matches one of them almost
perfectly (0.81-1.0 on real footage, even with cave ambience mixed in); eating, burps,
item pickups, damage, footsteps and the little ticks while mining scored at most 0.61. The sound files come from the player's own Minecraft launcher folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from kdcut import media
from kdcut.config import HOME_DIR

SR = 11025                 # same rate as montage.decode
LENGTH = 0.25              # seconds of each sound compared
PITCH = 0.8                # blocks break and get placed at 0.8 pitch
MATCH = 0.72               # real footage: block sounds 0.81-1.0; eating <= 0.54, damage <= 0.61
LEAD = 0.005               # templates start this long before the sound rises
CUTOFF = 3000.0            # compare below this: the hiss above it is too timing-sensitive after resampling
VERSION = 2


def lowpass(x):
    """Windowed-sinc low-pass at CUTOFF (both the sound files and the game audio go through it)."""
    import numpy as np
    n = np.arange(-40, 41)
    fc = CUTOFF / SR
    h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(len(n))
    return np.convolve(np.asarray(x, dtype=np.float64), h / h.sum(), mode="same")


def find_assets(override: str | None = None) -> Path | None:
    """Minecraft's assets folder (has indexes/ and objects/) from the usual launchers."""
    cands = [Path(override).expanduser()] if override else []
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    home = Path.home()
    cands += [appdata / "PrismLauncher" / "assets", appdata / ".minecraft" / "assets",
              appdata / "ModrinthApp" / "meta" / "assets", appdata / "com.modrinth.theseus" / "meta" / "assets",
              home / "curseforge" / "minecraft" / "Install" / "assets", appdata / "ATLauncher" / "assets",
              appdata / "MultiMC" / "assets", home / ".minecraft" / "assets",
              home / "Library" / "Application Support" / "minecraft" / "assets"]
    for c in cands:
        if (c / "indexes").is_dir() and (c / "objects").is_dir():
            return c
    return None


def _index(assets: Path) -> tuple[Path, dict]:
    best = max((assets / "indexes").glob("*.json"), key=lambda p: p.stat().st_mtime)
    return best, json.loads(best.read_text(encoding="utf-8")).get("objects", {})


def is_block_sound(name: str) -> bool:
    """minecraft/sounds/dig/stone1.ogg, minecraft/sounds/block/deepslate/break3.ogg, .../place2.ogg"""
    if name.startswith("minecraft/sounds/dig/"):
        return True
    return name.startswith("minecraft/sounds/block/") and ("/break" in name or "/place" in name)


def decode_sound(path: Path, pitch: float = 1.0):
    """A sound file at SR, played at `pitch` (lower and slower below 1, like the game)."""
    import numpy as np
    rate = int(round(SR / pitch))
    proc = media.run_quiet(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate),
                            "-f", "f32le", "-"], timeout=60, text=False)
    return np.frombuffer(proc.stdout or b"", dtype=np.float32)


def onset(x) -> int:
    """Where the sound in a file starts (files have a little silence before it)."""
    import numpy as np
    if not len(x):
        return 0
    peak = float(np.abs(x).max())
    above = np.nonzero(np.abs(x) > peak * 0.15)[0]
    return int(max(above[0] - int(LEAD * SR), 0)) if len(above) else 0


class Matcher:
    """Every block break/place sound, ready to be searched for in game audio."""

    def __init__(self, templates, names: list[str]):
        import numpy as np
        self.templates = np.asarray(templates, dtype=np.float32)        # (n, L), each unit length
        self.names = names
        self.length = self.templates.shape[1]
        self._nfft = 1 << int(np.ceil(np.log2(self.length + int(0.15 * SR))))
        self._spec = np.conj(np.fft.rfft(self.templates, self._nfft, axis=1))

    def match(self, x, at: int, search: float = 0.06) -> tuple[float, str]:
        """(best normalised correlation, sound name) for a sound starting near sample `at`."""
        score, name, _ = self.locate(x, at, search)
        return score, name

    def locate(self, x, at: int, search: float = 0.06) -> tuple[float, str, int]:
        """Like match(), plus the sample where that sound really starts: the waveform match is
        exact to a sample, much tighter than the rough onset it was found from."""
        import numpy as np
        a = max(at - int(search * SR), 0)
        seg = x[a:a + self.length + int(2 * search * SR)]
        if len(seg) < self.length:
            return 0.0, "", at
        seg = lowpass(seg)
        lags = len(seg) - self.length + 1
        c = np.fft.irfft(np.fft.rfft(seg, self._nfft)[None, :] * self._spec, self._nfft, axis=1)[:, :lags]
        energy = np.sqrt(np.convolve(seg.astype(np.float64) ** 2, np.ones(self.length), "valid"))[:lags] + 1e-9
        ncc = c / energy[None, :]
        i = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
        # Templates start LEAD before the sound rises (see onset()).
        return float(ncc[i]), self.names[i[0]], a + int(i[1]) + int(LEAD * SR)

    threshold = MATCH

    def is_block(self, x, at: int) -> bool:
        return self.match(x, at)[0] >= self.threshold

    @classmethod
    def load(cls, assets: Path | None = None, cache_dir: Path | None = None, log=None) -> "Matcher | None":
        import numpy as np
        assets = assets or find_assets()
        if assets is None:
            return None
        cache_dir = cache_dir or HOME_DIR
        idx_path, objects = _index(assets)
        key = f"{idx_path}|{int(idx_path.stat().st_mtime)}|{PITCH}|{LENGTH}|{SR}|v{VERSION}"
        meta, arr = cache_dir / "block_sounds.json", cache_dir / "block_sounds.npy"
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            if data.get("key") == key and arr.is_file():
                return cls(np.load(arr), data["names"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass
        names = sorted(k for k in objects if is_block_sound(k))
        if log:
            log(f"Reading Minecraft's block sounds ({len(names)} files, once)")
        L = int(LENGTH * SR)
        rows, kept = [], []
        for k in names:
            p = assets / "objects" / objects[k]["hash"][:2] / objects[k]["hash"]
            if not p.is_file():
                continue
            try:
                x = decode_sound(p, PITCH)
            except Exception:
                continue
            x = lowpass(x[onset(x):][:L])
            if len(x) < L // 4:
                continue
            x = np.pad(x - x.mean(), (0, L - len(x)))
            n = float(np.linalg.norm(x))
            if n > 0:
                rows.append((x / n).astype(np.float32))
                kept.append(k.split("sounds/", 1)[-1])
        if not rows:
            return None
        m = cls(np.stack(rows), kept)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            np.save(arr, m.templates)
            meta.write_text(json.dumps({"key": key, "names": kept}), encoding="utf-8")
        except OSError:
            pass
        return m
