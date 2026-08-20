"""Downloads episodes for offline viewing, via ffmpeg (same approach
ani-cli itself uses for -d/--download: HLS segments in, remuxed mp4 out
-- no separate yt-dlp dependency needed since sources already resolve
direct stream URLs). Supports single episodes, ranges, and whole series,
queued and run one at a time so a season download doesn't try to open
20 ffmpeg processes at once.
"""
import os
import re
import subprocess

import platform_utils

DOWNLOAD_ROOT = platform_utils.downloads_dir("aniani")
FFMPEG = platform_utils.find_ffmpeg() or "ffmpeg"


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def dest_path(anime_title, ep_no, ext="mp4"):
    show_dir = os.path.join(DOWNLOAD_ROOT, _safe(anime_title))
    os.makedirs(show_dir, exist_ok=True)
    return os.path.join(show_dir, f"Episode {ep_no}.{ext}")


def is_downloaded(anime_title, ep_no):
    return os.path.exists(dest_path(anime_title, ep_no))


def list_downloaded():
    """-> [{"show", "episodes": [ep_no, ...]}], for an offline library
    browser -- scans DOWNLOAD_ROOT, doesn't need any source/network."""
    if not os.path.isdir(DOWNLOAD_ROOT):
        return []
    library = []
    for show in sorted(os.listdir(DOWNLOAD_ROOT)):
        show_dir = os.path.join(DOWNLOAD_ROOT, show)
        if not os.path.isdir(show_dir):
            continue
        eps = []
        for f in os.listdir(show_dir):
            m = re.match(r"Episode (.+)\.\w+$", f)
            if m:
                eps.append(m.group(1))
        eps.sort(key=lambda e: (len(e), e))
        if eps:
            library.append({"show": show, "episodes": eps})
    return library


class DownloadJob:
    def __init__(self, anime_title, ep_no, url, referer=None):
        self.anime_title = anime_title
        self.ep_no = ep_no
        self.url = url
        self.referer = referer
        self.dest = dest_path(anime_title, ep_no)
        self.status = "queued"  # queued, downloading, done, failed
        self.progress = 0.0  # 0-1, best effort (only known once ffmpeg reports duration)


def run_job(job, on_progress=None):
    """Blocking -- run this on a worker thread, not the GUI thread."""
    job.status = "downloading"
    args = [FFMPEG, "-y", "-loglevel", "error", "-stats"]
    if job.referer:
        args += ["-headers", f"Referer: {job.referer}\r\n"]
    # anidb.app (and likely other sources) disguise HLS segment URLs with a
    # non-standard .xls extension -- ffmpeg's HLS demuxer and its general
    # format/extension safety check both reject that by default. ani-cli's
    # own download() function carries -extension_picky 0 for exactly this;
    # -allowed_segment_extensions ALL is the HLS-demuxer-specific half of
    # the same fix. Confirmed working against a real anidb.app stream.
    args += ["-extension_picky", "0", "-allowed_segment_extensions", "ALL"]
    args += ["-i", job.url, "-c", "copy", job.dest]

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    duration = None
    for line in proc.stdout:
        if duration is None:
            m = re.search(r"Duration: (\d+):(\d+):(\d+)", line)
            if m:
                h, mnt, s = map(int, m.groups())
                duration = h * 3600 + mnt * 60 + s
        m = re.search(r"time=(\d+):(\d+):(\d+)", line)
        if m and duration:
            h, mnt, s = map(int, m.groups())
            job.progress = min(1.0, (h * 3600 + mnt * 60 + s) / duration)
            if on_progress:
                on_progress(job)
    proc.wait()

    if proc.returncode == 0 and os.path.exists(job.dest) and os.path.getsize(job.dest) > 0:
        job.status = "done"
        job.progress = 1.0
    else:
        job.status = "failed"
        try:
            os.remove(job.dest)
        except OSError:
            pass
    if on_progress:
        on_progress(job)
    return job.status == "done"
