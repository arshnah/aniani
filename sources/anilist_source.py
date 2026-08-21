"""Discovery via AniList's public GraphQL API -- no auth/API key needed
for trending/popular queries. Powers a browse/discover tab so aniani
isn't just a search box (the single biggest qualitative UX gap versus
Crunchyroll/Hayase-style apps, per feature research comparing aniani
against Miru/Hayase/Aniyomi/Jellyfin-style clients).
"""
import json
import os
import random

import requests

import platform_utils

API_URL = "https://graphql.anilist.co"
# v2 -- v1's cache stored coverImage.medium (low-res) URLs; renaming
# rather than migrating means old low-res entries just get silently
# re-fetched at full ("large") quality instead of serving stale ones.
COVER_CACHE_PATH = os.path.join(platform_utils.state_dir("aniani"), "anilist_cover_cache_v2.json")

COVER_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    coverImage { large medium }
  }
}
"""

QUERY = """
query ($sort: [MediaSort], $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    media(type: ANIME, sort: $sort) {
      title { romaji english }
      episodes
      averageScore
      status
      format
      genres
      description(asHtml: false)
      coverImage { medium large extraLarge }
      bannerImage
    }
  }
}
"""


def _fetch(sort, per_page=25):
    # deliberately lets network/parse errors propagate (rather than
    # swallowing to []) -- an empty list used to mean both "AniList
    # genuinely has nothing" and "the request failed", which is exactly
    # why a real, intermittent failure here just showed up as a flat
    # "no results" in the UI with nothing to debug from.
    r = requests.post(
        API_URL,
        json={"query": QUERY, "variables": {"sort": [sort], "perPage": per_page}},
        timeout=8,
    )
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(body["errors"][0].get("message", "AniList API error"))
    media = body["data"]["Page"]["media"]
    out = []
    for m in media:
        title = (m.get("title") or {}).get("english") or (m.get("title") or {}).get("romaji")
        if not title:
            continue
        description = (m.get("description") or "").split("<br>")[0].strip()
        out.append({
            "title": title,
            "episodes": m.get("episodes"),
            "score": m.get("averageScore"),
            "status": m.get("status"),
            "format": m.get("format"),
            "genres": m.get("genres") or [],
            "description": description,
            "cover": (m.get("coverImage") or {}).get("medium"),
            "cover_large": (m.get("coverImage") or {}).get("large"),
            "cover_xl": (m.get("coverImage") or {}).get("extraLarge"),
            "banner": m.get("bannerImage"),
        })
    return out


def trending():
    return _fetch("TRENDING_DESC")


def popular():
    # a fixed top-25-by-popularity list is the same handful of shows
    # (One Piece, MHA, etc.) every single time -- not much of a
    # "recommendation." Pull a wider pool of genuinely popular shows and
    # randomly sample from it instead, so the row actually varies
    # between loads rather than reading as a static chart.
    pool = _fetch("POPULARITY_DESC", per_page=50)
    return random.sample(pool, min(25, len(pool)))


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
    """-> cover image URL or None. Best-effort title-search match, same
    limitation as everywhere else that cross-references a local anime
    title against AniList: no shared id, so this is a fuzzy match, not
    a guaranteed one. Cached across runs since this gets called once
    per continue-watching/history entry on every Home page load."""
    cache = _load_cover_cache()
    if title in cache:
        return cache[title]
    try:
        r = requests.post(API_URL, json={"query": COVER_QUERY, "variables": {"search": title}}, timeout=8)
        r.raise_for_status()
        media = r.json().get("data", {}).get("Media")
        image = (media or {}).get("coverImage") or {}
        cover = image.get("large") or image.get("medium")
    except (requests.RequestException, ValueError, AttributeError):
        return None  # not cached on failure -- worth retrying next load
    cache[title] = cover
    _save_cover_cache(cache)
    return cover
