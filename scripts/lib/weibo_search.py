"""Weibo search client for last30days.

Uses m.weibo.cn mobile API for keyword search — no API key needed,
but may require a cookie for anti-bot protection (optional WEIBO_COOKIE env var).

API endpoint:
  GET https://m.weibo.cn/api/container/getIndex
    ?containerid=100103type%3D1%26q%3D{keyword}
    &page_type=searchall
    &page={n}
"""

import math
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from . import http


SEARCH_URL = "https://m.weibo.cn/api/container/getIndex"

DEPTH_CONFIG = {
    "quick": 10,
    "default": 20,
    "deep": 40,
}

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 MicroMessenger/8.0.0"
)


def _log(msg: str):
    if sys.stderr.isatty():
        sys.stderr.write(f"[Weibo] {msg}\n")
        sys.stderr.flush()


def _strip_html(text: str) -> str:
    """Remove HTML tags from Weibo card text."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_created_at(raw: str) -> Optional[str]:
    """Convert Weibo date strings to YYYY-MM-DD.

    Weibo returns various formats:
      - "刚刚", "N分钟前", "N小时前", "昨天 HH:MM"
      - "MM-DD" (current year implied)
      - "YYYY-MM-DD" or full datetime strings
    """
    if not raw:
        return None
    raw = raw.strip()

    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]

    now = datetime.now(timezone.utc)

    if "分钟前" in raw or "刚刚" in raw or "秒前" in raw:
        return now.strftime("%Y-%m-%d")

    if "小时前" in raw:
        return now.strftime("%Y-%m-%d")

    if "昨天" in raw:
        from datetime import timedelta
        yesterday = now - timedelta(days=1)
        return yesterday.strftime("%Y-%m-%d")

    month_day = re.match(r"^(\d{1,2})-(\d{1,2})", raw)
    if month_day:
        return f"{now.year}-{int(month_day.group(1)):02d}-{int(month_day.group(2)):02d}"

    return None


def _engagement_score(reposts: int, comments: int, likes: int) -> float:
    """Heuristic relevance from engagement metrics."""
    weighted = reposts * 3.0 + comments * 2.0 + likes * 1.0
    return round(min(1.0, max(0.05, weighted / 10000.0)), 3)


def search_weibo(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    cookie: str = "",
) -> List[Dict[str, Any]]:
    """Search Weibo for a topic and return normalized web-item dicts.

    Args:
        topic: Search keyword
        from_date: Start date YYYY-MM-DD (for filtering)
        to_date: End date YYYY-MM-DD (for filtering)
        depth: 'quick', 'default', or 'deep'
        cookie: Optional Weibo cookie string for auth

    Returns:
        List of normalized item dicts.
    """
    limit = DEPTH_CONFIG.get(depth, 20)
    encoded_q = quote(topic)
    containerid = f"100103type%3D1%26q%3D{encoded_q}"

    headers = {
        "User-Agent": _MOBILE_UA,
        "Referer": "https://m.weibo.cn/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie

    items: List[Dict[str, Any]] = []
    pages_to_fetch = max(1, (limit + 9) // 10)

    _log(f"Searching for '{topic}' (depth={depth}, pages={pages_to_fetch})")

    for page in range(1, pages_to_fetch + 1):
        url = f"{SEARCH_URL}?containerid={containerid}&page_type=searchall&page={page}"

        try:
            resp = http.get(url, headers=headers, timeout=15, retries=2)
        except http.HTTPError as e:
            if e.status_code in (432, 403, 401):
                _log(f"Auth required — set WEIBO_COOKIE env var with a valid session cookie")
            else:
                _log(f"Page {page} failed: {e}")
            break

        if not isinstance(resp, dict) or resp.get("ok") != 1:
            _log(f"Page {page}: unexpected response")
            break

        cards = resp.get("data", {}).get("cards", [])
        if not cards:
            break

        for card in cards:
            card_group = card.get("card_group", [card])
            if not isinstance(card_group, list):
                card_group = [card]

            for sub in card_group:
                mblog = sub.get("mblog")
                if not mblog or not isinstance(mblog, dict):
                    continue

                mid = str(mblog.get("id", "")).strip()
                if not mid:
                    continue

                raw_text = _strip_html(mblog.get("text", ""))
                user_info = mblog.get("user") or {}
                screen_name = user_info.get("screen_name", "")

                reposts = int(mblog.get("reposts_count", 0) or 0)
                comments = int(mblog.get("comments_count", 0) or 0)
                likes = int(mblog.get("attitudes_count", 0) or 0)

                date_str = _parse_created_at(mblog.get("created_at", ""))
                url_link = f"https://m.weibo.cn/detail/{mid}"

                title = raw_text[:80] if raw_text else f"微博 {mid}"
                snippet = raw_text[:500] if raw_text else ""

                items.append({
                    "id": f"WB{len(items)+1}",
                    "title": title,
                    "url": url_link,
                    "source_domain": "weibo.com",
                    "snippet": snippet,
                    "date": date_str,
                    "date_confidence": "med" if date_str else "low",
                    "relevance": _engagement_score(reposts, comments, likes),
                    "why_relevant": f"Weibo @{screen_name}: reposts={reposts}, comments={comments}, likes={likes}",
                    "engagement": {
                        "reposts": reposts,
                        "likes": likes,
                        "num_comments": comments,
                    },
                    "author": screen_name,
                })

                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break

    _log(f"Found {len(items)} items")
    return items
