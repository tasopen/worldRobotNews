"""@scout のフィード取得がタイムアウト付きで行われることの回帰テスト。"""
from __future__ import annotations

import unittest
from unittest import mock

import agents.scout as scout


class FetchFeedBytesTests(unittest.TestCase):
    def test_passes_timeout_and_raises_for_http_error(self) -> None:
        response = mock.Mock()
        response.content = b"<rss/>"
        response.raise_for_status.return_value = None

        with mock.patch.object(scout.requests, "get", return_value=response) as get:
            data = scout._fetch_feed_bytes("https://example.com/feed", timeout=7.5)

        self.assertEqual(data, b"<rss/>")
        _, kwargs = get.call_args
        self.assertEqual(kwargs["timeout"], 7.5)
        self.assertIn("User-Agent", kwargs["headers"])
        response.raise_for_status.assert_called_once()

    def test_default_timeout_is_finite(self) -> None:
        self.assertIsInstance(scout.DEFAULT_REQUEST_TIMEOUT_SEC, float)
        self.assertGreater(scout.DEFAULT_REQUEST_TIMEOUT_SEC, 0)


class CollectTimeoutTests(unittest.TestCase):
    def test_collect_uses_configured_timeout_and_skips_duplicate_urls(self) -> None:
        config = {
            "keywords": [],
            "rss_feeds": [
                {"name": "A", "url": "https://example.com/a"},
                {"name": "A-dup", "url": "https://example.com/a"},
            ],
            "selection": {
                "max_articles": 5,
                "hours_lookback": 24,
                "max_per_source": 3,
                "request_timeout_sec": 3,
            },
        }

        calls: list[tuple[str, float]] = []

        def fake_fetch(feed_cfg, hours, timeout=scout.DEFAULT_REQUEST_TIMEOUT_SEC):
            calls.append((feed_cfg["url"], timeout))
            return []

        with (
            mock.patch.object(scout, "_load_config", return_value=config),
            mock.patch.object(scout, "_load_seen_urls", return_value=set()),
            mock.patch.object(scout, "fetch_rss", side_effect=fake_fetch),
            mock.patch.object(scout.time, "sleep", return_value=None),
        ):
            scout.collect()

        # 重複 URL は 1 回だけ取得され、timeout が config から渡る
        self.assertEqual(calls, [("https://example.com/a", 3.0)])


if __name__ == "__main__":
    unittest.main()
