"""@android: Podcast RSS フィード生成・更新エージェント

エピソード情報を受け取り、iTunes/Podcast 準拠の feed.xml を更新する。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from xml.etree import ElementTree as ET

import yaml


ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"

ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("content", CONTENT_NS)
ET.register_namespace("podcast", PODCAST_NS)



def _load_meta(meta_path: str = "config/podcast_meta.yml") -> dict:
    with open(meta_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _itunes(tag: str) -> str:
    return f"{{{ITUNES_NS}}}{tag}"


def _format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _create_new_feed(meta: dict, base_url: str) -> tuple[ET.ElementTree, ET.Element, ET.Element]:
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = meta["title"]
    ET.SubElement(channel, "link").text = base_url
    ET.SubElement(channel, "description").text = meta["description"]
    ET.SubElement(channel, _itunes("image"), {"href": f"{base_url}/podcast_cover.jpg"})
    ET.SubElement(channel, "language").text = meta.get("language", "ja")
    itunes_author = ET.SubElement(channel, _itunes("author"))
    itunes_author.text = meta["author"]
    itunes_cat = ET.SubElement(channel, _itunes("category"))
    itunes_cat.set("text", meta.get("category", "Technology"))
    itunes_explicit = ET.SubElement(channel, _itunes("explicit"))
    itunes_explicit.text = meta.get("explicit", "no")
    return ET.ElementTree(root), root, channel


def update_feed(
    date_str: str,
    mp3_path: str,
    script: str,
    duration_sec: int,
    srt_path: str | None = None,
    feed_path: str | None = None,
    meta_path: str = "config/podcast_meta.yml",
) -> str:
    """feed.xml に新エピソードを追加して保存する。feed_path を返す。"""
    # feed_path: Noneならデフォルト値を使用
    if feed_path is None:
        feed_path = "docs/feed.xml"
    assert feed_path is not None
    meta = _load_meta(meta_path)
    base_url = meta["base_url"].rstrip("/")
    mp3_filename = os.path.basename(mp3_path)
    mp3_url = f"{base_url}/episodes/{mp3_filename}"
    mp3_size = os.path.getsize(mp3_path)
    def _podcast(tag: str) -> str:
        return f"{{{PODCAST_NS}}}{tag}"

    # 既存 feed.xml をパース（なければ雛形から）
    from xml.etree.ElementTree import ParseError
    if os.path.exists(feed_path):
        try:
            ET.parse(feed_path)  # validate existing XML
            tree = ET.parse(feed_path)
            root = tree.getroot()
            channel = root.find("channel")
        except ParseError as e:
            # 壊れた場合はバックアップして新規生成
            import shutil
            import time
            bak_path = feed_path + ".bak_" + time.strftime("%Y%m%d_%H%M%S")
            shutil.copy(feed_path, bak_path)
            print(f"[android] feed.xml parse error: {e}. Backed up to {bak_path}. Regenerating...")
            tree, root, channel = _create_new_feed(meta, base_url)
            if channel is None:
                raise RuntimeError("Failed to create RSS channel")
    else:
        tree, root, channel = _create_new_feed(meta, base_url)

    # lastBuildDate を更新
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    if channel is None:
         raise RuntimeError("Channel is None - failed to parse or create feed")

    last_build = channel.find("lastBuildDate")
    if last_build is None:
        last_build = ET.SubElement(channel, "lastBuildDate")
    last_build.text = format_datetime(now)

    # 新しい <item> を構築
    item = ET.Element("item")
    short_title = meta.get("short_title", meta.get("title", "ニュース"))
    title_str = f"{short_title} {date_str}"
    ET.SubElement(item, "title").text = title_str
    ET.SubElement(item, "link").text = mp3_url
    ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = mp3_url
    ET.SubElement(item, "pubDate").text = format_datetime(now)
    ET.SubElement(item, "description").text = script[:300]
    ET.SubElement(item, "enclosure", {
        "url": mp3_url,
        "type": "audio/mpeg",
        "length": str(mp3_size),
    })
    # Podcasting 2.0 transcript tag if provided
    assert feed_path is not None
    if srt_path and os.path.exists(srt_path):
        srt_filename = os.path.basename(srt_path)
        srt_url = f"{base_url}/episodes/{srt_filename}"
        ET.SubElement(item, _podcast("transcript"), {
            "url": srt_url,
            "type": "application/x-subrip",
            "rel": "captions"
        })
    ET.SubElement(item, _itunes("duration")).text = _format_duration(duration_sec)
    ET.SubElement(item, _itunes("summary")).text = script[:300]

    # channel の先頭（lastBuildDate の後）に item を挿入
    channel.insert(list(channel).index(last_build) + 1, item)

    # 書き出し
    os.makedirs(os.path.dirname(feed_path), exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="unicode", xml_declaration=False)

    print(f"[android] feed.xml updated → {title_str}")
    return feed_path


if __name__ == "__main__":
    # ローカルテスト用
    update_feed(
        date_str="2025-01-01",
        mp3_path="docs/episodes/2025-01-01.mp3",
        script="本日のAIニュースです。テスト用エピソードです。",
        duration_sec=180,
    )
