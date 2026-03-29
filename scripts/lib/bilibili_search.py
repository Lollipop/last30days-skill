"""Bilibili video search client for last30days.

Uses the public Bilibili search API with Wbi signature authentication.

API endpoint:
  GET https://api.bilibili.com/x/web-interface/search/type
    ?keyword={keyword}&search_type=video&page={n}&order={order}&wts={ts}&w_rid={sig}
"""

import hashlib
import re
import sys
import time
from datetime import datetime, timezone
from functools import reduce
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from . import http


SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

DEPTH_CONFIG = {
    "quick": 10,
    "default": 20,
    "deep": 40,
}

ORDER_MAP = {
    "quick": "pubdate",
    "default": "totalrank",
    "deep": "totalrank",
}

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_cached_wbi_keys: Optional[Tuple[str, str, float]] = None


def _log(msg: str):
    if sys.stderr.isatty():
        sys.stderr.write(f"[Bilibili] {msg}\n")
        sys.stderr.flush()


def _get_mixin_key(orig: str) -> str:
    """Generate mixin key from img_key + sub_key via permutation table."""
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def _get_wbi_keys() -> Tuple[str, str]:
    """Fetch Wbi signing keys from Bilibili nav API. Cached for 1 hour."""
    global _cached_wbi_keys
    now = time.time()
    if _cached_wbi_keys and (now - _cached_wbi_keys[2]) < 3600:
        return _cached_wbi_keys[0], _cached_wbi_keys[1]

    headers = {"User-Agent": _BROWSER_UA, "Referer": "https://www.bilibili.com"}
    resp = http.get(NAV_URL, headers=headers, timeout=10, retries=2)
    wbi_img = resp.get("data", {}).get("wbi_img", {})
    img_url = wbi_img.get("img_url", "")
    sub_url = wbi_img.get("sub_url", "")

    img_key = img_url.rsplit("/", 1)[-1].split(".")[0] if img_url else ""
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0] if sub_url else ""

    if img_key and sub_key:
        _cached_wbi_keys = (img_key, sub_key, now)
        _log(f"Wbi keys refreshed: img={img_key[:8]}... sub={sub_key[:8]}...")

    return img_key, sub_key


def _sign_params(params: dict) -> dict:
    """Add Wbi signature (w_rid, wts) to request parameters."""
    img_key, sub_key = _get_wbi_keys()
    if not img_key or not sub_key:
        _log("Wbi keys unavailable, sending unsigned request")
        return params

    mixin_key = _get_mixin_key(img_key + sub_key)
    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))
    # Filter special characters per Bilibili spec
    filtered = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in params.items()
    }
    query = urlencode(filtered)
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered["w_rid"] = w_rid
    return filtered


def _strip_html(text: str) -> str:
    """Remove <em> highlight tags from Bilibili search results."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _timestamp_to_date(ts: Any) -> Optional[str]:
    """Convert Unix timestamp to YYYY-MM-DD."""
    try:
        iv = int(ts)
        if iv <= 0:
            return None
        dt = datetime.fromtimestamp(iv, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _engagement_score(play: int, danmaku: int, favorites: int) -> float:
    """Heuristic relevance from engagement metrics."""
    weighted = play * 0.001 + danmaku * 0.5 + favorites * 1.0
    return round(min(1.0, max(0.05, weighted / 500.0)), 3)


def search_bilibili(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> List[Dict[str, Any]]:
    """Search Bilibili videos and return normalized web-item dicts.

    Args:
        topic: Search keyword
        from_date: Start date YYYY-MM-DD (for context, not API-filtered)
        to_date: End date YYYY-MM-DD
        depth: 'quick', 'default', or 'deep'

    Returns:
        List of normalized item dicts.
    """
    limit = DEPTH_CONFIG.get(depth, 20)
    order = ORDER_MAP.get(depth, "totalrank")

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer": "https://search.bilibili.com",
        "Accept": "application/json",
    }

    items: List[Dict[str, Any]] = []
    pages_to_fetch = max(1, (limit + 19) // 20)

    _log(f"Searching for '{topic}' (depth={depth}, order={order}, pages={pages_to_fetch})")

    for page in range(1, pages_to_fetch + 1):
        params = {
            "keyword": topic,
            "search_type": "video",
            "page": page,
            "order": order,
        }
        signed_params = _sign_params(params)
        url = f"{SEARCH_URL}?{urlencode(signed_params)}"

        try:
            resp = http.get(url, headers=headers, timeout=15, retries=2)
        except http.HTTPError as e:
            _log(f"Page {page} failed: {e}")
            break

        if not isinstance(resp, dict):
            break

        code = resp.get("code", -1)
        if code != 0:
            _log(f"API error code={code}: {resp.get('message', '')}")
            break

        results = resp.get("data", {}).get("result", [])
        if not results:
            break

        for entry in results:
            if not isinstance(entry, dict):
                continue

            bvid = str(entry.get("bvid", "")).strip()
            aid = str(entry.get("aid", entry.get("id", ""))).strip()
            if not bvid and not aid:
                continue

            title = _strip_html(entry.get("title", ""))
            description = _strip_html(entry.get("description", ""))
            author = str(entry.get("author", "")).strip()

            play = int(entry.get("play", 0) or 0)
            danmaku = int(entry.get("danmaku", entry.get("video_review", 0)) or 0)
            favorites = int(entry.get("favorites", 0) or 0)
            review = int(entry.get("review", 0) or 0)

            pubdate = _timestamp_to_date(entry.get("pubdate", entry.get("senddate", 0)))
            duration = str(entry.get("duration", "")).strip()

            video_url = (
                f"https://www.bilibili.com/video/{bvid}"
                if bvid else f"https://www.bilibili.com/video/av{aid}"
            )

            items.append({
                "id": f"BL{len(items)+1}",
                "title": title[:200] if title else f"Bilibili {bvid or aid}",
                "url": video_url,
                "source_domain": "bilibili.com",
                "snippet": description[:500],
                "date": pubdate,
                "date_confidence": "high" if pubdate else "low",
                "relevance": _engagement_score(play, danmaku, favorites),
                "why_relevant": f"Bilibili @{author}: play={play}, danmaku={danmaku}, favorites={favorites}",
                "engagement": {
                    "views": play,
                    "danmaku": danmaku,
                    "favorites": favorites,
                    "num_comments": review,
                },
                "author": author,
                "duration": duration,
            })

            if len(items) >= limit:
                break
        if len(items) >= limit:
            break

    _log(f"Found {len(items)} items")
    return items
