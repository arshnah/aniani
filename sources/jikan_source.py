"""Discovery via Jikan (unofficial MyAnimeList API, api.jikan.moe) --
no API key needed, generous rate limit (3 req/s, 60/min). Replaces
anilist_source.py for the Discover dashboard: images are served from
MAL's own CDN (cdn.myanimelist.net) instead of AniList's, at the
user's explicit direction after AniList's image CDN proved unreliable
under this app's concurrent-fetch load (confirmed directly across many
tests -- the network path to AniList's image domain specifically, not
MAL's, was the flaky one).

Note found while wiring this up: several Jikan query-parameter
variants (filter=bypopularity, filter=airing, /seasons/now, and even
/anime?q=... search) were returning 504s consistently across repeated
tests -- Jikan's own upstream connection to MAL failing, not something
this app can work around. The plain /top/anime endpoint (no filter)
was reliable throughout, so that's what both trending() and popular()
build on (different pages for variety) rather than the currently-flaky
filtered variants. cover_for_title() still uses the search endpoint
since there's no alternative for a title lookup -- it'll just come back
empty (existing graceful degradation) if Jikan's search stays down.
"""
import json
import os
import time
import random

import requests

import platform_utils

API_URL = "https://api.jikan.moe/v4"
COVER_CACHE_PATH = os.path.join(platform_utils.state_dir("aniani"), "jikan_cover_cache.json")


def _image_urls(entry):
    images = entry.get("images") or {}
    jpg = images.get("jpg") or {}
    webp = images.get("webp") or {}
    return {
        "cover": jpg.get("image_url") or webp.get("image_url"),
        "cover_large": jpg.get("large_image_url") or webp.get("large_image_url"),
        "cover_xl": jpg.get("large_image_url") or webp.get("large_image_url"),
    }


def _to_entry(m):
    title = m.get("title_english") or m.get("title")
    if not title:
        return None
    genres = [g.get("name") for g in (m.get("genres") or []) if g.get("name")]
    out = {
        "title": title,
        "episodes": m.get("episodes"),
        "score": int(m["score"] * 10) if m.get("score") else None,  # match AniList's 0-100 scale
        "status": m.get("status"),
        "format": m.get("type"),
        "genres": genres,
        "description": (m.get("synopsis") or "").split("\n")[0].strip(),
        "banner": None,  # MAL/Jikan has no wide-banner concept, only cover art
    }
    out.update(_image_urls(m))
    return out


def _fetch_top(page=1, per_page=25, _attempt=0):
    # Jikan proved noticeably flakier than AniList's GraphQL API during
    # testing -- the exact same endpoint/params 200'd once and 504'd
    # moments later on a bare retry, repeatedly, including a real
    # sustained outage where every attempt failed even with backoff
    # (confirmed directly). This module is only ever called through
    # discovery_source.py's fallback-to-AniList wrapper now, so it
    # deliberately does NOT retry hard here -- one quick extra attempt,
    # short timeout, then let the caller fall back fast instead of
    # making every dashboard load sit through a long retry sequence
    # against a service that might be down for a while.
    try:
        r = requests.get(f"{API_URL}/top/anime", params={"page": page, "limit": per_page}, timeout=5)
        r.raise_for_status()
        body = r.json()
        if "status" in body and body.get("status") != 200 and "data" not in body:
            raise RuntimeError(body.get("message", "Jikan API error"))
    except (requests.RequestException, RuntimeError, ValueError):
        if _attempt >= 1:
            raise
        time.sleep(0.5)
        return _fetch_top(page, per_page, _attempt + 1)
    out = []
    for m in body.get("data", []):
        entry = _to_entry(m)
        if entry:
            out.append(entry)
    return out


def trending():
    return _fetch_top(page=1)


def popular():
    # /top/anime's filter=bypopularity variant was consistently 504ing
    # (confirmed directly, repeatedly) -- a random page of the plain
    # (score-ranked) endpoint gives real variety between loads without
    # depending on the currently-broken filtered endpoint.
    return _fetch_top(page=random.randint(2, 15))


def _load_cover_cache():
    if os.path.exists(COVER_CACHE_PATH):
        try:
            with open(COVER_CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cover_cache(cache):
    try:
        os.makedirs(platform_utils.state_dir("aniani"), exist_ok=True)
        with open(COVER_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def cover_for_title(title):
    """-> cover image URL or None. Best-effort title-search match,
    cached across runs. Called once per continue-watching/history entry
    on every Home page load."""
    cache = _load_cover_cache()
    if title in cache:
        return cache[title]
    for attempt in range(2):  # one quick retry -- see _fetch_top for why this stays fast, not aggressive
        try:
            r = requests.get(f"{API_URL}/anime", params={"q": title, "limit": 1}, timeout=5)
            r.raise_for_status()
            data = r.json().get("data") or []
            cover = _image_urls(data[0]).get("cover_large") if data else None
            break
        except (requests.RequestException, ValueError, IndexError, AttributeError):
            if attempt == 1:
                return None  # falls back to anilist_source via discovery_source.py
            time.sleep(0.5)
    cache[title] = cover
    _save_cover_cache(cache)
    return cover
