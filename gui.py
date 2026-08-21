#!/usr/bin/env python3
"""aniani: merged search/watch/download GUI.

- Two sources: anidb.app (default, always reachable so far) and the
  vendored YumaAPI/aniwatchtv.to (blocked by some ISPs -- built anyway
  since it works fine off that network). A third, nyaa.si, is torrent-
  based and follows a different flow (search -> release -> stream/
  download via qbittorrent-nox, no separate episode list).
- Two players: VLC (default -- confirmed its HTTP interface gives
  everything needed for RPC/controls) or mpv, both driven the same way
  mpv alone was before: a real separate window, controlled over
  IPC/HTTP, not embedded (embedding was tried and abandoned -- see
  ani-cli-discord-rpc/ani_gui.py's history for why).
- Download episodes (single/range/whole series) via ffmpeg for offline
  viewing, browsable without any source/network at all.
- Discord Rich Presence always on (browsing while searching, watching
  while playing), handed off to a standalone daemon on close so it
  keeps updating while the player runs standalone, and reattaches
  instead of restarting when the panel reopens.
"""
import os
import signal
import subprocess
import sys

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize
from PyQt6.QtGui import QGuiApplication, QPixmap, QIcon, QColor, QPainter
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QStackedWidget, QCheckBox,
    QSlider, QComboBox, QProgressBar, QFrame, QScrollArea,
    QMessageBox,
)
import qtawesome as qta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "player"))

import anidb_source
import yuma_source
import nyaa_source
import anilist_source
import vlc_backend
import mpv_backend
import torrent_backend
import discord_presence
import state
import download_manager
import platform_utils
import tracker
import image_cache
import webbrowser

DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON_PID_PATH = platform_utils.temp_path("aniani-rpc-daemon.pid")
DAEMON_LOG_PATH = platform_utils.temp_path("aniani-rpc-daemon.log")

SOURCES = {"anidb": anidb_source, "yuma": yuma_source}

# Catppuccin Mocha (https://catppuccin.com/palette -- Mocha flavor), with
# pink/mauve as the kawaii accent pair instead of Mocha's usual blue/lavender.
MOCHA = {
    "base": "#1e1e2e", "mantle": "#181825", "crust": "#11111b",
    "surface0": "#313244", "surface1": "#45475a", "surface2": "#585b70",
    "overlay0": "#6c7086", "overlay1": "#7f849c", "overlay2": "#9399b2",
    "text": "#cdd6f4", "subtext1": "#bac2de", "subtext0": "#a6adc8",
    "pink": "#f5c2e7", "mauve": "#cba6f7", "rosewater": "#f5e0dc",
    "red": "#f38ba8", "peach": "#fab387", "green": "#a6e3a1",
}

DARK_QSS = f"""
QWidget {{ background: {MOCHA["base"]}; color: {MOCHA["text"]}; font-size: 13px; }}

QLabel#brand {{ font-size: 20px; font-weight: 700; color: {MOCHA["rosewater"]}; padding: 2px 0 4px 0; }}
QLabel#heading {{ color: {MOCHA["overlay2"]}; font-size: 10.5px; font-weight: 700; letter-spacing: 1px; }}
QLabel#title {{ font-size: 19px; font-weight: 800; color: {MOCHA["pink"]}; padding: 2px 0 10px 0; }}
QLabel#status {{ color: {MOCHA["subtext0"]}; font-size: 12px; }}
QLabel#cardTitle {{ font-size: 12px; font-weight: 600; }}
/* deliberately NOT the same small-caps/letter-spaced style as #heading
   (used for form labels like SOURCE/PLAYER) -- reusing one label style
   for both form fields and section titles is what read as generic/
   templated. A real heading, not a form caption pretending to be one. */
QLabel#sectionHeading {{ color: {MOCHA["text"]}; font-size: 14px; font-weight: 700; padding: 4px 0 2px 0; }}

QWidget#sidebar {{ background: {MOCHA["mantle"]}; border-right: 1px solid {MOCHA["surface0"]}; }}
QLabel#logoMark {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {MOCHA["pink"]}, stop:1 {MOCHA["mauve"]});
    color: {MOCHA["crust"]}; font-size: 17px; font-weight: 800;
    border-radius: 12px; min-width: 40px; max-width: 40px; min-height: 40px; max-height: 40px;
}}
QPushButton#navBtn {{
    background: transparent; border: none; border-radius: 12px; padding: 0;
}}
QPushButton#navBtn:hover {{ background: {MOCHA["surface0"]}; }}
QPushButton#navBtn:checked {{ background: {MOCHA["surface0"]}; border: 1px solid {MOCHA["mauve"]}; }}

QWidget#coverThumb {{ background: {MOCHA["surface0"]}; border-radius: 8px; }}
/* no default border -- background contrast against the page alone is
   enough to read as a card; a border on every single one (there are
   dozens on screen at once in the dashboard rows) added visual noise
   without adding information. The border only earns its keep as a
   hover accent, same as a real lift/elevation would. */
QWidget#coverCard {{
    background: {MOCHA["mantle"]}; border: 1px solid transparent; border-radius: 12px;
}}
QWidget#coverCard:hover {{
    background: {MOCHA["surface0"]}; border-color: {MOCHA["pink"]};
}}
QLabel#cardTitle {{ color: {MOCHA["subtext1"]}; }}
QWidget#coverCard:hover QLabel#cardTitle {{ color: {MOCHA["rosewater"]}; }}
QFrame#divider {{ background: {MOCHA["surface0"]}; border: none; margin: 4px 0 8px 0; }}

QWidget#heroCard {{ background: transparent; }}
QLabel#heroBanner {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {MOCHA["surface0"]}, stop:1 {MOCHA["mantle"]});
    border-top-left-radius: 18px; border-top-right-radius: 18px;
}}
QWidget#heroPanel {{
    background: {MOCHA["mantle"]}; border: 1px solid {MOCHA["surface0"]};
    border-bottom-left-radius: 18px; border-bottom-right-radius: 18px;
}}
QLabel#heroBadge {{
    background: {MOCHA["surface0"]}; color: {MOCHA["pink"]}; font-size: 10px; font-weight: 800;
    letter-spacing: 0.5px; border-radius: 9px; padding: 3px 9px; margin-bottom: 2px;
}}
QLabel#heroTitle {{ font-size: 21px; font-weight: 800; color: {MOCHA["rosewater"]}; }}
QLabel#heroDesc {{ color: {MOCHA["subtext0"]}; font-size: 12px; }}
QPushButton#linkButton {{
    background: transparent; border: none; padding: 0; margin: -4px 0 4px 0;
    color: {MOCHA["mauve"]}; font-size: 11.5px; font-weight: 700; text-align: left;
}}
QPushButton#linkButton:hover {{ color: {MOCHA["pink"]}; }}

QLineEdit {{
    background: {MOCHA["mantle"]}; border: 1.5px solid {MOCHA["surface0"]}; border-radius: 12px;
    padding: 9px 10px; selection-background-color: {MOCHA["pink"]}; selection-color: {MOCHA["crust"]};
}}
QLineEdit:focus {{ border-color: {MOCHA["pink"]}; }}

QComboBox {{
    background: {MOCHA["mantle"]}; border: 1.5px solid {MOCHA["surface0"]}; border-radius: 12px; padding: 7px 10px;
}}
QComboBox:hover {{ border-color: {MOCHA["mauve"]}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {MOCHA["mantle"]}; border: 1px solid {MOCHA["surface0"]}; border-radius: 10px;
    selection-background-color: {MOCHA["surface0"]}; selection-color: {MOCHA["pink"]}; outline: none; padding: 4px;
}}

QListWidget {{
    background: {MOCHA["mantle"]}; border: 1px solid {MOCHA["surface0"]}; border-radius: 14px; padding: 4px;
    outline: none;
}}
QListWidget::item {{ padding: 9px 8px; border-radius: 10px; margin: 1px 0; }}
QListWidget::item:hover {{ background: {MOCHA["surface0"]}; }}
QListWidget::item:selected {{ background: {MOCHA["surface0"]}; color: {MOCHA["pink"]}; }}
/* custom row widgets set via QListWidget.setItemWidget() paint on top
   of the item's own hover/selected background with no background of
   their own -- wherever the row widget's edges don't line up exactly
   with the item's rect, the item's rounded highlight shows through,
   looking like a mismatched "ghost" card sitting behind the real one
   (confirmed directly via a screenshot). Qt's QSS :hover/:selected
   state doesn't cascade from ::item down into a child widget set this
   way, so this can only match the list's resting background -- that's
   the state that actually needs it, since a plain white/blue OS
   selection-highlight peeking out is the jarring case, not a
   same-toned hover blip. */
QWidget#listRow {{ background: {MOCHA["mantle"]}; border-radius: 10px; }}

/* NOT "transparent" -- a literal transparent QSS background disables
   Qt's automatic erase-before-repaint for the widget, which meant these
   never cleared their old painted pixels before drawing new ones. As
   cards loaded asynchronously and the layout kept reflowing, stale text
   from a widget's earlier position stayed ghosted on screen permanently
   instead of being cleared -- confirmed directly via a screenshot
   showing "TRENDING NOW"/"RECOMMENDED" text stuck inside a Continue
   Watching card. Using the real (opaque) page background color instead
   looks identical -- seamless against the page -- while keeping erase
   behavior intact. */
QScrollArea {{ background: {MOCHA["base"]}; border: none; }}
QScrollArea > QWidget > QWidget {{ background: {MOCHA["base"]}; }}

QPushButton {{
    background: {MOCHA["surface0"]}; border: 1.5px solid {MOCHA["surface1"]}; border-radius: 12px;
    padding: 8px 14px; font-weight: 600; color: {MOCHA["text"]};
}}
QPushButton:hover {{ border-color: {MOCHA["pink"]}; color: {MOCHA["rosewater"]}; }}
QPushButton:pressed {{ background: {MOCHA["mantle"]}; }}
QPushButton:disabled {{ color: {MOCHA["overlay0"]}; border-color: {MOCHA["surface0"]}; }}
QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {MOCHA["pink"]}, stop:1 {MOCHA["mauve"]});
    color: {MOCHA["crust"]}; border: none; font-weight: 700;
}}
QPushButton#primary:hover {{ background: {MOCHA["rosewater"]}; color: {MOCHA["crust"]}; }}
QPushButton#primary:pressed {{ background: {MOCHA["mauve"]}; }}

QCheckBox {{ spacing: 8px; padding: 2px 0; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px; border: 1.5px solid {MOCHA["surface2"]}; background: {MOCHA["mantle"]};
}}
QCheckBox::indicator:checked {{ background: {MOCHA["pink"]}; border-color: {MOCHA["pink"]}; }}

/* gradient reserved for the one primary CTA button -- everything else
   (slider fill, progress fill, this included) goes flat. A gradient on
   every accent element in sight is its own "generated dashboard" tell. */
QSlider::groove:horizontal {{ background: {MOCHA["surface0"]}; height: 5px; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {MOCHA["pink"]}; height: 5px; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {MOCHA["rosewater"]}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider::handle:horizontal:hover {{ background: {MOCHA["pink"]}; }}

QProgressBar {{
    background: {MOCHA["mantle"]}; border: 1px solid {MOCHA["surface0"]}; border-radius: 6px;
    text-align: center; height: 16px;
}}
QProgressBar::chunk {{ background: {MOCHA["pink"]}; border-radius: 5px; }}

QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {MOCHA["surface1"]}; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {MOCHA["mauve"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {MOCHA["surface1"]}; border-radius: 4px; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: {MOCHA["mauve"]}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
"""


def fmt_time(seconds):
    s = max(0, int(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def _truncate(text, limit):
    return text if len(text) <= limit else text[:limit - 2] + "…"


def _cover_bytes_for_title(title):
    """Continue-watching entries only have a title (from anidb.app/
    YumaAPI, neither of which hands back cover art) -- best-effort
    cross-reference against AniList by title search to get one anyway,
    same fuzzy-match limitation as anilist_source.cover_for_title()."""
    url = anilist_source.cover_for_title(title)
    if not url:
        return None
    return image_cache.fetch(url)


def kill_lingering_rpc_daemon():
    if not os.path.exists(DAEMON_PID_PATH):
        return
    try:
        with open(DAEMON_PID_PATH) as f:
            pid = int(f.read().strip())
        platform_utils.kill_pid(pid)
    except (OSError, ValueError):
        pass
    try:
        os.remove(DAEMON_PID_PATH)
    except OSError:
        pass


def spawn_rpc_daemon(backend_name):
    try:
        if getattr(sys, "frozen", False):
            # frozen exe: sys.executable is aniani.exe itself
            args = [sys.executable, "--rpc-daemon", "--backend", backend_name]
        else:
            args = [sys.executable, os.path.join(DIR, "gui.py"), "--rpc-daemon", "--backend", backend_name]
        # stdout/stderr used to go straight to DEVNULL -- if the daemon
        # crashed, or Discord's RPC connection silently failed (its own
        # exception handling swallows everything), there was genuinely
        # no way to find out why short of guessing. A real log file, not
        # a black hole, so "the RPC just doesn't show up" is diagnosable
        # instead of unreproducible.
        with open(DAEMON_LOG_PATH, "w") as log:
            proc = platform_utils.popen_detached(args, stdout=log, stderr=subprocess.STDOUT)
        with open(DAEMON_PID_PATH, "w") as f:
            f.write(str(proc.pid))
    except OSError:
        pass


class ClickableWidget(QWidget):
    """A plain QWidget with a clicked signal -- used for dashboard cover
    cards instead of QToolButton, since QToolButton's built-in text
    label doesn't support real word-wrap: it silently elides to one
    line, which on top of this app's own manual title truncation
    produced garbled double-truncated text like "The Exile...w to
    Gam..." (confirmed directly via screenshot). A real child QLabel
    with setWordWrap(True) wraps properly across 2-3 lines instead."""
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class Worker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn = fn
        self.args = args

    def run(self):
        try:
            self.done.emit(self.fn(*self.args))
        except Exception as e:
            self.failed.emit(str(e))


class DownloadWorker(QThread):
    """run_job's on_progress fires from this thread -- can't touch Qt
    widgets directly from there (undefined/unsafe: Qt widgets only have
    thread affinity for the GUI thread). Routes progress through a
    signal instead, which Qt auto-queues across threads safely."""
    progress = pyqtSignal(object)
    finished_ok = pyqtSignal(bool)

    def __init__(self, job):
        super().__init__()
        self.job = job

    def run(self):
        ok = download_manager.run_job(self.job, on_progress=lambda j: self.progress.emit(j))
        self.finished_ok.emit(ok)


class AniAni(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("aniani")
        self.setMinimumSize(420, 480)

        self.presence = discord_presence.DiscordPresence()
        self.rpc_enabled = True
        self.skip_intro = True

        self.prefs = state.load_prefs()
        self.source_name = self.prefs["source"]
        self.player_name = self.prefs["player"]
        self._compact_mode = self.prefs.get("compact_mode", False)
        self.resize(420, 680) if self._compact_mode else self.resize(1280, 800)
        self.player = vlc_backend.VlcPlayer() if self.player_name == "vlc" else mpv_backend.MpvPlayer()

        self.anime_id = None
        self.anime_title = None
        self.mode = "sub"
        self.episode_maps = []
        self.current_ep_no = None
        self.current_ep_display = None  # real episode number for presence/display -- see _update_presence
        self.current_ep_ref = None
        self.mal_id = None
        self.current_skip = {}
        self.skipped_segments = set()
        self._seeking = False
        self._advancing = False
        self._synced_this_ep = False

        self.torrent_engine = torrent_backend.TorrentEngine()

        # qtawesome (FontAwesome) instead of QStyle's native icons -- the
        # native ones render tiny/blurry/inconsistent under the Fusion
        # style this app forces (needed for QComboBox popup theming, see
        # main()). FontAwesome glyphs are vector, crisp at any size, and
        # themeable -- colored to match the Catppuccin Mocha palette (see
        # MOCHA / DARK_QSS).
        ic = MOCHA["subtext1"]
        self._icons = {
            "back": qta.icon("fa5s.arrow-left", color=ic),
            "down": qta.icon("fa5s.download", color=ic),
            "dir": qta.icon("fa5s.folder-open", color=ic),
            "browse": qta.icon("fa5s.compass", color=ic),
            "cancel": qta.icon("fa5s.times", color=ic),
            "reload": qta.icon("fa5s.redo", color=ic),
            "close": qta.icon("fa5s.trash-alt", color=ic),
            "prev": qta.icon("fa5s.step-backward", color=ic),
            "next": qta.icon("fa5s.step-forward", color=ic),
            "play": qta.icon("fa5s.play", color=MOCHA["crust"]),
            "pause": qta.icon("fa5s.pause", color=MOCHA["crust"]),
            "stop": qta.icon("fa5s.stop", color=ic),
            "volume": qta.icon("fa5s.volume-up", color=ic),
            "user": qta.icon("fa5s.user-circle", color=ic),
            "link": qta.icon("fa5s.external-link-alt", color=ic),
            "home": qta.icon("fa5s.home", color=MOCHA["overlay2"]),
            "compact": qta.icon("fa5s.compress-arrows-alt", color=ic),
            "expand": qta.icon("fa5s.expand-arrows-alt", color=ic),
            "film": qta.icon("fa5s.film", color=MOCHA["surface2"]),
        }
        self._nav_icons = {
            "home": qta.icon("fa5s.home", color=MOCHA["overlay2"], color_active=MOCHA["pink"]),
            "browse": qta.icon("fa5s.compass", color=MOCHA["overlay2"], color_active=MOCHA["pink"]),
            "downloads": qta.icon("fa5s.download", color=MOCHA["overlay2"], color_active=MOCHA["pink"]),
            "library": qta.icon("fa5s.folder-open", color=MOCHA["overlay2"], color_active=MOCHA["pink"]),
            "account": qta.icon("fa5s.user-circle", color=MOCHA["overlay2"], color_active=MOCHA["pink"]),
        }

        self.stack = QStackedWidget()
        self.home = self._build_home()
        self.eplist = self._build_episode_list()
        self._player_return_page = self.eplist  # see _go_back_from_player
        self.player_page = self._build_player_page()
        self.downloads_page = self._build_downloads_page()
        self.library_page = self._build_library_page()
        self.browse_page = self._build_browse_page()
        self.account_page = self._build_account_page()
        for w in (
            self.home, self.eplist, self.player_page, self.downloads_page,
            self.library_page, self.browse_page, self.account_page,
        ):
            self.stack.addWidget(w)
        self.stack.currentChanged.connect(self._on_page_changed)

        self._showing_results = False
        self._apply_home_view()
        if self._compact_mode:
            self.compact_btn.setIcon(self._icons["expand"])
            self.compact_btn.setToolTip("Switch to full dashboard")

        self.sidebar = self._build_sidebar()

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.sidebar)
        content_wrap = QWidget()
        content_v = QVBoxLayout(content_wrap)
        content_v.setContentsMargins(20, 18, 20, 16)
        content_v.addWidget(self.stack)
        root.addWidget(content_wrap, 1)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_player)
        self.poll_timer.start(1000)

        self.rpc_timer = QTimer(self)
        self.rpc_timer.timeout.connect(self._update_presence)
        self.rpc_timer.start(5000)

        self.download_jobs = []
        self._pending_downloads = []  # [(job, ep), ...] awaiting their turn
        self._active_download_job = None  # only one ffmpeg download runs at a time
        self._restore_download_queue()

        kill_lingering_rpc_daemon()
        self._refresh_history()
        self._update_presence()
        self._restore_last_session()
        # _on_page_changed (which normally triggers this) is wired to
        # QStackedWidget.currentChanged -- a signal that only fires on an
        # actual *change*, so it never fires for Home since Home is
        # already the active page from the moment the stack is built.
        # Without this, Trending/Recommended silently never loaded on a
        # fresh launch (confirmed directly: both rows stayed empty
        # indefinitely, not just slow).
        if not self._compact_mode:
            self._load_dashboard_recommendations()

    def _restore_last_session(self):
        last = state.load_last_session()
        if not last or not last.get("anime_id"):
            return
        self.source_name = last.get("source", "anidb")
        alive = self.player.is_running() if hasattr(self.player, "is_running") else False
        if not alive:
            status = self.player.get_status() if hasattr(self.player, "get_status") else None
            alive = status is not None
        self._open_anime(last["anime_id"], last["anime_title"], jump_to_ep=last["ep_no"], reattach=alive)

    # ---------- sidebar nav ----------

    def _build_sidebar(self):
        bar = QWidget()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(68)
        v = QVBoxLayout(bar)
        v.setContentsMargins(0, 18, 0, 14)
        v.setSpacing(6)

        logo = QLabel()
        logo.setObjectName("logoMark")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(qta.icon("fa5s.play", color=MOCHA["crust"]).pixmap(QSize(18, 18)))
        v.addWidget(logo, alignment=Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(14)

        self.nav_buttons = {}
        nav_items = [
            ("home", "Home", lambda: self.stack.setCurrentWidget(self.home)),
            ("browse", "Browse", self._open_browse),
            ("downloads", "Downloads", lambda: self.stack.setCurrentWidget(self.downloads_page)),
            ("library", "Offline Library", self._open_library),
            ("account", "Account", self._open_account),
        ]
        for key, tooltip, handler in nav_items:
            btn = QPushButton(self._nav_icons[key], "")
            btn.setObjectName("navBtn")
            btn.setCheckable(True)
            btn.setToolTip(tooltip)
            btn.setFixedSize(44, 44)
            btn.setIconSize(QSize(18, 18))
            btn.clicked.connect(handler)
            v.addWidget(btn, alignment=Qt.AlignmentFlag.AlignHCenter)
            self.nav_buttons[key] = btn
        self.nav_buttons["home"].setChecked(True)

        v.addStretch()
        return bar

    def _sync_nav_highlight(self):
        page_to_key = {
            self.home: "home", self.browse_page: "browse", self.downloads_page: "downloads",
            self.library_page: "library", self.account_page: "account",
        }
        active = page_to_key.get(self.stack.currentWidget())
        for key, btn in self.nav_buttons.items():
            btn.setChecked(key == active)

    # ---------- home ----------

    def _build_home(self):
        # the dashboard's content (hero + 3 fixed-height card rows +
        # search results) very easily adds up to more vertical space
        # than the window has -- without an outer scroll area here, Qt's
        # layout engine has no choice but to compress every widget below
        # its set size to make everything fit, which is what was causing
        # rows to visually overlap/ghost into each other (confirmed via
        # screenshots: nothing was actually broken about the rows
        # themselves, the page as a whole just had nowhere to put them).
        # The individual rows already scroll horizontally; the page
        # itself needs to scroll vertically too.
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        w = QWidget()
        scroll.setWidget(w)
        v = QVBoxLayout(w)
        v.setSpacing(10)

        top_row = QHBoxLayout()
        title = QLabel("Discover")
        title.setObjectName("title")
        top_row.addWidget(title, 1)
        self.compact_btn = QPushButton(self._icons["compact"], "")
        self.compact_btn.setToolTip("Toggle compact (floating-panel) mode")
        self.compact_btn.setFixedWidth(36)
        self.compact_btn.clicked.connect(self._toggle_compact_mode)
        top_row.addWidget(self.compact_btn)
        v.addLayout(top_row)

        prefs_row = QHBoxLayout()
        prefs_row.setSpacing(10)
        source_col = QVBoxLayout()
        source_col.setSpacing(3)
        source_label = QLabel("SOURCE")
        source_label.setObjectName("heading")
        self.source_combo = QComboBox()
        self.source_combo.addItems(["anidb", "yuma", "nyaa (torrent)"])
        self.source_combo.setCurrentText("nyaa (torrent)" if self.source_name == "nyaa" else self.source_name)
        self.source_combo.currentTextChanged.connect(self._on_source_changed)
        source_col.addWidget(source_label)
        source_col.addWidget(self.source_combo)
        player_col = QVBoxLayout()
        player_col.setSpacing(3)
        player_label = QLabel("PLAYER")
        player_label.setObjectName("heading")
        self.player_combo = QComboBox()
        self.player_combo.addItems(["vlc", "mpv"])
        self.player_combo.setCurrentText(self.player_name)
        self.player_combo.currentTextChanged.connect(self._on_player_changed)
        player_col.addWidget(player_label)
        player_col.addWidget(self.player_combo)
        prefs_row.addLayout(source_col, 1)
        prefs_row.addLayout(player_col, 1)
        v.addLayout(prefs_row)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("search anime…")
        self.search_box.addAction(qta.icon("fa5s.search", color=MOCHA["overlay2"]), QLineEdit.ActionPosition.LeadingPosition)
        self.search_box.returnPressed.connect(self._do_search)
        search_btn = QPushButton("Search")
        search_btn.setObjectName("primary")
        search_btn.clicked.connect(self._do_search)
        search_row.addWidget(self.search_box, 1)
        search_row.addWidget(search_btn)
        v.addLayout(search_row)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        v.addWidget(divider)

        # RESULTS -- hidden by default (in dashboard mode) and swapped
        # in for the dashboard rows the moment a search actually runs,
        # right at the top so it doesn't sit buried below three rows of
        # recommendations that have nothing to do with what was searched.
        # Always visible in compact mode, which has no dashboard at all.
        self.results_container = QWidget()
        rv = QVBoxLayout(self.results_container)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)
        res_top = QHBoxLayout()
        res_top.addWidget(self._section_heading("RESULTS"))
        back_to_discover = QPushButton(self._icons["back"], "Back to Discover")
        back_to_discover.setObjectName("backToDiscover")
        back_to_discover.clicked.connect(self._clear_search)
        res_top.addStretch()
        res_top.addWidget(back_to_discover)
        rv.addLayout(res_top)
        self.results_list = QListWidget()
        self.results_list.setMinimumHeight(280)
        self.results_list.itemActivated.connect(self._result_selected)
        rv.addWidget(self.results_list)
        v.addWidget(self.results_container)
        self.results_container.setVisible(False)

        # dashboard rows -- hidden entirely in compact mode (see
        # _toggle_compact_mode), and swapped out for results_container
        # while an active search is showing (see _do_search/_clear_search).
        self.dashboard_widgets = []

        self.hero = self._build_hero()
        v.addWidget(self.hero)
        self.dashboard_widgets.append(self.hero)

        cont_label = self._section_heading("Continue Watching")
        v.addWidget(cont_label)
        self.continue_row, self.continue_row_layout = self._build_card_row()
        v.addWidget(self.continue_row)
        self.dashboard_widgets += [cont_label, self.continue_row]

        trend_label = self._section_heading("Trending Now")
        v.addWidget(trend_label)
        self.trending_row, self.trending_row_layout = self._build_card_row()
        v.addWidget(self.trending_row)
        self.dashboard_widgets += [trend_label, self.trending_row]

        pop_label = self._section_heading("Recommended")
        v.addWidget(pop_label)
        self.popular_row, self.popular_row_layout = self._build_card_row()
        v.addWidget(self.popular_row)
        self.dashboard_widgets += [pop_label, self.popular_row]
        self._recs_loaded = False

        self.status = QLabel("")
        self.status.setObjectName("status")
        v.addWidget(self.status)
        return page

    def _set_dashboard_visible(self, visible):
        for item in self.dashboard_widgets:
            if isinstance(item, QHBoxLayout):
                for i in range(item.count()):
                    widget = item.itemAt(i).widget()
                    if widget:
                        widget.setVisible(visible)
            else:
                item.setVisible(visible)

    def _apply_home_view(self):
        """Single source of truth for what the Home page shows:
        compact mode has no dashboard at all (results always visible,
        matching the original small floating-panel behavior); full mode
        shows either the dashboard OR results -- never both, since a
        search result buried below three rows of unrelated
        recommendations is exactly what was just complained about."""
        if self._compact_mode:
            self._set_dashboard_visible(False)
            self.results_container.setVisible(True)
        else:
            self._set_dashboard_visible(not self._showing_results)
            self.results_container.setVisible(self._showing_results)

    def _clear_search(self):
        self.search_box.clear()
        self.results_list.clear()
        self._showing_results = False
        self._apply_home_view()

    def _toggle_compact_mode(self):
        self._compact_mode = not self._compact_mode
        self._apply_home_view()
        if self._compact_mode:
            self.compact_btn.setIcon(self._icons["expand"])
            self.compact_btn.setToolTip("Switch to full dashboard")
            self.resize(420, 680)
        else:
            self.compact_btn.setIcon(self._icons["compact"])
            self.compact_btn.setToolTip("Toggle compact (floating-panel) mode")
            self.resize(1280, 800)
        state.save_prefs(compact_mode=self._compact_mode)

    def _load_dashboard_recommendations(self):
        if self._recs_loaded:
            return
        self._recs_loaded = True
        self._trending_worker = Worker(anilist_source.trending)
        self._trending_worker.done.connect(lambda r: self._set_card_row(self.trending_row_layout, r, self._search_title))
        self._trending_worker.done.connect(self._load_hero)
        self._trending_worker.start()
        self._popular_worker = Worker(anilist_source.popular)
        self._popular_worker.done.connect(lambda r: self._set_card_row(self.popular_row_layout, r, self._search_title))
        self._popular_worker.start()

    def _build_card_row(self):
        """A QScrollArea with a container+layout created ONCE and reused
        for the lifetime of the app, instead of building a fresh
        container and swapping it in via setWidget() on every refresh.
        Swapping containers repeatedly (continue-watching, trending, and
        popular all refresh within milliseconds of each other at
        startup) left stale painted pixels behind -- confirmed directly
        via a screenshot showing one row's heading text ghosted inside
        another row's still-loading card. Reusing one persistent
        container and clearing/repopulating its layout in place avoids
        ever swapping a widget QScrollArea has already painted."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(238)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 8)
        layout.setSpacing(10)
        layout.addStretch()
        scroll.setWidget(container)
        return scroll, layout

    def _clear_row(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

    def _set_card_row(self, layout, results, on_click):
        self._clear_row(layout)
        for r in results or []:
            card = self._cover_card(r["title"], r.get("cover_large") or r.get("cover"), lambda _, t=r["title"]: on_click(t))
            layout.addWidget(card)
        if not results:
            empty = QLabel("nothing to show right now")
            empty.setObjectName("status")
            layout.addWidget(empty)
        layout.addStretch()

    def _cover_card(self, title, cover_url, on_click):
        card = ClickableWidget()
        card.setObjectName("coverCard")
        card.setFixedSize(138, 224)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setToolTip(title)
        v = QVBoxLayout(card)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)

        cover = QLabel()
        cover.setFixedSize(126, 162)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover.setPixmap(self._icons["film"].pixmap(QSize(64, 64)))
        v.addWidget(cover)
        card.cover_label = cover  # _set_cover_icon needs this back

        text = QLabel(_truncate(title, 80))
        text.setObjectName("cardTitle")
        text.setWordWrap(True)
        v.addWidget(text, 1)

        card.clicked.connect(on_click)
        if cover_url:
            self._queue_image_fetch(cover_url, lambda data, c=card: self._set_cover_icon(c, data))
        return card

    # loading the dashboard fires 50+ cover requests at once (25 trending
    # + 25 recommended, each on its own QThread) -- several would just
    # never resolve, staying on the placeholder icon forever, even with
    # a retry (reported directly via screenshot, and confirmed: a plain
    # curl to AniList's CDN is fast, but firing dozens of concurrent
    # `requests` connections at once left some hanging/failing outright,
    # and an immediate retry just re-entered the same congestion). A
    # shared cap on concurrent fetches -- queuing the rest instead of
    # firing them all at once -- fixes the actual cause instead of
    # papering over it with more retries.
    MAX_CONCURRENT_IMAGE_FETCHES = 6

    def _queue_image_fetch(self, url, on_done, _retried=False):
        """on_done(bytes_or_None) fires once the fetch (or its one retry
        on failure) resolves. A cache hit resolves synchronously,
        immediately, no queueing needed."""
        cached = image_cache.cached_bytes(url)
        if cached is not None:
            on_done(cached)
            return
        self._image_fetch_queue = getattr(self, "_image_fetch_queue", [])
        self._image_fetch_active = getattr(self, "_image_fetch_active", 0)
        if self._image_fetch_active >= self.MAX_CONCURRENT_IMAGE_FETCHES:
            self._image_fetch_queue.append((url, on_done, _retried))
            return
        self._start_image_fetch(url, on_done, _retried)

    def _start_image_fetch(self, url, on_done, _retried):
        self._image_fetch_active = getattr(self, "_image_fetch_active", 0) + 1
        worker = Worker(image_cache.fetch, url)
        # a stuck worker permanently holds its concurrency slot and jams
        # the entire queue behind it -- confirmed directly: active count
        # stuck at the cap with items still queued, 15+ seconds later,
        # well past image_cache.fetch()'s own 8s per-request timeout.
        # Whatever the exact cause (thread-level hang, not just a slow
        # request), a real fetch always resolves in well under this, so
        # anything still pending past it gets treated as failed instead
        # of blocking every other card behind it forever. `handled`
        # guards against both the real completion and the watchdog
        # firing for the same fetch.
        handled = {"done": False}

        def finish(data):
            if handled["done"]:
                return
            handled["done"] = True
            self._on_image_fetch_done(url, on_done, data, _retried)

        worker.done.connect(finish)
        worker.start()
        self._cover_workers = getattr(self, "_cover_workers", [])
        self._cover_workers.append(worker)
        QTimer.singleShot(12000, lambda: finish(None))

    def _on_image_fetch_done(self, url, on_done, data, _retried):
        self._image_fetch_active = max(0, getattr(self, "_image_fetch_active", 1) - 1)
        if not data and not _retried:
            # append to the BACK of the queue rather than starting the
            # retry immediately -- starting it right here re-occupies
            # the very slot that just freed up before anything else
            # waiting gets a turn. Confirmed directly: several fetches
            # that kept failing (each hitting the watchdog below) kept
            # re-claiming their own freed slot via instant retry, so the
            # active count oscillated at the cap forever and the real
            # queue never advanced at all. A retry is just another
            # normal queue entry now -- one FIFO, no cutting in line.
            self._image_fetch_queue = getattr(self, "_image_fetch_queue", [])
            self._image_fetch_queue.append((url, on_done, True))
        else:
            on_done(data)
        self._advance_image_fetch_queue()

    def _advance_image_fetch_queue(self):
        queue = getattr(self, "_image_fetch_queue", [])
        while queue and getattr(self, "_image_fetch_active", 0) < self.MAX_CONCURRENT_IMAGE_FETCHES:
            url, on_done, retried = queue.pop(0)
            self._start_image_fetch(url, on_done, retried)

    def _set_cover_icon(self, card, data):
        if not data:
            return
        pix = QPixmap()
        if pix.loadFromData(data):
            try:
                card.cover_label.setPixmap(pix.scaled(
                    card.cover_label.width(), card.cover_label.height(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                ))
            except RuntimeError:
                pass  # row was rebuilt (e.g. Continue Watching refreshed) before this fetch landed

    def _search_title(self, title):
        if self.source_name == "nyaa":
            self.source_combo.setCurrentText("nyaa (torrent)")
        self.search_box.setText(title)
        self._do_search()

    def _section_heading(self, text):
        heading = QLabel(text)
        heading.setObjectName("sectionHeading")
        return heading

    def _build_hero(self):
        card = QWidget()
        card.setObjectName("heroCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.hero_banner = QLabel()
        self.hero_banner.setObjectName("heroBanner")
        self.hero_banner.setFixedHeight(260)
        # NOT setScaledContents(True) -- that stretches the pixmap to
        # exactly fill the label with no regard for aspect ratio, which
        # is what was distorting/warping banner art (confirmed directly
        # via a screenshot). The pixmap is scaled+center-cropped by hand
        # in _set_hero_pixmap() instead, matching normal "cover" image
        # behavior (fill the box, keep proportions, crop the overflow).
        self.hero_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.hero_banner)

        panel = QWidget()
        panel.setObjectName("heroPanel")
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(18, 14, 18, 16)
        pv.setSpacing(6)

        badge = QLabel("TRENDING #1")
        badge.setObjectName("heroBadge")
        pv.addWidget(badge, alignment=Qt.AlignmentFlag.AlignLeft)
        self.hero_title = QLabel("Loading…")
        self.hero_title.setObjectName("heroTitle")
        self.hero_title.setWordWrap(True)
        pv.addWidget(self.hero_title)
        self.hero_desc = QLabel("")
        self.hero_desc.setObjectName("heroDesc")
        self.hero_desc.setWordWrap(True)
        pv.addWidget(self.hero_desc)
        self.hero_more_btn = QPushButton("See more")
        self.hero_more_btn.setObjectName("linkButton")
        self.hero_more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hero_more_btn.clicked.connect(self._toggle_hero_desc)
        self.hero_more_btn.setVisible(False)
        pv.addWidget(self.hero_more_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        self._hero_desc_expanded = False
        self._hero_full_desc = ""
        self.hero_btn = QPushButton(qta.icon("fa5s.search", color=MOCHA["crust"]), "Find it on my source")
        self.hero_btn.setObjectName("primary")
        pv.addWidget(self.hero_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        v.addWidget(panel)
        return card

    def _toggle_hero_desc(self):
        self._hero_desc_expanded = not self._hero_desc_expanded
        if self._hero_desc_expanded:
            self.hero_desc.setText(self._hero_full_desc)
            self.hero_more_btn.setText("See less")
        else:
            self.hero_desc.setText(_truncate(self._hero_full_desc, 200))
            self.hero_more_btn.setText("See more")

    def _load_hero(self, results):
        if not results:
            self.hero.setVisible(False)
            return
        top = results[0]
        self.hero_title.setText(top["title"])
        desc = top.get("description") or ""
        # a hard pixel-height clip on a word-wrapped label cuts text off
        # mid-sentence/mid-word with no indication more existed --
        # truncating the string itself with an ellipsis reads cleanly
        # and keeps the panel's height predictable regardless of how
        # long AniList's description happens to be. A "See more" toggle
        # covers anyone who actually wants the rest.
        self._hero_full_desc = desc
        self._hero_desc_expanded = False
        self.hero_desc.setText(_truncate(desc, 200))
        self.hero_more_btn.setText("See more")
        self.hero_more_btn.setVisible(len(desc) > 200)
        try:
            self.hero_btn.clicked.disconnect()
        except TypeError:
            pass  # nothing connected yet on the very first load
        self.hero_btn.clicked.connect(lambda: self._search_title(top["title"]))
        cover = top.get("banner") or top.get("cover_xl") or top.get("cover_large") or top.get("cover")
        if cover:
            self._queue_image_fetch(cover, self._set_hero_pixmap)

    def _set_hero_pixmap(self, data):
        if not data:
            return
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        target_w, target_h = self.hero_banner.width(), self.hero_banner.height()
        fill_scale = max(target_w / pix.width(), target_h / pix.height())
        if fill_scale <= 1.0:
            # native resolution already covers the box -- scale to fill
            # (keeping aspect ratio) then crop the overflow around
            # center, same "cover" fit a real <img> tag would use. We're
            # only ever downscaling here, so no quality loss.
            scaled = pix.scaled(
                target_w, target_h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = max(0, (scaled.width() - target_w) // 2)
            y = max(0, (scaled.height() - target_h) // 2)
            result = scaled.copy(x, y, target_w, target_h)
        else:
            # native resolution is smaller than the banner box in at
            # least one dimension -- most shows have no real wide
            # bannerImage, so this is almost always the portrait cover
            # art falling back here, and stretching it to fill the full
            # width blew it up 2-3x past its actual resolution (reported
            # directly: "hero images are not full quality"). Keep it at
            # native size instead, centered on a plain backdrop, rather
            # than upscale past what the source image actually has.
            result = QPixmap(target_w, target_h)
            result.fill(QColor(MOCHA["mantle"]))
            painter = QPainter(result)
            painter.drawPixmap((target_w - pix.width()) // 2, (target_h - pix.height()) // 2, pix)
            painter.end()
        try:
            self.hero_banner.setPixmap(result)
        except RuntimeError:
            pass

    def _on_source_changed(self, text):
        self.source_name = "nyaa" if text.startswith("nyaa") else text
        state.save_prefs(source=self.source_name)

    def _on_player_changed(self, text):
        if self.player.is_running():
            self.player.stop()
        self.player_name = text
        self.player = vlc_backend.VlcPlayer() if text == "vlc" else mpv_backend.MpvPlayer()
        state.save_prefs(player=self.player_name)
        self._update_track_buttons_visibility()

    def _refresh_history(self):
        entries = list(reversed(state.read_history()))
        positions = state.load_positions()
        self._clear_row(self.continue_row_layout)
        for e in entries:
            bits = [f"[{e['source']}]", f"episode {e['ep_no']}"]
            resume_at = positions.get(state.position_key(e["source"], e["anime_title"], e["ep_no"]))
            if resume_at:
                bits.append(f"resume {fmt_time(resume_at)}")
            card = self._cover_card(e["anime_title"], None, lambda _, ent=e: self._continue_selected(ent))
            card.setToolTip(f"{e['anime_title']}  ·  " + "  ·  ".join(bits))
            self.continue_row_layout.addWidget(card)
            worker = Worker(_cover_bytes_for_title, e["anime_title"])
            worker.done.connect(lambda data, b=card: self._set_cover_icon(b, data))
            worker.start()
            self._cover_workers = getattr(self, "_cover_workers", [])
            self._cover_workers.append(worker)
        if not entries:
            empty = QLabel("nothing in progress yet -- start watching something")
            empty.setObjectName("status")
            self.continue_row_layout.addWidget(empty)
        self.continue_row_layout.addStretch()

    def _do_search(self):
        q = self.search_box.text().strip()
        if not q:
            return
        self._showing_results = True
        self._apply_home_view()
        self.status.setText("searching…")
        self.results_list.clear()
        if self.source_name == "nyaa":
            self._search_worker = Worker(nyaa_source.search, q)
            self._search_worker.done.connect(self._nyaa_search_done)
        else:
            self._search_worker = Worker(SOURCES[self.source_name].search, q)
            self._search_worker.done.connect(self._search_done)
        self._search_worker.failed.connect(lambda e: self.status.setText(f"search failed: {e}"))
        self._search_worker.start()

    def _search_done(self, results):
        self.status.setText(f"{len(results)} results" if results else "no results")
        for r in results:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, r)
            # plain QListWidgetItem text doesn't wrap -- long titles were
            # overflowing into a horizontal scrollbar instead of wrapping
            # to a second line, same issue the nyaa results row already
            # had a fix for. Reuse it here too.
            row = self._two_line_row(r["title"])
            item.setSizeHint(self._row_height(row))
            self.results_list.addItem(item)
            self.results_list.setItemWidget(item, row)

    def _nyaa_search_done(self, results):
        self.status.setText(f"{len(results)} releases" if results else "no results (nyaa.si can be flaky -- try again)")
        for r in results:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, r)
            # plain single-line item text doesn't wrap, and release titles
            # + size + seeders together are routinely wider than the panel
            # -- was overflowing into a horizontal scrollbar instead of
            # reading cleanly. A real two-line row wraps the title and
            # keeps size/seeders on their own muted line underneath.
            row = self._two_line_row(r["title"], f"{r['size']} · {r['seeders']} seeders")
            item.setSizeHint(self._row_height(row))
            self.results_list.addItem(item)
            self.results_list.setItemWidget(item, row)

    def _two_line_row(self, primary_text, secondary_text=None):
        row = QWidget()
        row.setObjectName("listRow")
        rv = QVBoxLayout(row)
        rv.setContentsMargins(6, 4, 6, 4)
        rv.setSpacing(2)
        primary = QLabel(primary_text)
        primary.setWordWrap(True)
        rv.addWidget(primary)
        if secondary_text:
            secondary = QLabel(secondary_text)
            secondary.setObjectName("status")
            rv.addWidget(secondary)
        # QVBoxLayout's automatic sizeHint() for a word-wrapped QLabel is
        # unreliable before the widget has a real width to wrap against
        # (Qt can't resolve heightForWidth without one) -- confirmed
        # directly: rows using row.sizeHint() as the QListWidgetItem's
        # size hint came back too short and overlapped the row below
        # instead of stacking. A fixed height sidesteps needing Qt to
        # guess at all; see _row_height() for what callers should pass
        # to QListWidgetItem.setSizeHint() instead of row.sizeHint().
        row.setFixedHeight(64 if secondary_text else 32)
        return row

    def _row_height(self, row):
        # width is irrelevant here -- QListWidget stretches item widgets
        # to the view's width regardless of what's in the size hint, only
        # the height actually matters for row placement.
        return QSize(200, row.minimumHeight())

    def _cover_row(self, primary_text, secondary_text):
        """Like _two_line_row but with a cover-art thumbnail slot on the
        left -- the label starts as an empty placeholder and gets its
        pixmap filled in asynchronously (see _load_cover), since the
        network fetch can't block list construction."""
        row = QWidget()
        row.setObjectName("listRow")
        rh = QHBoxLayout(row)
        rh.setContentsMargins(6, 4, 6, 4)
        rh.setSpacing(10)
        cover = QLabel()
        cover.setObjectName("coverThumb")
        cover.setFixedSize(40, 56)
        cover.setScaledContents(True)
        rh.addWidget(cover)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        primary = QLabel(primary_text)
        primary.setWordWrap(True)
        text_col.addWidget(primary)
        secondary = QLabel(secondary_text)
        secondary.setObjectName("status")
        text_col.addWidget(secondary)
        text_col.addStretch()
        rh.addLayout(text_col, 1)
        row.setFixedHeight(64)  # matches the cover thumbnail's 56px + margins -- see _two_line_row
        return row, cover

    def _load_cover(self, label, url):
        self._queue_image_fetch(url, lambda data, lbl=label: self._set_cover_pixmap(lbl, data))

    def _set_cover_pixmap(self, label, data):
        if not data:
            return
        pix = QPixmap()
        if pix.loadFromData(data):
            try:
                label.setPixmap(pix)
            except RuntimeError:
                pass  # list was cleared (e.g. Trending/Popular switched) before this fetch landed

    def _result_selected(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if self.source_name == "nyaa":
            self._play_torrent(data, download_fully=False)
        else:
            self._open_anime(data["id"], data["title"])

    def _continue_selected(self, e):
        self.source_name = e["source"]
        self._open_anime(e["anime_id"], e["anime_title"], jump_to_ep=e["ep_no"])

    def _open_library(self):
        self._populate_library()
        self.stack.setCurrentWidget(self.library_page)

    # ---------- browse/discover (AniList trending/popular) ----------

    def _build_browse_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        title = QLabel("Browse")
        title.setObjectName("title")
        v.addWidget(title)

        mode_row = QHBoxLayout()
        self.browse_mode_combo = QComboBox()
        self.browse_mode_combo.addItems(["Trending", "Popular"])
        self.browse_mode_combo.currentTextChanged.connect(lambda _: self._load_browse())
        mode_row.addWidget(self.browse_mode_combo, 1)
        v.addLayout(mode_row)

        self.browse_status = QLabel("")
        self.browse_status.setObjectName("status")
        v.addWidget(self.browse_status)

        self.browse_list = QListWidget()
        self.browse_list.itemActivated.connect(self._browse_item_selected)
        v.addWidget(self.browse_list)

        hint = QLabel("selecting a title searches it on your current source")
        hint.setObjectName("status")
        hint.setWordWrap(True)
        v.addWidget(hint)
        return w

    def _open_browse(self):
        self.stack.setCurrentWidget(self.browse_page)
        self._load_browse()

    def _load_browse(self):
        self.browse_list.clear()
        self.browse_status.setText("loading…")
        fn = anilist_source.trending if self.browse_mode_combo.currentText() == "Trending" else anilist_source.popular
        self._browse_worker = Worker(fn)
        self._browse_worker.done.connect(self._browse_loaded)
        self._browse_worker.failed.connect(lambda e: self.browse_status.setText(f"failed: {e}"))
        self._browse_worker.start()

    def _browse_loaded(self, results):
        self.browse_status.setText(f"{len(results)} shows" if results else "no results (AniList may be down)")
        for r in results:
            bits = []
            if r.get("format"):
                bits.append(r["format"])
            if r.get("episodes"):
                bits.append(f"{r['episodes']} eps")
            if r.get("score"):
                bits.append(f"{r['score']}%")
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, r["title"])
            row, cover = self._cover_row(r["title"], " · ".join(bits))
            item.setSizeHint(self._row_height(row))
            self.browse_list.addItem(item)
            self.browse_list.setItemWidget(item, row)
            if r.get("cover"):
                self._load_cover(cover, r["cover"])

    def _browse_item_selected(self, item):
        title = item.data(Qt.ItemDataRole.UserRole)
        if self.source_name == "nyaa":
            self.source_combo.setCurrentText("nyaa (torrent)")
        self.search_box.setText(title)
        self.stack.setCurrentWidget(self.home)
        self._do_search()

    # ---------- account / AniList tracker sync ----------

    def _build_account_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        title = QLabel("Account")
        title.setObjectName("title")
        v.addWidget(title)

        self.anilist_status = QLabel("")
        self.anilist_status.setObjectName("status")
        self.anilist_status.setWordWrap(True)
        v.addWidget(self.anilist_status)

        client_label = QLabel("ANILIST CLIENT ID")
        client_label.setObjectName("heading")
        v.addWidget(client_label)
        client_hint = QLabel(
            "One-time setup: create a free client at anilist.co/settings/developer "
            "with redirect URL https://anilist.co/api/v2/oauth/pin, then paste its id below."
        )
        client_hint.setObjectName("status")
        client_hint.setWordWrap(True)
        v.addWidget(client_hint)
        self.client_id_input = QLineEdit()
        self.client_id_input.setPlaceholderText("client id…")
        self.client_id_input.setText(self.prefs.get("anilist_client_id", ""))
        self.client_id_input.editingFinished.connect(self._on_client_id_changed)
        v.addWidget(self.client_id_input)

        authorize_btn = QPushButton(self._icons["link"], "Open AniList authorization page")
        authorize_btn.clicked.connect(self._open_anilist_authorize)
        v.addWidget(authorize_btn)

        token_label = QLabel("TOKEN")
        token_label.setObjectName("heading")
        v.addWidget(token_label)
        token_row = QHBoxLayout()
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("paste the token shown on that page…")
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        connect_btn = QPushButton("Connect")
        connect_btn.setObjectName("primary")
        connect_btn.clicked.connect(self._connect_anilist)
        token_row.addWidget(self.token_input, 1)
        token_row.addWidget(connect_btn)
        v.addLayout(token_row)

        disconnect_btn = QPushButton("Disconnect")
        disconnect_btn.clicked.connect(self._disconnect_anilist)
        v.addWidget(disconnect_btn)

        self.sync_toggle = QCheckBox("Sync watched episodes to AniList")
        self.sync_toggle.setChecked(self.prefs.get("anilist_sync", False))
        self.sync_toggle.toggled.connect(self._on_sync_toggle)
        v.addWidget(self.sync_toggle)

        v.addStretch()
        return w

    def _open_account(self):
        self._refresh_anilist_status()
        self.stack.setCurrentWidget(self.account_page)

    def _on_client_id_changed(self):
        state.save_prefs(anilist_client_id=self.client_id_input.text().strip())
        self.prefs = state.load_prefs()

    def _open_anilist_authorize(self):
        client_id = self.client_id_input.text().strip()
        if not client_id:
            self.anilist_status.setText("enter a client id first")
            return
        webbrowser.open(tracker.authorize_url(client_id))

    def _connect_anilist(self):
        token = self.token_input.text().strip()
        if not token:
            return
        tracker.save_token(token)
        self.token_input.clear()
        self.anilist_status.setText("checking…")
        self._whoami_worker = Worker(tracker.whoami)
        self._whoami_worker.done.connect(self._anilist_connected)
        self._whoami_worker.failed.connect(lambda e: self.anilist_status.setText(f"failed: {e}"))
        self._whoami_worker.start()

    def _anilist_connected(self, username):
        if username:
            self.anilist_status.setText(f"connected as {username}")
        else:
            tracker.clear_token()
            self.anilist_status.setText("couldn't verify that token -- check the client id and try again")

    def _disconnect_anilist(self):
        tracker.clear_token()
        self.anilist_status.setText("disconnected")

    def _on_sync_toggle(self, checked):
        state.save_prefs(anilist_sync=checked)
        self.prefs = state.load_prefs()

    def _refresh_anilist_status(self):
        if not tracker.load_token():
            self.anilist_status.setText("not connected")
            return
        self.anilist_status.setText("checking…")
        self._whoami_worker = Worker(tracker.whoami)
        self._whoami_worker.done.connect(self._anilist_connected)
        self._whoami_worker.failed.connect(lambda e: self.anilist_status.setText(f"failed: {e}"))
        self._whoami_worker.start()

    # ---------- episode list ----------

    def _build_episode_list(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        back = QPushButton(self._icons["back"], "Back")
        back.clicked.connect(lambda: self.stack.setCurrentWidget(self.home))
        top.addWidget(back)
        self.ep_title = QLabel("")
        self.ep_title.setObjectName("title")
        top.addWidget(self.ep_title, 1)
        v.addLayout(top)

        self.ep_list_widget = QListWidget()
        self.ep_list_widget.itemActivated.connect(self._episode_selected)
        v.addWidget(self.ep_list_widget)

        actions = QHBoxLayout()
        dl_ep_btn = QPushButton("Download selected")
        dl_ep_btn.clicked.connect(self._download_selected_episode)
        dl_all_btn = QPushButton("Download whole series")
        dl_all_btn.clicked.connect(self._download_whole_series)
        actions.addWidget(dl_ep_btn)
        actions.addWidget(dl_all_btn)
        v.addLayout(actions)

        self.ep_status = QLabel("")
        self.ep_status.setObjectName("status")
        v.addWidget(self.ep_status)
        return w

    def _open_anime(self, aid, title, jump_to_ep=None, reattach=False):
        self.anime_id = aid
        self.anime_title = title
        self.mal_id = None
        self.ep_title.setText(title)
        self.ep_list_widget.clear()
        self.ep_status.setText("loading episodes…")
        self.stack.setCurrentWidget(self.eplist)
        src = SOURCES[self.source_name]
        self._ep_worker = Worker(src.episodes, aid)
        self._ep_worker.done.connect(lambda eps: self._episodes_loaded(eps, jump_to_ep, reattach))
        self._ep_worker.failed.connect(lambda e: self.ep_status.setText(f"failed: {e}"))
        self._ep_worker.start()

    def _episodes_loaded(self, eps, jump_to_ep, reattach=False):
        self.episode_maps = eps
        self.ep_status.setText(f"{len(eps)} episodes")
        selected_row = None
        for i, e in enumerate(eps):
            item = QListWidgetItem(f"Episode {e['ep_no']}")
            item.setData(Qt.ItemDataRole.UserRole, e)
            self.ep_list_widget.addItem(item)
            if jump_to_ep and e["ep_no"] == jump_to_ep:
                selected_row = i
        if selected_row is not None:
            self.ep_list_widget.setCurrentRow(selected_row)
            self.ep_list_widget.scrollToItem(self.ep_list_widget.item(selected_row))
            if jump_to_ep and reattach:
                self._reattach_playback(jump_to_ep, self.episode_maps[selected_row]["ep_ref"])
            elif jump_to_ep:
                self._play_episode(self.episode_maps[selected_row])

    def _reattach_playback(self, ep_no, ep_ref):
        self._player_return_page = self.eplist
        self.current_ep_no = ep_no
        self.current_ep_display = ep_no
        self.current_ep_ref = ep_ref
        self.now_title.setText(f"{self.anime_title}\nEpisode {ep_no}")
        self.now_status.setText("reconnected to running player")
        self.stack.setCurrentWidget(self.player_page)

    def _episode_selected(self, item):
        self._play_episode(item.data(Qt.ItemDataRole.UserRole))

    def _download_selected_episode(self):
        item = self.ep_list_widget.currentItem()
        if not item:
            return
        e = item.data(Qt.ItemDataRole.UserRole)
        self._queue_download(e)

    def _download_whole_series(self):
        for e in self.episode_maps:
            self._queue_download(e)
        self.stack.setCurrentWidget(self.downloads_page)

    def _queue_download(self, ep):
        job = download_manager.DownloadJob(
            self.anime_title, ep["ep_no"], None, None,
            source=self.source_name, ep_ref=ep["ep_ref"],
        )
        self.download_jobs.append(job)
        self._pending_downloads.append((job, ep))
        self._refresh_downloads_list()
        self._advance_download_queue()

    def _retry_download(self, job):
        if not (job.source and job.ep_ref):
            return
        job.status = "queued"
        job.progress = 0.0
        self._pending_downloads.append((job, {"ep_no": job.ep_no, "ep_ref": job.ep_ref}))
        self._refresh_downloads_list()
        self._advance_download_queue()

    def _remove_download_entry(self, job):
        self.download_jobs = [j for j in self.download_jobs if j is not job]
        self._refresh_downloads_list()

    def _advance_download_queue(self):
        # only one ffmpeg download runs at a time -- "download whole
        # series" was firing every episode's download concurrently
        # instead (confirmed: watched 6+ real ffmpeg processes running
        # at once from a live test), despite this being the whole point
        # of a queue. Each episode now waits its turn.
        if self._active_download_job is not None or not self._pending_downloads:
            return
        job, ep = self._pending_downloads.pop(0)
        self._active_download_job = job
        # job.source, not self.source_name -- the currently-selected
        # source can differ from whatever was active when this job was
        # queued (switched sources mid-queue, or this is a retry queued
        # much later)
        src = SOURCES[job.source or self.source_name]
        worker = Worker(src.watch, ep["ep_ref"], self.mode)
        worker.done.connect(lambda w, j=job: self._start_download_job(j, w))
        worker.failed.connect(lambda err, j=job: self._fail_job(j, err))
        worker.start()
        self._dl_link_workers = getattr(self, "_dl_link_workers", [])
        self._dl_link_workers.append(worker)

    def _start_download_job(self, job, watch_result):
        if not watch_result:
            self._fail_job(job, "no stream found")
            return
        job.url = watch_result["url"]
        job.referer = watch_result.get("referer")
        worker = DownloadWorker(job)
        # progress fires from the worker thread -- Qt auto-queues signal
        # delivery across threads, so this is safe where a raw callback
        # calling GUI methods directly (the previous approach) wasn't.
        worker.progress.connect(lambda j: self._refresh_downloads_list())
        worker.finished_ok.connect(lambda ok, j=job: self._on_download_finished(j))
        worker.start()
        job.worker = worker  # keep the QThread alive (and reachable to cancel)
        self._dl_run_workers = getattr(self, "_dl_run_workers", [])
        self._dl_run_workers.append(worker)

    def _fail_job(self, job, err):
        job.status = "failed"
        self._on_download_finished(job)

    def _on_download_finished(self, job):
        if self._active_download_job is job:
            self._active_download_job = None
        self._refresh_downloads_list()
        self._advance_download_queue()

    # ---------- downloads page ----------

    def _build_downloads_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        title = QLabel("Downloads")
        title.setObjectName("title")
        v.addWidget(title)
        self.downloads_list = QListWidget()
        v.addWidget(self.downloads_list)
        return w

    def _refresh_downloads_list(self):
        self.downloads_list.clear()
        for job in self.download_jobs:
            row = QWidget()
            row.setObjectName("listRow")
            rv = QVBoxLayout(row)
            rv.setContentsMargins(6, 4, 6, 4)
            rv.setSpacing(4)

            top = QHBoxLayout()
            title = QLabel(f"{job.anime_title} · Episode {job.ep_no}")
            title.setWordWrap(True)
            top.addWidget(title, 1)
            if job.status in ("queued", "downloading"):
                cancel_btn = QPushButton(self._icons["cancel"], "")
                cancel_btn.setFixedWidth(28)
                cancel_btn.setToolTip("Cancel download")
                cancel_btn.clicked.connect(lambda _, j=job: self._cancel_download(j))
                top.addWidget(cancel_btn)
            elif job.status == "failed":
                retry_btn = QPushButton(self._icons["reload"], "")
                retry_btn.setFixedWidth(28)
                retry_btn.setToolTip("Retry download")
                retry_btn.clicked.connect(lambda _, j=job: self._retry_download(j))
                top.addWidget(retry_btn)
            if job.status in ("done", "failed", "cancelled"):
                remove_btn = QPushButton(self._icons["close"], "")
                remove_btn.setFixedWidth(28)
                remove_btn.setToolTip("Remove from list")
                remove_btn.clicked.connect(lambda _, j=job: self._remove_download_entry(j))
                top.addWidget(remove_btn)
            rv.addLayout(top)

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(job.progress * 100))
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            rv.addWidget(bar)

            status = QLabel(f"{job.status} · {job.progress*100:.0f}%")
            status.setObjectName("status")
            rv.addWidget(status)

            row.setFixedHeight(72)  # see _two_line_row -- word-wrapped labels' sizeHint() isn't reliable here
            item = QListWidgetItem()
            item.setSizeHint(self._row_height(row))
            self.downloads_list.addItem(item)
            self.downloads_list.setItemWidget(item, row)
        self._save_download_queue()

    def _save_download_queue(self):
        # only incomplete jobs -- closing the app used to lose the
        # whole queue silently (in-memory only, nothing on disk), so a
        # download mid-progress or still waiting its turn just vanished
        # with no trace. Persisted here (piggybacking on the same
        # refresh that already runs after every queue mutation) so
        # _restore_download_queue() can re-queue them on next launch.
        pending = [
            {"anime_title": j.anime_title, "ep_no": j.ep_no, "source": j.source, "ep_ref": j.ep_ref}
            for j in self.download_jobs
            if j.status in ("queued", "downloading", "failed") and j.source and j.ep_ref
        ]
        state.save_download_queue(pending)

    def _restore_download_queue(self):
        for entry in state.load_download_queue():
            job = download_manager.DownloadJob(
                entry["anime_title"], entry["ep_no"], None, None,
                source=entry["source"], ep_ref=entry["ep_ref"],
            )
            self.download_jobs.append(job)
            self._pending_downloads.append((job, {"ep_no": entry["ep_no"], "ep_ref": entry["ep_ref"]}))
        if self.download_jobs:
            self._refresh_downloads_list()
            self._advance_download_queue()

    def _cancel_download(self, job):
        # still waiting its turn in the queue -- no ffmpeg process exists
        # yet, just drop it rather than starting it only to kill it
        self._pending_downloads = [(j, e) for j, e in self._pending_downloads if j is not job]
        job.cancel()  # no-op if it was never running; kills ffmpeg if it was
        if self._active_download_job is job:
            self._active_download_job = None
            self._advance_download_queue()
        self._refresh_downloads_list()

    # ---------- offline library ----------

    def _build_library_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        title = QLabel("Offline Library")
        title.setObjectName("title")
        v.addWidget(title)
        self.library_list = QListWidget()
        # a long title ("Show Name — Episode 25") could exceed the
        # list's width faster than word-wrap could react to it, which
        # showed up as a real horizontal scrollbar appearing mid-list --
        # its arrow buttons rendered as a stray shape overlapping the
        # rows around it (confirmed directly via screenshot). Disabling
        # horizontal scrolling outright forces the row to respect the
        # list's width instead of ever needing one.
        self.library_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.library_list.itemActivated.connect(self._play_offline_selected)
        v.addWidget(self.library_list)
        return w

    def _populate_library(self):
        self.library_list.clear()
        for show in download_manager.list_downloaded():
            for ep in show["episodes"]:
                label = f"{show['show']} — Episode {ep}"
                path = download_manager.dest_path(show["show"], ep)

                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, {"show": show["show"], "ep": ep, "path": path, "label": label})

                row = QWidget()
                row.setObjectName("listRow")
                rh = QHBoxLayout(row)
                rh.setContentsMargins(6, 4, 6, 4)
                text = QLabel(label)
                text.setWordWrap(True)
                rh.addWidget(text, 1)
                delete_btn = QPushButton(self._icons["close"], "")
                delete_btn.setFixedWidth(28)
                delete_btn.setToolTip("Delete from disk")
                delete_btn.clicked.connect(lambda _, s=show["show"], e=ep: self._delete_downloaded_episode(s, e))
                rh.addWidget(delete_btn)
                row.setFixedHeight(40)

                item.setSizeHint(self._row_height(row))
                self.library_list.addItem(item)
                self.library_list.setItemWidget(item, row)

    def _delete_downloaded_episode(self, anime_title, ep_no):
        # deleting a download is permanent and easy to fat-finger next
        # to a scrollable list of X buttons -- confirm before actually
        # touching disk, since the whole point of keeping a download
        # around is often "so I can rewatch it later."
        reply = QMessageBox.question(
            self, "Delete episode?",
            f"Delete {anime_title} Episode {ep_no} from disk? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        download_manager.delete_episode(anime_title, ep_no)
        self._populate_library()

    def _play_offline_selected(self, item):
        self._player_return_page = self.library_page
        data = item.data(Qt.ItemDataRole.UserRole)
        path = data["path"]
        self.anime_title = data["show"]
        self.current_ep_no = "offline"
        self.current_ep_display = data["ep"]
        self.now_title.setText(data["label"])
        self.stack.setCurrentWidget(self.player_page)
        try:
            self.player.play(path)
        except (OSError, FileNotFoundError):
            self.now_status.setText(f"{self.player_name} not found -- install it and try again")
            return
        self._apply_saved_volume()
        self._apply_saved_speed()
        self.now_status.setText("playing (offline)")

    # ---------- player controls ----------

    def _build_player_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        back = QPushButton(self._icons["back"], "Back")
        back.clicked.connect(self._go_back_from_player)
        top.addWidget(back)
        v.addLayout(top)

        self.now_title = QLabel("")
        self.now_title.setObjectName("title")
        self.now_title.setWordWrap(True)
        v.addWidget(self.now_title)

        hint = QLabel("playing in a separate player window →")
        hint.setObjectName("status")
        v.addWidget(hint)

        seek_row = QHBoxLayout()
        self.time_label = QLabel("0:00")
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek_slider.sliderReleased.connect(self._seek_released)
        self.dur_label = QLabel("0:00")
        seek_row.addWidget(self.time_label)
        seek_row.addWidget(self.seek_slider, 1)
        seek_row.addWidget(self.dur_label)
        v.addLayout(seek_row)

        self.now_status = QLabel("")
        self.now_status.setObjectName("status")
        v.addWidget(self.now_status)

        ctrl = QHBoxLayout()
        prev_btn = QPushButton(self._icons["prev"], "Previous")
        prev_btn.clicked.connect(self._play_previous)
        self.pause_btn = QPushButton(self._icons["pause"], "Pause")
        self.pause_btn.setObjectName("primary")
        self.pause_btn.clicked.connect(self._toggle_pause)
        next_btn = QPushButton(self._icons["next"], "Next")
        next_btn.clicked.connect(self._play_next)
        ctrl.addWidget(prev_btn)
        ctrl.addWidget(self.pause_btn)
        ctrl.addWidget(next_btn)
        v.addLayout(ctrl)

        row2 = QHBoxLayout()
        stop_btn = QPushButton(self._icons["stop"], "Stop")
        stop_btn.clicked.connect(self._stop_playback)
        self.download_current_btn = QPushButton(self._icons["down"], "Download this episode")
        self.download_current_btn.clicked.connect(self._download_current_episode)
        row2.addWidget(stop_btn)
        row2.addWidget(self.download_current_btn)
        v.addLayout(row2)

        vol_row = QHBoxLayout()
        vol_label = QLabel("Volume")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 150)
        self.volume_slider.setValue(self.prefs.get("volume", 100))
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        vol_row.addWidget(vol_label)
        vol_row.addWidget(self.volume_slider, 1)
        v.addLayout(vol_row)

        self.volume_pct_label = QLabel(f"{self.prefs.get('volume', 100)}%")
        self.volume_pct_label.setObjectName("status")
        self.volume_pct_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.volume_pct_label)

        speed_row = QHBoxLayout()
        speed_label = QLabel("Speed")
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.5x", "0.75x", "1x", "1.25x", "1.5x", "2x"])
        self.speed_combo.setCurrentText(f"{self.prefs.get('speed', 1.0):g}x")
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        speed_row.addWidget(speed_label)
        speed_row.addWidget(self.speed_combo, 1)
        v.addLayout(speed_row)

        # track cycling only wired up for mpv -- VLC's HTTP interface
        # doesn't expose a track *list* (just id-based set commands), so
        # there's no reliable way to know what to cycle to. Rather than
        # ship a button that's a coin-flip whether it does anything,
        # it's hidden entirely when VLC is the active backend (see
        # _on_player_changed / _got_stream toggling their visibility).
        track_row = QHBoxLayout()
        self.sub_btn = QPushButton("Subtitle track")
        self.sub_btn.clicked.connect(lambda: self.player.cycle_subtitle() if hasattr(self.player, "cycle_subtitle") else None)
        self.audio_btn = QPushButton(self._icons["volume"], "Audio track")
        self.audio_btn.clicked.connect(lambda: self.player.cycle_audio() if hasattr(self.player, "cycle_audio") else None)
        track_row.addWidget(self.sub_btn)
        track_row.addWidget(self.audio_btn)
        v.addLayout(track_row)
        self._update_track_buttons_visibility()

        v.addStretch()

        self.rpc_toggle = QCheckBox("Discord Rich Presence")
        self.rpc_toggle.setChecked(True)
        self.rpc_toggle.toggled.connect(self._on_rpc_toggle)
        v.addWidget(self.rpc_toggle)

        shortcuts_hint = QLabel("Space pause · ←/→ seek 10s · ↑/↓ volume · [ ] speed")
        shortcuts_hint.setObjectName("status")
        shortcuts_hint.setWordWrap(True)
        v.addWidget(shortcuts_hint)

        self.skip_toggle = QCheckBox("Skip intro/outro (ani-skip)")
        self.skip_toggle.setChecked(True)
        self.skip_toggle.toggled.connect(lambda v_: setattr(self, "skip_intro", v_))
        v.addWidget(self.skip_toggle)

        return w

    def _seek_released(self):
        self._seeking = False
        self.player.seek(self.seek_slider.value())

    def _go_back_from_player(self):
        self._save_position()
        self._stop_playback()
        # offline-library playback never touches self.eplist at all (no
        # source fetch happens), so it can be left showing whatever show
        # was last browsed normally -- hardcoding eplist here sent
        # "Back" to a stale, unrelated episode list (reported directly:
        # played Chainsaw Man from the library, Back showed Slime's
        # episode list from an earlier session). Return to wherever this
        # playback session actually started from instead.
        self.stack.setCurrentWidget(self._player_return_page)

    def _play_episode(self, ep):
        self._player_return_page = self.eplist
        self.current_ep_no = ep["ep_no"]
        self.current_ep_display = ep["ep_no"]
        self.current_ep_ref = ep["ep_ref"]
        self.current_skip = {}
        self.skipped_segments = set()
        self._advancing = False
        self._synced_this_ep = False
        self.now_title.setText(f"{self.anime_title}\nEpisode {ep['ep_no']}")
        self.stack.setCurrentWidget(self.player_page)

        # already downloaded -- play the local file instead of fetching
        # a stream (and definitely instead of downloading it again).
        # Keeps current_ep_no as the real episode number (not "offline"
        # like the standalone library uses) so resume/auto-advance/
        # history/RPC all keep working normally from here.
        if download_manager.is_downloaded(self.anime_title, ep["ep_no"]):
            self._play_downloaded_episode(ep["ep_no"])
            return

        self.now_status.setText("fetching stream…")
        src = SOURCES[self.source_name]
        self._link_worker = Worker(src.watch, ep["ep_ref"], self.mode)
        self._link_worker.done.connect(self._got_stream)
        self._link_worker.failed.connect(lambda e: self.now_status.setText(f"failed: {e}"))
        self._link_worker.start()

        if self.source_name == "anidb" and self.skip_intro and platform_utils.find_ani_skip():
            self._skip_worker = Worker(self._fetch_ani_skip_times, ep["ep_no"])
            self._skip_worker.done.connect(lambda t: setattr(self, "current_skip", t or {}))
            self._skip_worker.start()

    def _play_downloaded_episode(self, ep_no):
        path = download_manager.dest_path(self.anime_title, ep_no)
        positions = state.load_positions()
        key = state.position_key(self.source_name, self.anime_title, ep_no)
        resume_at = positions.get(key)
        try:
            self.player.play(
                path,
                title=f"{self.anime_title} Episode {ep_no}",
                start_seconds=resume_at if resume_at and resume_at > state.RESUME_MIN_SECONDS else None,
            )
        except (OSError, FileNotFoundError):
            self.now_status.setText(f"{self.player_name} not found -- install it and try again")
            return
        self._apply_saved_volume()
        self._apply_saved_speed()
        self.now_status.setText("playing (downloaded)" + (f" (resuming from {int(resume_at)}s)" if resume_at else ""))
        state.update_history(self.source_name, self.anime_id, self.anime_title, ep_no)
        state.save_last_session(self.source_name, self.anime_id, self.anime_title, ep_no)
        self._refresh_history()
        self._update_presence()

    def _fetch_ani_skip_times(self, ep_no):
        import re
        if self.mal_id is None:
            self.mal_id = anidb_source.mal_id_for(self.anime_id)
        if not self.mal_id:
            return {}
        canon = str(next((i for i, e in enumerate(self.episode_maps, 1) if e["ep_no"] == ep_no), 1))
        out = subprocess.run(
            [platform_utils.find_ani_skip() or "ani-skip", "-i", self.mal_id, "-e", canon],
            capture_output=True, text=True, timeout=8,
        ).stdout
        vals = {k: float(v) for k, v in re.findall(r"skip-(op_start|op_end|ed_start|ed_end)=([\d.]+)", out)}
        times = {}
        if "op_start" in vals and "op_end" in vals:
            times["op"] = (vals["op_start"], vals["op_end"])
        if "ed_start" in vals and "ed_end" in vals:
            times["ed"] = (vals["ed_start"], vals["ed_end"])
        return times

    def _got_stream(self, watch_result):
        if not watch_result:
            self.now_status.setText("no sources found")
            return
        if watch_result.get("skip"):
            self.current_skip = watch_result["skip"]

        positions = state.load_positions()
        key = state.position_key(self.source_name, self.anime_title, self.current_ep_no)
        resume_at = positions.get(key)

        try:
            self.player.play(
                watch_result["url"],
                title=f"{self.anime_title} Episode {self.current_ep_no}",
                referer=watch_result.get("referer"),
                start_seconds=resume_at if resume_at and resume_at > state.RESUME_MIN_SECONDS else None,
            )
        except (OSError, FileNotFoundError):
            # e.g. VLC/mpv isn't installed -- this used to be an uncaught
            # exception in a Qt slot instead of a message the user could
            # actually act on.
            self.now_status.setText(
                f"{self.player_name} not found -- install it "
                f"({'videolan.org/vlc' if self.player_name == 'vlc' else 'mpv.io'}) and try again"
            )
            return
        self._apply_saved_volume()
        self._apply_saved_speed()
        self.now_status.setText("playing" + (f" (resuming from {int(resume_at)}s)" if resume_at else ""))
        state.update_history(self.source_name, self.anime_id, self.anime_title, self.current_ep_no)
        state.save_last_session(self.source_name, self.anime_id, self.anime_title, self.current_ep_no)
        self._refresh_history()
        self._update_presence()

    def _play_torrent(self, release, download_fully):
        self._player_return_page = self.home  # nyaa has no episode-list page at all
        self.anime_title = release["title"]
        self.current_ep_no = "1"
        self.current_ep_display = "1"
        self.now_title.setText(release["title"])
        self.now_status.setText("starting torrent engine…")
        self.stack.setCurrentWidget(self.player_page)
        self._torrent_worker = Worker(self._start_torrent, release, download_fully)
        self._torrent_worker.done.connect(self._torrent_ready)
        self._torrent_worker.failed.connect(lambda e: self.now_status.setText(f"failed: {e}"))
        self._torrent_worker.start()

    def _start_torrent(self, release, download_fully):
        if not self.torrent_engine.ensure_running():
            raise RuntimeError("qbittorrent-nox failed to start")
        info_hash = self.torrent_engine.add_magnet(release["magnet"])
        if not info_hash:
            raise RuntimeError("failed to add magnet")
        percent = 100.0 if download_fully else torrent_backend.STREAM_BUFFER_PERCENT
        path = self.torrent_engine.wait_for_buffer(info_hash, percent=percent, timeout=600 if download_fully else 120)
        return path

    def _torrent_ready(self, path):
        if not path:
            self.now_status.setText("torrent buffering timed out")
            return
        try:
            self.player.play(path, title=self.anime_title)
        except (OSError, FileNotFoundError):
            self.now_status.setText(f"{self.player_name} not found -- install it and try again")
            return
        self._apply_saved_volume()
        self._apply_saved_speed()
        self.now_status.setText("playing (streaming from torrent)")

    def _poll_player(self):
        # orphan-window cleanup has to run regardless of which page is
        # showing -- the dashboard's whole point is that it's natural to
        # be back browsing Home while VLC keeps playing in the
        # background. This used to be gated behind the player-page
        # check below, which meant closing VLC's window via SUPER+Q
        # while on any other page left the process orphaned forever
        # (reported directly): nothing was polling it to notice.
        if hasattr(self.player, "window_gone") and self.player.window_gone():
            # e.g. VLC closed via SUPER+Q -- its -I dummy process doesn't
            # exit on its own when that happens (confirmed directly), so
            # without this it'd just linger as an orphan forever.
            self._stop_playback()
            if self.stack.currentWidget() is self.player_page:
                self.now_status.setText("player window closed")
            return

        if self.stack.currentWidget() is not self.player_page:
            return
        self._update_download_button_state()
        status = self.player.get_status()
        if not status:
            return
        pos, dur, paused = status["time"], status["duration"], status["paused"]

        if not self._seeking:
            self.time_label.setText(fmt_time(pos))
            if dur:
                self.seek_slider.setRange(0, int(dur))
                self.seek_slider.setValue(int(pos))
                self.dur_label.setText(fmt_time(dur))
        self.pause_btn.setIcon(self._icons["play"] if paused else self._icons["pause"])
        self.pause_btn.setText("Play" if paused else "Pause")
        self.now_status.setText("paused" if paused else "playing")

        for name, (start, end) in self.current_skip.items():
            if name in self.skipped_segments:
                continue
            if start <= pos < end:
                self.skipped_segments.add(name)
                self.player.seek(end)
                self.now_status.setText(f"skipped {'intro' if name == 'op' else 'outro'}")

        if self.current_ep_no and self.current_ep_no != "offline":
            key = state.position_key(self.source_name, self.anime_title, self.current_ep_no)
            positions = state.load_positions()
            if dur and pos > dur - state.RESUME_END_MARGIN:
                if positions.pop(key, None) is not None:
                    state.save_positions(positions)
                self._sync_to_anilist()
            elif pos > state.RESUME_MIN_SECONDS:
                if positions.get(key) != pos:
                    positions[key] = pos
                    state.save_positions(positions)

        # auto-advance: nothing detected the episode actually finishing
        # before -- playback would just sit there at the end with no
        # transition to the next one. Position-based (within 1.5s of the
        # end) rather than trusting a specific "ended" state string from
        # either backend, which isn't consistently exposed by both.
        # `dur > 60` guards against a transient/bogus status read during
        # a stream's initial buffering (VLC can briefly report a tiny
        # placeholder duration before the real one loads) -- without it,
        # a single bad read right after launch satisfied pos>=dur-1.5 on
        # its own and cascaded straight through several episodes in a
        # row (confirmed directly: reported jumping 25->26->27 on a
        # streamed episode, never on a downloaded/offline one, which is
        # explicitly exempt from this branch entirely).
        if (
            dur and dur > 60 and pos >= dur - 1.5
            and self.current_ep_no not in (None, "offline")
            and self.episode_maps
            and not self._advancing
        ):
            self._advancing = True
            self._play_next()

    def _sync_to_anilist(self):
        # this branch of _poll_player re-fires on every poll tick (1s)
        # for as long as playback sits inside the last RESUME_END_MARGIN
        # seconds -- without a per-episode guard this would queue a
        # fresh GraphQL mutation every second during the outro/credits.
        if self._synced_this_ep or not self.prefs.get("anilist_sync") or self.source_name == "nyaa":
            return
        self._synced_this_ep = True
        self._sync_worker = Worker(tracker.update_progress, self.anime_title, self.current_ep_no)
        self._sync_worker.start()

    def _toggle_pause(self):
        self.player.toggle_pause()

    def _on_volume_changed(self, value):
        if hasattr(self.player, "set_volume"):
            self.player.set_volume(value)
        state.save_prefs(volume=value)
        self.volume_pct_label.setText(f"{value}%")

    def _apply_saved_volume(self):
        if hasattr(self.player, "set_volume"):
            self.player.set_volume(self.prefs.get("volume", 100))

    def _on_speed_changed(self, text):
        rate = float(text.rstrip("x"))
        if hasattr(self.player, "set_speed"):
            self.player.set_speed(rate)
        state.save_prefs(speed=rate)

    def _apply_saved_speed(self):
        if hasattr(self.player, "set_speed"):
            self.player.set_speed(self.prefs.get("speed", 1.0))

    def _update_track_buttons_visibility(self):
        supported = hasattr(self.player, "cycle_subtitle")
        self.sub_btn.setVisible(supported)
        self.audio_btn.setVisible(supported)

    def _play_next(self):
        idx = next((i for i, e in enumerate(self.episode_maps) if e["ep_no"] == self.current_ep_no), None)
        if idx is not None and idx + 1 < len(self.episode_maps):
            self._play_episode(self.episode_maps[idx + 1])
        else:
            self.now_status.setText("no next episode")

    def _play_previous(self):
        idx = next((i for i, e in enumerate(self.episode_maps) if e["ep_no"] == self.current_ep_no), None)
        if idx is not None and idx > 0:
            self._play_episode(self.episode_maps[idx - 1])
        else:
            self.now_status.setText("no previous episode")

    def _save_position(self):
        if not self.anime_title or not self.current_ep_no or self.current_ep_no == "offline":
            return
        status = self.player.get_status()
        if not status:
            return
        pos, dur = status["time"], status["duration"]
        key = state.position_key(self.source_name, self.anime_title, self.current_ep_no)
        positions = state.load_positions()
        if dur and pos > dur - state.RESUME_END_MARGIN:
            positions.pop(key, None)
        elif pos > state.RESUME_MIN_SECONDS:
            positions[key] = pos
        state.save_positions(positions)

    def _stop_playback(self):
        self.player.stop()

    def _download_current_episode(self):
        if self.source_name == "nyaa" or self.current_ep_no in (None, "offline") or not self.current_ep_ref:
            self.now_status.setText("can't download this (torrent/offline sessions aren't queued the same way)")
            return
        self._queue_download({"ep_no": self.current_ep_no, "ep_ref": self.current_ep_ref})
        self.now_status.setText(f"queued episode {self.current_ep_no} for download")

    def _update_download_button_state(self):
        # was always shown as an actionable "Download this episode"
        # button regardless of whether it already was downloaded --
        # reported directly: playing an already-downloaded episode still
        # offered to download it again.
        already = (
            self.current_ep_no not in (None, "offline")
            and self.source_name != "nyaa"
            and self.anime_title
            and download_manager.is_downloaded(self.anime_title, self.current_ep_no)
        )
        self.download_current_btn.setEnabled(not already)
        self.download_current_btn.setText("Already downloaded" if already else "Download this episode")

    # ---------- discord rpc ----------

    def _on_rpc_toggle(self, checked):
        self.rpc_enabled = checked
        if checked:
            self._update_presence()
        else:
            self.presence.disconnect()

    def _on_page_changed(self, _index):
        self._update_presence()
        self._sync_nav_highlight()
        if self.stack.currentWidget() is self.home and not self._compact_mode:
            self._load_dashboard_recommendations()

    def _update_presence(self):
        if not self.rpc_enabled:
            return
        current = self.stack.currentWidget()
        if current is self.home:
            self.presence.browsing("Searching for something to watch")
        elif current is self.eplist:
            self.presence.browsing(f"Picking an episode of {self.anime_title}"[:128])
        elif current is self.player_page and self.anime_title and self.current_ep_no:
            status = self.player.get_status()
            if status:
                # current_ep_no is "offline" (a literal sentinel string,
                # not a real episode number) for playback started from
                # the standalone Offline Library, which has no source
                # fetch to know a "real" episode context from -- shown
                # as-is here it read as "Watching · Episode offline"
                # (reported directly). current_ep_display carries the
                # actual number for presence/display purposes while
                # current_ep_no keeps opting offline sessions out of
                # resume/auto-advance/sync logic that doesn't apply.
                ep_display = self.current_ep_display or self.current_ep_no
                self.presence.watching(self.anime_title, ep_display, status["time"], status["duration"], status["paused"])

    def keyPressEvent(self, event):
        if self.stack.currentWidget() is self.player_page:
            key = event.key()
            if key == Qt.Key.Key_Space:
                self._toggle_pause()
                return
            if key == Qt.Key.Key_Right:
                self.player.seek(self.seek_slider.value() + 10)
                return
            if key == Qt.Key.Key_Left:
                self.player.seek(max(0, self.seek_slider.value() - 10))
                return
            if key == Qt.Key.Key_Up:
                self.volume_slider.setValue(min(150, self.volume_slider.value() + 5))
                return
            if key == Qt.Key.Key_Down:
                self.volume_slider.setValue(max(0, self.volume_slider.value() - 5))
                return
            if key == Qt.Key.Key_BracketRight:
                self._step_speed(1)
                return
            if key == Qt.Key.Key_BracketLeft:
                self._step_speed(-1)
                return
        super().keyPressEvent(event)

    def _step_speed(self, direction):
        options = [self.speed_combo.itemText(i) for i in range(self.speed_combo.count())]
        idx = options.index(self.speed_combo.currentText())
        idx = max(0, min(len(options) - 1, idx + direction))
        self.speed_combo.setCurrentText(options[idx])

    def _cleanup_on_exit(self):
        """Shared by closeEvent and the SIGTERM handler in main() --
        Hyprland's killactive (bound to SUPER+Q) isn't guaranteed to send
        a graceful window-close request Qt turns into a closeEvent; it
        may just kill the process outright, in which case closeEvent()
        never fires and the RPC daemon handoff silently never happens
        (reported directly: closing via SUPER+Q while VLC kept playing
        left presence with nothing updating it). SIGTERM can still be
        caught even when the close event isn't delivered."""
        self._save_position()
        player_alive = self.player.is_running() if hasattr(self.player, "is_running") else False
        if not player_alive:
            status = self.player.get_status() if hasattr(self.player, "get_status") else None
            player_alive = status is not None
        self.presence.disconnect()
        if player_alive and self.rpc_enabled:
            spawn_rpc_daemon(self.player_name)

    def closeEvent(self, event):
        self._cleanup_on_exit()
        super().closeEvent(event)


def main():
    QGuiApplication.setDesktopFileName("aniani-gui")
    app = QApplication(sys.argv)
    app.setApplicationName("aniani-gui")
    # Fusion is Qt's own cross-platform style -- popups (QComboBox's
    # dropdown especially) are separate top-level windows that don't
    # reliably pick up a QSS background under the native/Wayland-
    # integrated style, and render transparent instead. Fusion renders
    # everything through Qt itself, sidestepping that.
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)
    win = AniAni()
    win.show()

    if not platform_utils.WINDOWS:
        # safety net for when Hyprland's killactive (SUPER+Q) kills the
        # process directly instead of sending a graceful close request --
        # closeEvent() doesn't fire in that case, so the RPC daemon
        # handoff would just silently never happen. SIGTERM can still be
        # caught even then; SIGKILL can't be, but killactive uses TERM.
        def _on_sigterm(signum, frame):
            win._cleanup_on_exit()
            app.quit()
        signal.signal(signal.SIGTERM, _on_sigterm)

    sys.exit(app.exec())


if __name__ == "__main__":
    # in a frozen PyInstaller exe, sys.executable IS aniani.exe -- there's
    # no separate python.exe or loose rpc_daemon.py file to point spawn_
    # rpc_daemon() at. Re-invoking ourselves with a mode flag handles both
    # the frozen exe and normal `python3 gui.py` dev runs the same way.
    if "--rpc-daemon" in sys.argv:
        sys.argv.remove("--rpc-daemon")
        import rpc_daemon
        rpc_daemon.main()
    else:
        main()
