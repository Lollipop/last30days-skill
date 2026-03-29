"""Chinese trending topics discovery for last30days.

Fetches real-time trending topics from Weibo, Toutiao, and Baidu hot lists.
Unlike the search modules (weibo_search, zhihu_search, bilibili_search) which
search for a specific topic, this module discovers *what's trending right now*.

Usage from last30days:
  python3 last30days.py --trending-cn --limit 20

All APIs are public and require no authentication.
"""

import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from . import http


TIMEOUT = 10

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _log(msg: str):
    if sys.stderr.isatty():
        sys.stderr.write(f"[CN-Trending] {msg}\n")
        sys.stderr.flush()


def fetch_weibo_trending() -> List[Dict[str, Any]]:
    """Fetch Weibo hot search list."""
    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        resp = http.get(
            "https://weibo.com/ajax/side/hotSearch",
            headers=headers,
            timeout=TIMEOUT,
            retries=2,
        )
        items = []
        for entry in resp.get("data", {}).get("realtime", []):
            note = entry.get("note", "")
            if not note:
                continue
            items.append({
                "title": note,
                "source": "weibo",
                "source_cn": "微博",
                "hot": int(entry.get("num", 0) or 0),
                "url": f"https://s.weibo.com/weibo?q=%23{note}%23",
                "label": entry.get("label_name", ""),
            })
        _log(f"Weibo: {len(items)} topics")
        return items
    except Exception as e:
        _log(f"Weibo failed: {e}")
        return []


def fetch_toutiao_trending() -> List[Dict[str, Any]]:
    """Fetch Toutiao (ByteDance) hot board."""
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
    }
    try:
        resp = http.get(
            "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
            headers=headers,
            timeout=TIMEOUT,
            retries=2,
        )
        items = []
        for entry in resp.get("data", []):
            title = entry.get("Title", "")
            if not title:
                continue
            items.append({
                "title": title,
                "source": "toutiao",
                "source_cn": "今日头条",
                "hot": int(entry.get("HotValue", 0) or 0),
                "url": entry.get("Url", ""),
                "label": "",
            })
        _log(f"Toutiao: {len(items)} topics")
        return items
    except Exception as e:
        _log(f"Toutiao failed: {e}")
        return []


def fetch_baidu_trending() -> List[Dict[str, Any]]:
    """Fetch Baidu hot search list."""
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
    }
    try:
        resp = http.get(
            "https://top.baidu.com/api/board?platform=wise&tab=realtime",
            headers=headers,
            timeout=TIMEOUT,
            retries=2,
        )
        items = []
        for card in resp.get("data", {}).get("cards", []):
            top_content = card.get("content", [])
            if not top_content:
                continue
            entries = (
                top_content[0].get("content", [])
                if isinstance(top_content[0], dict) else top_content
            )
            for entry in entries:
                word = entry.get("word", "")
                if not word:
                    continue
                items.append({
                    "title": word,
                    "source": "baidu",
                    "source_cn": "百度",
                    "hot": int(entry.get("hotScore", 0) or 0),
                    "url": entry.get("url", ""),
                    "label": "",
                })
        _log(f"Baidu: {len(items)} topics")
        return items
    except Exception as e:
        _log(f"Baidu failed: {e}")
        return []


def _deduplicate(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove exact title duplicates, keeping the first occurrence."""
    seen: set = set()
    result = []
    for item in items:
        title = item["title"].strip()
        if title and title not in seen:
            seen.add(title)
            result.append(item)
    return result


def _normalize_scores(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank-normalize hot scores across platforms to 0-100 scale.

    Different platforms use wildly different scales
    (Toutiao ~10M, Weibo ~1M, Baidu ~100K).
    """
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)

    for source, group in by_source.items():
        group.sort(key=lambda x: int(x.get("hot", 0) or 0), reverse=True)
        n = len(group)
        for rank, item in enumerate(group):
            item["hot_normalized"] = round(100 * (n - rank) / n, 1) if n > 0 else 0

    return items


def fetch_all_trending(limit: int = 20) -> Dict[str, Any]:
    """Fetch trending topics from all Chinese sources.

    Returns:
        Dict with keys: timestamp, sources, sources_failed, count, items
    """
    all_items: List[Dict[str, Any]] = []
    sources_ok: List[str] = []
    sources_fail: List[str] = []

    for name, fetcher in [
        ("weibo", fetch_weibo_trending),
        ("toutiao", fetch_toutiao_trending),
        ("baidu", fetch_baidu_trending),
    ]:
        result = fetcher()
        if result:
            sources_ok.append(name)
            all_items.extend(result)
        else:
            sources_fail.append(name)

    all_items = _deduplicate(all_items)
    all_items = _normalize_scores(all_items)
    all_items.sort(key=lambda x: x.get("hot_normalized", 0), reverse=True)
    all_items = all_items[:limit]

    tz = timezone(timedelta(hours=8))
    return {
        "timestamp": datetime.now(tz).isoformat(),
        "sources": sources_ok,
        "sources_failed": sources_fail,
        "count": len(all_items),
        "items": all_items,
    }
