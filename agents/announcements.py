"""臨時メッセージ（お知らせ）の管理と台本への挿入。

`config/announcements.yml` に放送日（JST）を指定して文言を登録すると、
その日のエピソードだけ Greeting の直後に読み上げられる。

LLM を介さず決定論的に挿入するため、文言は一字一句そのまま音声化される。
過去日付のエントリは自動的に無視されるので、削除し忘れても実害はない。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import yaml

ANNOUNCEMENTS_PATH = "config/announcements.yml"
META_PATH = "config/podcast_meta.yml"

_JST = timezone(timedelta(hours=9))


def today_jst() -> date:
    """日本時間の今日の日付。"""
    return datetime.now(_JST).date()


def _normalize_date(value: Any) -> date | None:
    """YAML の日付値を date に正規化する。

    `date: 2026-11-21` は PyYAML が datetime.date として解釈するため、
    文字列・date・datetime のいずれも受け付ける。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def load_announcements(path: str = ANNOUNCEMENTS_PATH) -> list[dict]:
    """announcements.yml を読み込む。ファイルが無い・壊れている場合は空リスト。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        print(f"[announcements] WARNING: failed to load {path}: {e}")
        return []
    if not isinstance(data, dict):
        print(f"[announcements] WARNING: {path} must be a mapping with an 'announcements' key")
        return []
    entries = data.get("announcements") or []
    if not isinstance(entries, list):
        print(f"[announcements] WARNING: 'announcements' in {path} must be a list")
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def select_announcements(entries: list[dict], day: date | None = None) -> list[str]:
    """指定日（既定は JST の今日）に該当する有効な文言を登録順で返す。"""
    day = day or today_jst()
    texts: list[str] = []
    for entry in entries:
        if entry.get("enabled", True) is False:
            continue
        if _normalize_date(entry.get("date")) != day:
            continue
        text = str(entry.get("text") or "").strip()
        if text:
            texts.append(text)
    return texts


def announcement_texts_for_today(
    path: str = ANNOUNCEMENTS_PATH,
    day: date | None = None,
) -> list[str]:
    """今日の臨時メッセージ文言一覧。"""
    return select_announcements(load_announcements(path), day)


def build_greeting(meta: dict, day: date | None = None) -> str:
    """editor と同じ規則で Greeting を再構成する。

    `agents/editor.py` の `generate_headline_and_body` と同一の組み立て方に
    そろえることで、headline の先頭一致判定を成立させる。
    """
    day = day or today_jst()
    category = meta.get("category", "Technology")
    short_title = meta.get("short_title", meta.get("title", "ニュース"))
    template = meta.get("prompt_greeting", "おはようございます、{short_title} です。")
    try:
        return template.format(
            category=category,
            short_title=short_title,
            date=f"{day.month}月{day.day}日",
        )
    except (KeyError, IndexError, ValueError):
        return ""


def insert_announcements(headline: str, texts: list[str], greeting: str = "") -> str:
    """headline の Greeting 直後に臨時メッセージを挿入する。

    Greeting が見つからない場合（記事ゼロ時のアナウンス等）は先頭に挿入する。
    前後に改行を入れることで、TTS の分割（句点・改行）で独立したセグメントになる。
    """
    if not texts:
        return headline
    block = "\n".join(texts)
    if greeting and headline.startswith(greeting):
        rest = headline[len(greeting):].lstrip("\n")
        return f"{greeting}\n{block}\n{rest}" if rest else f"{greeting}\n{block}"
    return f"{block}\n{headline}" if headline else block


def apply_announcements(
    headline: str,
    meta_path: str = META_PATH,
    announcements_path: str = ANNOUNCEMENTS_PATH,
    day: date | None = None,
) -> str:
    """今日の臨時メッセージを headline に適用した文字列を返す。

    該当が無ければ headline をそのまま返す。
    """
    day = day or today_jst()
    texts = announcement_texts_for_today(announcements_path, day)
    if not texts:
        return headline

    meta: dict = {}
    try:
        with open(meta_path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if isinstance(loaded, dict):
            meta = loaded
    except (OSError, yaml.YAMLError) as e:
        print(f"[announcements] WARNING: failed to load {meta_path}: {e}")

    greeting = build_greeting(meta, day)
    print(f"[announcements] inserting {len(texts)} message(s) for {day.isoformat()}")
    return insert_announcements(headline, texts, greeting)
