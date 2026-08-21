# Vendored, unmodified, from github.com/gammadevv/pyanimecli (MIT license,
# see LICENSE_PYANIMECLI at the repo root). aniani only imports the YumaAPI
# class from this file via sources/yuma_source.py -- the CLI/main() below
# is never invoked. Only the YumaAPI class and its module-level helpers are
# used; kept whole rather than hand-extracted since it depends on a
# MegaCloud AES-decryption pipeline that's fragile enough to want the
# upstream implementation exactly as maintained, not a hand port.
import sys
import argparse
import requests
import platform
import time
import subprocess
import tempfile
import shutil
import os
import re
import json
import base64
import asyncio
import httpx
from typing import List, Dict, Optional, Union, Any, Tuple
from urllib.parse import quote, urlparse, urljoin, quote_plus
from bs4 import BeautifulSoup
from tqdm import tqdm
from pathlib import Path
from datetime import datetime, timedelta, timezone

try:
    from pym3u8downloader import M3U8Downloader
except ImportError:
    M3U8Downloader = None

try:
    from packaging import version as semver
except ImportError:
    semver = None

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.live import Live
    from rich.spinner import Spinner
except ImportError:
    print("Error: The 'rich' library is required. Please install it using 'pip install rich'.")
    sys.exit(1)

try:
    from rich_pixels import Pixels
    from PIL import Image
except ImportError:
    Pixels = None
    Image = None

try:
    from Crypto.Cipher import AES
    from Crypto.Hash import MD5
    from Crypto.Util.Padding import unpad
    _CRYPTO_AVAILABLE = True
except ImportError:
    AES = None
    MD5 = None
    unpad = None
    _CRYPTO_AVAILABLE = False

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

__version__ = "1.3"
PACKAGE_NAME = "pyanimecli"

console = Console()

TIMEZONES = [
    "UTC", "GMT", "BST", "IST", "EST", "EDT", "CST", "CDT",
    "MST", "MDT", "PST", "PDT", "AKST", "AKDT", "HST",
    "AEST", "AEDT", "ACST", "ACDT", "AWST", "JST", "KST",
    "CET", "CEST", "EET", "EEST", "WET", "WEST", "MSK", "MSD", "AST", "ADT", "NST", "NDT"
]
DEFAULT_TZ = "BST"
CONFIG_DIR = Path.home() / ".pyanimecli"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_SETTINGS = {
    "source": "sub",
    "player": "vlc",
    "auto_update": True,
    "download_path": str(Path.home() / "Downloads" / "Anime")
}

TZ_MAP = {
    "UTC": "UTC", "GMT": "Etc/GMT", "BST": "Europe/London", "IST": "Asia/Kolkata",
    "EST": "America/New_York", "EDT": "America/New_York", "CST": "America/Chicago",
    "CDT": "America/Chicago", "MST": "America/Denver", "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage", "HST": "Pacific/Honolulu", "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney", "ACST": "Australia/Adelaide", "ACDT": "Australia/Adelaide",
    "AWST": "Australia/Perth", "JST": "Asia/Tokyo", "KST": "Asia/Seoul",
    "CET": "Europe/Paris", "CEST": "Europe/Paris", "EET": "Europe/Athens",
    "EEST": "Europe/Athens", "WET": "Europe/Lisbon", "WEST": "Europe/Lisbon",
    "MSK": "Europe/Moscow", "MSD": "Europe/Moscow",
}


class Settings:
    def __init__(self):
        CONFIG_DIR.mkdir(exist_ok=True)
        self.data = self.load()

    def load(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r") as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
        return DEFAULT_SETTINGS.copy()

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.data, f, indent=4)

    def update(self, key, value):
        if value.lower() == "default" and key in DEFAULT_SETTINGS:
            self.data[key] = DEFAULT_SETTINGS[key]
        else:
            self.data[key] = value
        self.save()


settings = Settings()


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int):
    if not _CRYPTO_AVAILABLE:
        raise ImportError("pycryptodome is required for decryption.")
    derived_bytes = b''
    block = b''
    while len(derived_bytes) < key_len + iv_len:
        hasher = MD5.new()
        if block:
            hasher.update(block)
        hasher.update(password)
        hasher.update(salt)
        block = hasher.digest()
        derived_bytes += block
    return derived_bytes[:key_len], derived_bytes[key_len:key_len + iv_len]


def _decrypt_cryptojs_aes(encrypted_b64: str, passphrase_str: str) -> str:
    if not _CRYPTO_AVAILABLE:
        raise ImportError("pycryptodome is required for decryption.")
    passphrase_bytes = passphrase_str.encode('utf-8')
    encrypted_data_bytes = base64.b64decode(encrypted_b64)
    if not encrypted_data_bytes.startswith(b"Salted__"):
        try:
            key = bytes.fromhex(passphrase_str)
            iv = encrypted_data_bytes[:16]
            actual_ciphertext = encrypted_data_bytes[16:]
            if len(key) != 32:
                raise ValueError("Direct key is not 32 bytes for AES-256.")
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted_padded = cipher.decrypt(actual_ciphertext)
            decrypted = unpad(decrypted_padded, AES.block_size, style='pkcs7')
            return decrypted.decode('utf-8')
        except Exception:
            raise ValueError("Ciphertext not in OpenSSL salted format and direct key decryption failed.")
    salt = encrypted_data_bytes[8:16]
    actual_ciphertext = encrypted_data_bytes[16:]
    key, iv = _evp_bytes_to_key(passphrase_bytes, salt, 32, 16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted_padded = cipher.decrypt(actual_ciphertext)
    try:
        decrypted = unpad(decrypted_padded, AES.block_size, style='pkcs7')
    except ValueError as e:
        raise ValueError(f"Failed to unpad data. Error: {e}") from e
    return decrypted.decode('utf-8')


_YUMA_KEYS_URL = "https://raw.githubusercontent.com/yogesh-hacker/MegacloudKeys/refs/heads/main/keys.json"
_YUMA_DECODE_URL = "https://script.google.com/macros/s/AKfycbxHbYHbrGMXYD2-bC-C43D3njIbU-wGiYQuJL61H4vyy6YVXkybMNNEPJNPPuZrD1gRVA/exec"
_YUMA_MEGACLOUD_KEY_URL = "https://raw.githubusercontent.com/carlosesteven/e1-player-deobf/main/output/key.json"
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_yuma_keys_cache: Optional[Dict] = None
_yuma_keys_last_fetch: float = 0
_yuma_keys_cache_ttl: int = 3600

_megacloud_key_cache: Optional[str] = None
_megacloud_key_last_fetch: float = 0
_megacloud_key_cache_ttl: int = 3600


def _fetch_megacloud_key(session: requests.Session) -> str:
    global _megacloud_key_cache, _megacloud_key_last_fetch
    current_time = time.time()
    if _megacloud_key_cache and (current_time - _megacloud_key_last_fetch < _megacloud_key_cache_ttl):
        return _megacloud_key_cache
    url = f"{_YUMA_MEGACLOUD_KEY_URL}?v={int(current_time)}"
    response = session.get(url, timeout=10)
    response.raise_for_status()
    key_data = response.json()
    decrypt_key = key_data.get("decryptKey")
    if not decrypt_key or not isinstance(decrypt_key, str):
        raise ValueError("Decrypt key not found or invalid in fetched data.")
    _megacloud_key_cache = decrypt_key
    _megacloud_key_last_fetch = current_time
    return decrypt_key


def _decrypt_sources_sync_fallback(full_url: str) -> List[Dict]:
    headers = {"User-Agent": _BROWSER_UA}
    response = requests.get(full_url, headers=headers, timeout=20)
    response.raise_for_status()
    response_text = response.text
    if not response_text:
        raise ValueError("Decryption service returned an empty response.")
    json_start_index = response_text.find('[')
    json_end_index = response_text.rfind(']')
    if json_start_index == -1 or json_end_index == -1 or json_end_index < json_start_index:
        raise ValueError(f"Could not find valid JSON. Response: {response_text[:200]}")
    json_string = response_text[json_start_index: json_end_index + 1]
    decoded_list = json.loads(json_string)
    if isinstance(decoded_list, list):
        return decoded_list
    raise ValueError("Decoded data is not in expected list format.")


async def _get_client_key(embed_url: str) -> Optional[str]:
    headers = {"Referer": "https://aniwatchtv.to", "User-Agent": _BROWSER_UA}
    async with httpx.AsyncClient() as client:
        for _ in range(5):
            try:
                response = await client.get(embed_url, headers=headers)
                html = response.text
                soup = BeautifulSoup(html, "html.parser")
                meta = soup.find("meta", attrs={"name": "_gg_fb"})
                if meta and meta.get("content"):
                    return meta["content"]
                comment_matches = re.findall(r"<!--\s*(_is_th:[^>]+?)\s*-->", html)
                for comment in comment_matches:
                    match = re.match(r"_is_th:([^\n\r]+)", comment)
                    if match:
                        return match.group(1).strip()
                dpi_div = soup.find(attrs={"data-dpi": True})
                if dpi_div:
                    return dpi_div["data-dpi"]
                scripts = soup.find_all("script")
                for script in scripts:
                    script_text = script.text.strip()
                    xy_match = re.search(r'window\._xy_ws\s*=\s*[\'"]([^\'"]+)[\'"]', script_text)
                    if xy_match:
                        return xy_match.group(1)
                    lk_match = re.search(
                        r'window\._lk_db\s*=\s*{x:\s*[\'"]([^\'"]+)[\'"],\s*y:\s*[\'"]([^\'"]+)[\'"],\s*z:\s*[\'"]([^\'"]+)[\'"]}',
                        script_text
                    )
                    if lk_match:
                        return lk_match.group(1) + lk_match.group(2) + lk_match.group(3)
            except Exception:
                continue
    return None


async def _extract_sources_via_yuma_keys(embed_url: str, primary_data: Optional[Dict]) -> Optional[Dict]:
    global _yuma_keys_cache, _yuma_keys_last_fetch
    async with httpx.AsyncClient() as client:
        try:
            headers = {"Referer": embed_url, "User-Agent": _BROWSER_UA}
            embed_page_resp = await client.get(embed_url, headers={"Referer": embed_url, "User-Agent": _BROWSER_UA}, follow_redirects=True, timeout=15)
            embed_page_resp.raise_for_status()
            html_content = embed_page_resp.text

            match = re.search(r'\b[a-zA-Z0-9]{48}\b', html_content) or \
                    re.search(r'\b([a-zA-Z0-9]{16})\b.*?\b([a-zA-Z0-9]{16})\b.*?\b([a-zA-Z0-9]{16})\b', html_content)
            if not match:
                return None
            nonce = "".join(match.groups()) if match.lastindex == 3 else match.group(0)

            parsed_url = urlparse(embed_url)
            hostname = parsed_url.hostname
            if 'videostr' in hostname:
                provider_key_name, api_path = 'vidstr', 'embed-1/v3/e-1'
                id_match = re.search(r'/e-1/([a-zA-Z0-9]+)', embed_url)
                if not id_match:
                    return None
                video_id = id_match.group(1)
            elif 'megacloud' in hostname or 'vidcloud' in hostname:
                provider_key_name, api_path = 'mega', 'embed-2/v3/e-1'
                soup = BeautifulSoup(html_content, 'html.parser')
                video_tag = soup.select_one('[data-id]')
                if not video_tag or not video_tag.get('data-id'):
                    return None
                video_id = video_tag['data-id']
            else:
                return None

            api_url = f"https://{hostname}/{api_path}/getSources?id={video_id}&_k={nonce}"
            api_headers = {"Referer": embed_url, "X-Requested-With": "XMLHttpRequest", "User-Agent": _BROWSER_UA}
            sources_resp = await client.get(api_url, headers=api_headers, timeout=15)
            sources_resp.raise_for_status()
            data = sources_resp.json()

            if not data.get('encrypted'):
                decrypted_sources = data.get('sources', [])
            else:
                current_time = time.time()
                if not (_yuma_keys_cache and (current_time - _yuma_keys_last_fetch < _yuma_keys_cache_ttl)):
                    key_resp = await client.get(_YUMA_KEYS_URL, timeout=10)
                    _yuma_keys_cache = key_resp.json()
                    _yuma_keys_last_fetch = current_time

                secret_key = _yuma_keys_cache.get(provider_key_name)
                if not secret_key:
                    return None

                params = {"encrypted_data": data['sources'], "nonce": nonce, "secret": secret_key}
                query_string = f"encrypted_data={quote_plus(params['encrypted_data'])}&nonce={quote_plus(params['nonce'])}&secret={quote_plus(params['secret'])}"
                full_decode_url = f"{_YUMA_DECODE_URL}?{query_string}"

                decrypted_sources = await asyncio.to_thread(_decrypt_sources_sync_fallback, full_decode_url)

            final_sources = []
            for source in decrypted_sources:
                source_url = source.get("file")
                if source_url and ".m3u8" in source_url:
                    final_sources.append({"url": source_url, "quality": "auto", "isM3U8": True})
                    try:
                        m3u8_resp = await client.get(source_url, headers=api_headers, timeout=10)
                        m3u8_resp.raise_for_status()
                        stream_matches = re.findall(r'#EXT-X-STREAM-INF:.*?RESOLUTION=\d+x(\d+).*?\n(.*?\.m3u8)', m3u8_resp.text)
                        for quality, stream_url in stream_matches:
                            absolute_url = urljoin(source_url, stream_url)
                            final_sources.append({"url": absolute_url, "quality": f"{quality}p", "isM3U8": True})
                    except Exception:
                        pass
                elif source_url:
                    final_sources.append({"url": source_url, "quality": "unknown", "isM3U8": False})

            if not final_sources:
                return None

            tracks_data = data.get('tracks', [])
            subtitles = [
                {'url': t['file'], 'lang': t.get('label', 'Default')}
                for t in tracks_data if t.get('kind') in ['captions', 'subtitles'] and t.get('file')
            ]

            return {
                "sources": final_sources,
                "subtitles": subtitles,
                "headers": {"Referer": embed_url},
                "intro": data.get('intro', {'start': 0, 'end': 0}),
                "outro": data.get('outro', {'start': 0, 'end': 0}),
            }
        except Exception:
            return None


class YumaAPI:
    _SERVER_NAME_TO_ID = {
        "vidcloud": "1", "megacloud": "1", "upcloud": "6",
        "streamvid": "4", "vidstreaming": "4",
        "streamsb": "5", "watchsb": "5", "streamtape": "3",
    }

    def __init__(self):
        self._base = 'https://aniwatchtv.to'
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': _BROWSER_UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        })

    def _scrape_card_page(self, url: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {'current_page': 1, 'has_next_page': False, 'total_pages': 1, 'results': []}
        response = self._session.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        pagination = soup.select_one('ul.pagination')
        if pagination:
            active_page = pagination.select_one('.page-item.active a')
            if active_page and active_page.text.strip().isdigit():
                result['current_page'] = int(active_page.text.strip())
            next_page_item = pagination.select_one('li.page-item a[title="Next"]')
            result['has_next_page'] = bool(next_page_item)
            last_page_item = pagination.select_one('li.page-item a[title="Last"]')
            if last_page_item and 'page=' in last_page_item.get('href', ''):
                last_page_match = re.search(r'page=(\d+)', last_page_item['href'])
                if last_page_match:
                    result['total_pages'] = int(last_page_match.group(1))
            elif not result['has_next_page']:
                result['total_pages'] = result['current_page']

        for card in soup.select('div.flw-item'):
            title_elem = card.select_one('h3.film-name a')
            if not title_elem or not title_elem.get('href'):
                continue
            anime_id = title_elem.get('href').strip('/')
            poster_div = card.select_one('.film-poster')
            detail_div = card.select_one('.film-detail')
            img_elem = poster_div.select_one('img.film-poster-img') if poster_div else None
            sub_elem = poster_div.select_one('.tick-item.tick-sub') if poster_div else None
            dub_elem = poster_div.select_one('.tick-item.tick-dub') if poster_div else None
            eps_count_elem = poster_div.select_one('.tick-item.tick-eps') if poster_div else None
            sub_count = int(sub_elem.text.strip()) if sub_elem and sub_elem.text.strip().isdigit() else 0
            dub_count = int(dub_elem.text.strip()) if dub_elem and dub_elem.text.strip().isdigit() else 0
            eps_count = int(eps_count_elem.text.strip()) if eps_count_elem and eps_count_elem.text.strip().isdigit() else max(sub_count, dub_count)
            fdi_items = detail_div.select('.fd-infor .fdi-item') if detail_div else []
            anime_type = fdi_items[0].text.strip() if fdi_items else "UNKNOWN"
            duration = fdi_items[1].text.strip() if len(fdi_items) > 1 else ""
            result['results'].append({
                'id': anime_id,
                'title': title_elem.get('title', ''),
                'url': urljoin(self._base, title_elem['href']),
                'image': img_elem.get('data-src', '') if img_elem else '',
                'japanese_title': title_elem.get('data-jname', ''),
                'type': anime_type,
                'duration': duration,
                'sub': sub_count,
                'dub': dub_count,
                'episodes': eps_count,
                'nsfw': bool(poster_div.select_one('.tick-rate[title="18+"]')) if poster_div else False,
            })
        return result

    def search(self, query: str, page: int = 1) -> Dict:
        url = f"{self._base}/search?keyword={quote(query)}&page={page}"
        data = self._scrape_card_page(url)
        for item in data['results']:
            item['id'] = item['id'].replace('?ref=search', '')
            item['url'] = item['url'].replace('?ref=search', '')
        return data

    def info(self, anime_id: str) -> Dict:
        anime_url = f"{self._base}/{anime_id}"
        response = self._session.get(anime_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        result = {
            'id': anime_id, 'title': '', 'japanese_title': '', 'image': '', 'cover': '',
            'description': '', 'type': '', 'status': '', 'genres': [], 'aired': '',
            'premiered': '', 'duration': '', 'mal_score': '', 'studios': [], 'producers': [],
            'episodes': [], 'total_episodes': 0, 'mal_id': None, 'anilist_id': None,
            'url': anime_url, 'sub': 0, 'dub': 0, 'recommendations': [],
            'has_dub': False, 'has_sub': False, 'sub_or_dub': 'sub',
        }

        sync_data_script = soup.find('script', id='syncData')
        if sync_data_script and sync_data_script.string:
            try:
                data = json.loads(sync_data_script.string)
                result['mal_id'] = data.get('mal_id')
                result['anilist_id'] = data.get('anilist_id')
            except json.JSONDecodeError:
                pass

        title_elem = soup.select_one('h2.film-name')
        if title_elem:
            result['title'] = title_elem.text.strip()

        film_stats_div = soup.select_one('div.film-stats')
        if film_stats_div:
            type_elem = film_stats_div.select_one('span.item')
            duration_elem = film_stats_div.select('span.item')[-1]
            if type_elem:
                result['type'] = type_elem.text.strip().upper()
            if duration_elem and "m" in duration_elem.text:
                result['duration'] = duration_elem.text.strip()

        anisc_info_div = soup.select_one('div.anisc-info')
        if anisc_info_div:
            for item_div in anisc_info_div.select('.item.item-title, .item.item-list'):
                item_head_elem = item_div.select_one('.item-head')
                if not item_head_elem:
                    continue
                key = item_head_elem.text.strip().lower().replace(':', '')
                value_elems = item_div.select('a.name, span.name')
                if key == "japanese":
                    result['japanese_title'] = value_elems[0].text.strip() if value_elems else ''
                elif key == "aired":
                    result['aired'] = value_elems[0].text.strip() if value_elems else ''
                elif key == "premiered":
                    result['premiered'] = value_elems[0].text.strip() if value_elems else ''
                elif key == "duration" and not result['duration']:
                    result['duration'] = value_elems[0].text.strip() if value_elems else ''
                elif key == "status":
                    status_text = value_elems[0].text.strip() if value_elems else ''
                    if 'Finished' in status_text or 'Completed' in status_text:
                        result['status'] = 'COMPLETED'
                    elif 'Airing' in status_text:
                        result['status'] = 'ONGOING'
                    else:
                        result['status'] = 'UNKNOWN'
                elif key == "mal score":
                    result['mal_score'] = value_elems[0].text.strip() if value_elems else ''
                elif key == "genres":
                    result['genres'] = [elem.text.strip() for elem in item_div.select('a')]
                elif key == "studios":
                    result['studios'] = [elem.text.strip() for elem in value_elems]
                elif key == "producers":
                    result['producers'] = [elem.text.strip() for elem in value_elems]

        img_elem = soup.select_one('img.film-poster-img')
        if img_elem:
            result['image'] = img_elem.get('src', '')

        cover_elem = soup.select_one('.film-cover')
        if cover_elem:
            cover_style = cover_elem.get('style', '')
            cover_match = re.search(r"url\('(.*?)'\)", cover_style)
            if cover_match:
                result['cover'] = cover_match.group(1)

        desc_elem = soup.select_one('div.film-description div.text')
        if desc_elem:
            result['description'] = desc_elem.text.strip()

        sub_elem = soup.select_one('div.film-stats div.tick div.tick-item.tick-sub')
        dub_elem = soup.select_one('div.film-stats div.tick div.tick-item.tick-dub')
        result['sub'] = int(sub_elem.text.strip()) if sub_elem and sub_elem.text.strip().isdigit() else 0
        result['dub'] = int(dub_elem.text.strip()) if dub_elem and dub_elem.text.strip().isdigit() else 0
        result['has_sub'] = result['sub'] > 0
        result['has_dub'] = result['dub'] > 0
        if result['has_sub'] and result['has_dub']:
            result['sub_or_dub'] = "both"
        elif result['has_dub']:
            result['sub_or_dub'] = "dub"
        else:
            result['sub_or_dub'] = "sub"

        result['recommendations'] = []
        related_section = soup.select_one('.block_area-content .anif-block-ul')
        if related_section:
            for item in related_section.select('li'):
                title_elem = item.select_one('h3.film-name a')
                if not title_elem:
                    continue
                rec_id = title_elem['href'].strip('/')
                image_elem = item.select_one('img.film-poster-img')
                type_elem = item.select_one('.tick')
                anime_type = None
                if type_elem:
                    type_text = type_elem.text.strip()
                    if 'TV' in type_text:
                        anime_type = 'TV'
                    elif 'Movie' in type_text:
                        anime_type = 'MOVIE'
                    elif 'ONA' in type_text:
                        anime_type = 'ONA'
                result['recommendations'].append({
                    'id': rec_id,
                    'title': title_elem.get('title') or title_elem.text.strip(),
                    'image': image_elem.get('data-src') or image_elem.get('src', '') if image_elem else '',
                    'url': urljoin(self._base, title_elem['href']),
                    'type': anime_type,
                })

        ajax_id_elem = soup.select_one("div#wrapper[data-id]")
        ajax_episode_id = ajax_id_elem['data-id'] if ajax_id_elem else anime_id.split('-')[-1]
        if not ajax_episode_id:
            raise Exception("Could not find AJAX episode ID")

        episodes_ajax_url = f"{self._base}/ajax/v2/episode/list/{ajax_episode_id}"
        episodes_response = self._session.get(
            episodes_ajax_url,
            headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': anime_url}
        )
        episodes_response.raise_for_status()
        episodes_data = episodes_response.json()

        if 'html' in episodes_data:
            episodes_soup = BeautifulSoup(episodes_data['html'], 'html.parser')
            for ep_elem in episodes_soup.select('div.ss-list a.ssl-item.ep-item'):
                ep_href = ep_elem.get('href', '')
                if not ep_href:
                    continue
                parsed_href = urlparse(ep_href)
                ep_match = re.search(r'ep=(\d+)', parsed_href.query)
                if not ep_match:
                    continue
                actual_ep_id = ep_match.group(1)
                class_ep_id = f"{anime_id}$episode${actual_ep_id}"
                ep_number = int(ep_elem.get('data-number', '0'))
                ep_title = ep_elem.get('title', f"Episode {ep_number}")
                result['episodes'].append({
                    'id': class_ep_id,
                    'number': ep_number,
                    'title': ep_title,
                    'is_filler': 'filler' in ep_elem.get('class', []),
                    'url': urljoin(self._base, ep_href),
                    'is_subbed': ep_number <= result['sub'],
                    'is_dubbed': ep_number <= result['dub'],
                })
            result['total_episodes'] = len(result['episodes'])

        return result

    def watch(self, episode_id: str, audio_type: str = "sub", server: str = "vidcloud") -> Dict:
        if '$episode$' not in episode_id:
            raise ValueError("Invalid episode ID format.")

        anime_slug, ep_site_id = episode_id.split('$episode$')
        referer_url = f"{self._base}/watch/{anime_slug}"
        servers_ajax_url = f"{self._base}/ajax/v2/episode/servers?episodeId={ep_site_id}"

        servers_response = self._session.get(
            servers_ajax_url, headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': referer_url}
        )
        servers_response.raise_for_status()
        servers_soup = BeautifulSoup(servers_response.json()['html'], 'html.parser')

        target_block = servers_soup.select_one(f"div.ps_-block.servers-{audio_type.lower()}")
        if not target_block:
            fallback_audio_type = 'dub' if audio_type.lower() == 'sub' else 'sub'
            target_block = servers_soup.select_one(f"div.ps_-block.servers-{fallback_audio_type}")
            if not target_block:
                raise Exception(f"Neither {audio_type.upper()} nor {fallback_audio_type.upper()} server blocks found.")

        data_server_id = self._SERVER_NAME_TO_ID.get(server.lower())
        if not data_server_id:
            raise NotImplementedError(f"Server '{server}' is not supported.")

        server_item = target_block.select_one(f".server-item[data-server-id='{data_server_id}']")
        if not server_item:
            server_item = target_block.select_one(".server-item")
            if not server_item:
                raise Exception(f"Server '{server}' not found and no other servers available.")

        hianime_server_id = server_item['data-id']
        link_ajax_url = f"{self._base}/ajax/v2/episode/sources?id={hianime_server_id}"
        link_response = self._session.get(
            link_ajax_url, headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': referer_url}
        )
        link_response.raise_for_status()
        embed_url = link_response.json().get('link')

        if not embed_url:
            raise Exception("Could not retrieve embed URL.")

        if server.lower() not in ["vidcloud", "megacloud"]:
            raise NotImplementedError(f"Extractor for server '{server}' is not implemented.")

        return self._extract_sources(embed_url, referer_url, ep_site_id, audio_type)

    def _extract_sources(self, embed_url: str, referer: str, ep_site_id: str, audio_type: str) -> Dict:
        primary_data = None

        try:
            embed_host = urlparse(embed_url).netloc
            video_id = embed_url.split('/')[-1].split('?')[0]

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            key = loop.run_until_complete(_get_client_key(embed_url))

            if not key:
                raise Exception("No client key found for embed URL.")

            ajax_sources_url = f"https://{embed_host}/embed-2/v3/e-1/getSources?id={video_id}&_k={key}"
            ajax_response = self._session.get(
                ajax_sources_url,
                headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': referer}
            )
            ajax_response.raise_for_status()
            primary_data = ajax_response.json()

            decrypted_sources_list = []
            passphrase = _fetch_megacloud_key(self._session)
            if primary_data.get('encrypted') and isinstance(primary_data.get('sources'), str):
                decrypted_json_str = _decrypt_cryptojs_aes(primary_data['sources'], passphrase)
                decrypted_sources_list = json.loads(decrypted_json_str)
            elif isinstance(primary_data.get('sources'), list):
                decrypted_sources_list = primary_data['sources']

            if decrypted_sources_list:
                sources = [{'url': decrypted_sources_list[0]['file'], 'quality': 'auto', 'isM3U8': True}]
                subtitles = [
                    {'url': t['file'], 'lang': t.get('label', 'Default')}
                    for t in primary_data.get('tracks', []) if t.get('kind') in ['captions', 'subtitles']
                ]
                return {
                    'sources': sources,
                    'subtitles': subtitles,
                    'headers': {'Referer': embed_url},
                    'intro': primary_data.get('intro') or {'start': 0, 'end': 0},
                    'outro': primary_data.get('outro') or {'start': 0, 'end': 0},
                    'previews': [
                        {'url': t['file'], 'type': 'vtt'}
                        for t in primary_data.get('tracks', []) if t.get('kind') == 'thumbnails'
                    ],
                }

            raise Exception("No sources available from primary method.")

        except Exception as primary_err:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            yuma_keys_result = loop.run_until_complete(_extract_sources_via_yuma_keys(embed_url, primary_data))

            if yuma_keys_result and yuma_keys_result.get('sources'):
                return yuma_keys_result

            try:
                fallback_host = "megaplay.buzz"
                stream_url = f"https://{fallback_host}/stream/s-2/{ep_site_id}/{audio_type}"
                html_response = self._session.get(stream_url, headers={'Referer': f"https://{fallback_host}/"})
                html_response.raise_for_status()
                match = re.search(r'data-id=["\'](\d+)["\']', html_response.text)
                if not match:
                    raise Exception("data-id not found in fallback HTML")
                real_id = match.group(1)
                sources_url = f"https://{fallback_host}/stream/getSources"
                json_response = self._session.get(
                    sources_url, headers={'X-Requested-With': 'XMLHttpRequest'}, params={'id': real_id}
                )
                json_response.raise_for_status()
                fallback_data = json_response.json()
                file_url = fallback_data.get('sources', {}).get('file')
                if not file_url:
                    raise Exception("No source file URL found in fallback response.")
                subtitles = []
                if primary_data and isinstance(primary_data, dict):
                    subtitles = [
                        {'url': t['file'], 'lang': t.get('label', 'Default')}
                        for t in primary_data.get('tracks', []) if t.get('kind') in ['captions', 'subtitles']
                    ]
                else:
                    subtitles = fallback_data.get('tracks', [])
                return {
                    'sources': [{'url': file_url, 'quality': 'AUTO', 'isM3U8': True}],
                    'subtitles': subtitles,
                    'headers': {'Referer': stream_url},
                    'intro': (primary_data or fallback_data).get('intro', {'start': 0, 'end': 0}),
                    'outro': (primary_data or fallback_data).get('outro', {'start': 0, 'end': 0}),
                    'previews': [],
                }
            except Exception as fallback_err:
                raise Exception(
                    f"Primary extraction failed: [{primary_err}]. "
                    f"YumaKeys fallback failed. "
                    f"Final fallback also failed: [{fallback_err}]"
                )

    def recent_episodes(self, page: int = 1) -> Dict:
        return self._scrape_card_page(f"{self._base}/recently-updated?page={page}")

    def top_airing(self, page: int = 1) -> Dict:
        return self._scrape_card_page(f"{self._base}/top-airing?page={page}")

    def genres(self) -> List[str]:
        res = self._session.get(f"{self._base}/home")
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        return [a.text.lower().replace(' ', '-') for a in soup.select('#main-sidebar ul.sb-genre-list li a')]

    def genre_search(self, genre: str, page: int = 1) -> Dict:
        return self._scrape_card_page(f"{self._base}/genre/{genre}?page={page}")

    def studio_search(self, studio_id: str, page: int = 1) -> Dict:
        return self._scrape_card_page(f"{self._base}/producer/{studio_id}?page={page}")

    def schedule(self, date: str) -> List[Dict]:
        ajax_url = f"{self._base}/ajax/schedule/list?tzOffset=0&date={date}"
        res = self._session.get(ajax_url)
        res.raise_for_status()
        soup = BeautifulSoup(res.json()['html'], 'html.parser')
        results = []
        for item in soup.select('li'):
            link_elem = item.select_one('a.tsl-link')
            if not link_elem:
                continue
            href = link_elem.get('href')
            if not href:
                continue
            anime_id = href.strip('/')
            title_elem = link_elem.select_one('h3.film-name')
            time_elem = link_elem.select_one('div.time')
            episode_button = link_elem.select_one('div.fd-play button')
            result_item = {
                'id': anime_id,
                'title': title_elem.text.strip() if title_elem else '',
                'japanese_title': title_elem.get('data-jname', '') if title_elem else '',
                'url': urljoin(self._base, href),
                'type': 'SCHEDULED',
                'image': '',
                'other_data': {
                    'airingTime': time_elem.text.strip() if time_elem else '',
                    'airingEpisode': episode_button.text.strip() if episode_button else '',
                },
            }
            try:
                anime_info = self.info(anime_id)
                result_item['image'] = anime_info.get('image', '')
            except Exception:
                pass
            results.append(result_item)
        return results

    def spotlight(self) -> List[Dict]:
        res = self._session.get(f"{self._base}/home")
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        results = []
        for slide in soup.select('#slider .swiper-slide:not(.swiper-slide-duplicate)'):
            detail_link = slide.select_one('.desi-buttons a.btn-secondary')
            if not detail_link or not detail_link.get('href'):
                continue
            anime_id = detail_link.get('href').strip('/')
            title_elem = slide.select_one('.desi-head-title')
            img_elem = slide.select_one('img.film-poster-img')
            description_elem = slide.select_one('.desi-description')
            rank_elem = slide.select_one('.desi-sub-text')
            sc_detail = slide.select_one('.sc-detail')
            sc_items = sc_detail.select('.scd-item') if sc_detail else []
            anime_type = sc_items[0].text.strip() if len(sc_items) > 0 else "UNKNOWN"
            duration = sc_items[1].text.strip() if len(sc_items) > 1 else ""
            release_date = sc_items[2].text.strip() if len(sc_items) > 2 else ""
            tick_div = slide.select_one('.tick')
            sub_elem = tick_div.select_one('.tick-sub') if tick_div else None
            dub_elem = tick_div.select_one('.tick-dub') if tick_div else None
            eps_elem = tick_div.select_one('.tick-eps') if tick_div else None
            sub_count = int(sub_elem.text.strip()) if sub_elem and sub_elem.text.strip().isdigit() else 0
            dub_count = int(dub_elem.text.strip()) if dub_elem and dub_elem.text.strip().isdigit() else 0
            eps_count = int(eps_elem.text.strip()) if eps_elem and eps_elem.text.strip().isdigit() else 0
            results.append({
                'id': anime_id,
                'title': title_elem.text.strip() if title_elem else '',
                'japanese_title': title_elem.get('data-jname', '') if title_elem else '',
                'image': img_elem.get('data-src') if img_elem else '',
                'url': urljoin(self._base, detail_link['href']),
                'type': anime_type,
                'duration': duration,
                'sub': sub_count,
                'dub': dub_count,
                'episodes': eps_count,
                'other_data': {
                    'description': description_elem.text.strip() if description_elem else '',
                    'rank': rank_elem.text.strip() if rank_elem else '',
                    'releaseDate': release_date,
                },
            })
        return results

    def search_suggestions(self, query: str) -> List[Dict]:
        ajax_url = f"{self._base}/ajax/search/suggest?keyword={quote(query)}"
        res = self._session.get(ajax_url)
        res.raise_for_status()
        soup = BeautifulSoup(res.json()['html'], 'html.parser')
        results = []
        for item in soup.select('a.nav-item:not(.nav-bottom)'):
            href = item.get('href')
            if not href:
                continue
            anime_id = href.strip('/')
            title_elem = item.select_one('h3.film-name')
            img_elem = item.select_one('img.film-poster-img')
            film_infor = item.select_one('.film-infor')
            title_text = title_elem.text.strip() if title_elem else ''
            japanese_title = title_elem.get('data-jname', '') if title_elem else ''
            release_date_elem = film_infor.select_one('span') if film_infor else None
            release_date = release_date_elem.text.strip() if release_date_elem else ''
            info_texts = [text.strip() for text in film_infor.find_all(string=True, recursive=False) if text.strip()] if film_infor else []
            anime_type = info_texts[0] if info_texts else ''
            duration_span = film_infor.select('span')[-1] if film_infor and film_infor.select('span') else None
            duration = duration_span.text.strip() if duration_span and duration_span != release_date_elem else ''
            alias_elem = item.select_one('.alias-name')
            results.append({
                'id': anime_id,
                'title': alias_elem.text.strip() if alias_elem else title_text,
                'japanese_title': japanese_title,
                'image': img_elem.get('data-src') if img_elem else '',
                'url': urljoin(self._base, href),
                'type': anime_type,
                'duration': duration,
                'other_data': {
                    'releaseDate': release_date,
                    'alias': alias_elem.text.strip() if alias_elem else '',
                },
            })
        return results

    def next_ep(self, anime_id: str, timezone_abbr: str = "BST") -> Dict:
        tz_abbr = timezone_abbr.upper()
        if tz_abbr not in TZ_MAP:
            tz_abbr = "BST"

        if ZoneInfo is None:
            return {'found': False, 'message': 'zoneinfo module not available.'}

        iana_tz = TZ_MAP[tz_abbr]
        user_tz = ZoneInfo(iana_tz)
        utc_now = datetime.now(timezone.utc)

        for i in range(8):
            search_date = utc_now.date() + timedelta(days=i)
            date_str = search_date.strftime('%Y-%m-%d')
            try:
                schedule_data = self.schedule(date_str)
            except Exception:
                continue
            for anime in schedule_data:
                if anime['id'] == anime_id:
                    airing_time_str = anime.get('other_data', {}).get('airingTime')
                    if airing_time_str:
                        try:
                            hour, minute = map(int, airing_time_str.split(':'))
                            airing_datetime_utc = datetime(
                                search_date.year, search_date.month, search_date.day,
                                hour, minute, tzinfo=timezone.utc
                            )
                            if airing_datetime_utc > utc_now:
                                time_remaining = airing_datetime_utc - utc_now
                                airing_datetime_local = airing_datetime_utc.astimezone(user_tz)
                                days = time_remaining.days
                                hours, remainder = divmod(time_remaining.seconds, 3600)
                                minutes, seconds = divmod(remainder, 60)
                                return {
                                    'found': True,
                                    'animeId': anime['id'],
                                    'title': anime['title'],
                                    'episode': anime.get('other_data', {}).get('airingEpisode'),
                                    'airingAtUTC': airing_datetime_utc.isoformat(),
                                    'airingAtLocal': airing_datetime_local.isoformat(),
                                    'localTimezone': airing_datetime_local.tzname(),
                                    'countdown': f"{days} days, {hours} hours, {minutes} minutes, {seconds} seconds",
                                }
                        except ValueError:
                            continue

        return {'found': False, 'message': 'No upcoming episode found for this anime in the next 7 days.'}

    def trailer(self, anime_id: str) -> Dict:
        if anime_id.isdigit():
            result = self._get_anilist_trailer(int(anime_id))
            if result and 'error' not in result:
                return result

        try:
            anime_info = self.info(anime_id)
            anilist_id = anime_info.get('anilist_id')
            if anilist_id:
                result = self._get_anilist_trailer(int(anilist_id))
                if result and 'error' not in result:
                    return result
        except Exception:
            pass

        return {'error': 'Trailer not found.'}

    def _get_anilist_trailer(self, anime_id: int) -> Dict:
        graphql_query = """
        query ($id: Int) {
          Media(id: $id, type: ANIME) {
            trailer {
              id
              site
              thumbnail
            }
          }
        }
        """
        try:
            response = requests.post("https://graphql.anilist.co", json={"query": graphql_query, "variables": {"id": anime_id}})
            response.raise_for_status()
            data = response.json()
            media_data = data.get('data', {}).get('Media')
            if not media_data:
                return {"error": f"No media found for AniList ID {anime_id}."}
            trailer_info = media_data.get('trailer')
            if trailer_info and trailer_info.get('site') == 'youtube' and trailer_info.get('id'):
                return {
                    "id": trailer_info['id'],
                    "url": f"https://www.youtube.com/watch?v={trailer_info['id']}",
                    "embed_url": f"https://www.youtube.com/embed/{trailer_info['id']}",
                    "site": "youtube",
                    "thumbnail": trailer_info.get('thumbnail'),
                }
            return {"error": "Trailer not found for given AniList ID."}
        except Exception as e:
            return {"error": str(e)}


yuma = YumaAPI()


def clean_description(description):
    if not description:
        return "No description available."
    cleaned = re.sub(r'(\r\n)?\r?\n?\[Written by MAL Rewrite\]', '', description, flags=re.IGNORECASE).strip()
    return cleaned


def check_executable(name):
    return shutil.which(name) is not None


def display_search_results(results, title="Search Results"):
    if not results or not results.get("results"):
        console.print("[yellow]No results found.[/yellow]")
        return

    try:
        term_width = console.size.width
    except Exception:
        term_width = 120

    id_width = 32 if term_width < 100 else 40
    title_width = max(20, term_width // 5)

    table = Table(title=f"[bold cyan]{title}[/bold cyan]", show_header=True, header_style="bold magenta")
    table.add_column("ID", style="dim", width=id_width)
    table.add_column("Title", style="bold white", min_width=title_width)
    table.add_column("Type", style="green", width=8)
    table.add_column("Sub", style="blue", width=5)
    table.add_column("Dub", style="red", width=5)
    table.add_column("Duration", style="yellow", width=10)

    for item in results["results"]:
        anime_id = item.get("id", "N/A")
        title_text = Text(item.get("title", "N/A"), style="bold white")
        title_text.stylize("link pyanimecli -i {}".format(anime_id))
        table.add_row(
            anime_id,
            title_text,
            item.get("type", "N/A"),
            str(item.get("sub", "0")),
            str(item.get("dub", "0")),
            item.get("duration", "N/A")
        )

    console.print(table)
    console.print(f"Page [bold]{results.get('current_page', 1)}[/bold] of [bold]{results.get('total_pages', 1)}[/bold]. Use -p <page_number> to navigate.")


def display_anime_info(info):
    if not info:
        console.print("[bold red]Could not retrieve anime info.[/bold red]")
        return

    title = info.get("title", "No Title")
    description = clean_description(info.get("description"))

    info_text = Text()
    info_text.append(f"ID: ", style="bold magenta")
    info_text.append(f"{info.get('id', 'N/A')}\n")
    info_text.append(f"Type: ", style="bold magenta")
    info_text.append(f"{info.get('type', 'N/A')}\n")
    info_text.append(f"Total Episodes: ", style="bold magenta")
    info_text.append(f"{info.get('total_episodes', 'N/A')}\n")
    info_text.append(f"Sub Episodes: ", style="bold magenta")
    info_text.append(f"{info.get('sub', 'N/A')}\n")
    info_text.append(f"Dub Episodes: ", style="bold magenta")
    info_text.append(f"{info.get('dub', 'N/A')}\n")
    info_text.append(f"Status: ", style="bold magenta")
    info_text.append(f"{info.get('status', 'N/A')}\n")
    info_text.append(f"Genres: ", style="bold magenta")
    info_text.append(f"{', '.join(info.get('genres', ['N/A']))}\n")
    info_text.append(f"Image: ", style="bold magenta")
    info_text.append(f"{info.get('image', '')}\n", style="cyan")
    info_text.append(f"Aname: ", style="bold magenta")
    info_text.append(f"https://aname.vercel.app/details/{info.get('id', 'N/A')}", style="cyan")

    img_url = info.get("image")
    if img_url and Pixels and Image:
        try:
            img_data = requests.get(img_url, timeout=5).content
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_data)
                tmp_path = tmp.name
            with Image.open(tmp_path) as img:
                img = img.resize((32, 42))
                pixels = Pixels.from_image(img)
            console.print(Panel(pixels, title="[bold]Anime Image[/bold]", border_style="cyan"))
            os.remove(tmp_path)
        except Exception as e:
            console.print(f"[yellow]Could not render image: {e}[/yellow]")

    console.print(Panel(info_text, title=f"[bold green]{title}[/bold green]", border_style="green", expand=False))
    console.print(Panel(description, title="[bold]Description[/bold]", border_style="blue"))

    episodes = info.get("episodes", [])
    if episodes:
        episode_table = Table(title="[bold cyan]Episodes[/bold cyan]", show_header=True, header_style="bold magenta")
        episode_table.add_column("Ep #", style="dim")
        episode_table.add_column("Title", style="bold white")
        episode_table.add_column("Episode ID", style="dim")

        for ep in episodes:
            ep_id = ep.get("id", "N/A")
            ep_title = Text(ep.get("title", "N/A"), style="bold white")
            episode_table.add_row(
                str(ep.get("number", "N/A")),
                ep_title,
                ep_id
            )
        console.print(episode_table)


def sanitize_filename(name):
    if not name:
        return "download"
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', '_', name)
    return name.strip()


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        return False


def install_ffmpeg_windows():
    try:
        console.print("Attempting to install FFmpeg using Chocolatey...")
        subprocess.run(["choco", "install", "ffmpeg", "-y"], check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        console.print("[yellow]Chocolatey installation failed.[/yellow]")
        console.print("\nTo install FFmpeg manually:")
        console.print("1. Visit [link=https://github.com/BtbN/FFmpeg-Builds/releases]https://github.com/BtbN/FFmpeg-Builds/releases[/link]")
        console.print("2. Download the latest ffmpeg-master-latest-win64-gpl.zip")
        console.print("3. Extract the contents")
        console.print("4. Add the bin folder to your system's PATH environment variable")
        return False


def display_next_ep(data):
    if not data or not data.get("found"):
        console.print("[yellow]No next episode info found.[/yellow]")
        return
    table = Table(title=f"[bold cyan]Next Episode: {data.get('title', '')}[/bold cyan]", show_header=True, header_style="bold magenta")
    table.add_column("Episode", style="bold white")
    table.add_column("Airing At (Local)", style="green")
    table.add_column("Airing At (UTC)", style="blue")
    table.add_column("Countdown", style="yellow")
    table.add_row(
        data.get("episode", "N/A"),
        data.get("airingAtLocal", "N/A"),
        data.get("airingAtUTC", "N/A"),
        data.get("countdown", "N/A")
    )
    console.print(table)
    console.print(f"Timezone: [bold]{data.get('localTimezone', 'N/A')}[/bold]")


def display_trailer(data):
    if not data or data.get("error"):
        console.print(f"[yellow]No trailer found. {data.get('error', '')}[/yellow]")
        return
    table = Table(title=f"[bold cyan]Trailer[/bold cyan]", show_header=True, header_style="bold magenta")
    table.add_column("Site", style="bold white")
    table.add_column("URL", style="blue")
    table.add_column("Thumbnail", style="green")
    table.add_row(
        data.get("site", "N/A"),
        data.get("url", "N/A"),
        data.get("thumbnail", "N/A")
    )
    console.print(table)
    console.print(f"Embed URL: [cyan]{data.get('embed_url', '')}[/cyan]")


def check_yt_dlp():
    return check_executable("yt-dlp")


def check_ffplay():
    return check_executable("ffplay")


def play_trailer(url):
    if check_executable("vlc"):
        player = "vlc"
    elif check_ffplay():
        player = "ffplay"
    else:
        player = None

    def try_install_ytdlp():
        console.print("[yellow]yt-dlp not found or broken. Attempting auto-install...[/yellow]")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "yt-dlp"])
        return result.returncode == 0 and check_yt_dlp()

    ytdlp_ok = check_yt_dlp()
    if not ytdlp_ok:
        if not try_install_ytdlp():
            console.print("[bold red]yt-dlp installation failed. Please install manually: pip install yt-dlp[/bold red]")
            return
        ytdlp_ok = True

    if not player:
        console.print("[bold red]Neither VLC nor ffplay found. Cannot play trailer.")
        choice = input("Download trailer video instead? (y/n): ").lower()
        if choice == "y":
            if not ytdlp_ok and not try_install_ytdlp():
                console.print("[bold red]yt-dlp installation failed. Cannot download trailer.[/bold red]")
                return
            subprocess.run(["yt-dlp", url])
        return

    if player == "vlc":
        cmd = f'yt-dlp -o - "{url}" | vlc -'
    else:
        cmd = f'yt-dlp -o - "{url}" | ffplay -'
    console.print(f"[bold green]Playing trailer with {player}...[bold green]")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0 and not ytdlp_ok:
        if try_install_ytdlp():
            console.print("[yellow]Retrying trailer playback after yt-dlp install...[/yellow]")
            subprocess.run(cmd, shell=True)


def download_episode(episode_id, download_type, output_path=None):
    if not check_ffmpeg():
        console.print("[bold red]FFmpeg is required but not found.[/bold red]")
        if platform.system() == "Windows":
            response = input("Would you like to attempt automatic installation? (y/n): ").lower()
            if response == 'y':
                if not install_ffmpeg_windows():
                    return
            else:
                return
        else:
            console.print("Please install FFmpeg using your system's package manager.")
            return

    if not output_path:
        console.print("Auto-generating filename (requires fetching anime info)...")
        try:
            anime_id = episode_id.split("$episode$")[0]
            anime_info = _api_request(lambda: yuma.info(anime_id))
            if not anime_info:
                raise ValueError("Failed to get anime info for filename generation.")
            anime_title = anime_info.get("title", "Unknown_Anime")
            ep_num = "Unknown"
            for ep in anime_info.get("episodes", []):
                if ep.get("id") == episode_id:
                    ep_num = str(ep.get("number", "Unknown")).zfill(2)
                    break
            safe_title = sanitize_filename(anime_title)
            output_path = f"./{safe_title}-Episode-{ep_num}-[{download_type}].mp4"
        except Exception as e:
            console.print(f"[bold red]Could not generate filename:[/bold red] {e}. Aborting download.")
            return

    console.print(f"Preparing to download to: [green]{os.path.abspath(output_path)}[/green]")
    console.print("Fetching stream data...")
    stream_data = _api_request(lambda: yuma.watch(episode_id, download_type))

    if not stream_data or not stream_data.get("sources"):
        console.print("[bold red]Could not retrieve stream sources for download.[/bold red]")
        return

    stream_url = stream_data["sources"][0].get("url")
    if not stream_url:
        console.print("[bold red]Incomplete stream data received.[/bold red]")
        return

    try:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            stream_url
        ]
        try:
            duration = float(subprocess.check_output(probe_cmd).decode().strip())
        except Exception:
            duration = 0
            console.print("[yellow]Could not determine video duration.[/yellow]")

        console.print("Starting video download...")

        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", stream_url,
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            output_path
        ]

        process = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)
        time_pattern = re.compile(r'time=(\d+):(\d+):(\d+\.?\d*)')

        with tqdm(total=duration, unit="sec", desc="Downloading", disable=None) as pbar:
            for line in process.stderr:
                match = time_pattern.search(line)
                if match:
                    h, m, s = match.groups()
                    seconds = int(h) * 3600 + int(m) * 60 + float(s)
                    pbar.n = min(seconds, duration)
                    pbar.refresh()

        process.wait()

        if process.returncode == 0:
            console.print(f"\n[bold green]Video download complete![/bold green]")
        else:
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_cmd)

    except Exception as e:
        console.print(f"[bold red]An error occurred during video download:[/bold red] {e}")
        return

    if download_type == "sub" and stream_data.get("subtitles"):
        sub_url = stream_data["subtitles"][0].get("url")
        if sub_url:
            sub_filename = os.path.splitext(output_path)[0] + ".vtt"
            console.print(f"Downloading subtitles to [cyan]{sub_filename}[/cyan]...")
            try:
                sub_response = requests.get(sub_url)
                sub_response.raise_for_status()
                with open(sub_filename, 'wb') as f:
                    f.write(sub_response.content)
                console.print("[green]Subtitle download complete.[/green]")
            except requests.exceptions.RequestException as e:
                console.print(f"[bold red]Failed to download subtitles:[/bold red] {e}")


def get_and_download_episode(anime_id, ep_num_str, download_type, output_path=None):
    try:
        episode_number = int(ep_num_str)
    except ValueError:
        console.print(f"[bold red]Error:[/bold red] Episode number must be an integer. You provided '{ep_num_str}'.")
        return

    console.print(f"Fetching info for anime [cyan]{anime_id}[/cyan] to find episode {episode_number}...")
    data = _api_request(lambda: yuma.info(anime_id))

    if not data or not data.get("episodes"):
        console.print(f"[bold red]Could not retrieve info or episode list for anime ID '{anime_id}'.[/bold red]")
        return

    target_episode = next((ep for ep in data["episodes"] if ep.get("number") is not None and int(ep.get("number")) == episode_number), None)

    if target_episode and target_episode.get("id"):
        episode_id = target_episode["id"]
        console.print(f"Found Episode ID: [green]{episode_id}[/green]. Proceeding to download...")

        if not output_path:
            anime_title = data.get("title", "Unknown_Anime")
            ep_num = str(target_episode.get("number", "Unknown")).zfill(2)
            safe_title = sanitize_filename(anime_title)
            output_path = f"./{safe_title}-Episode-{ep_num}-[{download_type}].mp4"

        download_episode(episode_id, download_type, output_path)
    else:
        console.print(f"[bold red]Could not find episode number {episode_number} for this anime.[/bold red]")
        console.print("Use the -i <anime_id> command to see a list of available episodes.")

def check_for_updates():
    if semver is None:
        console.print("[yellow]Skipping update check: 'packaging' library not found. Install with 'pip install packaging'[/yellow]")
        return
    try:
        console.print("Checking for updates...")
        url = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        latest_version_str = response.json()["info"]["version"]
        current_version = semver.parse(__version__)
        latest_version = semver.parse(latest_version_str)
        if latest_version > current_version:
            console.print(f"\n[bold yellow]A new version is available: {latest_version_str}[/bold yellow]")
            console.print(f"To update, run: [cyan]pip install --upgrade {PACKAGE_NAME}[/cyan]")
        else:
            console.print("[green]You are using the latest version.[/green]")
    except Exception:
        console.print("[yellow]Could not check for updates.[/yellow]")

def handle_update_check():
    if semver is None:
        no = "no"
        return
    try:
        console.print("Checking for updates...")
        url = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        latest_version_str = response.json()["info"]["version"]
        current_version = semver.parse(__version__)
        latest_version = semver.parse(latest_version_str)
        if latest_version > current_version:
            console.print(f"\n[bold yellow]A new version is available: {latest_version_str}[/bold yellow]")
            console.print(f"To update, run: [cyan]pyanimecli -u[/cyan]")
            return True
        else:
            return False
    except Exception:
        return False

def get_and_watch_episode(anime_id, ep_num_str, watch_type, player_override=None):
    try:
        episode_number = int(ep_num_str)
    except ValueError:
        console.print(f"[bold red]Error:[/bold red] Episode number must be an integer. You provided '{ep_num_str}'.")
        return

    console.print(f"Fetching info for anime [cyan]{anime_id}[/cyan] to find episode {episode_number}...")
    data = _api_request(lambda: yuma.info(anime_id))

    if not data or not data.get("episodes"):
        console.print(f"[bold red]Could not retrieve info or episode list for anime ID '{anime_id}'.[/bold red]")
        return

    target_episode = None
    for ep in data["episodes"]:
        if ep.get("number") is not None and int(ep.get("number")) == episode_number:
            target_episode = ep
            break

    if target_episode and target_episode.get("id"):
        episode_id = target_episode["id"]
        console.print(f"Found Episode ID: [green]{episode_id}[/green]. Proceeding to watch...")
        watch_episode(episode_id, watch_type, player_override=player_override)
    else:
        console.print(f"[bold red]Could not find episode number {episode_number} for this anime.[/bold red]")
        console.print("Use the -i <anime_id> command to see a list of available episodes.")


def display_spotlight(spotlight_data):
    if not spotlight_data:
        console.print("[yellow]No spotlight data found.[/yellow]")
        return

    console.print("[bold yellow]🌟 Spotlight 🌟[/bold yellow]")
    for item in spotlight_data:
        rank = item.get("other_data", {}).get("rank", "")
        title = item.get("title", "N/A")
        description = clean_description(item.get("other_data", {}).get("description", ""))
        release_date = item.get("other_data", {}).get("releaseDate", "N/A")

        panel_content = Text()
        panel_content.append(f"ID: ", style="bold magenta")
        panel_content.append(f"{item.get('id', 'N/A')}\n")
        panel_content.append(f"Release Date: ", style="bold magenta")
        panel_content.append(f"{release_date}\n\n")
        panel_content.append(description)

        console.print(Panel(
            panel_content,
            title=f"[bold green]{rank}: {title}[/bold green]",
            border_style="green"
        ))


def display_schedule(schedule_data, date):
    if not schedule_data:
        console.print(f"[yellow]No schedule found for {date}.[/yellow]")
        return

    table = Table(title=f"[bold cyan]Airing Schedule for {date}[/bold cyan]", show_header=True, header_style="bold magenta")
    table.add_column("Time (UTC)", style="yellow")
    table.add_column("Title", style="bold white")
    table.add_column("Airing Episode", style="green")
    table.add_column("ID", style="dim")

    for item in schedule_data:
        other_data = item.get("other_data", {})
        table.add_row(
            other_data.get("airingTime", "N/A"),
            item.get("title", "N/A"),
            other_data.get("airingEpisode", "N/A"),
            item.get("id", "N/A")
        )
    console.print(table)


def display_suggestions(suggestions_data):
    if not suggestions_data:
        console.print("[yellow]No suggestions found.[/yellow]")
        return

    table = Table(title="[bold cyan]Search Suggestions[/bold cyan]", show_header=True, header_style="bold magenta")
    table.add_column("Title", style="bold white")
    table.add_column("Alias", style="dim")
    table.add_column("Release Date", style="green")
    table.add_column("ID", style="dim")

    for item in suggestions_data:
        other_data = item.get("other_data", {})
        table.add_row(
            item.get("title", "N/A"),
            other_data.get("alias", "N/A"),
            other_data.get("releaseDate", "N/A"),
            item.get("id", "N/A")
        )
    console.print(table)


def _api_request(fn):
    spinner = Spinner("dots", text=Text("Fetching data...", style="cyan"))
    with Live(spinner, console=console, transient=True, refresh_per_second=20):
        try:
            return fn()
        except requests.exceptions.RequestException as e:
            console.print(f"[bold red]Request Error:[/bold red] {e}")
            return None
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            return None


def watch_episode(episode_id, watch_type, player_override=None):
    has_vlc = check_executable("vlc")
    has_ffplay = check_ffplay()
    
    if not has_vlc and not has_ffplay:
        console.print("[bold red]Neither VLC nor ffplay found.[/bold red] Please install one and ensure it's in your system's PATH.")
        return

    player = "vlc" if has_vlc else "ffplay"
    if player_override:
            if player_override in ["vlc", "ffplay"]:
                player = player_override
            else:
                console.print(f"[yellow]Unknown player '{player_override}'. Using {player}.[/yellow]")
    is_windows = platform.system() == "Windows"
    downloader = "curl" if is_windows else "wget"

    if not check_executable(downloader):
        console.print(f"[bold red]{downloader.capitalize()} not found.[/bold red] Please install it and ensure it's in your system's PATH.")
        return

    data = _api_request(lambda: yuma.watch(episode_id, watch_type))

    if not data or not data.get("sources"):
        console.print("[bold red]Could not retrieve stream sources.[/bold red]")
        return

    stream_url = data["sources"][0].get("url")
    referrer = data.get("headers", {}).get("Referer", "")

    if not stream_url:
        console.print("[bold red]Incomplete stream data received.[/bold red]")
        return

    sub_file_path = None
    
    if player == "vlc":
        vlc_command = ["vlc", stream_url]
        if referrer:
            vlc_command.append(f"--http-referrer={referrer}")
        
        if watch_type == "sub" and data.get("subtitles"):
            subs = data["subtitles"]
            chosen_sub = None

            if len(subs) == 1:
                chosen_sub = subs[0]
                console.print(f"Only one subtitle available: [cyan]{chosen_sub['lang']}[/cyan]")
            else:
                console.print("\nAvailable subtitles:")
                for idx, sub in enumerate(subs, start=1):
                    console.print(f"[{idx}] {sub['lang']}")

                while True:
                    try:
                        choice = int(input("\nEnter the number of the subtitle you want: "))
                        if 1 <= choice <= len(subs):
                            chosen_sub = subs[choice - 1]
                            break
                        else:
                            console.print("[bold red]Invalid choice. Try again.[/bold red]")
                    except ValueError:
                        console.print("[bold red]Please enter a valid number.[/bold red]")

            if chosen_sub:
                sub_url = chosen_sub.get("url")
                if sub_url:
                    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".vtt") as tmp_file:
                        sub_file_path = tmp_file.name

                    console.print(f"Downloading subtitles ([cyan]{chosen_sub['lang']}[/cyan]) to [cyan]{sub_file_path}[/cyan]...")

                    if is_windows:
                        download_cmd = ["curl", "-s", "-L", "-o", sub_file_path, sub_url]
                    else:
                        download_cmd = ["wget", "-q", "-O", sub_file_path, sub_url]

                    try:
                        subprocess.run(download_cmd, check=True)
                        vlc_command.append(f"--sub-file={sub_file_path}")
                        console.print("[green]Subtitle download complete.[/green]")
                    except (subprocess.CalledProcessError, FileNotFoundError) as e:
                        console.print(f"[bold red]Failed to download subtitles:[/bold red] {e}")
                        sub_file_path = None

        command_str = ' '.join(f'"{c}"' if ' ' in c else c for c in vlc_command)
        console.print(f"\n[bold]Executing command:[/bold]\n[yellow]{command_str}[/yellow]\n")

        try:
            subprocess.run(vlc_command)
        except Exception as e:
            console.print(f"[bold red]Failed to launch VLC:[/bold red] {e}")
        finally:
            if sub_file_path and os.path.exists(sub_file_path):
                os.remove(sub_file_path)
    
    else:
        ffplay_command = ["ffplay", "-autoexit", stream_url]
        
        if watch_type == "sub" and data.get("subtitles"):
            console.print("[yellow]Note: ffplay has limited subtitle support. Consider installing VLC for better subtitle handling.[/yellow]")

        console.print(f"\n[bold]Playing with ffplay...[/bold]\n")

        try:
            subprocess.run(ffplay_command)
        except Exception as e:
            console.print(f"[bold red]Failed to launch ffplay:[/bold red] {e}")


def search(query, page=1, pretty_print=False):
    data = _api_request(lambda: yuma.search(query, page))
    if pretty_print:
        display_search_results(data)
        return None
    if data and data.get("results"):
        return data["results"]
    return []


def info(anime_id, pretty_print=False):
    data = _api_request(lambda: yuma.info(anime_id))
    if pretty_print:
        display_anime_info(data)
        return None
    return data if data else {}


def recent_episodes(page=1, pretty_print=False):
    data = _api_request(lambda: yuma.recent_episodes(page))
    if pretty_print:
        display_search_results(data, title="Recently Updated Episodes")
        return None
    if data and data.get("results"):
        return data["results"]
    return []


def top_airing(page=1, pretty_print=False):
    data = _api_request(lambda: yuma.top_airing(page))
    if pretty_print:
        display_search_results(data, title="Top Airing Anime")
        return None
    if data and data.get("results"):
        return data["results"]
    return []


def genres(pretty_print=False):
    data = _api_request(lambda: yuma.genres())
    if pretty_print:
        if data:
            console.print(Panel(", ".join(data), title="[bold cyan]Available Genres[/bold cyan]", border_style="cyan"))
        return None
    return data if data else []


def genre_search(genre, page=1, pretty_print=False):
    data = _api_request(lambda: yuma.genre_search(genre, page))
    if pretty_print:
        display_search_results(data, title=f"Results for Genre: {genre.capitalize()}")
        return None
    if data and data.get("results"):
        return data["results"]
    return []


def studio_search(studio_id, page=1, pretty_print=False):
    data = _api_request(lambda: yuma.studio_search(studio_id, page))
    if pretty_print:
        display_search_results(data, title=f"Results for Studio: {studio_id}")
        return None
    if data and data.get("results"):
        return data["results"]
    return []


def schedule(date, pretty_print=False):
    data = _api_request(lambda: yuma.schedule(date))
    if pretty_print:
        display_schedule(data, date)
        return None
    return data if data else []


def spotlight(pretty_print=False):
    data = _api_request(lambda: yuma.spotlight())
    if pretty_print:
        display_spotlight(data)
        return None
    return data if data else []


def search_suggestions(query, pretty_print=False):
    data = _api_request(lambda: yuma.search_suggestions(query))
    if pretty_print:
        display_suggestions(data)
        return None
    return data if data else []


def download(episode_id_or_anime_id, ep_num_or_type, dl_type=None, output_path=None):
    if "$episode$" in episode_id_or_anime_id:
        episode_id = episode_id_or_anime_id
        download_type = ep_num_or_type
        return download_episode(episode_id, download_type, output_path)
    else:
        anime_id = episode_id_or_anime_id
        ep_num_str = ep_num_or_type
        return get_and_download_episode(anime_id, ep_num_str, dl_type, output_path)


def watch(episode_id_or_anime_id, ep_num_or_type, watch_type=None, player_override=None):
    if "$episode$" in episode_id_or_anime_id:
        episode_id = episode_id_or_anime_id
        return watch_episode(episode_id, ep_num_or_type, player_override=player_override)
    else:
        anime_id = episode_id_or_anime_id
        ep_num_str = ep_num_or_type
        return get_and_watch_episode(anime_id, ep_num_str, watch_type, player_override=player_override)

def version():
    return __version__


def check_updates():
    check_for_updates()


def next_ep(anime_id, timezone=DEFAULT_TZ, pretty_print=False):
    if timezone not in TIMEZONES:
        timezone = DEFAULT_TZ
    data = _api_request(lambda: yuma.next_ep(anime_id, timezone))
    if pretty_print:
        display_next_ep(data)
        return None
    return data if data else {}


def trailer(anime_id, play=False, pretty_print=False):
    data = _api_request(lambda: yuma.trailer(anime_id))
    if pretty_print:
        display_trailer(data)
    if play and data and data.get("url"):
        play_trailer(data["url"])
    return data if data else {}


def display_help(command=None):
    console.print(Panel(f"[bold yellow]pyanimecli v{__version__} - A CLI for Watching & Downloading Anime[/bold yellow]", expand=False, border_style="yellow"))

    help_data = {
        "search": ("-s, -search <query>", "Search for an anime."),
        "info": ("-i, -info <id>", "Get detailed information about an anime by its ID."),
        "watch": ("-w, -watch <id> <ep#> <type> [player] | <ep_id> <type> [player]", "Watch an episode."),
        "download": ("-d, -download <id> <ep#> <type> [out] | <ep_id> <type> [out]", "Download an episode. '[out]' is an optional file path."),
        "recent": ("-re, -recent-episodes", "List recently updated episodes."),
        "top_airing": ("-ta, -top-airing", "List top airing anime."),
        "genres": ("-g, -genres", "List all available genres."),
        "genre_search": ("-gs, -genre-search <genre>", "Search for anime by a specific genre."),
        "studio": ("-st, -studio <studio_id>", "Search for anime by a studio ID."),
        "schedule": ("-sc, -schedule <YYYY-MM-DD>", "Get the airing schedule for a specific date."),
        "spotlight": ("-sp, -spotlight", "Show spotlight anime."),
        "suggestions": ("-ss, -search-suggestions <query>", "Get search suggestions for a query."),
        "next_ep": ("-ne, -next-ep <anime_id> [timezone]", "Get next episode info. Optionally specify a timezone (default BST)."),
        "trailer": ("-tr, -trailer <anime_id> [play]", "Get trailer info for an anime. Add 'play' to play the trailer."),
        "pagination": ("-p, -page <number>", "Used with commands that support pages (search, recent, etc.)."),
        "version": ("-v, -version", "Show the script version and check for updates.")
    }

    if command and command in help_data:
        usage, desc = help_data[command]
        console.print(f"\n[bold]Help for '{command}':[/bold]")
        console.print(f"  [cyan]Usage:[/cyan] {usage}")
        console.print(f"  [cyan]Description:[/cyan] {desc}")
    else:
        table = Table(title="[bold]Available Commands[/bold]", show_header=False, box=None)
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description")
        for key, (usage, desc) in help_data.items():
            table.add_row(usage, desc)
        console.print(table)
        console.print("\nUse -h <command_name> (e.g., -h download) for specific command help.")


def main():
    parser = argparse.ArgumentParser(description=f"pyanimecli v{__version__} - A CLI for anime.", add_help=False)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-s', '-search', dest='search', nargs='+', help='Search for an anime.')
    group.add_argument('-i', '-info', dest='info', help='Get info for an anime by ID.')
    group.add_argument('-w', '-watch', dest='watch', nargs='+', metavar=('ID', '...'), help='Watch an episode. Usage: <id> <ep#> <type> [player] or <ep_id> <type> [player]')
    group.add_argument('-d', '-download', dest='download', nargs='+', metavar=('ID', '...'), help='Download an episode. See -h download.')
    group.add_argument('-re', '-recent-episodes', dest='recent', action='store_true', help='Get recent episodes.')
    group.add_argument('-ta', '-top-airing', dest='top_airing', action='store_true', help='Get top airing anime.')
    group.add_argument('-g', '-genres', dest='genres', action='store_true', help='List all genres.')
    group.add_argument('-gs', '-genre-search', dest='genre_search', nargs='+', help='Search by genre.')
    group.add_argument('-st', '-studio', dest='studio', nargs='+', help='Search by studio.')
    group.add_argument('-sc', '-schedule', dest='schedule', help='Get schedule for a date (YYYY-MM-DD).')
    group.add_argument('-sp', '-spotlight', dest='spotlight', action='store_true', help='Get spotlight anime.')
    group.add_argument('-ss', '-search-suggestions', dest='suggestions', nargs='+', help='Get search suggestions.')
    group.add_argument('-ne', '-next-ep', dest='next_ep', nargs='+', help='Get next episode info. Usage: -ne <anime_id> [timezone]')
    group.add_argument('-tr', '-trailer', dest='trailer', nargs='+', help='Get trailer. Usage: -tr <anime_id> [play]')
    group.add_argument('-h', '-help', dest='help', nargs='?', const='all', help='Show help message.')
    group.add_argument('-v', '-version', dest='version', action='store_true', help='Show script version.')
    group.add_argument('-u', '-update', dest='update', action='store_true', help='Update pyanimecli to the latest version.')

    parser.add_argument('-p', '-page', dest='page', type=int, default=1, help='Page number for paginated results.')
    parser.add_argument('--settings', nargs='*', help='Open settings menu or set key="value"')

    if len(sys.argv) == 1:
        display_help()
        sys.exit(0)

    try:
        args = parser.parse_args()
        if args.help:
            cmd_map = {
                "search": "search", "s": "search", "info": "info", "i": "info",
                "watch": "watch", "w": "watch", "download": "download", "d": "download",
                "recent": "recent", "re": "recent", "recent-episodes": "recent",
                "top": "top_airing", "ta": "top_airing", "top-airing": "top_airing",
                "genres": "genres", "g": "genres", "genre-search": "genre_search", "gs": "genre_search",
                "studio": "studio", "st": "studio", "schedule": "schedule", "sc": "schedule",
                "spotlight": "spotlight", "sp": "spotlight",
                "suggestions": "suggestions", "ss": "suggestions", "search-suggestions": "suggestions",
                "next_ep": "next_ep", "ne": "next_ep", "next-ep": "next_ep",
                "trailer": "trailer", "tr": "trailer",
                "page": "pagination", "p": "pagination", "version": "version", "v": "version",
            }
            command_to_help = cmd_map.get(args.help) if args.help != 'all' else None
            display_help(command_to_help)
        elif args.version:
            console.print(f"pyanimecli version [bold cyan]{__version__}[/bold cyan]")
            check_for_updates()
        elif args.search:
            search(' '.join(args.search), args.page, pretty_print=True)
        elif args.info:
            info(args.info, pretty_print=True)
        elif args.watch:
            first_arg = args.watch[0]
            player_override = None
            if "$episode$" in first_arg:
                if len(args.watch) >= 2:
                    player_override = args.watch[2].lower() if len(args.watch) == 3 else None
                    watch(first_arg, args.watch[1].lower(), player_override=player_override)
                else:
                    console.print("[bold red]Invalid Usage:[/bold red] Use: <episode_id> <sub|dub> [player]")
                    display_help('watch')
            else:
                if len(args.watch) >= 3:
                    player_override = args.watch[3].lower() if len(args.watch) == 4 else None
                    watch(first_arg, args.watch[1], args.watch[2].lower(), player_override=player_override)
                else:
                    console.print("[bold red]Invalid Usage:[/bold red] Use: <anime_id> <ep_num> <sub|dub> [player]")
                    display_help('watch')
        elif args.download:
            args_list = args.download
            first_arg = args_list[0]
            is_full_id = "$episode$" in first_arg
            if is_full_id:
                if len(args_list) not in [2, 3]:
                    console.print("[bold red]Invalid Usage:[/bold red] Use: <episode_id> <type> [output_path]")
                    display_help('download')
                    return
                episode_id, dl_type = args_list[0], args_list[1]
                output_path = args_list[2] if len(args_list) == 3 else None
                download(episode_id, dl_type.lower(), output_path=output_path)
            else:
                if len(args_list) not in [3, 4]:
                    console.print("[bold red]Invalid Usage:[/bold red] Use: <anime_id> <ep_num> <type> [output_path]")
                    display_help('download')
                    return
                anime_id, ep_num_str, dl_type = args_list[0], args_list[1], args_list[2]
                output_path = args_list[3] if len(args_list) == 4 else None
                download(anime_id, ep_num_str, dl_type.lower(), output_path)
        elif args.recent:
            recent_episodes(args.page, pretty_print=True)
        elif args.top_airing:
            top_airing(args.page, pretty_print=True)
        elif args.genres:
            genres(pretty_print=True)
        elif args.genre_search:
            genre_search(' '.join(args.genre_search), args.page, pretty_print=True)
        elif args.studio:
            studio_search(' '.join(args.studio), args.page, pretty_print=True)
        elif args.schedule:
            schedule(args.schedule, pretty_print=True)
        elif args.spotlight:
            spotlight(pretty_print=True)
        elif args.suggestions:
            search_suggestions(' '.join(args.suggestions), pretty_print=True)
        elif args.next_ep:
            anime_id = args.next_ep[0]
            tz = args.next_ep[1] if len(args.next_ep) > 1 else DEFAULT_TZ
            next_ep(anime_id, tz, pretty_print=True)
        elif args.trailer:
            anime_id = args.trailer[0]
            play = False
            if len(args.trailer) > 1 and args.trailer[1].lower() == "play":
                play = True
            trailer(anime_id, play=play, pretty_print=True)
        elif args.update:
            console.print("Attempting to update pyanimecli...")
            result = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME])
            sys.exit(result.returncode)
        elif args.settings is not None:
            if len(args.settings) == 0:
                console.print(Panel("[bold cyan]pyanimecli Configuration[/bold cyan]"))
                for k, v in settings.data.items():
                    console.print(f"[bold]{k}[/bold]: [yellow]{v}[/yellow]")
                key = input("\nEnter setting to change (or 'exit'): ").strip()
                if key in settings.data:
                    val = input(f"Enter new value for {key}: ").strip()
                    settings.update(key, val)
                    console.print("[green]Settings saved.[/green]")
            else:
                for arg in args.settings:
                    if "=" in arg:
                        k, v = arg.split("=", 1)
                        settings.update(k.strip(), v.strip())
                        console.print(f"[green]Set {k} to {v}[/green]")
            sys.exit(0)
        else:
            display_help()
    except argparse.ArgumentError as e:
        console.print(f"[bold red]Argument Error:[/bold red] {e}")
        display_help()
    except Exception as e:
        console.print(f"[bold red]An unexpected error occurred:[/bold red] {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation cancelled by user.[/yellow]")
        sys.exit(0)