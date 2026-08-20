"""Drives VLC over its HTTP control interface. Chosen over mpv's JSON IPC
as the primary backend on request -- confirmed working end to end
(state/time/length polling, pause/seek commands) against a real HLS
stream before building this.
"""
import os
import secrets
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_utils  # noqa: E402

HTTP_PORT = 9091
PASSWORD_PATH = platform_utils.temp_path("aniani-vlc-password")
VLC_BIN = platform_utils.find_vlc() or "vlc"
_PASSWORD = None  # cached per-process once read/generated


def _password():
    # must be shared across processes: the GUI launches VLC with this
    # password, and the standalone RPC daemon (spawned separately on
    # close) needs the *same* one to poll VLC's HTTP interface, or every
    # request gets rejected with 401 and get_status() silently returns
    # None. A per-process-random global broke exactly this.
    global _PASSWORD
    if _PASSWORD is not None:
        return _PASSWORD
    if os.path.exists(PASSWORD_PATH):
        try:
            with open(PASSWORD_PATH) as f:
                _PASSWORD = f.read().strip()
                if _PASSWORD:
                    return _PASSWORD
        except OSError:
            pass
    _PASSWORD = secrets.token_hex(16)
    try:
        with open(PASSWORD_PATH, "w") as f:
            f.write(_PASSWORD)
    except OSError:
        pass
    return _PASSWORD


class VlcPlayer:
    def __init__(self):
        self.proc = None
        self.base_url = f"http://127.0.0.1:{HTTP_PORT}/requests/status.json"
        self._auth = ("", _password())

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def play(self, url, title=None, referer=None, start_seconds=None):
        self.stop()
        args = [
            VLC_BIN, "-I", "dummy", "--no-video-title-show",
            "--extraintf", "http", "--http-password", _password(), "--http-port", str(HTTP_PORT),
        ]
        if referer:
            args += [f"--http-referrer={referer}"]
        if title:
            args += [f"--meta-title={title}"]
        if start_seconds:
            args += [f"--start-time={int(start_seconds)}"]
        args.append(url)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.is_running():
            self.proc.terminate()
        self.proc = None

    def _status(self, command=None, **params):
        try:
            p = dict(params)
            if command:
                p["command"] = command
            r = requests.get(self.base_url, params=p, auth=self._auth, timeout=1.5)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            return None

    def get_status(self):
        """-> {"time": s, "duration": s, "paused": bool, "connected": bool} or None"""
        data = self._status()
        if data is None:
            return None
        return {
            "time": data.get("time", 0),
            "duration": data.get("length", 0),
            "paused": data.get("state") != "playing",
            "connected": True,
        }

    def set_pause(self, paused):
        status = self._status()
        currently_paused = status is None or status.get("state") != "playing"
        if paused != currently_paused:
            self._status(command="pl_pause")

    def toggle_pause(self):
        self._status(command="pl_pause")

    def seek(self, seconds):
        self._status(command="seek", val=str(int(seconds)))
