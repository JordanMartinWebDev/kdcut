"""Write a Timeline as FCPXML 1.9, which DaVinci Resolve (free) imports via
File > Import > Timeline. Clips stay linked to the original files, so nothing is re-rendered."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path, PureWindowsPath

from kdcut import media
from kdcut.timeline import Clip, Timeline

_FORMAT_NAMES = {
    (1920, 1080, Fraction(60)): "FFVideoFormat1080p60",
    (1920, 1080, Fraction(30)): "FFVideoFormat1080p30",
    (1920, 1080, Fraction(60000, 1001)): "FFVideoFormat1080p5994",
    (1920, 1080, Fraction(30000, 1001)): "FFVideoFormat1080p2997",
    (2560, 1440, Fraction(60)): "FFVideoFormat2560x1440p60",
    (3840, 2160, Fraction(60)): "FFVideoFormat3840x2160p60",
    (3840, 2160, Fraction(30)): "FFVideoFormat3840x2160p30",
}


def file_url(path: Path | str) -> str:
    s = str(path)
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith("\\\\"):
        return PureWindowsPath(s).as_uri()
    return Path(s).resolve().as_uri()


class _Writer:
    def __init__(self, tl: Timeline):
        self.tl = tl
        self.fd = Fraction(tl.fps.denominator, tl.fps.numerator)  # frame duration
        self.resources = ET.Element("resources")
        self.assets: dict[str, str] = {}
        self._next = 0
        self.seq_format = self._format(tl.width, tl.height, with_rate=True)

    def t(self, frames: int) -> str:
        if frames == 0:
            return "0s"
        # Keep the frame-duration denominator (e.g. 231/60s, 1001/30000s) rather than
        # reducing the fraction; that's the form FCP and Resolve write themselves.
        return f"{frames * self.fd.numerator}/{self.fd.denominator}s"

    def _id(self) -> str:
        self._next += 1
        return f"r{self._next}"

    def _format(self, w: int, h: int, with_rate: bool) -> str:
        fid = self._id()
        attrs = {"id": fid, "width": str(w or 1920), "height": str(h or 1080)}
        if with_rate:
            attrs["frameDuration"] = self.t(1)
            name = _FORMAT_NAMES.get((w, h, self.tl.fps))
            if name:
                attrs["name"] = name
        else:
            attrs["name"] = "FFVideoFormatRateUndefined"
        ET.SubElement(self.resources, "format", attrs)
        return fid

    def asset(self, clip: Clip) -> str:
        key = str(clip.path)
        if key in self.assets:
            return self.assets[key]
        info = media.probe(clip.path)
        aid = self._id()
        attrs = {"id": aid, "name": clip.path.stem, "start": "0s"}
        if info.is_image or (info.has_video and info.duration <= 0):
            attrs.update(duration="0s", hasVideo="1",
                         format=self._format(info.width, info.height, with_rate=False))
        else:
            dur_frames = max(int(info.duration * self.tl.fps), clip.src_in + clip.dur)
            attrs["duration"] = self.t(dur_frames)
            if info.has_video:
                attrs["hasVideo"] = "1"
                attrs["format"] = self.seq_format
            if info.has_audio:
                attrs.update(hasAudio="1", audioSources=str(len(info.audio)),
                             audioChannels=str(max(a.channels for a in info.audio)), audioRate="48000")
        el = ET.SubElement(self.resources, "asset", attrs)
        ET.SubElement(el, "media-rep", {"kind": "original-media", "src": file_url(clip.path)})
        self.assets[key] = aid
        return aid

    def clip_element(self, parent: ET.Element, clip: Clip, offset: int, start: int) -> ET.Element:
        ref = self.asset(clip)
        info = media.probe(clip.path)
        still = info.is_image or (info.has_video and info.duration <= 0)
        tag = "video" if still else "asset-clip"
        attrs = {"ref": ref, "name": clip.name or clip.path.stem,
                 "offset": self.t(offset), "start": self.t(start), "duration": self.t(clip.dur)}
        if clip.lane:
            attrs["lane"] = str(clip.lane)
        if tag == "asset-clip":
            attrs["tcFormat"] = "NDF"
            if clip.enable != "all":
                attrs["srcEnable"] = clip.enable
        el = ET.SubElement(parent, tag, attrs)
        if clip.volume_db is not None and tag == "asset-clip":
            ET.SubElement(el, "adjust-volume", {"amount": f"{clip.volume_db:g}dB"})
        return el


def write(tl: Timeline, dest: Path, event: str = "KDPlays") -> Path:
    w = _Writer(tl)
    root = ET.Element("fcpxml", {"version": "1.9"})
    root.append(w.resources)
    library = ET.SubElement(root, "library")
    ev = ET.SubElement(library, "event", {"name": event})
    project = ET.SubElement(ev, "project", {"name": tl.name})
    seq = ET.SubElement(project, "sequence", {
        "format": w.seq_format, "duration": w.t(tl.duration), "tcStart": "0s",
        "tcFormat": "NDF", "audioLayout": "stereo", "audioRate": "48k",
    })
    spine = ET.SubElement(seq, "spine")

    main_els = []
    for clip in tl.main:
        main_els.append((clip, w.clip_element(spine, clip, offset=clip.rec, start=clip.src_in)))

    def parent_for(rec: int):
        for clip, el in main_els:
            if clip.rec <= rec < clip.rec_end:
                return clip, el
        return main_els[-1] if main_els else (None, None)

    # Connected clips must come before markers inside a clip (FCPXML DTD order).
    for c in sorted(tl.connected, key=lambda c: c.rec):
        pclip, pel = parent_for(c.rec)
        if pel is None:
            continue
        w.clip_element(pel, c, offset=pclip.src_in + c.rec - pclip.rec, start=c.src_in)

    for m in sorted(tl.markers, key=lambda m: m.rec):
        pclip, pel = parent_for(m.rec)
        if pel is None:
            continue
        attrs = {"start": w.t(pclip.src_in + m.rec - pclip.rec), "duration": w.t(1), "value": m.name}
        if m.kind == "note":
            attrs["note"] = "kdcut"
        ET.SubElement(pel, "marker", attrs)

    # Resources must list formats before assets; they were appended in creation order,
    # so re-sort to be safe for strict importers.
    w.resources[:] = sorted(w.resources, key=lambda e: 0 if e.tag == "format" else 1)

    ET.indent(root, space="  ")
    dest.parent.mkdir(parents=True, exist_ok=True)
    xml = ET.tostring(root, encoding="unicode")
    dest.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + xml + "\n",
                    encoding="utf-8")
    return dest
