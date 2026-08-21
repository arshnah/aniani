"""aniwatchtv.to source, via the vendored YumaAPI class from pyanimecli
(see _vendor_pyanimecli.py). Blocked by some ISPs (confirmed: India, via
both DNS poisoning and SNI-level filtering) -- built anyway since it works
fine off that network (mobile data, a different country, a VPN, etc).
Fails gracefully (empty results / None) when unreachable rather than
raising, so the GUI just shows "no results" instead of crashing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _vendor_pyanimecli as _pac  # noqa: E402

NAME = "yuma"
_api = _pac.YumaAPI()


def search(query):
    """-> [{"id", "title", "image"}, ...]"""
    try:
        data = _api.search(query)
    except Exception:
        return []
    return [{"id": r["id"], "title": r["title"], "image": r.get("image")} for r in data.get("results", [])]


def episodes(anime_id):
    """-> [{"ep_ref", "ep_no"}, ...]. ep_ref is YumaAPI's composite episode
    id (needed as-is for watch()); ep_no is the display/resume-key number."""
    try:
        info = _api.info(anime_id)
    except Exception:
        return []
    return [{"ep_ref": e["id"], "ep_no": str(e["number"])} for e in info.get("episodes", [])]


def mal_id_for(anime_id):
    try:
        info = _api.info(anime_id)
        mal_id = info.get("mal_id")
        return str(mal_id) if mal_id else None
    except Exception:
        return None


def watch(ep_ref, mode="sub"):
    """One call gets everything for playback: stream url + referer header
    (this source needs it or the CDN rejects the request -- pass through
    to VlcPlayer.play(referer=...)/MpvPlayer the same way) + skip-intro/
    outro timestamps, which this source returns directly, no ani-skip/MAL
    lookup needed the way the anidb source requires.
    -> {"url", "referer", "skip": {"op": (s,e), "ed": (s,e)}} or None"""
    try:
        data = _api.watch(ep_ref, mode)
    except Exception:
        return None
    sources = data.get("sources") or []
    if not sources:
        return None
    skip = {}
    intro = data.get("intro") or {}
    outro = data.get("outro") or {}
    if intro.get("end"):
        skip["op"] = (intro.get("start", 0), intro["end"])
    if outro.get("end"):
        skip["ed"] = (outro.get("start", 0), outro["end"])
    return {
        "url": sources[0]["url"],
        "referer": (data.get("headers") or {}).get("Referer"),
        "skip": skip,
    }
