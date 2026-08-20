"""Discord Rich Presence, shared by the GUI (in-process, while open) and
the standalone daemon (spawned on close so presence keeps updating while
the player keeps running on its own).
"""
import json
import os
import re
import time

import requests
from pypresence import Presence

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(DIR, "config.json")
CACHE_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "aniani", "cover_cache.json",
)
ANILIST_URL = "https://graphql.anilist.co"
ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    title { romaji english }
    coverImage { extraLarge large }
    siteUrl
  }
}
"""
BROWSING_ICON = "https://cdn.simpleicons.org/myanimelist"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(
            f"Missing config file at {CONFIG_PATH}.\n"
            "Create a Discord application at https://discord.com/developers/applications, "
            'copy its Client ID, and write {"client_id": "..."} to that file.'
        )
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def fetch_media(show_title, cache):
    if show_title in cache:
        return cache[show_title]
    result = {"image": None, "url": None}
    try:
        resp = requests.post(
            ANILIST_URL,
            json={"query": ANILIST_QUERY, "variables": {"search": show_title}},
            timeout=5,
        )
        resp.raise_for_status()
        media = resp.json().get("data", {}).get("Media")
        if media:
            result["image"] = media["coverImage"]["extraLarge"] or media["coverImage"]["large"]
            result["url"] = media.get("siteUrl")
    except requests.RequestException:
        pass
    cache[show_title] = result
    save_cache(cache)
    return result


class DiscordPresence:
    def __init__(self):
        self.rpc = None
        self.connected = False
        self.cache = load_cache()
        self.media_info = {"image": None, "url": None}
        self.media_show = None

    def ensure_connected(self):
        if self.connected:
            return True
        try:
            config = load_config()
        except SystemExit:
            return False
        try:
            self.rpc = Presence(config["client_id"])
            self.rpc.connect()
            self.connected = True
        except Exception:
            self.connected = False
        return self.connected

    def disconnect(self):
        if self.connected and self.rpc:
            try:
                self.rpc.clear()
                self.rpc.close()
            except Exception:
                pass
        self.connected = False

    def _media_for(self, show):
        if show != self.media_show:
            self.media_info = fetch_media(show, self.cache)
            self.media_show = show
        return self.media_info

    def browsing(self, detail):
        if not self.ensure_connected():
            return
        try:
            self.rpc.update(
                details="Browsing aniani", state=detail, small_text="Browsing",
                large_image=BROWSING_ICON, large_text="aniani",
            )
        except Exception:
            pass

    def watching(self, show, ep_no, pos_seconds, duration_seconds, paused):
        if not self.ensure_connected():
            return
        info = self._media_for(show)
        kwargs = dict(
            details=show[:128],
            state=(f"{'Paused' if paused else 'Watching'} · Episode {ep_no}")[:128],
            small_text="Paused" if paused else "Playing",
        )
        if info.get("image"):
            kwargs["large_image"] = info["image"]
            kwargs["large_text"] = show[:128]
        if not paused:
            now = time.time()
            kwargs["start"] = int(now - pos_seconds)
            if duration_seconds:
                kwargs["end"] = int(now - pos_seconds + duration_seconds)
        if info.get("url"):
            kwargs["buttons"] = [{"label": "View on AniList", "url": info["url"]}]
        try:
            self.rpc.update(**kwargs)
        except Exception:
            pass
