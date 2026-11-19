#!/usr/bin/env python3
"""エピソード音声を GitHub Releases へ退避し、feed.xml の URL を差し替える。

GitHub Pages の 1 GB 上限を回避するため、MP3 の実体を Pages から追い出す。
`guid` は変更しないので、購読者は同じエピソードのまま新 URL から取得できる。

SRT（字幕）は台本の重複回避のため docs/episodes に残す。
保持するのは直近 `archive.srt_retention_days`（既定は `dedupe_days`）日分で、
それより古い SRT は MP3 と同様に削除する（実体は Releases にある）。

使い方:
  # 最新エピソードだけ退避（Actions の日次実行用）
  python scripts/archive_episode.py --latest

  # 既存の全エピソードを一括移行
  python scripts/archive_episode.py --all

  # 何が起きるかだけ確認
  python scripts/archive_episode.py --all --dry-run

必要な環境変数:
  GITHUB_TOKEN     GitHub Releases へのアップロード（Actions では secrets.GITHUB_TOKEN）

任意の環境変数:
  GITHUB_REPOSITORY   owner/repo。未設定なら git remote origin、
                      次に config/podcast_meta.yml の base_url から解決する
  ARCHIVE_SKIP_GITHUB 1 で GitHub Releases へのアップロードを省略
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.android import (  # noqa: E402
    ATOM_NS,
    CONTENT_NS,
    ITUNES_NS,
    PODCAST_NS,
    _load_meta,
)
from agents.repo import resolve_repo_slug  # noqa: E402

from xml.etree import ElementTree as ET  # noqa: E402

ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("content", CONTENT_NS)
ET.register_namespace("podcast", PODCAST_NS)

RELEASE_TAG_PREFIX = "episodes-"
EPISODES_DIR = "docs/episodes"
FEED_PATH = "docs/feed.xml"
DEFAULT_DEDUPE_DAYS = 6

GH_API = "https://api.github.com"


def log(msg: str) -> None:
    """Actions のログに即時反映させる（stdout はパイプだとブロックバッファされる）。"""
    print(msg, flush=True)


# --------------------------------------------------------------------------
# GitHub Releases
# --------------------------------------------------------------------------
def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def ensure_release(tag: str, token: str, repo: str) -> dict:
    """指定タグのリリースを取得する。無ければ作成する。"""
    headers = _gh_headers(token)
    r = requests.get(f"{GH_API}/repos/{repo}/releases/tags/{tag}", headers=headers, timeout=30)
    if r.status_code == 200:
        return r.json()
    if r.status_code != 404:
        r.raise_for_status()

    month = tag.removeprefix(RELEASE_TAG_PREFIX)
    r = requests.post(
        f"{GH_API}/repos/{repo}/releases",
        headers=headers,
        json={
            "tag_name": tag,
            "name": f"Episodes {month}",
            "body": (
                f"{month} のエピソード音声アーカイブ。\n\n"
                "GitHub Pages の容量上限を避けるため、音声実体はここに置いています。"
            ),
            "draft": False,
            "prerelease": False,
        },
        timeout=30,
    )
    r.raise_for_status()
    log(f"[archive] created release {tag}")
    return r.json()


def upload_release_asset(release: dict, path: str, token: str) -> str:
    """リリースにファイルを添付し、ダウンロード URL を返す。既存なら再利用する。"""
    name = os.path.basename(path)
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            log(f"[archive] release asset already exists: {name}")
            return asset["browser_download_url"]

    upload_url = release["upload_url"].split("{", 1)[0]
    with open(path, "rb") as f:
        payload = f.read()

    r = requests.post(
        f"{upload_url}?name={quote(name)}",
        headers={**_gh_headers(token), "Content-Type": "application/octet-stream"},
        data=payload,
        timeout=600,
    )
    r.raise_for_status()
    url = r.json()["browser_download_url"]
    log(f"[archive] uploaded to release: {name} ({len(payload) / 1e6:.2f} MB)")
    return url


# --------------------------------------------------------------------------
# 設定（config/podcast_meta.yml の archive セクション）
# --------------------------------------------------------------------------
def load_archive_config(meta_path: str = "config/podcast_meta.yml") -> dict:
    """archive セクションを読む。無ければ空 dict（＝既定値を使う）。"""
    try:
        meta = _load_meta(meta_path)
    except Exception as exc:  # noqa: BLE001
        log(f"[archive] WARNING: cannot read {meta_path}: {exc}")
        return {}
    section = (meta or {}).get("archive") or {}
    return section if isinstance(section, dict) else {}


def _meta_int(key: str, default: int) -> int:
    """config/podcast_meta.yml の整数値を取得する（不正値は既定値）。"""
    try:
        meta = _load_meta() or {}
    except Exception:  # noqa: BLE001
        return default
    try:
        return max(0, int(meta.get(key, default)))
    except (TypeError, ValueError):
        return default


def srt_retention_days() -> int:
    """docs/episodes に残す SRT の日数。

    `archive.srt_retention_days` があればそれを、無ければ editor と同じ
    `dedupe_days` を使う（参照できない古い SRT は残しても意味がないため）。
    """
    cfg = load_archive_config()
    value = cfg.get("srt_retention_days")
    if value is not None:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            log(f"[archive] WARNING: invalid archive.srt_retention_days={value!r}")
    return _meta_int("dedupe_days", DEFAULT_DEDUPE_DAYS)


def _is_recent_srt(path: str, days: int, today: date | None = None) -> bool:
    """ファイル名の日付が保持対象（当日 ～ days 日前）なら True。"""
    if days <= 0:
        return False
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})_\d{6}\.srt$", os.path.basename(path))
    if not m:
        return False
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    today = today or datetime.now(timezone(timedelta(hours=9))).date()
    return d >= today - timedelta(days=days)


def prune_old_srts(
    days: int,
    url_map: dict[str, str] | None = None,
    feed_path: str = FEED_PATH,
    dry_run: bool = False,
) -> tuple[int, int]:
    """保持日数を超えた SRT を削除する。Returns (removed, kept)。

    Releases に実体があると確認できたもの（今回アップロードした url_map、または
    feed.xml の transcript URL が Pages 以外を指している）だけを削除する。
    """
    url_map = url_map or {}
    archived_in_feed = _archived_srt_names(feed_path)
    removed = 0
    kept = 0
    for path in sorted(glob.glob(os.path.join(EPISODES_DIR, "*.srt"))):
        if _is_recent_srt(path, days):
            kept += 1
            continue
        name = os.path.basename(path)
        if name not in url_map and name not in archived_in_feed:
            log(f"[archive] keep old SRT (not archived yet): {name}")
            kept += 1
            continue
        if dry_run:
            log(f"[archive] (dry-run) would remove old SRT {path}")
        else:
            os.remove(path)
        removed += 1
    log(f"[archive] SRT retention: kept {kept}, removed {removed} (days={days})")
    return removed, kept


def _archived_srt_names(feed_path: str) -> set[str]:
    """feed.xml の transcript URL が Pages 以外を指している SRT 名の集合。"""
    try:
        root = ET.parse(feed_path).getroot()
    except Exception:  # noqa: BLE001
        return set()
    names: set[str] = set()
    for transcript in root.iter(f"{{{PODCAST_NS}}}transcript"):
        url = transcript.get("url") or ""
        if not url.endswith(".srt"):
            continue
        # base_url 配下（docs/episodes 配信）以外なら退避済みとみなす
        if not url.startswith("https://github.com/"):
            continue
        names.add(os.path.basename(url))
    return names


# --------------------------------------------------------------------------
# feed.xml の URL 差し替え
# --------------------------------------------------------------------------
def rewrite_feed(feed_path: str, url_map: dict[str, str], dry_run: bool = False) -> int:
    """feed.xml の enclosure / transcript URL を url_map に従って差し替える。

    guid と link は変更しない（購読者の同一性を保つため）。
    """
    tree = ET.parse(feed_path)
    root = tree.getroot()
    changed = 0

    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        if enclosure is not None:
            old = enclosure.get("url", "")
            new = url_map.get(os.path.basename(old))
            if new and new != old:
                enclosure.set("url", new)
                changed += 1

        # <link> も実体を指すようにする（<guid> は購読者の同一性のため変更しない）
        link = item.find("link")
        if link is not None and link.text:
            new = url_map.get(os.path.basename(link.text))
            if new and new != link.text:
                link.text = new
                changed += 1

        for transcript in item.findall(f"{{{PODCAST_NS}}}transcript"):
            old = transcript.get("url", "")
            new = url_map.get(os.path.basename(old))
            if new and new != old:
                transcript.set("url", new)
                changed += 1

    if changed and not dry_run:
        ET.indent(tree, space="  ")
        with open(feed_path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            tree.write(f, encoding="unicode", xml_declaration=False)
        log(f"[archive] feed.xml updated: {changed} URL(s) rewritten")
        _regenerate_index(feed_path, root)
    elif changed:
        log(f"[archive] (dry-run) would rewrite {changed} URL(s) in feed.xml")
    else:
        log("[archive] feed.xml already up to date")
    return changed


def _regenerate_index(feed_path: str, root: ET.Element) -> None:
    """index.html の再生リンクも新しい URL に合わせて作り直す。"""
    from agents.android import (
        _build_podcast,
        _extract_episodes,
        _load_meta,
        generate_index_html,
        _write_index_html,
    )

    channel = root.find("channel")
    if channel is None:
        return
    try:
        meta = _load_meta()
        base_url = meta["base_url"].rstrip("/")
        podcast = _build_podcast(meta, base_url)
        episodes = _extract_episodes(channel)
        html = generate_index_html(podcast, episodes)
        index_path = os.path.join(os.path.dirname(feed_path), "index.html")
        _write_index_html(html, index_path)
        log(f"[archive] index.html regenerated -> {index_path}")
    except Exception as exc:  # noqa: BLE001
        log(f"[archive] index.html regeneration skipped: {exc}")


# --------------------------------------------------------------------------
# 対象エピソードの収集
# --------------------------------------------------------------------------
def _episode_files(mp3_path: str) -> list[str]:
    files = [mp3_path]
    srt = mp3_path[:-4] + ".srt"
    if os.path.exists(srt):
        files.append(srt)
    return files


def _month_tag(mp3_path: str) -> str:
    """2026-11-07_084127.mp3 -> episodes-2026-11"""
    return RELEASE_TAG_PREFIX + os.path.basename(mp3_path)[:7]


def collect_targets(all_episodes: bool) -> list[str]:
    if not os.path.isdir(EPISODES_DIR):
        raise SystemExit(f"episodes directory not found: {EPISODES_DIR}")
    mp3s = sorted(
        os.path.join(EPISODES_DIR, n)
        for n in os.listdir(EPISODES_DIR)
        if n.endswith(".mp3")
    )
    if not mp3s:
        raise SystemExit("no mp3 files found")
    return mp3s if all_episodes else mp3s[-1:]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true", help="最新エピソードのみ退避")
    group.add_argument("--all", action="store_true", help="既存の全エピソードを一括移行")
    parser.add_argument("--dry-run", action="store_true", help="アップロードせず対象だけ表示")
    parser.add_argument(
        "--delete-local",
        action="store_true",
        help="アップロード成功後に docs/episodes の実体を削除する",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "一部の退避に失敗しても続行する。"
            "URL が確定したファイルだけ削除し、未退避の実体は Pages 配信を継続する"
        ),
    )
    args = parser.parse_args()

    repo = resolve_repo_slug()
    if not repo:
        raise SystemExit(
            "cannot determine GitHub repository: set GITHUB_REPOSITORY=owner/repo "
            "or configure base_url in config/podcast_meta.yml"
        )
    token = os.environ.get("GITHUB_TOKEN", "")

    cfg = load_archive_config()

    # GitHub Releases は無料・容量無制限なので既定で有効
    skip_github = os.environ.get("ARCHIVE_SKIP_GITHUB") == "1" or not cfg.get(
        "github_releases", True
    )
    if not args.dry_run:
        if skip_github:
            raise SystemExit("nothing to do: GitHub Releases is disabled")
        if not token:
            raise SystemExit("GITHUB_TOKEN is required (or set ARCHIVE_SKIP_GITHUB=1)")

    targets = collect_targets(args.all)
    log(f"[archive] target episodes: {len(targets)}")
    log(f"[archive] destinations: github_releases={'off' if skip_github else 'on'}")

    if args.dry_run:
        for mp3 in targets:
            log(f"  {mp3}  -> tag={_month_tag(mp3)}")
        log(f"[archive] srt_retention_days={srt_retention_days()}")
        prune_old_srts(srt_retention_days(), dry_run=True)
        return 0

    # 月ごとにまとめてアップロードする
    by_month: dict[str, list[str]] = {}
    for mp3 in targets:
        by_month.setdefault(_month_tag(mp3), []).append(mp3)

    url_map: dict[str, str] = {}
    failures: list[str] = []

    total_months = len(by_month)
    for month_idx, (tag, mp3s) in enumerate(sorted(by_month.items()), start=1):
        month = tag.removeprefix(RELEASE_TAG_PREFIX)
        files: list[str] = []
        for mp3 in mp3s:
            files.extend(_episode_files(mp3))

        log(
            f"[archive] === [{month_idx}/{total_months}] {month}: "
            f"{len(mp3s)} episode(s), {len(files)} file(s) ==="
        )
        started = time.monotonic()

        gh_urls: dict[str, str] = {}
        if not skip_github:
            try:
                release = ensure_release(tag, token, repo)
                for i, path in enumerate(files, start=1):
                    log(f"[archive]   github {i}/{len(files)}: {os.path.basename(path)}")
                    gh_urls[os.path.basename(path)] = upload_release_asset(release, path, token)
            except Exception as exc:  # noqa: BLE001
                log(f"[archive] ERROR github release {tag}: {exc}")
                failures.append(f"github:{tag}")

        url_map.update(gh_urls)

        log(
            f"[archive] --- [{month_idx}/{total_months}] {month} done "
            f"in {time.monotonic() - started:.1f}s ---"
        )

    rewrite_feed(FEED_PATH, url_map)

    if failures:
        log(f"[archive] completed with failures: {', '.join(failures)}")
        if not args.allow_partial:
            # 退避に失敗したファイルは消さない（Pages から配信を継続する）
            log("[archive] local files kept to avoid broken enclosures")
            return 1
        log("[archive] --allow-partial: deleting only files with a resolved URL")

    if args.delete_local:
        removed = 0
        kept = 0
        for path in targets:
            # MP3 は容量が大きいので退避できた時点で削除する
            if not os.path.exists(path):
                continue
            # URL が確定したファイルだけ削除する（未退避の実体は Pages 配信を継続）
            if os.path.basename(path) not in url_map:
                kept += 1
                continue
            os.remove(path)
            removed += 1
        log(f"[archive] removed {removed} local mp3 file(s) from {EPISODES_DIR}")
        if kept:
            log(f"[archive] kept {kept} local mp3 file(s) without a resolved URL")

        # SRT は台本の重複回避で editor が参照するため、保持日数分だけ残す
        prune_old_srts(srt_retention_days(), url_map=url_map)

    log("[archive] done")
    return 1 if failures and not args.allow_partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
