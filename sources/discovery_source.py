"""Unifies the two discovery backends (jikan_source, anilist_source)
behind one interface with automatic fallback: try Jikan first (MAL's
own image CDN, chosen at the user's direction after AniList's image
domain proved unreliable under this app's concurrent-fetch load), fall
back to AniList if Jikan's own connection to MAL is down.

Confirmed directly why this needs to be a fallback rather than a
straight swap: Jikan had a real, sustained outage during testing --
every attempt 504'd even with retries and backoff -- while AniList's
GraphQL API had zero failures across the same session. Depending on
either one exclusively means a full dashboard outage whenever that one
service has a bad day; trying both means it only goes down if BOTH do.
"""
import jikan_source
import anilist_source


def _with_fallback(jikan_fn, anilist_fn):
    try:
        result = jikan_fn()
        if result:
            return result
    except Exception:
        pass
    return anilist_fn()


def trending():
    return _with_fallback(jikan_source.trending, anilist_source.trending)


def popular():
    return _with_fallback(jikan_source.popular, anilist_source.popular)


def cover_for_title(title):
    try:
        cover = jikan_source.cover_for_title(title)
        if cover:
            return cover
    except Exception:
        pass
    return anilist_source.cover_for_title(title)
