#!/usr/bin/env python3
"""GitHub Releases から直近の SRT を docs/episodes へ取り戻す（重複回避用）。

アーカイブ後に docs/episodes が空になると、`agents/editor.py` は過去の放送内容を
参照できず、同じニュースを繰り返してしまう。このスクリプトは Releases
（`episodes-YYYY-MM` タグ）から直近 `dedupe_days` 日分の SRT を取得して
docs/episodes に戻す。日次のパイプラインからも呼ばれる（冪等）。

使い方:
  # 何を取り戻すか確認するだけ
  python scripts/backfill_srts.py --dry-run

  # 直近 dedupe_days 日分を取得
  python scripts/backfill_srts.py

任意の環境変数:
  GITHUB_TOKEN        Release 取得用（公開リポジトリなら不要）
  GITHUB_REPOSITORY   owner/repo。未設定なら git remote origin、
                      次に config/podcast_meta.yml の base_url から解決する
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.editor import DEFAULT_DEDUPE_DAYS, _load_meta, _parse_episode_date  # noqa: E402
from agents.repo import resolve_repo_slug  # noqa: E402

RELEASE_TAG_PREFIX = "episodes-"
EPISODES_DIR = "docs/episodes"
GH_API = "https://api.github.com"


def log(msg: str) -> None:
    print(msg, flush=True)


def _gh_headers(token: str) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def dedupe_days() -> int:
    try:
        meta = _load_meta() or {}
    except Exception:  # noqa: BLE001
        return DEFAULT_DEDUPE_DAYS
    try:
        return max(0, int(meta.get("dedupe_days", DEFAULT_DEDUPE_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_DEDUPE_DAYS


def release_srt_assets(month: str, token: str, repo: str) -> dict[str, str]:
    """episodes-YYYY-MM リリースの SRT 名 -> ダウンロード URL。"""
    url = f"{GH_API}/repos/{repo}/releases/tags/{RELEASE_TAG_PREFIX}{month}"
    r = requests.get(url, headers=_gh_headers(token), timeout=30)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return {
        a["name"]: a["browser_download_url"]
        for a in r.json().get("assets", [])
        if a["name"].endswith(".srt")
    }


def download(url: str, dest: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    log(f"[backfill] restored {os.path.basename(dest)} ({os.path.getsize(dest)} B)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="ダウンロードせず対象だけ表示")
    parser.add_argument("--days", type=int, default=None, help="対象日数（既定: dedupe_days）")
    args = parser.parse_args()

    repo = resolve_repo_slug()
    if not repo:
        log(
            "[backfill] cannot determine GitHub repository: "
            "set GITHUB_REPOSITORY=owner/repo or configure base_url"
        )
        return 1
    token = os.environ.get("GITHUB_TOKEN", "")
    days = args.days if args.days is not None else dedupe_days()
    if days <= 0:
        log("[backfill] dedupe_days=0 - nothing to restore")
        return 0

    today = datetime.now(timezone(timedelta(hours=9))).date()
    cutoff = today - timedelta(days=days)
    os.makedirs(EPISODES_DIR, exist_ok=True)

    wanted: list[tuple[date, str, str]] = []
    months = {(cutoff - timedelta(days=i)).strftime("%Y-%m") for i in range(0, days + 2)}
    months.add(today.strftime("%Y-%m"))
    for month in sorted(months):
        for name, url in release_srt_assets(month, token, repo).items():
            d = _parse_episode_date(name)
            if d is None or d < cutoff:
                continue
            if os.path.exists(os.path.join(EPISODES_DIR, name)):
                continue
            wanted.append((d, name, url))

    wanted.sort(reverse=True)
    # editor は「今日」を除いた days 日分を参照するので、当日分を含めて days+1 件まで
    wanted = wanted[: days + 1]
    if not wanted:
        log(f"[backfill] no missing SRT in the last {days} day(s)")
        return 0

    for d, name, url in wanted:
        log(f"[backfill] {d} {name}")
        if args.dry_run:
            continue
        download(url, os.path.join(EPISODES_DIR, name), token)

    if args.dry_run:
        log(f"[backfill] (dry-run) would restore {len(wanted)} SRT file(s)")
    else:
        log(f"[backfill] restored {len(wanted)} SRT file(s) into {EPISODES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
