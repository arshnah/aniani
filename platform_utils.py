"""Centralizes every OS-specific decision so the rest of the codebase
stays platform-agnostic: state/download directories, temp-file paths,
external binary discovery, and detached-process spawning.

Built and reasoned through carefully, but only ever run/tested on Linux
(Arch/Hyprland) -- the Windows branches follow documented platform
conventions (%LOCALAPPDATA%, named pipes for mpv IPC, DETACHED_PROCESS
for spawning) but haven't been verified on an actual Windows machine.
Treat WINDOWS-tagged code as "should work, needs real testing."
"""
import os
import shutil
import subprocess
import sys
import tempfile

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

# Every network call across the app (AniList, anidb.app, cover-art
# downloads, Discord's own AniList lookups) goes through `requests`,
# which -- via urllib3 -- tries every address getaddrinfo() returns in
# order, IPv6 first. On a network where IPv6 is present in DNS but
# actually broken/blackholed, that means EVERY request stalls for the
# full connect timeout on the dead IPv6 address before falling back to
# IPv4 -- confirmed directly: a plain curl to AniList took 0.8s, the
# exact same request through `requests` took 9-16s, and forcing IPv4-
# only resolution brought it back to under 1s. This patches urllib3's
# address-family selection once, globally, for every module that
# imports platform_utils (which is effectively all of them) -- wrapped
# in a try/except since it reaches into a private urllib3 module that
# could change shape in a future version, and this is a pure
# performance optimization, never worth a hard crash over.
try:
    import socket
    import urllib3.util.connection as _urllib3_conn

    def _prefer_ipv4(*args, **kwargs):
        return socket.AF_INET

    _urllib3_conn.allowed_gai_family = _prefer_ipv4
except ImportError:
    pass


def state_dir(app_name):
    """Per-user persistent state (history, positions, config cache)."""
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, app_name)
    if MACOS:
        return os.path.expanduser(f"~/Library/Application Support/{app_name}")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, app_name)


def downloads_dir(app_name):
    """Where downloaded episodes/torrents land."""
    videos = os.path.join(os.path.expanduser("~"), "Videos")
    if WINDOWS:
        # %USERPROFILE%\Videos exists by default on Windows too
        videos = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Videos")
    return os.path.join(videos, app_name)


def temp_path(filename):
    """A shared cross-process scratch file (RPC daemon pidfile, VLC HTTP
    password, mpv IPC socket/pipe name) -- tempfile.gettempdir() instead
    of a hardcoded /tmp, since that's not a thing on Windows."""
    return os.path.join(tempfile.gettempdir(), filename)


def mpv_ipc_path(name="aniani-mpv"):
    """mpv's --input-ipc-server takes a named pipe path on Windows
    (\\\\.\\pipe\\<name>), not a filesystem path -- there's no real
    Unix-domain-socket equivalent there."""
    if WINDOWS:
        return f"\\\\.\\pipe\\{name}"
    return temp_path(f"{name}.sock")


def find_binary(*names):
    """First name found on PATH, trying each in order (e.g. a Linux-only
    binary name first, then a generic fallback). -> path or None."""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def find_vlc():
    found = find_binary("vlc", "vlc.exe")
    if found:
        return found
    if WINDOWS:
        for candidate in (
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ):
            if os.path.exists(candidate):
                return candidate
    return None


def find_mpv():
    found = find_binary("mpv", "mpv.exe")
    if found:
        return found
    if WINDOWS:
        # mpv has no standard Windows install location -- common ones
        # from scoop/choco/manual zip installs, best-effort only
        for candidate in (
            os.path.expanduser(r"~\scoop\apps\mpv\current\mpv.exe"),
            r"C:\Program Files\mpv\mpv.exe",
        ):
            if os.path.exists(candidate):
                return candidate
    return None


def find_ffmpeg():
    return find_binary("ffmpeg", "ffmpeg.exe")


def find_curl_impersonate():
    """anidb.app needs a Chrome-TLS-fingerprint curl build to get past
    its anti-bot check -- plain curl (even on Windows) gets a plain 403.
    curl-impersonate ships prebuilt Windows binaries under this name."""
    return find_binary("curl_chrome136", "curl_chrome136.exe", "curl-impersonate-chrome.exe")


def find_ani_skip():
    return find_binary("ani-skip", "ani-skip.exe")


def find_qbittorrent_nox():
    # qbittorrent-nox isn't packaged for Windows the way it is on Linux;
    # the regular qBittorrent GUI's WebUI (Tools > Options > Web UI) is
    # the practical equivalent there, same REST API either way
    return find_binary("qbittorrent-nox", "qbittorrent-nox.exe", "qbittorrent.exe")


def popen_detached(args, **kwargs):
    """Spawn a process that survives this one exiting -- used for the
    standalone RPC daemon handoff on close. Linux: start_new_session.
    Windows: DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP so it isn't
    killed when the parent's console/job closes."""
    if WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(args, creationflags=flags, **kwargs)
    return subprocess.Popen(args, start_new_session=True, **kwargs)


def kill_pid(pid):
    """Best-effort terminate by PID, used to clean up a lingering RPC
    daemon from a previous run. os.kill(pid, SIGTERM) already maps to
    TerminateProcess on Windows in CPython, but wrapped here so any
    future platform quirk has one place to live."""
    try:
        os.kill(pid, 15)
    except (OSError, ValueError):
        pass


def hyprland_window_exists(window_class, pid=None):
    """Best-effort, Linux/Hyprland-only: True/False if a matching window
    is (not) currently open, or None when hyprctl isn't
    available/reachable -- callers should only act on a definite
    answer, never treat "can't tell" as "gone".

    Exists for VLC's -I dummy quirk: its process lifecycle isn't tied
    to the video window at all, so closing that window via the
    compositor (SUPER+Q) doesn't make the process exit -- confirmed
    directly (closed the window, the underlying vlc process kept
    running). This lets the poll loop notice "window's gone, process
    isn't" and clean up the orphan itself, without touching global
    window-close keybinds that every other app also relies on.

    Matches on window class OR pid (whichever is given and matches) --
    class name alone is fragile (varies by VLC build/version/desktop
    file, and isn't guaranteed to be exactly "vlc"), so pid is checked
    too whenever the caller has the actual process handle. Doesn't
    catch the case where VLC forks a separate child process that owns
    the actual window (pid wouldn't match then), but that's strictly
    better than trusting the class string alone."""
    hyprctl = shutil.which("hyprctl")
    if not hyprctl:
        return None
    try:
        out = subprocess.run([hyprctl, "clients", "-j"], capture_output=True, text=True, timeout=2).stdout
        import json
        clients = json.loads(out)
        return any(
            c.get("class", "").lower() == window_class.lower() or (pid is not None and c.get("pid") == pid)
            for c in clients
        )
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
