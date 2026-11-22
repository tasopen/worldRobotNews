"""フリガナ展開がTTS入力を壊さないための回帰テスト。"""
from __future__ import annotations

import unittest

from agents.voice import (
    _build_tts_prompt,
    _clean_text_for_tts,
    _format_tts_prompt_block,
    _prepare_tts_script,
    _tts_input_diagnostics,
)


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


class PrepareTtsScriptTests(unittest.TestCase):
    def test_disabled_conversion_preserves_description_and_reading(self) -> None:
        script = "ソフトウェア開発を手掛ける開普雲（カイプーユン）が、四川省の国有投資プラットフォームである四川発展（しせんはってん）と連携し、"
        self.assertEqual(_prepare_tts_script(script, {"furigana_conversion": False}), script)

    def test_enabled_or_missing_setting_keeps_existing_conversion(self) -> None:
        for meta in ({"furigana_conversion": True}, {}):
            with self.subTest(meta=meta):
                self.assertEqual(_prepare_tts_script("開普雲（カイプーユン）", meta), "カイプーユン")


class BuildTtsPromptTests(unittest.TestCase):
    def test_build_tts_prompt_prepends_request_id(self) -> None:
        prompt = _build_tts_prompt("abc123", "PERSONA\n", "今日のニュースです。")
        self.assertTrue(
            prompt.startswith("[request_id=abc123]\nPERSONA\n今日のニュースです。")
        )

    def test_prompt_block_embeds_prompt_verbatim(self) -> None:
        prompt = _build_tts_prompt("abc123", "PERSONA\n", "今日のニュースです。")
        block = _format_tts_prompt_block(prompt, "abc123")

        self.assertIn("request_id=abc123", block)
        self.assertIn(f"chars={len(prompt)}", block)
        self.assertIn("----- TTS PROMPT BEGIN -----", block)
        self.assertIn("----- TTS PROMPT END -----", block)
        # プロンプト本文は加工されずにそのまま含まれる
        self.assertIn(prompt, block)


if __name__ == "__main__":
    unittest.main()