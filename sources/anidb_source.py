"""anidb.app scraping source. Mirrors ani-cli (github.com/pystardust/ani-cli)'s
SCRAPING section -- same endpoints, same regex approach, reimplemented so a
GUI can call it directly instead of driving ani-cli's fzf menu flow.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_utils  # noqa: E402

NAME = "anidb"
# anidb.app rejects plain curl's TLS fingerprint (403) -- curl-impersonate
# is required to get past it, on every platform, not just Linux.
CURL = platform_utils.find_curl_impersonate()
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BASE = "https://anidb.app"


def _curl(url, timeout=12):
    if not CURL:
        raise RuntimeError(
            "curl_chrome136 (curl-impersonate) not found on PATH -- required for anidb.app "
            "(plain curl gets a 403 from its anti-bot check). Install it and retry."
        )
    r = subprocess.run(
        [CURL, "-sL", "-A", AGENT, "--max-time", str(timeout), url],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    return r.stdout or ""


def _unescape(s):
    return s.replace("&#039;", "'").replace("&quot;", '"').replace("\\/", "/")


def search(query):
    """-> [{"id", "title", "image": None}, ...]"""
    q = query.strip().replace(" ", "+")
    if not q:
        return []
    page = _curl(f"{BASE}/browse?q={q}").replace("\n", " ")
    page = re.sub(r"<a href", "\n<a href", page)
    results = []
    for line in page.split("\n"):
        m = re.search(r'anime/([a-z0-9-]+-[0-9]+)".*alt="([^"]+)"', line)
        if m:
            results.append({"id": m.group(1), "title": _unescape(m.group(2)), "image": None})
    return results


def episodes(anime_id):
    """-> [{"ep_ref", "ep_no"}, ...] in release order"""
    numeric_id = anime_id.rsplit("-", 1)[-1]
    page = _curl(f"{BASE}/api/frontend/anime/{numeric_id}/episodes").replace("},{", "}\n{")
    eps = []
    for line in page.split("\n"):
        m = re.search(r'"id":(\d+).*?"number":(\d+)', line)
        if m:
            eps.append({"ep_ref": m.group(1), "ep_no": m.group(2)})
    return eps


def mal_id_for(anime_id):
    """anime detail page -> MyAnimeList id, used for ani-skip intro timing"""
    page = _curl(f"{BASE}/anime/{anime_id}")
    m = re.search(r"https://myanimelist\.net/anime/(\d+)/", page)
    return m.group(1) if m else None


def _stream_links(ep_ref, mode="sub"):
    """-> [(quality_int, url), ...] sorted best-first"""
    lang = "eng" if mode == "dub" else "jpn"
    page = _curl(f"{BASE}/api/frontend/episode/{ep_ref}/languages").replace("},{", "}\n{")
    embed = None
    for line in page.split("\n"):
        if lang in line:
            m = re.search(r'embed_url":"([^"]+)"', line)
            if m:
                embed = _unescape(m.group(1))
                break
    if not embed:
        return []

    embed_page = _curl(embed)
    m = re.search(r"file: '([^']*)'", embed_page)
    if not m:
        return []
    master_url = m.group(1)

    master = _curl(master_url)
    links = []
    pending_quality = None
    for line in master.replace("\r", "").split("\n"):
        if line.startswith("#EXT-X-STREAM"):
            if "EXT-X-I-FRAME" in line:
                pending_quality = None
                continue
            qm = re.search(r"x(\d+)", line)
            pending_quality = int(qm.group(1)) if qm else 0
        elif line and not line.startswith("#"):
            if pending_quality is not None:
                links.append((pending_quality, line.strip()))
            pending_quality = None
    links.sort(key=lambda t: t[0], reverse=True)
    return links


def watch(ep_ref, mode="sub"):
    """-> {"url", "referer", "skip": {}} or None. Matches yuma_source's
    shape so the GUI can call any source's watch() the same way. This
    source has no referer requirement and no built-in skip timestamps
    (the GUI falls back to ani-skip/MAL lookup for those, same as
    before)."""
    links = _stream_links(ep_ref, mode)
    if not links:
        return None
    return {"url": links[0][1], "referer": None, "skip": {}}
