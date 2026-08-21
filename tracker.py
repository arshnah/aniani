"""AniList progress sync -- pushes watched-episode updates to the
user's AniList list so aniani doesn't fork into a second, disconnected
watch-history silo. This was the #1 most-requested-feature finding
from researching comparable anime players (Aniyomi, Hayase/Miru):
users who already track on AniList expect existing progress to sync,
not start a second list from scratch.

Auth: AniList's OAuth2 implicit grant via its own built-in
`/api/v2/oauth/pin` page -- no local HTTP server or redirect handler
needed, which matters here since aniani has no long-running local
webserver to catch a normal OAuth redirect. The user authorizes in
their browser, AniList's pin page extracts the token from the URL
fragment client-side and shows it as plain text, and the user pastes
it into aniani once. This is the same pattern other unofficial
AniList desktop clients use for exactly this reason.

Requires a free AniList API client (Settings -> Developer -> Create
Client at anilist.co) with redirect URL set to
`https://anilist.co/api/v2/oauth/pin` -- the client id is entered once
in the Account page and stored in prefs, not hardcoded here, since
(unlike the Discord app id) this isn't something that can be
pre-registered on the user's behalf without their AniList login.
"""
import json
import os

import requests

import state

API_URL = "https://graphql.anilist.co"
TOKEN_PATH = os.path.join(state.STATE_DIR, "anilist_token.json")
MEDIA_CACHE_PATH = os.path.join(state.STATE_DIR, "anilist_media_cache.json")

WHOAMI_QUERY = "query { Viewer { id name } }"

SEARCH_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    episodes
    title { romaji english }
  }
}
"""

SAVE_MUTATION = """
mutation ($mediaId: Int, $progress: Int, $status: MediaListStatus) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status) { id }
}
"""


def authorize_url(client_id):
    return f"https://anilist.co/api/v2/oauth/authorize?client_id={client_id}&response_type=token"


def load_token():
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH) as f:
                return json.load(f).get("token")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_token(token):
    try:
        os.makedirs(state.STATE_DIR, exist_ok=True)
        with open(TOKEN_PATH, "w") as f:
            json.dump({"token": token}, f)
    except OSError:
        pass


def clear_token():
    try:
        os.remove(TOKEN_PATH)
    except OSError:
        pass


def _headers():
    token = load_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _query(query, variables=None):
    headers = _headers()
    if not headers:
        return None
    try:
        r = requests.post(API_URL, json={"query": query, "variables": variables or {}}, headers=headers, timeout=8)
        if r.status_code == 401:
            return None  # expired/revoked token -- caller treats this as "not connected"
        r.raise_for_status()
        return r.json().get("data")
    except (requests.RequestException, ValueError):
        return None


def whoami():
    """-> AniList username, or None if not connected / token invalid."""
    data = _query(WHOAMI_QUERY)
    return data["Viewer"]["name"] if data and data.get("Viewer") else None


def _load_media_cache():
    if os.path.exists(MEDIA_CACHE_PATH):
        try:
            with open(MEDIA_CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_media_cache(cache):
    try:
        os.makedirs(state.STATE_DIR, exist_ok=True)
        with open(MEDIA_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def find_media(title):
    """-> {"id", "episodes"} or None. Cached across runs, keyed by our
    local anime_title string -- title-search matching AniList's fuzzy
    search is best-effort (same limitation ani-cli's own AniList
    integration has: there's no cross-reference id shared between
    anidb.app/YumaAPI and AniList, only titles to match on)."""
    cache = _load_media_cache()
    if title in cache:
        return cache[title]
    data = _query(SEARCH_QUERY, {"search": title})
    media = data.get("Media") if data else None
    result = {"id": media["id"], "episodes": media.get("episodes")} if media else None
    cache[title] = result
    _save_media_cache(cache)
    return result


def update_progress(anime_title, ep_no):
    """Push a watched-episode update. Best-effort throughout: no
    AniList account, no cached/matched media, or a non-numeric episode
    number (specials, "S1", etc.) are all silently skipped rather than
    surfaced as errors -- this runs from a background poll loop, not a
    user-initiated action, so there's no good place to show a failure
    even if there were one worth showing."""
    if not load_token():
        return
    try:
        progress = int(float(ep_no))
    except (TypeError, ValueError):
        return
    media = find_media(anime_title)
    if not media:
        return
    status = "COMPLETED" if media.get("episodes") and progress >= media["episodes"] else "CURRENT"
    _query(SAVE_MUTATION, {"mediaId": media["id"], "progress": progress, "status": status})
