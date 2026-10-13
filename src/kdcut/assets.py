"""Index editing packs (sound effects, overlays, transitions) so the AI can ask for them by name.

The AI never sees the thousands of files. It asks for things like "level up sound" or
"diamond pickaxe item spin", and this module finds the best-matching file from filenames
and folder names, which editing packs keep very descriptive.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from kdcut.media import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS

ANIM_EXTS = {".gif"}
SKIP_DIRS = {"__macosx", ".git", "build", "fonts", "font", "premiere pro presets & mogrts",
             "davichi resolve packs", "davinci resolve packs", "mods, shaders & texture packs",
             "useful websites", "test output"}
SKIP_NAMES = {".ds_store", "desktop.ini", "thumbs.db"}

STOPWORDS = {
    "the", "and", "of", "a", "an", "to", "in", "on", "for", "with", "by", "v", "vs",
    "mp4", "mov", "webm", "png", "gif", "wav", "mp3", "jpg", "webp", "flac",
    "minecraft", "pack", "editing", "assets", "asset", "ultimate", "brazey", "brazeys",
    "zenitsulordplayz", "item", "items", "for thumbnails", "videos", "and videos", "extra",
    "resolve", "store", "the resolve store", "copy", "final", "new",
}

# Folder keywords -> category. First match wins, checked against the lowercased path.
CATEGORY_RULES: list[tuple[str, str]] = [
    ("sound effect", "sfx"),
    ("sfx", "sfx"),
    ("music", "music"),
    ("transition", "transition"),
    ("wipe", "transition"),
    ("thumbnail", "image"),
    ("background", "background"),
    ("animation", "overlay"),
    ("animated icon", "overlay"),
    ("arrow", "overlay"),
    ("label", "overlay"),
    ("icon", "overlay"),
    ("overlay", "overlay"),
    ("emoji", "overlay"),
    ("mobs", "overlay"),
    ("ui", "overlay"),
    ("progress bar", "overlay"),
    ("graphics", "overlay"),
    ("vector", "image"),
]


@dataclass
class Asset:
    path: str
    category: str      # sfx | music | transition | overlay | image | background
    kind: str          # audio | video | image
    tokens: list[str]
    name: str


def _tokenize(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    parts = re.split(r"[^A-Za-z0-9]+", text.lower())
    out = []
    for p in parts:
        if not p:
            continue
        stripped = re.sub(r"\d+$", "", p)  # break3 -> break
        if len(stripped) > 1 and stripped not in STOPWORDS:
            out.append(stripped)
    return out


def _singular(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _kind(ext: str) -> str | None:
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS or ext in ANIM_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def _category(rel_path: str, kind: str) -> str:
    low = rel_path.lower().replace("\\", "/")
    if kind == "audio":
        return "music" if "music" in low else "sfx"
    folders = low.rsplit("/", 1)[0] if "/" in low else ""
    for key, cat in CATEGORY_RULES:
        if re.search(r"\b" + re.escape(key), folders):
            if cat in ("sfx", "music"):
                continue
            return cat
    return "overlay" if kind == "video" else "image"


def scan_pack(root: Path) -> list[Asset]:
    root = Path(root)
    found: list[Asset] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("._") or fn.lower() in SKIP_NAMES:
                continue
            ext = Path(fn).suffix.lower()
            kind = _kind(ext)
            if kind is None:
                continue
            full = Path(dirpath) / fn
            rel = str(full.relative_to(root))
            # Tokens: filename counts fully, plus the two nearest folders.
            parents = Path(rel).parent.parts[-2:]
            tokens = _tokenize(Path(fn).stem) + [t for p in parents for t in _tokenize(p)]
            tokens = list(dict.fromkeys(_singular(t) for t in tokens))
            found.append(Asset(
                path=str(full), category=_category(rel, kind), kind=kind,
                tokens=tokens, name=Path(fn).stem,
            ))
    return found


class AssetIndex:
    def __init__(self, assets: list[Asset]):
        self.assets = assets
        df = Counter(t for a in assets for t in set(a.tokens))
        n = max(len(assets), 1)
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}

    # -- persistence -------------------------------------------------------
    @classmethod
    def build(cls, packs: list[Path]) -> "AssetIndex":
        assets: list[Asset] = []
        for pack in packs:
            pack = Path(pack).expanduser()
            if not pack.is_dir():
                print(f"  skipping missing pack folder: {pack}")
                continue
            got = scan_pack(pack)
            print(f"  {pack.name}: {len(got)} usable files")
            assets.extend(got)
        return cls(assets)

    def save(self, path: Path) -> None:
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "built": int(time.time()),
            "assets": [asdict(a) for a in self.assets],
        }), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "AssetIndex | None":
        path = Path(path).expanduser()
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls([Asset(**a) for a in data["assets"]])

    # -- querying ----------------------------------------------------------
    def summary(self) -> dict[str, int]:
        return dict(Counter(a.category for a in self.assets))

    def vocabulary(self, category: str, limit: int = 120) -> list[str]:
        """Most common descriptive words in a category, to hint the AI at what exists."""
        generic = {"sound", "effect", "animation", "animated", "spin", "half", "break in half",
                   "falling", "transition", "icon", "item", "static", "overlay", "short", "long"}
        c = Counter(t for a in self.assets if a.category == category
                    for t in set(_singular(x) for x in _tokenize(a.name)) if t not in generic)
        return [t for t, _ in c.most_common(limit)]

    def _match(self, q: list[str], toks: set[str]) -> tuple[float, float]:
        """Score one asset. Handles joined words ("level up" vs levelup.wav) and
        partial words ("pick" vs "pickaxe")."""
        covered: dict[int, float] = {}
        score = 0.0
        for i in range(len(q) - 1):          # bigrams: level+up -> levelup
            joined = q[i] + q[i + 1]
            if joined in toks:
                score += self.idf.get(joined, 1.0) * 1.2
                covered[i] = covered[i + 1] = 1.0
        for i, t in enumerate(q):
            if i in covered:
                continue
            if t in toks:
                score += self.idf.get(t, 1.0)
                covered[i] = 1.0
            elif len(t) >= 4:
                part = next((a for a in toks if a.startswith(t) or (len(a) >= 4 and t.startswith(a))), None)
                if part:
                    score += self.idf.get(part, 1.0) * 0.5
                    covered[i] = 0.5
        coverage = sum(covered.values()) / len(q)
        return score, coverage

    def search(self, query: str, category: str | None = None, k: int = 5,
               salt: str = "") -> list[tuple[float, Asset]]:
        q = list(dict.fromkeys(_singular(t) for t in _tokenize(query)))
        if not q:
            return []
        scored = []
        for a in self.assets:
            if category and a.category != category:
                continue
            score, coverage = self._match(q, set(a.tokens))
            if score <= 0:
                continue
            # Reward covering the whole query and short, specific filenames.
            score *= coverage ** 2
            score /= 1 + 0.05 * max(len(a.tokens) - len(q), 0)
            scored.append((score, a))
        scored.sort(key=lambda x: (-x[0], x[1].path))
        top = scored[: max(k, 1) * 4]
        if not top:
            return []
        # Several equally good variants (break1..break6): rotate deterministically
        # so repeated requests don't always pick the same sound.
        best = top[0][0]
        ties = [s for s in top if s[0] >= best * 0.999]
        if len(ties) > 1 and salt:
            shift = sum(map(ord, salt)) % len(ties)
            ties = ties[shift:] + ties[:shift]
            top = ties + [s for s in top if s[0] < best * 0.999]
        return top[:k]

    def best(self, query: str, category: str | None = None, salt: str = "",
             min_coverage: float = 0.5) -> Asset | None:
        res = self.search(query, category, k=1, salt=salt)
        if not res:
            return None
        _, asset = res[0]
        q = list(dict.fromkeys(_singular(t) for t in _tokenize(query)))
        _, coverage = self._match(q, set(asset.tokens))
        return asset if coverage >= min_coverage else None
