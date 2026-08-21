"""Drives VLC over its HTTP control interface. Chosen over mpv's JSON IPC
as the primary backend on request -- confirmed working end to end
(state/time/length polling, pause/seek commands) against a real HLS
stream before building this.
"""
import os
import secrets
import socket
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_utils  # noqa: E402

PASSWORD_PATH = platform_utils.temp_path("aniani-vlc-password")
PORT_PATH = platform_utils.temp_path("aniani-vlc-port")
PID_PATH = platform_utils.temp_path("aniani-vlc-pid")
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


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _read_shared_port(default=9091):
    if os.path.exists(PORT_PATH):
        try:
            with open(PORT_PATH) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            pass
    return default


def _read_shared_pid():
    if os.path.exists(PID_PATH):
        try:
            with open(PID_PATH) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            pass
    return None


class VlcPlayer:
    def __init__(self):
        self.proc = None
        # read whatever port the currently-running VLC (if any) actually
        # used -- needed so a freshly constructed VlcPlayer (reattach on
        # relaunch, or the standalone RPC daemon) talks to the right
        # instance instead of a hardcoded port.
        self.port = _read_shared_port()
        # same idea for the pid: a freshly constructed VlcPlayer (the
        # standalone RPC daemon, in particular) never called play()
        # itself, so self.proc is always None there and window_gone()'s
        # pid check would have nothing to compare against -- meaning the
        # daemon could never detect an orphaned window on its own,
        # silently, the whole time. Sharing the pid the same way the
        # port already is fixes that.
        self._shared_pid = _read_shared_pid()
        self._auth = ("", _password())
        self._window_ever_seen = False

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/requests/status.json"

    def is_running(self):
        if self.proc is not None:
            return self.proc.poll() is None
        # no local process handle -- this is the standalone RPC daemon's
        # VlcPlayer, which never called play() itself. Fall back to
        # checking the shared pid actually exists (signal 0 doesn't kill
        # anything, just probes) instead of always reporting False,
        # which used to make window_gone() (see below) a no-op there.
        # POSIX-only -- os.kill's signal-0 existence-probe trick isn't
        # portable to Windows, and window_gone()'s only real consumer
        # (hyprland_window_exists) is already Linux/Hyprland-only.
        if self._shared_pid is None or platform_utils.WINDOWS:
            return False
        try:
            os.kill(self._shared_pid, 0)
            return True
        except OSError:
            return False

    def window_gone(self):
        """True only when we're confident VLC's window was closed (e.g.
        via SUPER+Q) but the process is still running -- see
        platform_utils.hyprland_window_exists for why -I dummy needs
        this. False (don't act) whenever we can't tell for sure.

        Requires having actually seen the window open at least once
        first -- confirmed directly that without this, the very first
        poll immediately after launch (before VLC's window has had time
        to map at all) reads as "gone" and kills a process that never
        even finished starting."""
        if not self.is_running():
            return False
        pid = self.proc.pid if self.proc is not None else self._shared_pid
        exists = platform_utils.hyprland_window_exists("vlc", pid=pid)
        if exists:
            self._window_ever_seen = True
            return False
        if exists is False and self._window_ever_seen:
            return True
        return False

    def play(self, url, title=None, referer=None, start_seconds=None):
        self.stop()
        self._window_ever_seen = False
        # a fixed port meant a stale VLC process left running from a
        # previous session (crash, force-quit, another test run) would
        # silently keep the port bound -- the new VLC process would
        # still launch and stay "running" per proc.poll(), but every
        # status/control request would actually hit the OLD instance
        # instead. Confirmed directly: a freshly played local file
        # reported a stream position 14 minutes in from second one,
        # because get_status() was talking to a leftover VLC from an
        # earlier test. A fresh port every launch makes that collision
        # impossible instead of just unlikely.
        self.port = _free_port()
        try:
            with open(PORT_PATH, "w") as f:
                f.write(str(self.port))
        except OSError:
            pass
        args = [
            VLC_BIN, "-I", "dummy", "--no-video-title-show",
            "--extraintf", "http", "--http-password", _password(), "--http-port", str(self.port),
        ]
        if referer:
            args += [f"--http-referrer={referer}"]
        if title:
            args += [f"--meta-title={title}"]
        if start_seconds:
            args += [f"--start-time={int(start_seconds)}"]
        args.append(url)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._shared_pid = self.proc.pid
        try:
            with open(PID_PATH, "w") as f:
                f.write(str(self.proc.pid))
        except OSError:
            pass

    def stop(self):
        # VLC under -I dummy doesn't always exit cleanly on SIGTERM,
        # especially mid-stream (network I/O cleanup can hang) --
        # reported directly: it lingers as a process after Stop/close.
        # Escalate to SIGKILL if it doesn't exit promptly instead of
        # leaving a zombie.
        if self.proc is not None:
            if self.is_running():
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            self.proc = None
        elif self.is_running():
            # is_running()'s shared-pid fallback path (see above) --
            # this is the standalone RPC daemon, which has a pid to kill
            # but no Popen handle to .terminate()/.wait() on, so the
            # same SIGTERM-then-SIGKILL escalation above has to be
            # done by hand with a plain poll loop instead of
            # subprocess.Popen.wait().
            platform_utils.kill_pid(self._shared_pid)
            for _ in range(20):  # ~2s, matching the Popen path's timeout
                time.sleep(0.1)
                try:
                    os.kill(self._shared_pid, 0)
                except OSError:
                    break  # gone
            else:
                try:
                    os.kill(self._shared_pid, 9)
                except OSError:
                    pass

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

    def set_volume(self, percent):
        """percent: 0-200 (VLC allows boosting past 100%)"""
        self._status(command="volume", val=str(int(percent * 2.56)))  # VLC's raw scale is 0-256

    def set_speed(self, rate):
        """rate: 1.0 = normal. VLC's HTTP interface exposes this directly
        as the "rate" command (see http/requests/README.txt in VLC's
        source -- lua intf maps it straight to var "rate" on the input)."""
        self._status(command="rate", val=str(rate))
