# PyInstaller spec for the KDcut desktop app (Windows). Run from the repo root:
#   pyinstaller packaging/kdcut.spec --noconfirm
# Expects ffmpeg.exe and ffprobe.exe in packaging/ffmpeg/ (the GitHub build downloads them).
import os
from PyInstaller.utils.hooks import collect_all

here = os.path.abspath(SPECPATH)
root = os.path.dirname(here)

datas = [(os.path.join(here, "kdcut.ico"), ".")]
binaries = []
hiddenimports = ["kdcut.selftest", "kdcut.gpu"]

ffmpeg_dir = os.path.join(here, "ffmpeg")
if os.path.isdir(ffmpeg_dir):
    for exe in ("ffmpeg.exe", "ffprobe.exe"):
        binaries.append((os.path.join(ffmpeg_dir, exe), "ffmpeg"))

for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers", "huggingface_hub", "hf_xet"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(here, "kdcut_app.py")],
    pathex=[os.path.join(root, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "torch"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="KDcut",
    icon=os.path.join(here, "kdcut.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="KDcut", upx=False)
