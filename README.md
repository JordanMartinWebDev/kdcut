# KDcut

AI-assisted editor for the **KDPlays** Minecraft channel.

Drag your OBS recordings into the window and click **Edit my video**. KDcut gives you back a
**DaVinci Resolve timeline** that's already cut, with B-roll, sound effects, pop-up graphics and
chapter markers placed on their own tracks. It also writes titles, a description and chapters for
YouTube. You review the timeline in Resolve, tweak it, and export.

## Install

1. **[Download KDcut-Setup.exe](https://github.com/JordanMartinWebDev/kdcut/releases/latest/download/KDcut-Setup.exe)** and run it.
   Windows may say *"Windows protected your PC"* because the app isn't code-signed. Click
   **More info → Run anyway**.
2. The first time KDcut opens, it asks for two things:
   - your **Claude API key** (from [platform.claude.com](https://platform.claude.com/settings/keys)), and
   - your **editing pack folders**, which it indexes once.
3. If you have an NVIDIA GPU, click **Download GPU support** in Settings (one time, about 1 GB)
   so transcription runs on the GPU.

**Updating:** KDcut checks for a new version when it starts. When one is out, a bar appears at the
top; click **Update now** and it downloads the installer, updates itself and reopens, keeping your
settings and videos. You can also use **Settings → Updates → Check now**.

The first video also downloads the speech-recognition model, about 3 GB and one time only.
After that, the only thing that leaves your PC is transcript text sent to Claude.

## Making a video

1. Type a **name** and a line about **what happens** in the video.
2. Drag your **recordings** into the left box. You can add one or several sessions; they're
   sorted by filename, which matches OBS's date-time naming. Drag to reorder.
3. Optionally, drag **B-roll** (Flashback renders, timelapses) into the right box. Name files
   after what they show, e.g. `castle-walls-timelapse.mp4`, so the AI knows where to put them.
4. Click **Edit my video**. When it finishes, click **How to open in Resolve**, then import the
   `.fcpxml` with **File > Import > Timeline**.

Each video gets its own folder in `Videos\KDcut\<name>` containing the timeline, `review.md`
(every cut and why), `upload_kit.md` (titles, description, chapters, tags) and thumbnail frame
candidates. Re-running the same video is fast because the transcript and the AI's decisions are saved.

## Thumbnails

Drag an in-game screenshot (F2) into the **Thumbnail** box. KDcut makes three 1280x720
thumbnails using:

- **2–4 punchy words** with a thick outline, plus one word in the series' accent colour
- **your character**: transparent PNG poses of your skin. Set the folder in Settings, and name
  files by mood (`waving.png`, `thinking.png`, `shocked.png`) so the AI picks the right one.
  Optionally add a `poses.json` beside them:

  ```json
  {
    "_style": {"glow": "#FFC440"},
    "shocked": {"mood": "hands up beside the face; surprise, danger, a big reveal", "faces": "left"},
    "victory": {"mood": "fist in the air; won, finished, beat the boss", "faces": "left"}
  }
  ```

  `mood` tells the AI when to use each pose, `faces` (left / right / front) is which way the
  character looks in the PNG so KDcut can flip it toward the words, and `_style.glow` adds a
  soft brand-coloured halo behind the character (a series theme's `character_glow` overrides it).
  Files starting with `_` are ignored.
- **one or two item icons** from your packs, when they tell the story
- a **ring and arrow** on the key thing in the screenshot, when there is one
- **series branding**: your series logo (the **Series logo…** button), episode badge, and
  accent colours

Every video gets a different composition: words beside a big character, a wide banner across
the top, or low words with icons above. The logo and episode tab go in whichever corner is free.
The first time a series gets thumbnails, the AI designs its style (colours and font, matched to
your logo and character) and saves it, so every later episode looks consistent. The bottom-right
corner is kept clear because YouTube puts the video length there. Click **New thumbnail ideas**
for another round; earlier ones are never overwritten.

### End screen

Every edit ends with a 20-second end screen (tick **Add an end screen** before editing, on by
default): a blurred last frame of the episode, two "watch next" spots on the left, a subscribe
circle in the middle and your character on the right pointing at them (the `point_side` pose,
or `presenting` / `point_viewer` / `wave` if you don't have it). Music keeps playing underneath.

YouTube's upload API can't place end screen elements, so do this once in YouTube Studio:
Content > the video > Editor > **End screen** > add Video (Most recent upload) on the top spot,
Video (Best for viewer) or your series playlist on the bottom spot, and Subscribe on the circle.
For every video after that: **End screen > Import from video** and pick the last one. The steps
are also in `review.md`.

Settings in `edit`: `end_screen`, `end_screen_seconds` (5-20), `end_screen_pose`, and
`end_screen_labels` (`{series}` becomes the series name).

## How the AI edits

It's a let's-play, so your talking is the content: the AI keeps the story, your stories and your
reactions, and what moves toward the goals you state. It cuts detours that go nowhere (checking a
shore for iron, finding none, moving on) and clear waste (mic checks, retakes, the same point twice,
stray fragments, talk after the goodbye). There's no target length. It never cuts in the middle of a sentence; if it
tries, KDcut puts the sentence back and says so in `review.md`. Pauses while you think of what to
say next are cut for a snappy edit; the AI only keeps silent gameplay where something is happening.

On-screen graphics are yours to add: the AI doesn't place item spins or text unless you turn on
**Let the AI add on-screen graphics** in Settings (then it uses them only for "list" moments, like
announcing the goal). Sound effects are kept to clear moments: punchlines, fails, scares, reveals.

**Start with a teaser** puts the episode's biggest moment first (a reveal, a near-death, your
biggest reaction; never walking or setup), cut to the exact words from where the excitement starts
to the peak of the reaction, in black and white with
**COMING UP** in the corner, then a quick flash into the episode.

## Music

Add background music in **Settings → Music**: pick one or more folders of music you're licensed
to use, for example your **Epidemic Sound** downloads. Remember to connect your YouTube channel in
your Epidemic account so the music isn't claimed. KDcut never downloads music itself.

The AI matches music to each part of the video: calm for building, tense for caves and fights,
upbeat for the intro. It leaves silence where that's stronger, and avoids tracks you used in the
last week. Music goes on its own audio track at background volume (Quiet, Normal or Louder), after
the cold open. Epidemic files carry their mood, energy, genre and tempo inside the file, and KDcut
reads those, so the AI knows "Peaceful, low energy" from "Dark, suspense". For music from elsewhere,
mood subfolders (`Calm`, `Tense`, `Upbeat`, `Funny`) do the same job.

Sound effects can live in the same folder. Anything credited to "SFX Producer", anything under 20
seconds, and ambiences with no mood or tempo are kept out of the music and added to the sound-effect
choices instead. Duplicate downloads (`Song (1).mp3`) are only used once.

**Check my library…** in Settings counts your tracks per mood (calm, adventure, upbeat, funny, tense,
action, emotional), flags the thin ones with what to search for on Epidemic, and lists untagged
tracks and duplicate copies.

With **Lower the music while I talk** on (the default), KDcut pre-mixes one music bed that dips
about 10 dB under your voice and comes back up in the gaps. Turn it off to get separate track clips
you can swap in Resolve.

Every build also writes `captions.srt`, timed to the edit. Resolve can import it as subtitles, and
YouTube accepts it as captions.

## Uploading to YouTube

When your edit is done, export it from Resolve, then click **Upload to YouTube…** on the results
screen. Pick the export, choose a title and thumbnail, and pick **Private**, **Unlisted** or
**Schedule**. KDcut then:

1. listens to the exported video so the captions and chapter times match your final cut, even
   if you changed things in Resolve
2. uploads it (resumable, so a dropped connection doesn't start over) in the Gaming category,
   marked "not made for kids"
3. sets the thumbnail and adds the captions

Then you check it in YouTube Studio and press Publish.

### Connecting YouTube

This is a one-time setup that takes about 5 minutes. YouTube only lets apps upload through a Google
Cloud project you own; it's free.

1. Go to <https://console.cloud.google.com>, create a project (name it "KDcut").
2. **APIs & Services → Library**, search **YouTube Data API v3**, and click **Enable**.
3. **Google Auth Platform** (or "OAuth consent screen"): choose **External**, name it KDcut, and use your
   email. Under **Audience**, add your Google account as a test user.
4. **Clients → Create client → Desktop app**. Download the JSON file.
5. In KDcut: **Settings → YouTube → Choose client file…**, pick that JSON, then **Sign in** and
   approve in the browser. Pick the Google account (or brand account) that owns your channel.

Two things to know:
- While the app is in **Testing**, Google signs you out every 7 days, and you click Sign in again.
  To avoid that, click **Publish app** on the Audience page. Google will then show an "unverified app"
  warning when you sign in, which you can pass with **Advanced → Go to KDcut**. That's expected for a
  private tool only you use.
- Custom thumbnails need a verified channel (phone verification at youtube.com/verify).

## Letting the AI look at the footage

With **Let the AI look at the footage** ticked, KDcut finds stretches where you weren't talking,
checks them for loud game audio and scene changes (free, on your PC), then sends small frames
from those spots to Claude. The editor sees notes like *"[on screen, 24s without talking] fighting
zombies at night, low health (action 3)"*, so it keeps wordless action and cuts AFK time. It's
capped at 120 frames per video and adds a little to the API cost.

## How it works

1. **Transcribe** every recording on your own NVIDIA GPU with Whisper (free, local, word-level timestamps).
2. **Split** the speech into numbered blocks and apply your **spoken cues** (below).
3. **AI edit**: Claude reads the transcript and decides what to cut (mic checks, retakes, dead air,
   rambles), where to keep wordless gameplay, which B-roll goes where, which sound effects and
   overlays fit, and where chapters start.
4. **Build the timeline**: frame-accurate cuts linked to your original files (nothing is re-rendered),
   your voice on A1 and game audio on A2 (each pulled out of the recording and levelled, because
   Resolve only imports a recording's first audio track), music on A3, sound effects below that at
   a consistent loudness, B-roll on V2, overlays on V3, and chapter and highlight markers.
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

## Developer setup (command line)

Only needed to work on KDcut itself. You need Python 3.11+ and ffmpeg.

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

Get an API key at <https://platform.claude.com/settings/keys> and save it:

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

### Command-line usage

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
  Then choose **Track 2** under Settings → Mic audio track. Transcription is much more accurate
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
keep_pauses_up_to = 0.6   # longer pauses (thinking of what to say next) are cut
pad_after = 0.3           # breathing room after each sentence

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
pip install -e ".[app]"
kdcut-app                                 # run the desktop app from source
python -m unittest discover -s tests -v   # needs ffmpeg; no GPU or API key required
```

Every push to `main` runs the tests on Linux and Windows, builds the app with PyInstaller,
self-tests the built `.exe` (window, bundled ffmpeg, Whisper, and an end-to-end edit), wraps it in an
Inno Setup installer, and publishes it as the latest release. See `.github/workflows/build.yml`.

`tests/test_e2e.py` builds synthetic recordings, a fake editing pack and a fake AI, then checks the
whole pipeline, including that every cut in the FCPXML lands on a whole frame.

## Roadmap

- [x] Transcribe, spoken cues, AI cut list, Resolve timeline
- [x] Editing pack index, SFX and overlays, green-screen keying
- [x] B-roll placement, chapters, titles, description, thumbnail frames
- [x] Thumbnails with a consistent style per series (character, icons, logo, episode badge)
- [x] Optional footage check (the AI looks at frames from quiet stretches)
- [x] Background music from your own licensed library, picked per section
- [x] Music ducking under your voice
- [x] Captions (.srt) timed to the edit
- [x] Upload to YouTube as private or scheduled, with title, chapters, captions and thumbnail
- [x] Desktop app with drag and drop, and a Windows installer built by GitHub Actions
