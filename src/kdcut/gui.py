"""KDcut desktop app: drag in recordings, click Edit, get a Resolve timeline."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from kdcut import __version__, appcore, config, gpu, llm, media, pipeline
from kdcut.assets import AssetIndex

APP_NAME = "KDcut"

STYLE = """
* { font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 10.5pt; color: #e9eaec; }
QMainWindow, QDialog, QWidget#page { background: #1b1c1f; }
QLabel#title { font-size: 20pt; font-weight: 700; color: #ffffff; }
QLabel#subtitle { color: #9a9ca3; }
QLabel#section { font-size: 11.5pt; font-weight: 600; color: #ffffff; margin-top: 6px; }
QLabel#hint { color: #8e9097; font-size: 9.5pt; }
QLabel#big { font-size: 13pt; color: #ffffff; }
QLineEdit, QPlainTextEdit, QComboBox {
  background: #26282c; border: 1px solid #36383e; border-radius: 6px; padding: 7px 9px;
  selection-background-color: #3d8b40;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: #5fbf63; }
QListWidget#drop {
  background: #222428; border: 2px dashed #3c3f46; border-radius: 10px; padding: 6px;
}
QListWidget#drop[hover="true"] { border-color: #5fbf63; background: #1f2a20; }
QListWidget::item { padding: 5px 6px; border-radius: 4px; }
QListWidget::item:selected { background: #2f4a31; }
QPushButton {
  background: #2c2e33; border: 1px solid #3a3d44; border-radius: 6px; padding: 7px 14px;
}
QPushButton:hover { background: #34373d; }
QPushButton:disabled { color: #6b6d73; }
QPushButton#primary {
  background: #3f9a43; border: 1px solid #4cb050; color: #ffffff; font-size: 12.5pt;
  font-weight: 600; padding: 12px 28px; border-radius: 8px;
}
QPushButton#primary:hover { background: #47aa4b; }
QPushButton#primary:disabled { background: #2d4a2f; color: #8fa891; border-color: #2d4a2f; }
QPushButton#danger { background: #4a2a2a; border-color: #6b3434; }
QProgressBar {
  background: #26282c; border: 1px solid #36383e; border-radius: 6px; height: 16px;
  text-align: center;
}
QProgressBar::chunk { background: #4cb050; border-radius: 5px; }
QFrame#card { background: #232529; border: 1px solid #2f3136; border-radius: 10px; }
QCheckBox { spacing: 8px; }
"""


# --------------------------------------------------------------------------------------
# Background work
# --------------------------------------------------------------------------------------

class Bridge(QObject):
    """Carries progress from worker threads to the window (Qt queues these safely)."""
    step = Signal(int, str)
    progress = Signal(float)
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class QtReporter(pipeline.Reporter):
    def __init__(self, bridge: Bridge, stop: threading.Event):
        self.bridge, self.stop = bridge, stop

    def step(self, index, label):
        self.bridge.step.emit(index, label)

    def progress(self, fraction):
        self.bridge.progress.emit(float(fraction))

    def log(self, message):
        self.bridge.log.emit(str(message))

    def cancelled(self):
        return self.stop.is_set()


def run_in_thread(bridge: Bridge, fn, stop: threading.Event | None = None) -> threading.Thread:
    def target():
        try:
            bridge.done.emit(fn())
        except pipeline.Cancelled:
            bridge.cancelled.emit()
        except Exception as exc:  # show the user something readable, keep details in the log
            if stop is not None and stop.is_set():
                bridge.cancelled.emit()
                return
            bridge.log.emit(traceback.format_exc())
            bridge.failed.emit(str(exc) or exc.__class__.__name__)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------------------

def open_path(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def reveal(path: Path) -> None:
    if sys.platform == "win32" and path.exists():
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        open_path(path.parent if path.is_file() else path)


def label(text: str, name: str = "", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


class DropList(QListWidget):
    """A list you can drag files or folders into, reorder by dragging, and delete from."""
    changed = Signal()

    def __init__(self, exts: set[str], empty_text: str, sort_on_add: bool):
        super().__init__()
        self.setObjectName("drop")
        self.exts, self.empty_text, self.sort_on_add = exts, empty_text, sort_on_add
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumHeight(130)
        self.model().rowsMoved.connect(lambda *a: self.changed.emit())
        QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self, self.remove_selected)

    # -- data --------------------------------------------------------------------------
    def paths(self) -> list[Path]:
        return [Path(self.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.count())]

    def set_paths(self, paths: list[Path]) -> None:
        self.clear()
        for p in paths:
            p = Path(p)
            try:
                size = f"{p.stat().st_size / 1e9:.1f} GB" if p.stat().st_size > 5e8 else \
                    f"{p.stat().st_size / 1e6:.0f} MB"
            except OSError:
                size = "missing!"
            item = QListWidgetItem(f"{p.name}    ·  {size}")
            item.setToolTip(str(p))
            item.setData(Qt.ItemDataRole.UserRole, str(p))
            self.addItem(item)
        self.changed.emit()
        self.viewport().update()

    def add_paths(self, raw: list[str | Path]) -> list[Path]:
        accepted, rejected = appcore.collect_files(raw, self.exts)
        self.set_paths(appcore.merge_unique(self.paths(), accepted, self.sort_on_add))
        return rejected

    def remove_selected(self):
        for item in self.selectedItems():
            self.takeItem(self.row(item))
        self.changed.emit()
        self.viewport().update()

    # -- drag and drop -----------------------------------------------------------------
    def _external(self, event) -> bool:
        md = event.mimeData()
        return md.hasUrls() and event.source() is not self and any(u.isLocalFile() for u in md.urls())

    def _hover(self, on: bool):
        self.setProperty("hover", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):
        if self._external(event):
            self._hover(True)
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._external(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        self._hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._hover(False)
        if self._external(event):
            rejected = self.add_paths([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])
            event.acceptProposedAction()
            if rejected:
                QMessageBox.information(self, APP_NAME, "These aren't video files, so they were skipped:\n\n"
                                        + "\n".join(p.name for p in rejected[:10]))
        else:
            super().dropEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() == 0:
            p = QPainter(self.viewport())
            p.setPen(Qt.GlobalColor.gray)
            p.drawText(self.viewport().rect().adjusted(16, 0, -16, 0),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.empty_text)
            p.end()


class ThumbLabel(QLabel):
    """A thumbnail preview; click to open the full image."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        pm = QPixmap(str(path))
        self.setPixmap(pm.scaled(288, 162, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation))
        self.setToolTip(f"{path.name}: click to open")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("border: 2px solid #3a3d44; border-radius: 6px;")

    def mousePressEvent(self, event):
        open_path(self.path)


def drop_section(title: str, hint: str, lst: DropList, file_filter: str, parent: QWidget) -> QWidget:
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    row = QHBoxLayout()
    row.addWidget(label(title, "section", wrap=False))
    row.addStretch(1)
    add = QPushButton("Add files…")
    clear = QPushButton("Clear")
    row.addWidget(add)
    row.addWidget(clear)
    v.addLayout(row)
    v.addWidget(label(hint, "hint"))
    v.addWidget(lst, 1)

    def pick():
        files, _ = QFileDialog.getOpenFileNames(parent, title, "", file_filter)
        if files:
            lst.add_paths(files)

    add.clicked.connect(pick)
    clear.clicked.connect(lambda: lst.set_paths([]))
    return box


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------

MODELS = [("Best quality (large-v3)", "large-v3"), ("Faster (medium)", "medium"),
          ("Fastest (small)", "small")]


class SettingsDialog(QDialog):
    def __init__(self, parent=None, welcome: bool = False):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} settings")
        self.setMinimumWidth(640)
        self.settings = config.load_settings()
        cfg = config.load()
        self.bridge = Bridge()
        self.stop = threading.Event()

        root = QVBoxLayout(self)
        if welcome:
            root.addWidget(label("Welcome! Two quick things before your first video:", "big"))
            root.addWidget(label("1) your Claude API key, and 2) where your editing packs live. "
                                 "You only do this once.", "hint"))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(12)

        # API key
        self.key = QLineEdit(cfg["llm"].get("api_key", ""))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("sk-ant-…")
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        test = QPushButton("Test")
        test.clicked.connect(self.test_key)
        krow = QHBoxLayout()
        krow.addWidget(self.key, 1)
        krow.addWidget(show)
        krow.addWidget(test)
        kbox = QVBoxLayout()
        kbox.addLayout(krow)
        link = label('Get a key at <a href="https://console.anthropic.com/settings/keys" '
                     'style="color:#7fd483">console.anthropic.com</a> (Settings → API Keys).', "hint")
        link.setOpenExternalLinks(True)
        kbox.addWidget(link)
        self.key_status = label("", "hint")
        kbox.addWidget(self.key_status)
        form.addRow("Claude API key", kbox)

        # Packs
        self.packs = QListWidget()
        self.packs.setMinimumHeight(90)
        for p in cfg["assets"]["packs"]:
            self.packs.addItem(p)
        prow = QHBoxLayout()
        add = QPushButton("Add folder…")
        rem = QPushButton("Remove")
        self.index_btn = QPushButton("Index packs now")
        add.clicked.connect(self.add_pack)
        rem.clicked.connect(lambda: [self.packs.takeItem(self.packs.row(i)) for i in self.packs.selectedItems()])
        self.index_btn.clicked.connect(self.index_packs)
        prow.addWidget(add)
        prow.addWidget(rem)
        prow.addStretch(1)
        prow.addWidget(self.index_btn)
        pbox = QVBoxLayout()
        pbox.addWidget(self.packs)
        pbox.addLayout(prow)
        self.pack_status = label(self._index_summary(cfg), "hint")
        pbox.addWidget(self.pack_status)
        form.addRow("Editing packs", pbox)

        # Mic track
        self.mic = QComboBox()
        self.mic.addItem("Automatic", "auto")
        for n in range(1, 7):
            self.mic.addItem(f"Track {n}", str(n))
        cur = str(cfg["whisper"].get("mic_track", "auto"))
        self.mic.setCurrentIndex(max(self.mic.findData(cur), 0))
        mbox = QVBoxLayout()
        mbox.addWidget(self.mic)
        mbox.addWidget(label("If OBS records your mic on its own audio track, pick it here for much "
                             "better accuracy.", "hint"))
        form.addRow("Mic audio track", mbox)

        # Model
        self.model = QComboBox()
        for text, value in MODELS:
            self.model.addItem(text, value)
        self.model.setCurrentIndex(max(self.model.findData(cfg["whisper"].get("model")), 0))
        form.addRow("Speech recognition", self.model)

        # GPU
        self.gpu_status = label(gpu.status(), "hint")
        self.gpu_btn = QPushButton("Download GPU support (~1 GB, one time)")
        self.gpu_btn.clicked.connect(self.install_gpu)
        self.gpu_btn.setVisible(sys.platform == "win32" and gpu.has_nvidia_gpu() and not gpu.libs_installed())
        self.gpu_progress = QProgressBar()
        self.gpu_progress.setVisible(False)
        gbox = QVBoxLayout()
        gbox.addWidget(self.gpu_status)
        gbox.addWidget(self.gpu_btn)
        gbox.addWidget(self.gpu_progress)
        form.addRow("NVIDIA GPU", gbox)

        # Style notes
        self.notes = QPlainTextEdit(cfg["channel"].get("style_notes", ""))
        self.notes.setPlaceholderText("e.g. Keep the jokes and deaths. Cut inventory sorting. "
                                      "I call my viewers 'the crew'.")
        self.notes.setFixedHeight(80)
        form.addRow("Your editing style", self.notes)

        # Characters for thumbnails
        self.chars = QLineEdit(cfg["thumbnails"].get("characters_dir", ""))
        self.chars.setPlaceholderText("Folder with transparent PNG poses of your skin")
        cbrowse = QPushButton("Browse…")
        cbrowse.clicked.connect(lambda: self._pick_dir(self.chars, "Folder with your character poses"))
        crow = QHBoxLayout()
        crow.addWidget(self.chars, 1)
        crow.addWidget(cbrowse)
        cbox = QVBoxLayout()
        cbox.addLayout(crow)
        cbox.addWidget(label("Tip: Brazey's Emote Generator (in Brazey's pack, Pose Tool folder) makes 70 poses "
                             "from your skin. Name your favourites by mood, e.g. shocked.png, celebrating.png.",
                             "hint"))
        form.addRow("Your character", cbox)

        # Projects folder
        self.projects = QLineEdit(cfg["app"]["projects_dir"])
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.pick_projects)
        prj = QHBoxLayout()
        prj.addWidget(self.projects, 1)
        prj.addWidget(browse)
        form.addRow("Save videos in", prj)

        root.addLayout(form)
        self.log = label("", "hint")
        root.addWidget(self.log)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.bridge.log.connect(self.log.setText)
        self.bridge.progress.connect(lambda f: self.gpu_progress.setValue(int(f * 100)))

    # -- helpers -----------------------------------------------------------------------
    def _index_summary(self, cfg) -> str:
        idx = AssetIndex.load(Path(cfg["assets"]["index_path"]))
        if not idx:
            return "Not indexed yet."
        s = idx.summary()
        return (f"Indexed {len(idx.assets)} files: {s.get('sfx', 0)} sounds, {s.get('overlay', 0)} overlays, "
                f"{s.get('transition', 0)} transitions, {s.get('image', 0)} images.")

    def pack_list(self) -> list[str]:
        return [self.packs.item(i).text() for i in range(self.packs.count())]

    def add_pack(self):
        d = QFileDialog.getExistingDirectory(self, "Choose an editing pack folder")
        if d and d not in self.pack_list():
            self.packs.addItem(str(Path(d)))

    def _pick_dir(self, field: QLineEdit, title: str):
        d = QFileDialog.getExistingDirectory(self, title, field.text())
        if d:
            field.setText(str(Path(d)))

    def pick_projects(self):
        d = QFileDialog.getExistingDirectory(self, "Where should videos be saved?", self.projects.text())
        if d:
            self.projects.setText(str(Path(d)))

    def test_key(self):
        self.key_status.setText("Checking…")
        QApplication.processEvents()
        ok, msg = llm.check_key(self.key.text())
        self.key_status.setText(("✔ " if ok else "✖ ") + msg)

    def index_packs(self):
        packs = self.pack_list()
        if not packs:
            QMessageBox.information(self, APP_NAME, "Add at least one editing pack folder first.")
            return
        self.index_btn.setEnabled(False)
        self.pack_status.setText("Indexing… (a few seconds to a minute)")
        index_path = Path(config.load()["assets"]["index_path"])

        def work():
            idx = AssetIndex.build([Path(p) for p in packs], log=self.bridge.log.emit)
            idx.save(index_path)
            return idx

        def done(_):
            self.index_btn.setEnabled(True)
            self.pack_status.setText(self._index_summary(config.load()))
            self.log.setText("")

        self._connect_once(done, lambda e: (self.index_btn.setEnabled(True), self.pack_status.setText(f"Failed: {e}")))
        run_in_thread(self.bridge, work)

    def install_gpu(self):
        self.gpu_btn.setEnabled(False)
        self.gpu_progress.setVisible(True)

        def done(_):
            self.gpu_status.setText(gpu.status())
            self.gpu_btn.setVisible(False)
            self.gpu_progress.setVisible(False)

        def failed(e):
            self.gpu_btn.setEnabled(True)
            self.gpu_status.setText(f"Download failed: {e}")

        self._connect_once(done, failed)
        run_in_thread(self.bridge, lambda: gpu.install(self.bridge.progress.emit, self.bridge.log.emit,
                                                         self.stop.is_set), self.stop)

    def _connect_once(self, on_done, on_fail):
        for sig in (self.bridge.done, self.bridge.failed):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self.bridge.done.connect(on_done)
        self.bridge.failed.connect(on_fail)

    def collect(self) -> dict:
        s = dict(self.settings)
        s.setdefault("llm", {})["api_key"] = self.key.text().strip()
        s.setdefault("assets", {})["packs"] = self.pack_list()
        w = s.setdefault("whisper", {})
        w["mic_track"] = self.mic.currentData()
        w["model"] = self.model.currentData()
        s.setdefault("channel", {})["style_notes"] = self.notes.toPlainText().strip()
        s.setdefault("app", {})["projects_dir"] = self.projects.text().strip()
        s.setdefault("thumbnails", {})["characters_dir"] = self.chars.text().strip()
        return s

    def save(self):
        new = self.collect()
        old_packs = config.load()["assets"]["packs"]
        config.save_settings(new)
        index_missing = not Path(config.load()["assets"]["index_path"]).expanduser().exists()
        if new["assets"]["packs"] and (new["assets"]["packs"] != old_packs or index_missing):
            if QMessageBox.question(self, APP_NAME, "Index the editing packs now? It takes under a minute.") \
                    == QMessageBox.StandardButton.Yes:
                self.index_packs()
                return  # stay open so they can watch it finish; Save again closes
        self.accept()

    def closeEvent(self, event):
        self.stop.set()
        super().closeEvent(event)


# --------------------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(980, 760)
        self.bridge = Bridge()
        self.stop = threading.Event()
        self.result: dict | None = None
        self.out_dir: Path | None = None

        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)
        self.pages.addWidget(self._build_setup_page())
        self.pages.addWidget(self._build_work_page())
        self.pages.addWidget(self._build_done_page())

        b = self.bridge
        b.step.connect(self.on_step)
        b.progress.connect(lambda f: self.bar.setValue(int(f * 1000)))
        b.log.connect(self.on_log)
        b.done.connect(self.on_done)
        b.failed.connect(self.on_failed)
        b.cancelled.connect(self.on_cancelled)
        self.refresh_ready()

    # -- page 1: set up the video ------------------------------------------------------
    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 22, 28, 22)
        v.setSpacing(10)

        top = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label(APP_NAME, "title", wrap=False))
        titles.addWidget(label("Drop in your recordings. Get back an edited DaVinci Resolve timeline.", "subtitle"))
        top.addLayout(titles, 1)
        open_btn = QPushButton("Open a previous video…")
        open_btn.clicked.connect(self.open_previous)
        settings_btn = QPushButton("Settings")
        settings_btn.clicked.connect(self.open_settings)
        top.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(settings_btn, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(top)

        self.setup_warning = label("", "hint")
        self.setup_warning.setStyleSheet("color:#e0b050;")
        v.addWidget(self.setup_warning)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Hardcore Survival Ep 12")
        self.topic = QLineEdit()
        self.topic.setPlaceholderText("e.g. Finishing the castle walls, then diamond hunting in the deep dark")
        grid.addWidget(label("Video name", "section", wrap=False), 0, 0)
        grid.addWidget(self.name, 0, 1)
        grid.addWidget(label("What happens", "section", wrap=False), 1, 0)
        grid.addWidget(self.topic, 1, 1)
        self.series = QComboBox()
        self.series.setEditable(True)
        self.series.lineEdit().setPlaceholderText("e.g. Hardcore Survival (keeps thumbnails consistent)")
        self.series.addItems(appcore.known_series(config.load()))
        self.series.setCurrentText("")
        self.episode = QLineEdit()
        self.episode.setPlaceholderText("Ep #")
        self.episode.setMaximumWidth(90)
        logo_btn = QPushButton("Series logo…")
        logo_btn.setToolTip("A transparent PNG of the series logo. It goes on every thumbnail in this series.")
        logo_btn.clicked.connect(self.pick_series_logo)
        srow = QHBoxLayout()
        srow.addWidget(self.series, 1)
        srow.addWidget(logo_btn)
        srow.addWidget(label("Episode", "section", wrap=False))
        srow.addWidget(self.episode)
        grid.addWidget(label("Series", "section", wrap=False), 2, 0)
        grid.addLayout(srow, 2, 1)
        v.addLayout(grid)

        vid_filter = "Videos (*.mkv *.mp4 *.mov *.flv *.webm *.m4v *.avi)"
        self.recs = DropList(appcore.RECORDING_EXTS,
                             "Drag your OBS recordings here\n(one or several sessions, in the order you recorded them)",
                             sort_on_add=True)
        self.broll = DropList(appcore.BROLL_EXTS,
                              "Optional: drag Flashback renders, timelapses or screenshots here",
                              sort_on_add=False)
        lists = QHBoxLayout()
        lists.setSpacing(16)
        lists.addWidget(drop_section("Recordings", "Drag to reorder. Delete key removes.", self.recs,
                                     vid_filter, self), 3)
        lists.addWidget(drop_section("B-roll", "Name files after what they show, e.g. "
                                     "castle-walls-timelapse.mp4", self.broll,
                                     vid_filter[:-1] + " *.png *.jpg)", self), 2)
        self.shots = DropList(appcore.SCREENSHOT_EXTS,
                              "Drag your in-game screenshot for the thumbnail here (up to 3)",
                              sort_on_add=False)
        lists.addWidget(drop_section("Thumbnail", "F2 screenshots from Minecraft work great.", self.shots,
                                     "Images (*.png *.jpg *.jpeg *.webp *.bmp)", self), 2)
        v.addLayout(lists, 1)

        opts = QHBoxLayout()
        self.use_ai = QCheckBox("AI edit (cuts, B-roll, sounds, overlays, titles)")
        self.use_ai.setChecked(True)
        self.cold_open = QCheckBox("Start with a teaser")
        self.cold_open.setToolTip("Cold open: a few seconds of the best moment before the intro, to hook viewers.")
        self.cold_open.setChecked(bool(config.load()["edit"].get("cold_open", True)))
        self.fresh = QCheckBox("Start over (redo transcription and AI edit)")
        self.fresh.setToolTip("Normally re-running keeps the transcript and the AI's decisions so it's fast.")
        self.vision = QCheckBox("Let the AI look at the footage")
        self.vision.setToolTip("Sends small frames from quiet stretches so the AI can keep wordless action "
                               "and cut AFK time. Adds a little to the API cost.")
        self.vision.setChecked(bool(config.load()["vision"].get("enabled", False)))
        opts.addWidget(self.use_ai)
        opts.addSpacing(20)
        opts.addWidget(self.vision)
        opts.addSpacing(20)
        opts.addWidget(self.cold_open)
        opts.addSpacing(20)
        opts.addWidget(self.fresh)
        opts.addStretch(1)
        self.go = QPushButton("Edit my video")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self.start)
        opts.addWidget(self.go)
        v.addLayout(opts)

        for w in (self.name.textChanged, self.recs.changed, self.use_ai.toggled):
            w.connect(lambda *a: self.refresh_ready())
        self.use_ai.toggled.connect(self.vision.setEnabled)
        return page

    # -- page 2: working ---------------------------------------------------------------
    def _build_work_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 22, 28, 22)
        v.setSpacing(12)
        self.work_title = label("Editing…", "title")
        v.addWidget(self.work_title)
        v.addWidget(label("You can leave this running. Long sessions take a while to transcribe the "
                          "first time; re-runs are fast.", "subtitle"))
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        self.step_labels = []
        for name in pipeline.Reporter.STEPS:
            lbl = label(f"○  {name}", "big", wrap=False)
            self.step_labels.append(lbl)
            cv.addWidget(lbl)
        v.addWidget(card)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        v.addWidget(self.bar)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setFont(QFont("Consolas", 9))
        v.addWidget(self.logbox, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.clicked.connect(self.cancel)
        row.addWidget(self.cancel_btn)
        v.addLayout(row)
        return page

    # -- page 3: done ------------------------------------------------------------------
    def _build_done_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 22, 28, 22)
        v.setSpacing(14)
        v.addWidget(label("Your edit is ready ✔", "title"))
        self.summary = label("", "big")
        v.addWidget(self.summary)

        card = QFrame()
        card.setObjectName("card")
        g = QGridLayout(card)
        g.setVerticalSpacing(10)
        buttons = [
            ("Show timeline file", "The .fcpxml to import into Resolve", self.show_timeline),
            ("How to open in Resolve", "Four steps", self.resolve_help),
            ("Review what was cut", "Every cut, with the reason", lambda: self.open_out("review.md")),
            ("Open upload kit", "Titles, description, chapters, tags", lambda: self.open_out("upload_kit.md")),
            ("Copy best title", "", lambda: self.copy_kit("title")),
            ("Copy description + chapters", "", lambda: self.copy_kit("description")),
            ("Open video folder", "Everything for this video", lambda: open_path(self.out_dir)),
        ]
        for i, (text, hint, fn) in enumerate(buttons):
            b = QPushButton(text)
            b.clicked.connect(fn)
            g.addWidget(b, i, 0)
            g.addWidget(label(hint, "hint"), i, 1)
        v.addWidget(card)

        trow = QHBoxLayout()
        trow.addWidget(label("Thumbnails", "section", wrap=False))
        trow.addStretch(1)
        self.more_thumbs = QPushButton("New thumbnail ideas")
        self.more_thumbs.clicked.connect(self.remake_thumbnails)
        open_thumbs = QPushButton("Open thumbnails folder")
        open_thumbs.clicked.connect(lambda: open_path(self.out_dir / "thumbnails") if self.out_dir else None)
        trow.addWidget(self.more_thumbs)
        trow.addWidget(open_thumbs)
        v.addLayout(trow)
        self.thumb_row = QHBoxLayout()
        self.thumb_row.setSpacing(12)
        v.addLayout(self.thumb_row)
        self.notes_box = label("", "hint")
        v.addWidget(self.notes_box)
        v.addStretch(1)
        row = QHBoxLayout()
        back = QPushButton("← Change something and re-run")
        back.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        new = QPushButton("Edit another video")
        new.clicked.connect(self.new_video)
        row.addWidget(back)
        row.addStretch(1)
        row.addWidget(new)
        v.addLayout(row)
        return page

    # -- state -------------------------------------------------------------------------
    def cfg(self) -> dict:
        return config.load()

    def project(self) -> appcore.Project:
        return appcore.Project(name=self.name.text().strip(), topic=self.topic.text().strip(),
                               recordings=[str(p) for p in self.recs.paths()],
                               broll=[str(p) for p in self.broll.paths()],
                               series=self.series.currentText().strip(), episode=self.episode.text().strip(),
                               screenshots=[str(p) for p in self.shots.paths()[:3]])

    def video_info(self, proj: appcore.Project) -> dict:
        return {"title": proj.name, "topic": proj.topic, "series": proj.series, "episode": proj.episode,
                "screenshots": proj.screenshots, "cold_open": self.cold_open.isChecked(),
                "vision": self.vision.isChecked()}

    def refresh_ready(self):
        cfg = self.cfg()
        problems = appcore.setup_problems(cfg)
        if not self.use_ai.isChecked():
            problems = [p for p in problems if "API key" not in p]
        self.setup_warning.setText(("⚠  Setup: " + " ".join(problems) + "  (Settings)") if problems else "")
        p = self.project()
        self.go.setEnabled(bool(p.name and p.recordings and ("API key" not in " ".join(problems))))

    def open_settings(self, welcome: bool = False):
        SettingsDialog(self, welcome=welcome).exec()
        self.refresh_ready()

    def open_previous(self):
        start = self.cfg()["app"]["projects_dir"]
        d = QFileDialog.getExistingDirectory(self, "Open a video folder", start)
        if not d:
            return
        p = appcore.Project.load(Path(d))
        if not p:
            QMessageBox.information(self, APP_NAME, "That folder isn't a KDcut video folder.")
            return
        self.name.setText(p.name)
        self.topic.setText(p.topic)
        self.recs.set_paths([Path(x) for x in p.recordings])
        self.broll.set_paths([Path(x) for x in p.broll])
        self.series.setCurrentText(p.series)
        self.episode.setText(p.episode)
        self.shots.set_paths([Path(x) for x in p.screenshots])

    def new_video(self):
        self.name.clear()
        self.topic.clear()
        self.recs.set_paths([])
        self.broll.set_paths([])
        self.shots.set_paths([])
        try:  # next episode of the same series is the common case
            self.episode.setText(str(int(self.episode.text()) + 1))
        except ValueError:
            self.episode.clear()
        self.fresh.setChecked(False)
        self.pages.setCurrentIndex(0)

    # -- running -----------------------------------------------------------------------
    def start(self):
        proj = self.project()
        problems = appcore.validate(proj)
        if problems:
            QMessageBox.warning(self, APP_NAME, "\n\n".join(problems))
            return
        cfg = self.cfg()
        out = proj.out_dir(Path(cfg["app"]["projects_dir"]).expanduser())
        prev = appcore.Project.load(out)
        if prev and set(prev.recordings) != set(proj.recordings) and not self.fresh.isChecked():
            # Different footage under the same name: the saved AI plan no longer applies.
            self.fresh.setChecked(True)
        proj.save(out)
        self.out_dir = out

        self.stop.clear()
        self.logbox.clear()
        self.bar.setValue(0)
        for lbl, name in zip(self.step_labels, pipeline.Reporter.STEPS):
            lbl.setText(f"○  {name}")
        self.work_title.setText(f"Editing “{proj.name}”…")
        self.cancel_btn.setEnabled(True)
        self.pages.setCurrentIndex(1)

        fresh, use_ai = self.fresh.isChecked(), self.use_ai.isChecked()
        reporter = QtReporter(self.bridge, self.stop)
        video = self.video_info(proj)
        settings = config.load_settings()   # remember the per-video switches for next time
        settings.setdefault("vision", {})["enabled"] = self.vision.isChecked()
        settings.setdefault("edit", {})["cold_open"] = self.cold_open.isChecked()
        config.save_settings(settings)

        def work():
            return pipeline.run(out, cfg, use_ai=use_ai, retranscribe=fresh, replan=fresh,
                                recordings=[Path(r) for r in proj.recordings],
                                broll_files=[Path(b) for b in proj.broll], video=video,
                                out_dir=out, reporter=reporter)

        run_in_thread(self.bridge, work, self.stop)

    def on_step(self, index: int, text: str):
        for i, lbl in enumerate(self.step_labels):
            base = pipeline.Reporter.STEPS[i]
            if i < index:
                lbl.setText(f"✔  {base}")
            elif i == index:
                lbl.setText(f"●  {text}")
        if index == 1:
            self.bar.setRange(0, 1000)
            self.bar.setValue(0)
        else:
            self.bar.setRange(0, 0)  # busy animation for steps without a percentage

    def on_log(self, text: str):
        self.logbox.appendPlainText(text)

    def on_done(self, result: dict):
        self.result = result
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000)
        for lbl, name in zip(self.step_labels, pipeline.Reporter.STEPS):
            lbl.setText(f"✔  {name}")
        self.summary.setText(appcore.summary_text(result))
        notes = result["plan"].notes
        self.notes_box.setText(("The AI asks you to check:\n• " + "\n• ".join(notes)) if notes else "")
        self.show_thumbnails(result.get("thumbnails") or [])
        self.fresh.setChecked(False)
        self.pages.setCurrentIndex(2)
        QApplication.alert(self)

    def show_thumbnails(self, paths: list[Path]):
        while self.thumb_row.count():
            w = self.thumb_row.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not paths:
            self.thumb_row.addWidget(label("No thumbnails yet. Drag a screenshot into the Thumbnail box "
                                           "and re-run, or click New thumbnail ideas.", "hint"))
            return
        for p in paths[-3:]:
            self.thumb_row.addWidget(ThumbLabel(Path(p)))
        self.thumb_row.addStretch(1)

    def pick_series_logo(self):
        from kdcut import thumbnails
        series = self.series.currentText().strip()
        if not series:
            QMessageBox.information(self, APP_NAME, "Type the series name first.")
            return
        f, _ = QFileDialog.getOpenFileName(self, f"Logo for {series}", "", "Images (*.png *.webp)")
        if not f:
            return
        cfg = self.cfg()
        path = thumbnails.theme_path(cfg, series)
        theme = thumbnails.Theme.load(path) or thumbnails.Theme(name=series)
        theme.logo = f
        theme.save(path)
        self.statusBar().showMessage(f"Logo saved for {series}", 4000)

    def remake_thumbnails(self):
        if not self.out_dir:
            return
        proj = self.project()
        shots = [Path(s) for s in proj.screenshots]
        if not shots:
            shots = sorted((self.out_dir / "thumbnail_frames").glob("frame_*.jpg"))[:3]
        if not shots:
            QMessageBox.information(self, APP_NAME, "Drag a screenshot into the Thumbnail box first "
                                                    "(← Change something and re-run).")
            return
        self.more_thumbs.setEnabled(False)
        self.more_thumbs.setText("Designing…")
        cfg, video, use_ai = self.cfg(), self.video_info(proj), self.use_ai.isChecked()
        bridge = self.thumb_bridge = Bridge()
        bridge.done.connect(lambda paths: (self.show_thumbnails(paths), self._thumbs_idle()))
        bridge.failed.connect(lambda e: (QMessageBox.warning(self, APP_NAME, f"Thumbnails failed: {e}"),
                                         self._thumbs_idle()))
        run_in_thread(bridge, lambda: pipeline.make_thumbnails(self.out_dir, cfg, video, shots, use_ai,
                                                               log=lambda m: None))

    def _thumbs_idle(self):
        self.more_thumbs.setEnabled(True)
        self.more_thumbs.setText("New thumbnail ideas")

    def on_failed(self, message: str):
        self.bar.setRange(0, 1000)
        self.cancel_btn.setEnabled(False)
        QMessageBox.critical(self, APP_NAME, f"Something went wrong:\n\n{message}\n\n"
                                             "The details are in the log on this screen.")
        self.pages.setCurrentIndex(0)

    def on_cancelled(self):
        self.bar.setRange(0, 1000)
        self.pages.setCurrentIndex(0)

    def cancel(self):
        self.stop.set()
        self.cancel_btn.setEnabled(False)
        self.on_log("Cancelling after the current step…")

    # -- results -----------------------------------------------------------------------
    def open_out(self, name: str):
        if self.out_dir:
            open_path(self.out_dir / name)

    def show_timeline(self):
        if self.result:
            reveal(Path(self.result["timeline"]))

    def resolve_help(self):
        QMessageBox.information(self, "Open in DaVinci Resolve", appcore.RESOLVE_STEPS)

    def copy_kit(self, what: str):
        import json
        try:
            kit = json.loads((self.out_dir / "upload_kit.json").read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            QMessageBox.information(self, APP_NAME, "No upload kit yet (it needs the AI edit turned on).")
            return
        if what == "title":
            text = (kit.get("titles") or [""])[0]
        else:
            text = kit.get("description", "")
            if kit.get("chapters"):
                text += "\n\nChapters\n" + kit["chapters"]
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage("Copied to clipboard", 3000)

    def closeEvent(self, event):
        if self.pages.currentIndex() == 1:
            if QMessageBox.question(self, APP_NAME, "An edit is running. Stop it and quit?") \
                    != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.stop.set()
        super().closeEvent(event)


# --------------------------------------------------------------------------------------

def _icon() -> QIcon:
    for base in filter(None, [appcore.bundle_dir(), Path(__file__).parent]):
        for p in (base / "kdcut.ico", base / "assets_app" / "kdcut.ico"):
            if p.exists():
                return QIcon(str(p))
    return QIcon()


def make_app(argv) -> QApplication:
    if sys.platform == "win32":
        try:  # own taskbar icon instead of python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KDPlays.KDcut")
        except Exception:
            pass
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLE)
    app.setWindowIcon(_icon())
    return app


def _redirect_output_if_windowed() -> None:
    """A windowed .exe has no console; libraries that print progress would crash."""
    if sys.stdout is None or sys.stderr is None:
        config.HOME_DIR.mkdir(parents=True, exist_ok=True)
        log = open(config.HOME_DIR / "kdcut.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _redirect_output_if_windowed()
    appcore.use_bundled_ffmpeg()
    if "--selftest" in argv:
        from kdcut import selftest
        out = argv[argv.index("--selftest") + 1] if len(argv) > argv.index("--selftest") + 1 else "selftest.json"
        return selftest.run(Path(out))
    app = make_app(argv)
    win = MainWindow()
    win.show()
    if not appcore.ffmpeg_ok():
        QMessageBox.critical(win, APP_NAME, "ffmpeg is missing, so videos can't be read. "
                                            "Please reinstall KDcut.")
    elif not config.SETTINGS_FILE.exists():
        win.open_settings(welcome=True)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
