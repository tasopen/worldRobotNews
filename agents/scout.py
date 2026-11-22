"""@scout: ニュース収集エージェント

RSS フィードから記事を収集し、キーワードスコアで上位N件を返す。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser
import requests
import yaml
from dateutil import parser as dtparser


@dataclass
class Article:
    title: str
    url: str
    summary: str
    published_at: datetime
    source: str
    score: float = 0.0
    origin: str = ""


SEEN_URLS_PATH = "docs/seen_urls.txt"
SEEN_URL_RETENTION_DAYS = 30
SEEN_URL_MAX_ENTRIES = 1000

# フィード取得のタイムアウト（秒）。config/sources.yml の
# selection.request_timeout_sec で上書きできる。
# feedparser.parse(url) は timeout を渡せず、応答しないホストで
# 永久にブロックするため、必ず requests 経由で取得する。
DEFAULT_REQUEST_TIMEOUT_SEC = 15.0

# feedparser の既定 UA（TechCrunch などはブラウザ UA を 403 で弾くため、
# feedparser と同じ UA を使う）。古い feedparser でも動くようフォールバックする。
_FEED_USER_AGENT = getattr(feedparser, "USER_AGENT", "worldRobotNews/0.1")


def _load_seen_url_entries(
    path: str = SEEN_URLS_PATH,
    retention_days: int = SEEN_URL_RETENTION_DAYS,
) -> list[tuple[datetime, str]]:
    """保持期間内の使用済み URL をタイムスタンプ付きで返す。"""
    if not os.path.exists(path):
        return []

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    entries_by_url: dict[str, datetime] = {}

    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            timestamp: datetime | None = None
            url = line
            if "\t" in line:
                timestamp_text, url = line.split("\t", 1)
                try:
                    parsed = dtparser.isoparse(timestamp_text)
                    timestamp = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    timestamp = None

            if not url:
                continue

            # 旧形式の URL のみの行は移行用に一度だけ救済し、次回保存時に新形式へ正規化する。
            if timestamp is None:
                timestamp = datetime.now(UTC)

            if timestamp < cutoff:
                continue

            current = entries_by_url.get(url)
            if current is None or timestamp > current:
                entries_by_url[url] = timestamp

    return sorted(((timestamp, url) for url, timestamp in entries_by_url.items()), key=lambda item: item[0])


def _load_seen_urls(path: str = SEEN_URLS_PATH) -> set[str]:
    """保持期間内の過去に使用した記事 URL を読み込む。"""
    return {url for _, url in _load_seen_url_entries(path)}


def save_seen_urls(
    urls: list[str],
    path: str = SEEN_URLS_PATH,
    retention_days: int = SEEN_URL_RETENTION_DAYS,
    max_entries: int = SEEN_URL_MAX_ENTRIES,
) -> None:
    """選択された記事 URL を保持期間付きで保存し、ファイルを圧縮する。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = datetime.now(UTC)
    entries = _load_seen_url_entries(path, retention_days=retention_days)
    entries_by_url = {url: timestamp for timestamp, url in entries}

    for url in urls:
        if url:
            entries_by_url[url] = now

    compacted = sorted(((timestamp, url) for url, timestamp in entries_by_url.items()), key=lambda item: item[0])
    if max_entries > 0:
        compacted = compacted[-max_entries:]

    with open(path, "w", encoding="utf-8") as f:
        for timestamp, url in compacted:
            f.write(f"{timestamp.isoformat()}\t{url}\n")



def _load_config(config_path: str = "config/sources.yml") -> dict[str, Any]:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _score(article: Article, config: dict[str, Any]) -> float:
    """キーワードマッチによるスコアリング。"""
    keywords = [kw.lower() for kw in config.get("keywords", [])]
    text = (article.title + " " + article.summary).lower()
    return sum(1.0 for kw in keywords if kw in text)


def _fetch_feed_bytes(url: str, timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC) -> bytes:
    """フィードを HTTP で取得する（タイムアウト付き）。

    feedparser.parse(url) は urllib に timeout を渡さないため、
    TCP/TLS が応答しないホストで無限に待ち続ける。requests で
    先に取得し、得たバイト列を feedparser に渡す。
    """
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": _FEED_USER_AGENT},
    )
    response.raise_for_status()
    return response.content


def fetch_rss(
    feed_cfg: dict[str, Any],
    hours: int,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
) -> list[Article]:
    """RSS フィードから記事を取得する。"""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    parsed = feedparser.parse(_fetch_feed_bytes(feed_cfg["url"], timeout))
    articles = []
    for entry in parsed.entries:
        pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if not pub_struct:
            continue
        pub = datetime(*pub_struct[:6], tzinfo=UTC)
        if pub < cutoff:
            continue
        articles.append(
            Article(
                title=entry.get("title") or "",
                url=entry.get("link") or "",
                summary=entry.get("summary") or "",
                published_at=pub,
                source=feed_cfg["name"],
                score=feed_cfg.get("weight", 1.0),
                origin="RSS",
            )
        )
    return articles


def collect(config_path: str = "config/sources.yml") -> list[Article]:
    """全ソースから記事を収集し、スコア順上位N件を返す（既出記事は除外）。"""
    config = _load_config(config_path)
    hours = config["selection"]["hours_lookback"]
    max_n = config["selection"]["max_articles"]
    try:
        timeout = float(config["selection"].get("request_timeout_sec", DEFAULT_REQUEST_TIMEOUT_SEC))
    except (TypeError, ValueError):
        timeout = DEFAULT_REQUEST_TIMEOUT_SEC

    all_articles: list[Article] = []

    # RSS フィード
    seen_feed_urls: set[str] = set()
    for feed_cfg in config.get("rss_feeds", []):
        url = feed_cfg.get("url", "")
        name = feed_cfg.get("name") or url or "?"
        if url and url in seen_feed_urls:
            print(f"[scout] skip duplicate feed URL: {name} ({url})", flush=True)
            continue
        if url:
            seen_feed_urls.add(url)

        # 進捗を逐次出力する（ハング時にどこで止まったか分かるようにする）
        print(f"[scout] fetching {name} ...", flush=True)
        started = time.perf_counter()
        try:
            fetched = fetch_rss(feed_cfg, hours, timeout=timeout)
            all_articles.extend(fetched)
            print(
                f"[scout]   ok {name}: {len(fetched)} article(s) "
                f"in {time.perf_counter() - started:.1f}s",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            print(
                f"[scout]   FAIL {name} after {time.perf_counter() - started:.1f}s: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )
        time.sleep(0.3)

    # 過去に使用した記事を除外
    seen_urls = _load_seen_urls()

    # 重複排除（URL ベース）＋既出除外
    seen: set[str] = set()
    unique: list[Article] = []
    for a in all_articles:
        if a.url not in seen and a.url and a.url not in seen_urls:
            seen.add(a.url)
            a.score += _score(a, config)
            unique.append(a)

    # スコア降順でソートして上位N件をソース上限付きで選択
    unique.sort(key=lambda a: (a.score, a.published_at), reverse=True)
    selected = []
    source_counts = {}
    
    max_per_source = config["selection"].get("max_per_source", 3)
    
    for a in unique:
        if len(selected) >= max_n:
            break
        count = source_counts.get(a.source, 0)
        if count < max_per_source:
            selected.append(a)
            source_counts[a.source] = count + 1

    skipped = len(all_articles) - len(unique)
    print(f"[scout] {len(all_articles)} fetched → {skipped} skipped (seen) → {len(selected)} selected")

    # 選択された記事の詳細をログに出力
    print("[scout] Selected articles for script generation:")
    for i, a in enumerate(selected):
        print(f"  - Article {i+1}:")
        print(f"    Title: {a.title}")
        print(f"    Source: [{a.origin}] {a.source}")
        print(f"    Summary: {a.summary[:150].replace(chr(10), ' ')}...")
    return selected


if __name__ == "__main__":
    articles = collect()
    for a in articles:
        print(f"  [{a.origin} | {a.source}] {a.title} ({a.published_at.date()})")
