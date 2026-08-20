"""Drives mpv over its JSON IPC socket. Kept as an alternative to VLC in
case a given stream/source behaves better with one or the other.
"""
import json
import os
import socket
import subprocess
import time

SOCKET_PATH = "/tmp/aniani-mpv.sock"


class MpvPlayer:
    def __init__(self):
        self.proc = None
        self._sock = None

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def play(self, url, title=None, referer=None, start_seconds=None):
        self.stop()
        try:
            os.remove(SOCKET_PATH)
        except OSError:
            pass
        args = ["mpv", f"--input-ipc-server={SOCKET_PATH}"]
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
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect(SOCKET_PATH)
                self._sock = s
                return
            except (OSError, ConnectionError):
                time.sleep(0.5)

    def _get(self, name):
        if not self._sock:
            return None
        try:
            self._sock.sendall(json.dumps({"command": ["get_property", name], "request_id": 1}).encode() + b"\n")
            buf = b""
            deadline = time.time() + 1
            while time.time() < deadline:
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
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
        except (OSError, ConnectionError):
            return None
        return None

    def _set(self, name, value):
        if not self._sock:
            return
        try:
            self._sock.sendall(json.dumps({"command": ["set_property", name, value]}).encode() + b"\n")
        except OSError:
            pass

    def get_status(self):
        if not self._sock:
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
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self.is_running():
            self.proc.terminate()
        self.proc = None
