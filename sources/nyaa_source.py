"""nyaa.si torrent search, via its RSS feed (no scraping needed, no anti-bot
protection, and actually reachable on networks that block most streaming
mirror sites). This is a different playback paradigm from the other
sources -- results are torrent releases, not a clean per-episode list, and
playback goes through player/torrent_backend.py (qbittorrent-nox sequential
download + hand off the growing file to VLC/mpv) instead of a direct URL.
"""
import subprocess
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

NAME = "nyaa"
BASE = "https://nyaa.si"
CURL = "curl"

# well-known public trackers, appended since nyaa's RSS doesn't list any --
# releases still swarm fine via DHT, but this speeds up peer discovery
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
]


def _curl(url, timeout=10, retries=3):
    # nyaa.si is a single-server operation with a real history of
    # intermittent timeouts -- observed firsthand: 2 failed attempts then
    # a clean 200 seconds later. Worth a few quick retries before giving up.
    for attempt in range(retries):
        r = subprocess.run(
            [CURL, "-sL", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        if r.stdout:
            return r.stdout
        if attempt < retries - 1:
            time.sleep(2)
    return ""


def search(query, category="1_2"):
    """category 1_2 = Anime - English-translated. -> [{"id", "title",
    "size", "seeders", "leechers", "magnet"}, ...] sorted by seeders desc."""
    q = quote(query.strip())
    if not q:
        return []
    xml_text = _curl(f"{BASE}/?page=rss&q={q}&c={category}&f=0")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    ns = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
    results = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        info_hash = (item.findtext("nyaa:infoHash", namespaces=ns) or "").strip()
        seeders = int(item.findtext("nyaa:seeders", default="0", namespaces=ns) or 0)
        leechers = int(item.findtext("nyaa:leechers", default="0", namespaces=ns) or 0)
        size = (item.findtext("nyaa:size", namespaces=ns) or "").strip()
        if not (title and info_hash):
            continue
        tr = "".join(f"&tr={quote(t)}" for t in TRACKERS)
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}{tr}"
        results.append({
            "id": guid or info_hash, "title": title, "size": size,
            "seeders": seeders, "leechers": leechers, "magnet": magnet,
        })
    results.sort(key=lambda r: r["seeders"], reverse=True)
    return results
