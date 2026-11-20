"""臨時メッセージ（announcements）の選択と挿入の回帰テスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

from agents.announcements import (
    apply_announcements,
    build_greeting,
    insert_announcements,
    load_announcements,
    select_announcements,
)

META = {
    "short_title": "ワールドロボットニュース",
    "category": "Technology",
    "prompt_greeting": "おはようございます。{date}の{short_title}です。",
}

GREETING = "おはようございます。11月21日のワールドロボットニュースです。"


class SelectAnnouncementsTests(unittest.TestCase):
    def test_selects_only_matching_date(self) -> None:
        entries = [
            {"date": date(2026, 11, 21), "text": "A"},
            {"date": date(2026, 11, 22), "text": "B"},
        ]
        self.assertEqual(select_announcements(entries, date(2026, 11, 21)), ["A"])

    def test_accepts_iso_string_date(self) -> None:
        entries = [{"date": "2026-11-21", "text": "A"}]
        self.assertEqual(select_announcements(entries, date(2026, 11, 21)), ["A"])

    def test_skips_disabled_entries(self) -> None:
        entries = [{"date": date(2026, 11, 21), "text": "A", "enabled": False}]
        self.assertEqual(select_announcements(entries, date(2026, 11, 21)), [])

    def test_ignores_past_dates(self) -> None:
        entries = [{"date": date(2026, 11, 20), "text": "A"}]
        self.assertEqual(select_announcements(entries, date(2026, 11, 21)), [])

    def test_keeps_registration_order(self) -> None:
        entries = [
            {"date": date(2026, 11, 21), "text": "first"},
            {"date": date(2026, 11, 21), "text": "second"},
        ]
        self.assertEqual(
            select_announcements(entries, date(2026, 11, 21)),
            ["first", "second"],
        )

    def test_skips_blank_text(self) -> None:
        entries = [{"date": date(2026, 11, 21), "text": "   "}]
        self.assertEqual(select_announcements(entries, date(2026, 11, 21)), [])


class LoadAnnouncementsTests(unittest.TestCase):
    def test_missing_file_returns_empty_list(self) -> None:
        self.assertEqual(load_announcements("config/__does_not_exist__.yml"), [])

    def test_broken_yaml_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "announcements.yml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("announcements: [unclosed\n")
            self.assertEqual(load_announcements(path), [])

    def test_non_mapping_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "announcements.yml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("- just\n- a\n- list\n")
            self.assertEqual(load_announcements(path), [])


class BuildGreetingTests(unittest.TestCase):
    def test_matches_editor_format(self) -> None:
        self.assertEqual(build_greeting(META, date(2026, 11, 21)), GREETING)

    def test_unknown_placeholder_returns_empty(self) -> None:
        meta = {"prompt_greeting": "こんにちは {unknown} です。"}
        self.assertEqual(build_greeting(meta, date(2026, 11, 21)), "")


class InsertAnnouncementsTests(unittest.TestCase):
    def test_inserts_after_greeting(self) -> None:
        headline = f"{GREETING}本日は新しいロボットの発表がありました。"
        result = insert_announcements(headline, ["お知らせです。"], GREETING)
        self.assertEqual(
            result,
            f"{GREETING}\nお知らせです。\n本日は新しいロボットの発表がありました。",
        )

    def test_inserts_at_head_when_greeting_missing(self) -> None:
        headline = "本日は新しいニュースがありませんでした。"
        result = insert_announcements(headline, ["お知らせです。"], GREETING)
        self.assertEqual(result, "お知らせです。\n本日は新しいニュースがありませんでした。")

    def test_joins_multiple_messages(self) -> None:
        result = insert_announcements(GREETING, ["A。", "B。"], GREETING)
        self.assertEqual(result, f"{GREETING}\nA。\nB。")

    def test_no_texts_returns_headline_unchanged(self) -> None:
        headline = f"{GREETING}本文。"
        self.assertEqual(insert_announcements(headline, [], GREETING), headline)

    def test_empty_headline_returns_block(self) -> None:
        self.assertEqual(insert_announcements("", ["A。"], GREETING), "A。")


class ApplyAnnouncementsTests(unittest.TestCase):
    def _write(self, tmp: str, body: str) -> str:
        path = os.path.join(tmp, "announcements.yml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def test_applies_matching_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "announcements:\n"
                "  - date: 2026-11-21\n"
                "    text: \"お知らせです。\"\n",
            )
            headline = f"{GREETING}本日のニュースです。"
            result = apply_announcements(
                headline,
                meta_path="config/podcast_meta.yml",
                announcements_path=path,
                day=date(2026, 11, 21),
            )
            self.assertIn("お知らせです。", result)
            self.assertTrue(result.startswith(GREETING))

    def test_returns_headline_when_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "announcements:\n"
                "  - date: 2026-11-22\n"
                "    text: \"お知らせです。\"\n",
            )
            headline = f"{GREETING}本日のニュースです。"
            result = apply_announcements(
                headline,
                meta_path="config/podcast_meta.yml",
                announcements_path=path,
                day=date(2026, 11, 21),
            )
            self.assertEqual(result, headline)


if __name__ == "__main__":
    unittest.main()
