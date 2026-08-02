"""フリガナ展開がTTS入力を壊さないための回帰テスト。"""
from __future__ import annotations

import unittest

from agents.voice import _clean_text_for_tts, _tts_input_diagnostics


class CleanTextForTtsTests(unittest.TestCase):
    def test_expands_supported_ruby_notation(self) -> None:
        cases = {
            "漢字（かんじ）": "かんじ",
            "OpenAI（オープンエーアイ）": "オープンエーアイ",
            "C++（シープラスプラス）": "シープラスプラス",
            "取り扱い（とりあつかい）": "とりあつかい",
            "株式会社（かぶしきがいしゃ）": "かぶしきがいしゃ",
            "これは東京都（とうきょうと）の発表です。": "これはとうきょうとの発表です。",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(_clean_text_for_tts(source), expected)

    def test_preserves_parentheses_that_are_not_kana_readings(self) -> None:
        cases = (
            "名称（よみ：Reading）",
            "設定（Version 2）",
            "（注）漢字（かんじ）",
        )

        for source in cases:
            with self.subTest(source=source):
                expected = "（注）かんじ" if source.startswith("（注）") else source
                self.assertEqual(_clean_text_for_tts(source), expected)

    def test_tts_input_diagnostics_reports_non_content_metadata(self) -> None:
        diagnostics = _tts_input_diagnostics("AI\n東京")

        self.assertIn("chars=5", diagnostics)
        self.assertIn("utf8_bytes=9", diagnostics)
        self.assertIn("control_chars=0", diagnostics)
        self.assertIn("non_bmp_chars=0", diagnostics)
        self.assertRegex(diagnostics, r"sha256=[0-9a-f]{12}")


if __name__ == "__main__":
    unittest.main()