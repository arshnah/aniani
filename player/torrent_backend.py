"""Drives qbittorrent-nox over its WebUI REST API for the nyaa.si source.
Two modes off the same download: "stream" (sequential download + first/
last-piece priority, hand the growing file to VLC/mpv once enough of the
start has buffered) and "download" (let it finish completely, for
offline/whole-season downloads -- same mechanism, just don't stop early).
"""
import os
import re
import subprocess
import sys
import threading
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_utils  # noqa: E402

WEBUI_PORT = 8095
WEBUI_BASE = f"http://127.0.0.1:{WEBUI_PORT}"
DOWNLOAD_DIR = platform_utils.downloads_dir("aniani-torrents")
QBT_PROFILE_DIR = os.path.join(platform_utils.state_dir("aniani"), "qbt-profile")
# qbittorrent-nox has no official Windows build -- find_qbittorrent_nox()
# falls back to the regular qBittorrent GUI there, which supports the
# same flags/WebUI API but will show a window (there's no way around
# that without a nox-equivalent build; documented, not a bug)
QBT_BIN = platform_utils.find_qbittorrent_nox() or "qbittorrent-nox"
STREAM_BUFFER_PERCENT = 3.0  # start playback once this much of the torrent has downloaded

VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".webm")


class TorrentEngine:
    """One qbittorrent-nox daemon, reused across torrents in a session."""

    def __init__(self):
        self.proc = None
        self._session = requests.Session()
        # qBittorrent's WebUI validates Referer/Origin as CSRF protection
        # on every state-changing request, not just login -- set once
        # here instead of per-call.
        self._session.headers.update({"Referer": WEBUI_BASE})
        self._temp_password = None

    def ensure_running(self):
        if self._ping():
            return True
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self.proc = subprocess.Popen(
            # --confirm-legal-notice: qbittorrent-nox otherwise blocks on
            # a first-run "accept this? [y/n]" stdin prompt under a fresh
            # --profile dir, which never gets answered since stdout/stderr
            # (and stdin, implicitly) are piped away -- it just hangs and
            # ensure_running() times out with "failed to start". Confirmed
            # directly: ran it foreground with stdin closed and hit exactly
            # that prompt.
            [QBT_BIN, f"--webui-port={WEBUI_PORT}", f"--profile={QBT_PROFILE_DIR}", "--confirm-legal-notice"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        # WebUI auth is on by default with a random temp password printed
        # to stdout on every launch (not persisted -- a fresh one shows up
        # each time). Tried disabling auth via WebUI\LocalHostAuth=false
        # in a pre-seeded config first; qbittorrent-nox silently dropped
        # it (this qBittorrent build -- 5.2.3 -- likely hardened that
        # setting away). Logging in with the real temp password is the
        # version-proof fix, confirmed working directly.
        threading.Thread(target=self._watch_stdout_for_password, daemon=True).start()

        for _ in range(30):
            if self._temp_password and self._login():
                self._configure_defaults()
                return True
            time.sleep(0.5)
        return False

    def _watch_stdout_for_password(self):
        if not self.proc or not self.proc.stdout:
            return
        for line in self.proc.stdout:
            m = re.search(r"temporary password is provided for this session:\s*(\S+)", line)
            if m:
                self._temp_password = m.group(1)
                return

    def _login(self):
        try:
            r = self._session.post(
                f"{WEBUI_BASE}/api/v2/auth/login",
                data={"username": "admin", "password": self._temp_password},
                timeout=2,
            )
            # older qBittorrent docs describe 200 + body "Ok." on success;
            # this build (5.2.3) actually returns 204 with a Set-Cookie
            # header and empty body -- confirmed directly. r.ok (2xx)
            # plus a cookie actually landing covers both conventions.
            return r.ok and bool(self._session.cookies)
        except requests.RequestException:
            return False

    def _ping(self):
        try:
            r = self._session.get(f"{WEBUI_BASE}/api/v2/app/version", timeout=1)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _configure_defaults(self):
        try:
            self._session.post(f"{WEBUI_BASE}/api/v2/app/setPreferences", data={
                "json": '{"save_path": "%s"}' % DOWNLOAD_DIR.replace("\\", "\\\\"),
            }, timeout=3)
        except requests.RequestException:
            pass

    def add_magnet(self, magnet):
        """-> info_hash (lowercase) or None"""
        try:
            self._session.post(f"{WEBUI_BASE}/api/v2/torrents/add", data={
                "urls": magnet, "sequentialDownload": "true", "firstLastPiecePrio": "true",
            }, timeout=8)
        except requests.RequestException:
            return None
        # nyaa magnets are just `magnet:?xt=urn:btih:<hash>&...`
        m = re.search(r"btih:([a-fA-F0-9]+)", magnet)
        return m.group(1).lower() if m else None

    def torrent_info(self, info_hash):
        try:
            r = self._session.get(f"{WEBUI_BASE}/api/v2/torrents/info", params={"hashes": info_hash}, timeout=3)
            r.raise_for_status()
            items = r.json()
            return items[0] if items else None
        except (requests.RequestException, ValueError, IndexError):
            return None

    def files(self, info_hash):
        try:
            r = self._session.get(f"{WEBUI_BASE}/api/v2/torrents/files", params={"hash": info_hash}, timeout=3)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            return []

    def largest_video_file_path(self, info_hash, save_path):
        files = self.files(info_hash)
        video_files = [f for f in files if f["name"].lower().endswith(VIDEO_EXTS)]
        if not video_files:
            return None
        biggest = max(video_files, key=lambda f: f["size"])
        return os.path.join(save_path, biggest["name"])

    def wait_for_buffer(self, info_hash, percent=STREAM_BUFFER_PERCENT, timeout=120):
        """Blocking -- call from a worker thread. -> file path once enough
        has downloaded to safely start playback, or None on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self.torrent_info(info_hash)
            if info and info.get("progress", 0) * 100 >= percent:
                return self.largest_video_file_path(info_hash, info["save_path"])
            time.sleep(1)
        return None

    def stop(self, info_hash):
        try:
            self._session.post(f"{WEBUI_BASE}/api/v2/torrents/stop", data={"hashes": info_hash}, timeout=3)
        except requests.RequestException:
            pass
