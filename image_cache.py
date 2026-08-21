"""Tiny on-disk cache for cover-art thumbnails (AniList browse tab). Just
bytes-in-bytes-out keyed by URL, on a worker thread -- no cache eviction
since a browse tab's worth of ~25 small thumbnails at a time is nowhere
near worth the complexity of a real LRU.
"""
import hashlib
import os

import requests

import platform_utils

CACHE_DIR = os.path.join(platform_utils.state_dir("aniani"), "cover_cache")


def _cache_path(url):
    h = hashlib.sha1(url.encode()).hexdigest()
    return os.path.join(CACHE_DIR, h)


def cached_bytes(url):
    """Non-blocking, disk-only -- safe to call straight from the GUI
    thread to check for a cache hit before bothering with a worker
    thread at all. -> bytes if already cached, None if not (a miss
    here doesn't mean fetch() will fail, just that it'll need the
    network)."""
    path = _cache_path(url)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            pass
    return None


def fetch(url):
    """Blocking -- call from a worker thread. -> raw image bytes or None."""
    cached = cached_bytes(url)
    if cached is not None:
        return cached
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.content
    except requests.RequestException:
        return None
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(url), "wb") as f:
            f.write(data)
    except OSError:
        pass
    return data
