# kdcut

AI-assisted editor for the **KDPlays** Minecraft channel.

Record an hour or two in OBS, drop the files in a folder, and run one command. kdcut gives you a
**DaVinci Resolve timeline** that's already cut, with B-roll, sound effects, pop-up graphics and
chapter markers placed on their own tracks. You review it, tweak it, and export.

```
recordings/ (1-2 hr raw)  ──►  kdcut build  ──►  Episode.fcpxml   (open in Resolve)
broll/      (Flashback)                          review.md         (what was cut and why)
                                                 upload_kit.md     (titles, description, chapters, tags)
                                                 thumbnail_frames/ (candidate frames)
```

## How it works

1. **Transcribe** every recording on your own NVIDIA GPU with Whisper (free, local, word-level timestamps).
2. **Split** the speech into numbered blocks and apply your **spoken cues** (below).
3. **AI edit**: Claude reads the transcript and decides what to cut (mic checks, retakes, dead air,
   rambles), where to keep wordless gameplay, which B-roll goes where, which sound effects and
   overlays fit, and where chapters start.
4. **Build the timeline**: frame-accurate cuts linked to your original files (nothing is re-rendered),
   B-roll on V2, overlays on V3, SFX on A2, and chapter and highlight markers.
5. **Write review notes and an upload kit**.

Only transcript text goes to the AI. Your video never leaves your PC.

### Editing while you record: spoken cues

Say these on their own, with a short pause before and after:

| Say | What happens |
|---|---|
| **"cut that"** / "scratch that" / "let me redo that" | Removes what you just said (the previous sentence) |
| **"clip that"** / "clip it" | Marks the moment as a highlight: never cut, gets a marker, the AI may add an SFX or overlay |
| **"new chapter"** / "chapter break" | Starts a YouTube chapter at the next thing you say |

The cue words are always removed. "I'm gonna cut that tree down" is left alone because a cue
needs a pause after it.

### Editing packs

kdcut indexes your packs (Brazey's, ZenitsuLordPlayz, or any folder of SFX and overlays) by
filename. The AI asks for things like "level up" or "diamond pickaxe" and kdcut picks the best
matching file. Overlays are prepared for Resolve automatically:

- **Green-screen MP4s** (most item spins and falling transitions) are keyed to transparent ProRes 4444.
- **WebM/GIF with transparency**, which Resolve free doesn't read reliably, are converted to ProRes 4444.
- **ProRes/PNG with alpha** are used as-is.

Converted files are cached in `~/.kdcut/cache`, so each one is only converted once.

## Install (Windows)

You need Python 3.11+, ffmpeg and DaVinci Resolve (the free version works).

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install Git.Git
# open a NEW terminal so PATH updates

git clone https://github.com/<you>/kdcut.git
cd kdcut
py -m venv .venv
.venv\Scripts\activate
pip install -e ".[all]"        # kdcut + faster-whisper + NVIDIA CUDA libraries
```

Get an API key at <https://console.anthropic.com> and save it:

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
# open a new terminal afterwards
```

Point kdcut at your editing packs and index them (one time, and again when you add packs):

```powershell
kdcut setup                    # creates %USERPROFILE%\.kdcut\config.toml
notepad $HOME\.kdcut\config.toml
```

```toml
[assets]
packs = [
  "C:/Users/jorda/Downloads/Brazey's Ultimate Minecraft Editing Pack (4000+ Assets + Emote Generator)/Brazey's Ultimate Minecraft Editing Pack (4000+ Assets + Emote Generator)",
  "C:/Users/jorda/Downloads/ZenitsuLordPlayz Ultimate 8000 Minecraft Editing Pack/ZenitsuLordPlayz Ultimate Minecraft Editing Pack",
]
```

```powershell
kdcut index-assets
kdcut find "level up" --sfx    # check what it picks
kdcut doctor                   # everything should say OK
```

The first transcription downloads the Whisper model (~3 GB, one time).

## Making a video

```powershell
kdcut init "D:\KDPlays\Hardcore Ep 12"
```

1. Copy your OBS recordings into `recordings\`. They're edited in filename order, and OBS's
   date-time names already sort correctly.
2. Put Flashback renders and timelapses in `broll\` with **descriptive names**
   (`castle-walls-timelapse.mp4`, `base-flyover.mp4`). The AI places them by matching names to what you say.
3. Optionally fill in `kdcut.toml` with the topic or series. It helps the AI judge what matters.
4. Run:

```powershell
kdcut build "D:\KDPlays\Hardcore Ep 12"
```

5. In Resolve: **File > Import > Timeline…** and pick `output\<name>.fcpxml`. Read `review.md` alongside it.

Re-running is cheap. Transcripts are cached, and the AI's decisions are saved in `output\plan.json`.
Edit that file by hand and rebuild, or use `--replan` for a fresh AI pass.

| Option | Use |
|---|---|
| `--no-ai` | Spoken cues and pause trimming only. No API key needed. |
| `--replan` | Ask the AI again (for example, after adding B-roll) |
| `--retranscribe` | Redo transcription (for example, after changing the Whisper model) |

## OBS tips

- **Put your mic on its own audio track.** In Settings > Output > Recording, enable tracks 1 and 2.
  In Edit > Advanced Audio Properties, set the mic to tracks 1+2 and game/desktop audio to track 1 only.
  Then set `mic_track = 2` under `[whisper]` in your config. Transcription is much more accurate
  without game audio mixed in.
- MKV or MP4 both work. MKV is safer because it survives crashes. If Resolve has trouble with
  one, use OBS's File > Remux Recordings.

## Settings

Defaults live in `src/kdcut/config.py`. Override them in `~/.kdcut/config.toml` (all videos) or
`<project>/kdcut.toml` (one video). The useful ones:

```toml
[channel]
style_notes = "Keep the jokes and the chat interaction. Cut inventory sorting. Don't cut deaths."

[edit]
keep_pauses_up_to = 2.5   # longer silences are cut
pad_after = 0.35          # breathing room after each sentence
target_keep = "60-80%"

[whisper]
mic_track = 2
model = "large-v3"        # "medium" is faster with slightly lower accuracy

[llm]
model = "claude-sonnet-5-5"
```

## Cost

Whisper, Resolve and kdcut are free. The Claude API is the only cost. Expect well under $1 for a
1–2 hour session (one edit pass plus one metadata pass). The exact cost depends on the model and
current pricing.

## Development

```bash
pip install -e .
python -m unittest discover -s tests -v   # needs ffmpeg; no GPU or API key required
```

`tests/test_e2e.py` builds synthetic recordings, a fake editing pack and a fake AI, then checks the
whole pipeline, including that every cut in the FCPXML lands on a whole frame.

## Roadmap

- [x] Transcribe, spoken cues, AI cut list, Resolve timeline
- [x] Editing pack index, SFX and overlays, green-screen keying
- [x] B-roll placement, chapters, titles, description, thumbnail frames
- [ ] Thumbnail compositing (frame + your face + big text)
- [ ] Upload to YouTube as private with the metadata filled in
- [ ] Desktop app window (drag and drop instead of the command line)
- [ ] Auto speed-ramp long wordless stretches into timelapses
