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
import subprocess
import sys

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QStackedWidget, QCheckBox,
    QSlider, QComboBox, QProgressBar,
)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "player"))

import anidb_source
import yuma_source
import nyaa_source
import vlc_backend
import mpv_backend
import torrent_backend
import discord_presence
import state
import download_manager
import platform_utils

DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON_PID_PATH = platform_utils.temp_path("aniani-rpc-daemon.pid")

SOURCES = {"anidb": anidb_source, "yuma": yuma_source}

DARK_QSS = """
QWidget { background: #0d0f12; color: #e8ebf0; font-size: 13px; }

QLabel#brand { font-size: 20px; font-weight: 700; color: #f2f4f7; padding: 2px 0 4px 0; }
QLabel#heading { color: #7d8590; font-size: 10.5px; font-weight: 600; letter-spacing: 1px; }
QLabel#title { font-size: 16px; font-weight: 600; }
QLabel#status { color: #8b93a1; font-size: 12px; }

QLineEdit {
    background: #14171c; border: 1px solid #232830; border-radius: 8px;
    padding: 9px 10px; selection-background-color: #8fb6ff; selection-color: #0d0f12;
}
QLineEdit:focus { border-color: #8fb6ff; }

QComboBox {
    background: #14171c; border: 1px solid #232830; border-radius: 8px; padding: 7px 10px;
}
QComboBox:hover { border-color: #3a4048; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #171a20; border: 1px solid #232830; border-radius: 6px;
    selection-background-color: #232830; selection-color: #8fb6ff; outline: none; padding: 4px;
}

QListWidget {
    background: #12151a; border: 1px solid #1e222a; border-radius: 10px; padding: 4px;
    outline: none;
}
QListWidget::item { padding: 9px 8px; border-radius: 6px; margin: 1px 0; }
QListWidget::item:hover { background: #181c23; }
QListWidget::item:selected { background: #1c2530; color: #8fb6ff; }

QPushButton {
    background: #171a20; border: 1px solid #262b33; border-radius: 8px;
    padding: 8px 14px; font-weight: 500;
}
QPushButton:hover { border-color: #8fb6ff; color: #cfe0ff; }
QPushButton:pressed { background: #101317; }
QPushButton:disabled { color: #4a4f57; border-color: #1c2027; }
QPushButton#primary { background: #8fb6ff; color: #0d0f12; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #a3c4ff; color: #0d0f12; }
QPushButton#primary:pressed { background: #7ba3ee; }

QCheckBox { spacing: 8px; padding: 2px 0; }
QCheckBox::indicator {
    width: 16px; height: 16px; border-radius: 4px; border: 1px solid #2b313b; background: #14171c;
}
QCheckBox::indicator:checked { background: #8fb6ff; border-color: #8fb6ff; }

QSlider::groove:horizontal { background: #232830; height: 4px; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #8fb6ff; height: 4px; border-radius: 2px; }
QSlider::handle:horizontal { background: #e8ebf0; width: 13px; height: 13px; margin: -5px 0; border-radius: 7px; }
QSlider::handle:horizontal:hover { background: #8fb6ff; }

QProgressBar { background: #14171c; border: 1px solid #232830; border-radius: 6px; text-align: center; height: 16px; }
QProgressBar::chunk { background: #8fb6ff; border-radius: 5px; }

QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #262b33; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #363c46; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def fmt_time(seconds):
    s = max(0, int(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


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
        proc = platform_utils.popen_detached(
            [sys.executable, os.path.join(DIR, "rpc_daemon.py"), "--backend", backend_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(DAEMON_PID_PATH, "w") as f:
            f.write(str(proc.pid))
    except OSError:
        pass


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


class AniAni(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("aniani")
        self.resize(400, 620)

        self.presence = discord_presence.DiscordPresence()
        self.rpc_enabled = True
        self.skip_intro = True

        self.source_name = "anidb"
        self.player_name = "vlc"
        self.player = vlc_backend.VlcPlayer()

        self.anime_id = None
        self.anime_title = None
        self.mode = "sub"
        self.episode_maps = []
        self.current_ep_no = None
        self.current_ep_ref = None
        self.mal_id = None
        self.current_skip = {}
        self.skipped_segments = set()
        self._seeking = False

        self.torrent_engine = torrent_backend.TorrentEngine()

        self.stack = QStackedWidget()
        self.home = self._build_home()
        self.eplist = self._build_episode_list()
        self.player_page = self._build_player_page()
        self.downloads_page = self._build_downloads_page()
        self.library_page = self._build_library_page()
        for w in (self.home, self.eplist, self.player_page, self.downloads_page, self.library_page):
            self.stack.addWidget(w)
        self.stack.currentChanged.connect(self._on_page_changed)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(self.stack)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_player)
        self.poll_timer.start(1000)

        self.rpc_timer = QTimer(self)
        self.rpc_timer.timeout.connect(self._update_presence)
        self.rpc_timer.start(5000)

        self.download_jobs = []

        kill_lingering_rpc_daemon()
        self._refresh_history()
        self._update_presence()
        self._restore_last_session()

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

    # ---------- home ----------

    def _build_home(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)

        brand = QLabel("aniani")
        brand.setObjectName("brand")
        v.addWidget(brand)

        prefs_row = QHBoxLayout()
        prefs_row.setSpacing(10)
        source_col = QVBoxLayout()
        source_col.setSpacing(3)
        source_label = QLabel("SOURCE")
        source_label.setObjectName("heading")
        self.source_combo = QComboBox()
        self.source_combo.addItems(["anidb", "yuma", "nyaa (torrent)"])
        self.source_combo.currentTextChanged.connect(self._on_source_changed)
        source_col.addWidget(source_label)
        source_col.addWidget(self.source_combo)
        player_col = QVBoxLayout()
        player_col.setSpacing(3)
        player_label = QLabel("PLAYER")
        player_label.setObjectName("heading")
        self.player_combo = QComboBox()
        self.player_combo.addItems(["vlc", "mpv"])
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
        self.search_box.returnPressed.connect(self._do_search)
        search_btn = QPushButton("Search")
        search_btn.setObjectName("primary")
        search_btn.clicked.connect(self._do_search)
        search_row.addWidget(self.search_box, 1)
        search_row.addWidget(search_btn)
        v.addLayout(search_row)

        nav_row = QHBoxLayout()
        nav_row.setSpacing(8)
        dl_btn = QPushButton("⬇ Downloads")
        dl_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.downloads_page))
        lib_btn = QPushButton("▤ Offline Library")
        lib_btn.clicked.connect(self._open_library)
        nav_row.addWidget(dl_btn)
        nav_row.addWidget(lib_btn)
        v.addLayout(nav_row)

        cont_label = QLabel("CONTINUE")
        cont_label.setObjectName("heading")
        v.addWidget(cont_label)
        self.continue_list = QListWidget()
        self.continue_list.setMaximumHeight(140)
        self.continue_list.itemActivated.connect(self._continue_selected)
        v.addWidget(self.continue_list)

        res_label = QLabel("RESULTS")
        res_label.setObjectName("heading")
        v.addWidget(res_label)
        self.results_list = QListWidget()
        self.results_list.itemActivated.connect(self._result_selected)
        v.addWidget(self.results_list)

        self.status = QLabel("")
        self.status.setObjectName("status")
        v.addWidget(self.status)
        return w

    def _on_source_changed(self, text):
        self.source_name = "nyaa" if text.startswith("nyaa") else text

    def _on_player_changed(self, text):
        if self.player.is_running():
            self.player.stop()
        self.player_name = text
        self.player = vlc_backend.VlcPlayer() if text == "vlc" else mpv_backend.MpvPlayer()

    def _refresh_history(self):
        self.continue_list.clear()
        for e in reversed(state.read_history()):
            item = QListWidgetItem(f"[{e['source']}] {e['anime_title']}  ·  ep {e['ep_no']}")
            item.setData(Qt.ItemDataRole.UserRole, e)
            self.continue_list.addItem(item)

    def _do_search(self):
        q = self.search_box.text().strip()
        if not q:
            return
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
            item = QListWidgetItem(r["title"])
            item.setData(Qt.ItemDataRole.UserRole, r)
            self.results_list.addItem(item)

    def _nyaa_search_done(self, results):
        self.status.setText(f"{len(results)} releases" if results else "no results (nyaa.si can be flaky -- try again)")
        for r in results:
            item = QListWidgetItem(f"{r['title'][:60]}  ·  {r['size']}  ·  {r['seeders']} seeders")
            item.setData(Qt.ItemDataRole.UserRole, r)
            self.results_list.addItem(item)

    def _result_selected(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if self.source_name == "nyaa":
            self._play_torrent(data, download_fully=False)
        else:
            self._open_anime(data["id"], data["title"])

    def _continue_selected(self, item):
        e = item.data(Qt.ItemDataRole.UserRole)
        self.source_name = e["source"]
        self._open_anime(e["anime_id"], e["anime_title"], jump_to_ep=e["ep_no"])

    def _open_library(self):
        self._populate_library()
        self.stack.setCurrentWidget(self.library_page)

    # ---------- episode list ----------

    def _build_episode_list(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        back = QPushButton("← Back")
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
        self.current_ep_no = ep_no
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
        job = download_manager.DownloadJob(self.anime_title, ep["ep_no"], None, None)
        self.download_jobs.append(job)
        self._refresh_downloads_list()
        src = SOURCES[self.source_name]
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
        worker = Worker(download_manager.run_job, job, lambda j: self._refresh_downloads_list())
        worker.start()
        self._dl_run_workers = getattr(self, "_dl_run_workers", [])
        self._dl_run_workers.append(worker)

    def _fail_job(self, job, err):
        job.status = "failed"
        self._refresh_downloads_list()

    # ---------- downloads page ----------

    def _build_downloads_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        back = QPushButton("← Back")
        back.clicked.connect(lambda: self.stack.setCurrentWidget(self.home))
        top.addWidget(back)
        title = QLabel("Downloads")
        title.setObjectName("title")
        top.addWidget(title, 1)
        v.addLayout(top)
        self.downloads_list = QListWidget()
        v.addWidget(self.downloads_list)
        return w

    def _refresh_downloads_list(self):
        self.downloads_list.clear()
        for job in self.download_jobs:
            label = f"{job.anime_title} · Episode {job.ep_no} — {job.status} ({job.progress*100:.0f}%)"
            self.downloads_list.addItem(label)

    # ---------- offline library ----------

    def _build_library_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        back = QPushButton("← Back")
        back.clicked.connect(lambda: self.stack.setCurrentWidget(self.home))
        top.addWidget(back)
        title = QLabel("Offline Library")
        title.setObjectName("title")
        top.addWidget(title, 1)
        v.addLayout(top)
        self.library_list = QListWidget()
        self.library_list.itemActivated.connect(self._play_offline_selected)
        v.addWidget(self.library_list)
        return w

    def _populate_library(self):
        self.library_list.clear()
        for show in download_manager.list_downloaded():
            for ep in show["episodes"]:
                item = QListWidgetItem(f"{show['show']} — Episode {ep}")
                item.setData(Qt.ItemDataRole.UserRole, download_manager.dest_path(show["show"], ep))
                self.library_list.addItem(item)

    def _play_offline_selected(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        self.anime_title = item.text().split(" — ")[0]
        self.current_ep_no = "offline"
        self.now_title.setText(item.text())
        self.stack.setCurrentWidget(self.player_page)
        self.player.play(path)
        self.now_status.setText("playing (offline)")

    # ---------- player controls ----------

    def _build_player_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        back = QPushButton("← Back")
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
        prev_btn = QPushButton("⏮ Previous")
        prev_btn.clicked.connect(self._play_previous)
        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.setObjectName("primary")
        self.pause_btn.clicked.connect(self._toggle_pause)
        next_btn = QPushButton("Next ⏭")
        next_btn.clicked.connect(self._play_next)
        ctrl.addWidget(prev_btn)
        ctrl.addWidget(self.pause_btn)
        ctrl.addWidget(next_btn)
        v.addLayout(ctrl)

        row2 = QHBoxLayout()
        stop_btn = QPushButton("⏹ Stop")
        stop_btn.clicked.connect(self._stop_playback)
        self.download_current_btn = QPushButton("⬇ Download this episode")
        self.download_current_btn.clicked.connect(self._download_current_episode)
        row2.addWidget(stop_btn)
        row2.addWidget(self.download_current_btn)
        v.addLayout(row2)
        v.addStretch()

        self.rpc_toggle = QCheckBox("Discord Rich Presence")
        self.rpc_toggle.setChecked(True)
        self.rpc_toggle.toggled.connect(self._on_rpc_toggle)
        v.addWidget(self.rpc_toggle)

        self.skip_toggle = QCheckBox("Skip intro (ani-skip / built-in)")
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
        self.stack.setCurrentWidget(self.eplist)

    def _play_episode(self, ep):
        self.current_ep_no = ep["ep_no"]
        self.current_ep_ref = ep["ep_ref"]
        self.current_skip = {}
        self.skipped_segments = set()
        self.now_title.setText(f"{self.anime_title}\nEpisode {ep['ep_no']}")
        self.now_status.setText("fetching stream…")
        self.stack.setCurrentWidget(self.player_page)
        src = SOURCES[self.source_name]
        self._link_worker = Worker(src.watch, ep["ep_ref"], self.mode)
        self._link_worker.done.connect(self._got_stream)
        self._link_worker.failed.connect(lambda e: self.now_status.setText(f"failed: {e}"))
        self._link_worker.start()

        if self.source_name == "anidb" and self.skip_intro and platform_utils.find_ani_skip():
            self._skip_worker = Worker(self._fetch_ani_skip_times, ep["ep_no"])
            self._skip_worker.done.connect(lambda t: setattr(self, "current_skip", t or {}))
            self._skip_worker.start()

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

        self.player.play(
            watch_result["url"],
            title=f"{self.anime_title} Episode {self.current_ep_no}",
            referer=watch_result.get("referer"),
            start_seconds=resume_at if resume_at and resume_at > state.RESUME_MIN_SECONDS else None,
        )
        self.now_status.setText("playing" + (f" (resuming from {int(resume_at)}s)" if resume_at else ""))
        state.update_history(self.source_name, self.anime_id, self.anime_title, self.current_ep_no)
        state.save_last_session(self.source_name, self.anime_id, self.anime_title, self.current_ep_no)
        self._refresh_history()
        self._update_presence()

    def _play_torrent(self, release, download_fully):
        self.anime_title = release["title"]
        self.current_ep_no = "1"
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
        self.player.play(path, title=self.anime_title)
        self.now_status.setText("playing (streaming from torrent)")

    def _poll_player(self):
        if self.stack.currentWidget() is not self.player_page:
            return
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
        self.pause_btn.setText("▶ Play" if paused else "⏸ Pause")
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
            elif pos > state.RESUME_MIN_SECONDS:
                if positions.get(key) != pos:
                    positions[key] = pos
                    state.save_positions(positions)

    def _toggle_pause(self):
        self.player.toggle_pause()

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

    # ---------- discord rpc ----------

    def _on_rpc_toggle(self, checked):
        self.rpc_enabled = checked
        if checked:
            self._update_presence()
        else:
            self.presence.disconnect()

    def _on_page_changed(self, _index):
        self._update_presence()

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
                self.presence.watching(self.anime_title, self.current_ep_no, status["time"], status["duration"], status["paused"])

    def closeEvent(self, event):
        self._save_position()
        player_alive = self.player.is_running() if hasattr(self.player, "is_running") else False
        if not player_alive:
            status = self.player.get_status() if hasattr(self.player, "get_status") else None
            player_alive = status is not None
        self.presence.disconnect()
        if player_alive and self.rpc_enabled:
            spawn_rpc_daemon(self.player_name)
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
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
