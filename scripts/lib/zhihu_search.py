"""Zhihu search client for last30days.

Uses the Zhihu search API to find questions, answers, and articles.
No API key needed, but may require cookie for anti-bot protection.

API endpoint:
  GET https://www.zhihu.com/api/v4/search_v3
    ?q={keyword}&t=general&offset={n}&limit=20
"""

import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import http


SEARCH_URL = "https://www.zhihu.com/api/v4/search_v3"

DEPTH_CONFIG = {
    "quick": 10,
    "default": 20,
    "deep": 40,
}


def _log(msg: str):
    if sys.stderr.isatty():
        sys.stderr.write(f"[Zhihu] {msg}\n")
        sys.stderr.flush()


def _strip_html(text: str) -> str:
    """Remove HTML tags from Zhihu content."""
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


def _engagement_score(voteup: int, comment: int) -> float:
    """Heuristic relevance from engagement."""
    weighted = voteup * 1.0 + comment * 2.0
    return round(min(1.0, max(0.05, weighted / 5000.0)), 3)


def _extract_item(obj: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    """Extract a normalized item from a Zhihu search result object.

    Zhihu search returns heterogeneous types: answers, articles, questions, etc.
    Each has a different nested structure.
    """
    obj_type = obj.get("type", "")
    target = obj.get("object") or obj.get("target") or obj

    if not isinstance(target, dict):
        return None

    item_type = target.get("type", obj_type)

    if item_type in ("answer", "Answer"):
        question = target.get("question") or {}
        title = question.get("title", target.get("title", ""))
        excerpt = _strip_html(target.get("excerpt", target.get("content", "")))
        voteup = int(target.get("voteup_count", 0) or 0)
        comment = int(target.get("comment_count", 0) or 0)
        answer_id = target.get("id", "")
        question_id = question.get("id", "")
        url = f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}" if question_id else ""
        created = target.get("created_time") or target.get("updated_time")
        author_info = target.get("author") or {}
        author = author_info.get("name", "")
    elif item_type in ("article", "Article"):
        title = target.get("title", "")
        excerpt = _strip_html(target.get("excerpt", target.get("content", "")))
        voteup = int(target.get("voteup_count", 0) or 0)
        comment = int(target.get("comment_count", 0) or 0)
        article_id = target.get("id", "")
        url = f"https://zhuanlan.zhihu.com/p/{article_id}" if article_id else target.get("url", "")
        created = target.get("created") or target.get("updated")
        author_info = target.get("author") or {}
        author = author_info.get("name", "")
    elif item_type in ("question", "Question"):
        title = target.get("title", "")
        excerpt = _strip_html(target.get("excerpt", target.get("detail", "")))
        voteup = int(target.get("follower_count", 0) or 0)
        comment = int(target.get("answer_count", 0) or 0)
        question_id = target.get("id", "")
        url = f"https://www.zhihu.com/question/{question_id}" if question_id else ""
        created = target.get("created") or target.get("updated_time")
        author = ""
    else:
        return None

    if not title and not excerpt:
        return None

    date_str = _timestamp_to_date(created)

    return {
        "id": f"ZH{idx}",
        "title": _strip_html(title[:200]) if title else f"知乎 {item_type}",
        "url": url,
        "source_domain": "zhihu.com",
        "snippet": excerpt[:500],
        "date": date_str,
        "date_confidence": "high" if date_str else "low",
        "relevance": _engagement_score(voteup, comment),
        "why_relevant": f"Zhihu {item_type} by {author}: voteup={voteup}, comments={comment}",
        "engagement": {
            "likes": voteup,
            "num_comments": comment,
        },
        "author": author,
        "content_type": item_type,
    }


def search_zhihu(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    cookie: str = "",
) -> List[Dict[str, Any]]:
    """Search Zhihu for a topic and return normalized web-item dicts.

    Args:
        topic: Search keyword
        from_date: Start date YYYY-MM-DD
        to_date: End date YYYY-MM-DD
        depth: 'quick', 'default', or 'deep'
        cookie: Optional Zhihu cookie for anti-bot

    Returns:
        List of normalized item dicts.
    """
    limit = DEPTH_CONFIG.get(depth, 20)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.zhihu.com/search",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie

    items: List[Dict[str, Any]] = []
    offset = 0
    page_size = 20

    _log(f"Searching for '{topic}' (depth={depth}, limit={limit})")

    while len(items) < limit:
        from urllib.parse import urlencode
        params = urlencode({
            "q": topic,
            "t": "general",
            "offset": offset,
            "limit": page_size,
        })
        url = f"{SEARCH_URL}?{params}"

        try:
            resp = http.get(url, headers=headers, timeout=15, retries=2)
        except http.HTTPError as e:
            if e.status_code in (400, 403, 401):
                _log(f"Auth required — set ZHIHU_COOKIE env var with a valid session cookie")
            else:
                _log(f"Request failed (offset={offset}): {e}")
            break

        if not isinstance(resp, dict):
            break

        data_list = resp.get("data", [])
        if not isinstance(data_list, list) or not data_list:
            break

        for obj in data_list:
            if not isinstance(obj, dict):
                continue
            item = _extract_item(obj, len(items) + 1)
            if item:
                items.append(item)
                if len(items) >= limit:
                    break

        paging = resp.get("paging", {})
        is_end = paging.get("is_end", True)
        if is_end:
            break

        offset += page_size

    _log(f"Found {len(items)} items")
    return items
