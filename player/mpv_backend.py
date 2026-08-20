"""Drives mpv over its JSON IPC channel. Kept as an alternative to VLC in
case a given stream/source behaves better with one or the other.

mpv's --input-ipc-server means a Unix domain socket on Linux/macOS, but
a named pipe (\\\\.\\pipe\\name) on Windows -- there's no real
AF_UNIX-equivalent there. Both are wrapped behind the same sendall()/
recv_line() shape so the rest of this file (and get_status/seek/etc.)
doesn't need to know which platform it's on. The Windows path opens the
pipe as a plain binary file (a well-known trick -- CreateFile handles
named pipes as files, and Python's open() calls through to it -- avoids
needing pywin32 as a dependency) and reads lines off a background
thread since a plain file handle has no socket-style timeout.
"""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_utils  # noqa: E402

PIPE_PATH = platform_utils.mpv_ipc_path()
MPV_BIN = platform_utils.find_mpv() or "mpv"


class _UnixConn:
    def __init__(self):
        self.sock = None

    def connect(self, path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(path)
        self.sock = s

    def sendall(self, data):
        self.sock.sendall(data)

    def recv(self, n):
        return self.sock.recv(n)

    def close(self):
        self.sock.close()


class _WindowsPipeConn:
    """Wraps a Windows named pipe as a plain file handle, with a
    background reader thread feeding a queue so recv() can still honor
    a timeout the way socket.recv() does."""

    def __init__(self):
        self.f = None
        self._q = queue.Queue()
        self._stop = False

    def connect(self, path):
        self.f = open(path, "r+b", buffering=0)
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()

    def _reader(self):
        while not self._stop:
            try:
                chunk = self.f.read(4096)
            except OSError:
                break
            if not chunk:
                break
            self._q.put(chunk)

    def sendall(self, data):
        self.f.write(data)

    def recv(self, n, timeout=1):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return b""

    def close(self):
        self._stop = True
        if self.f:
            try:
                self.f.close()
            except OSError:
                pass


class MpvPlayer:
    def __init__(self):
        self.proc = None
        self._conn = None

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def play(self, url, title=None, referer=None, start_seconds=None):
        self.stop()
        if not platform_utils.WINDOWS:
            try:
                os.remove(PIPE_PATH)
            except OSError:
                pass
        args = [MPV_BIN, f"--input-ipc-server={PIPE_PATH}"]
        if title:
            args.append(f"--force-media-title={title}")
        if referer:
            args.append(f"--http-header-fields=Referer: {referer}")
        if start_seconds:
            args.append(f"--start={start_seconds}")
        args.append(url)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        self._connect()

    def _connect(self, attempts=6):
        for _ in range(attempts):
            try:
                conn = _WindowsPipeConn() if platform_utils.WINDOWS else _UnixConn()
                conn.connect(PIPE_PATH)
                self._conn = conn
                return
            except (OSError, ConnectionError):
                time.sleep(0.5)

    def _get(self, name):
        if not self._conn:
            return None
        try:
            self._conn.sendall(json.dumps({"command": ["get_property", name], "request_id": 1}).encode() + b"\n")
            buf = b""
            deadline = time.time() + 1
            while time.time() < deadline:
                chunk = self._conn.recv(4096, timeout=max(0.05, deadline - time.time())) if platform_utils.WINDOWS else self._conn.recv(4096)
                if not chunk:
                    if platform_utils.WINDOWS:
                        continue  # queue just empty this tick, not closed
                    return None
                buf += chunk
                for line in buf.split(b"\n"):
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("request_id") == 1:
                        return msg.get("data") if msg.get("error") == "success" else None
        except (OSError, ConnectionError, socket.timeout):
            return None
        return None

    def _set(self, name, value):
        if not self._conn:
            return
        try:
            self._conn.sendall(json.dumps({"command": ["set_property", name, value]}).encode() + b"\n")
        except OSError:
            pass

    def get_status(self):
        if not self._conn:
            return None
        pos = self._get("time-pos")
        if pos is None:
            return None
        return {
            "time": pos,
            "duration": self._get("duration") or 0,
            "paused": bool(self._get("pause")),
            "connected": True,
        }

    def set_pause(self, paused):
        self._set("pause", paused)

    def toggle_pause(self):
        cur = self._get("pause")
        self._set("pause", not cur)

    def seek(self, seconds):
        self._set("time-pos", seconds)

    def stop(self):
        if self._conn:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
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
