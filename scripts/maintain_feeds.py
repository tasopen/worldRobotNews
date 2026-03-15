"""フィード自動メンテナンスツール

週次で実行し、以下を自動化する:
1. 全 RSS フィードの生存確認
2. 応答なしフィードのフラグ管理（3週連続で削除）
3. Gemini Grounding Search で URL 変更を検出して自動更新
4. Gemini Grounding Search で新しい人気 RSS フィードを発見・追加
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests
import yaml

# リポジトリルートを sys.path に追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


SOURCES_PATH = "config/sources.yml"
HEALTH_PATH = "config/feed_health.json"

LANGUAGE_LABELS = {
    "en": "English",
    "ja": "Japanese (日本語)",
    "zh": "Chinese (中文)",
}


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _load_sources(path: str = SOURCES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _save_sources(data: dict, path: str = SOURCES_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _load_health(path: str = HEALTH_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()

    # Treat empty/invalid files as fresh state instead of crashing CI.
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[warn] Invalid JSON in {path}; resetting feed health state.")
        return {}

    return data if isinstance(data, dict) else {}


def _save_health(data: dict, path: str = HEALTH_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Phase 1: フィード生存確認
# ---------------------------------------------------------------------------

def check_feed_health(feed: dict, timeout: int = 15) -> bool:
    """フィードが正常にアクセスできるかチェック。"""
    url = feed["url"]
    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (NewsPicker FeedChecker)"
        })
        if resp.status_code >= 400:
            print(f"  ✗ HTTP {resp.status_code}: {feed['name']}")
            return False
        parsed = feedparser.parse(resp.text)
        if parsed.bozo and not parsed.entries:
            print(f"  ✗ Parse error: {feed['name']}")
            return False
        print(f"  ✓ OK ({len(parsed.entries)} entries): {feed['name']}")
        return True
    except Exception as e:
        print(f"  ✗ Error: {feed['name']} — {e}")
        return False


def run_health_checks(sources: dict, health: dict, dry_run: bool = False) -> tuple[dict, dict, list[dict]]:
    """
    全フィードの生存確認を実行。
    Returns: (updated_sources, updated_health, failed_feeds)
    """
    print("\n=== Phase 1: フィード生存確認 ===")
    feeds = sources.get("rss_feeds", [])
    failed_feeds = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for feed in feeds:
        url = feed["url"]
        alive = check_feed_health(feed)
        time.sleep(0.5)

        if alive:
            # 正常 → fail_count リセット
            if url in health:
                health[url]["fail_count"] = 0
                health[url]["last_success"] = now_iso
            else:
                health[url] = {"fail_count": 0, "last_success": now_iso, "name": feed["name"]}
        else:
            # 応答なし → fail_count インクリメント
            if url not in health:
                health[url] = {"fail_count": 0, "last_success": None, "name": feed["name"]}
            health[url]["fail_count"] = health[url].get("fail_count", 0) + 1
            health[url]["last_failure"] = now_iso
            failed_feeds.append(feed)
            print(f"    → fail_count: {health[url]['fail_count']}")

    # 3週連続応答なしのフィードを削除
    max_fail = sources.get("maintenance", {}).get("max_fail_count", 3)
    to_remove = []
    for feed in feeds:
        url = feed["url"]
        if url in health and health[url].get("fail_count", 0) >= max_fail:
            to_remove.append(feed)
            print(f"  ✗✗ REMOVING (fail_count >= {max_fail}): {feed['name']}")

    if to_remove and not dry_run:
        sources["rss_feeds"] = [f for f in feeds if f not in to_remove]

    return sources, health, failed_feeds


# ---------------------------------------------------------------------------
# Phase 2: Gemini Grounding Search — URL 修復 & 新規発見
# ---------------------------------------------------------------------------

def _gemini_grounding_search(prompt: str, api_key: str) -> str | None:
    """Gemini Grounding Search を使ってプロンプトに回答を得る。"""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[grounding_tool])

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
            config=config,
        )
        return response.text.strip() if response.text else None
    except Exception as e:
        print(f"  [grounding] Error: {e}")
        return None


def search_new_feed_url(feed: dict, api_key: str, keywords: list[str]) -> str | None:
    """応答なしフィードの新しい RSS URL を Grounding Search で探す。"""
    category_hint = ", ".join(keywords[:3]) if keywords else ""
    prompt = (
        f"The RSS feed for '{feed['name']}' at URL '{feed['url']}' is no longer responding. "
        f"This is a {LANGUAGE_LABELS.get(feed.get('language', 'en'), 'English')} news source about {category_hint}. "
        f"Please find the current, working RSS feed URL for this site. "
        f"Return ONLY the URL, nothing else. If you cannot find it, return 'NOT_FOUND'."
    )
    result = _gemini_grounding_search(prompt, api_key)
    if result and "NOT_FOUND" not in result:
        # URL を抽出（余計なテキストが含まれる可能性がある）
        import re
        urls = re.findall(r'https?://[^\s<>"\']+', result)
        if urls:
            return urls[0]
    return None


def discover_new_feeds(language: str, keywords: list[str], existing_names: set[str],
                       api_key: str) -> list[dict]:
    """Gemini Grounding Search で人気の RSS フィードを発見する。"""
    lang_label = LANGUAGE_LABELS.get(language, language)
    category_hint = ", ".join(keywords[:5])
    existing_list = ", ".join(sorted(existing_names)[:10])

    prompt = (
        f"Find popular and reliable RSS feed URLs for {lang_label} news sites "
        f"covering topics: {category_hint}. "
        f"Exclude these already-known sources: {existing_list}. "
        f"Only include professional news organizations."
        f"Exclude personal blogs and community platforms."
        f"Prioritize feeds with an {category_hint} specific category."
        f"Prefer feeds from established media companies or recognized {category_hint} industry publications."

        f"Return a JSON array of objects with 'name', 'url', and 'weight' (1.0-1.3) fields. "
        f"Only include feeds that are currently active and frequently updated. "
        f"Return at most 3 new feeds. Return ONLY valid JSON, no markdown formatting."
    )
    result = _gemini_grounding_search(prompt, api_key)
    if not result:
        return []

    # JSON パース試行
    import re
    # マークダウンのコードブロックを除去
    result = re.sub(r'```(?:json)?\s*', '', result).strip()
    result = re.sub(r'```\s*$', '', result).strip()
    try:
        feeds = json.loads(result)
        if isinstance(feeds, list):
            valid = []
            for f in feeds:
                if isinstance(f, dict) and "name" in f and "url" in f:
                    valid.append({
                        "name": f["name"],
                        "url": f["url"],
                        "language": language,
                        "weight": float(f.get("weight", 1.0)),
                    })
            return valid
    except (json.JSONDecodeError, ValueError):
        print(f"  [discover] Could not parse response for {lang_label}")
    return []


def run_grounding_maintenance(sources: dict, health: dict, failed_feeds: list[dict],
                              api_key: str, auto_add: bool = False,
                              dry_run: bool = False) -> dict:
    """
    Grounding Search を使って:
    1. 応答なしフィードの URL 修復
    2. 新規フィード発見
    """
    keywords = sources.get("keywords", [])
    maintenance_cfg = sources.get("maintenance", {})

    # --- URL 修復 ---
    print("\n=== Phase 2a: URL 修復 (Grounding Search) ===")
    for feed in failed_feeds:
        # まだ削除されていないフィードのみ
        if feed not in sources.get("rss_feeds", []):
            continue
        print(f"  Searching new URL for: {feed['name']}...")
        new_url = search_new_feed_url(feed, api_key, keywords)
        if new_url and new_url != feed["url"]:
            print(f"    → Found: {new_url}")
            if not dry_run:
                feed["url"] = new_url
                # fail_count リセット
                if feed.get("url") in health:
                    del health[feed["url"]]
                health[new_url] = {
                    "fail_count": 0,
                    "last_success": datetime.now(timezone.utc).isoformat(),
                    "name": feed["name"],
                }
        else:
            print(f"    → Not found")
        time.sleep(1)  # API レート制限考慮

    # --- 新規フィード発見 ---
    print("\n=== Phase 2b: 新規フィード発見 (Grounding Search) ===")
    discover_languages = maintenance_cfg.get("auto_discover_languages", ["en", "ja", "zh"])
    max_per_lang = maintenance_cfg.get("max_feeds_per_language", 8)
    existing_names = {f["name"] for f in sources.get("rss_feeds", [])}

    for lang in discover_languages:
        current_count = sum(1 for f in sources.get("rss_feeds", []) if f.get("language") == lang)
        if current_count >= max_per_lang:
            print(f"  [{lang}] Already at max ({current_count}/{max_per_lang}), skipping discovery.")
            continue

        slots = max_per_lang - current_count
        print(f"  [{lang}] Searching for up to {slots} new feeds...")
        new_feeds = discover_new_feeds(lang, keywords, existing_names, api_key)

        for nf in new_feeds[:slots]:
            # 重複チェック（URL ベース）
            existing_urls = {f["url"] for f in sources.get("rss_feeds", [])}
            if nf["url"] in existing_urls:
                print(f"    → Already exists: {nf['name']}")
                continue

            # 生存確認
            if check_feed_health(nf):
                if auto_add and not dry_run:
                    sources.setdefault("rss_feeds", []).append(nf)
                    existing_names.add(nf["name"])
                    print(f"    ★ ADDED: {nf['name']} ({nf['url']})")
                else:
                    print(f"    → Candidate: {nf['name']} ({nf['url']})")
            else:
                print(f"    → Unhealthy, skipped: {nf['name']}")
            time.sleep(0.5)

        time.sleep(1)

    return sources


# ---------------------------------------------------------------------------
# メインエントリポイント
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RSS フィード自動メンテナンス")
    parser.add_argument("--dry-run", action="store_true", help="変更を保存せずにシミュレーションのみ")
    parser.add_argument("--auto-add", action="store_true", help="新規発見フィードを自動追加")
    parser.add_argument("--skip-grounding", action="store_true", help="Grounding Search をスキップ")
    args = parser.parse_args()

    print("=" * 60)
    print("RSS Feed Maintenance Tool")
    print("=" * 60)

    sources = _load_sources()
    health = _load_health()

    # Phase 1: 生存確認
    sources, health, failed_feeds = run_health_checks(sources, health, dry_run=args.dry_run)

    # Phase 2: Grounding Search（API キーが必要）
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key and not args.skip_grounding:
        sources = run_grounding_maintenance(
            sources, health, failed_feeds, api_key,
            auto_add=args.auto_add, dry_run=args.dry_run,
        )
    elif not api_key:
        print("\n[!] GEMINI_API_KEY not set — skipping Grounding Search phases.")

    # 保存
    if not args.dry_run:
        _save_sources(sources)
        _save_health(health)
        print(f"\n✓ Saved {SOURCES_PATH} and {HEALTH_PATH}")
    else:
        print(f"\n[dry-run] No files were modified.")

    # サマリー
    total = len(sources.get("rss_feeds", []))
    failed = len(failed_feeds)
    print(f"\nSummary: {total} feeds total, {failed} failed this run")


if __name__ == "__main__":
    main()
