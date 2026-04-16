"""@scout: ニュース収集エージェント

RSS フィードから記事を収集し、キーワードスコアで上位N件を返す。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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


def _load_seen_url_entries(
    path: str = SEEN_URLS_PATH,
    retention_days: int = SEEN_URL_RETENTION_DAYS,
) -> list[tuple[datetime, str]]:
    """保持期間内の使用済み URL をタイムスタンプ付きで返す。"""
    if not os.path.exists(path):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
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
                    timestamp = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    timestamp = None

            if not url:
                continue

            # 旧形式の URL のみの行は移行用に一度だけ救済し、次回保存時に新形式へ正規化する。
            if timestamp is None:
                timestamp = datetime.now(timezone.utc)

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
    now = datetime.now(timezone.utc)
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


def fetch_rss(feed_cfg: dict[str, Any], hours: int) -> list[Article]:
    """RSS フィードから記事を取得する。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    parsed = feedparser.parse(feed_cfg["url"])
    articles = []
    for entry in parsed.entries:
        pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if not pub_struct:
            continue
        pub = datetime(*pub_struct[:6], tzinfo=timezone.utc)
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

    all_articles: list[Article] = []

    # RSS フィード
    for feed_cfg in config.get("rss_feeds", []):
        try:
            all_articles.extend(fetch_rss(feed_cfg, hours))
            time.sleep(0.3)
        except Exception as e:
            print(f"[scout] RSS error ({feed_cfg['name']}): {e}")

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
