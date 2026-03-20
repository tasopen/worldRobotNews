"""@android: Podcast RSS フィード生成・更新エージェント

エピソード情報を受け取り、iTunes/Podcast 準拠の feed.xml を更新する。
"""
from __future__ import annotations

from email.utils import format_datetime, parsedate_to_datetime
from html import escape
import os
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

import yaml


ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"

ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("content", CONTENT_NS)
ET.register_namespace("podcast", PODCAST_NS)



def _load_meta(meta_path: str = "config/podcast_meta.yml") -> dict:
    with open(meta_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _itunes(tag: str) -> str:
    return f"{{{ITUNES_NS}}}{tag}"


def _atom(tag: str) -> str:
    return f"{{{ATOM_NS}}}{tag}"


def _format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _normalize_explicit(value: str | None) -> str:
    normalized = str(value or "false").strip().lower()
    if normalized in {"yes", "true", "explicit"}:
        return "true"
    if normalized in {"no", "false", "clean"}:
        return "false"
    return normalized


def trim(text: str, length: int = 120) -> str:
    return text[:length] + "..." if len(text) > length else text


def render_episode(ep: dict) -> str:
    """1エピソード分のHTMLを返す"""
    title = escape(str(ep.get("title", "")))
    description = escape(trim(str(ep.get("description", ""))))
    pub_date = ep.get("pub_date")
    date_str = pub_date.strftime("%Y-%m-%d") if isinstance(pub_date, datetime) else ""
    audio_url = escape(str(ep.get("audio_url", "")))

    return f"""
    <div class="episode">
        <h3>{title}</h3>
        <div class="date">{date_str}</div>
        <p>{description}</p>
        <audio controls src="{audio_url}"></audio>
    </div>
    """


def generate_index_html(podcast: dict, episodes: list[dict]) -> str:
    """index.html のHTML文字列を返す"""
    latest_episodes = episodes[:5]
    episodes_html = "\n".join(render_episode(ep) for ep in latest_episodes)

    html = f"""
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(str(podcast.get("title", "Podcast")))}</title>

<meta name="description" content="{escape(str(podcast.get("description", "")))}">

<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  max-width: 720px;
  margin: auto;
  padding: 20px;
}}
header {{
  text-align: center;
}}
img.cover {{
  width: 200px;
  border-radius: 16px;
}}
.episode {{
  border-bottom: 1px solid #eee;
  padding: 16px 0;
}}
audio {{
  width: 100%;
}}
.date {{
  color: #888;
  font-size: 0.9em;
}}
</style>

</head>
<body>

<header>
  <img class="cover" src="{escape(str(podcast.get("cover_image", "")))}">
  <h1>{escape(str(podcast.get("title", "Podcast")))}</h1>
  <p>{escape(str(podcast.get("description", "")))}</p>
  <p><a href="{escape(str(podcast.get("rss_url", "")))}">RSS</a></p>
</header>

<section>
<h2>最新エピソード</h2>
{episodes_html}
</section>

<footer>
<p>Generated automatically</p>
</footer>

</body>
</html>
"""
    return html


def _write_index_html(html: str, index_path: str = "docs/index.html") -> str:
    directory = os.path.dirname(index_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    return index_path


def _extract_episodes(channel: ET.Element) -> list[dict]:
    episodes: list[dict] = []
    default_date = datetime.now(timezone.utc)
    for item in channel.findall("item"):
        title = item.findtext("title", default="")
        description = item.findtext(
            "description",
            default=item.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}summary", default=""),
        )
        pub_date_text = item.findtext("pubDate", default="")
        try:
            pub_date = parsedate_to_datetime(pub_date_text) if pub_date_text else default_date
        except (TypeError, ValueError):
            pub_date = default_date

        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None and enclosure.get("url") else item.findtext("link", default="")
        link = item.findtext("link", default=audio_url)
        episodes.append(
            {
                "title": title,
                "description": description,
                "pub_date": pub_date,
                "audio_url": audio_url,
                "link": link,
            }
        )

    return episodes


def _build_podcast(meta: dict, base_url: str) -> dict:
    return {
        "title": meta.get("title", "Podcast"),
        "description": meta.get("description", ""),
        "cover_image": meta.get("cover_image", f"{base_url}/podcast_cover.jpg"),
        "rss_url": meta.get("rss_url", f"{base_url}/feed.xml"),
    }


def _ensure_text_element(parent: ET.Element, tag: str, text: str) -> ET.Element:
    element = parent.find(tag)
    if element is None:
        element = ET.SubElement(parent, tag)
    element.text = text
    return element


def _ensure_channel_metadata(channel: ET.Element, meta: dict, base_url: str) -> None:
    rss_url = meta.get("rss_url", f"{base_url}/feed.xml")
    cover_image = meta.get("cover_image", f"{base_url}/podcast_cover.jpg")
    author_name = meta.get("name") or meta.get("author") or "Podcast"
    owner_name = meta.get("name") or author_name
    explicit_value = _normalize_explicit(meta.get("explicit", "false"))

    _ensure_text_element(channel, "title", meta["title"])
    _ensure_text_element(channel, "link", base_url)
    _ensure_text_element(channel, "description", meta["description"])
    _ensure_text_element(channel, "language", meta.get("language", "ja"))
    _ensure_text_element(channel, _itunes("author"), author_name)
    _ensure_text_element(channel, _itunes("explicit"), explicit_value)

    atom_link = None
    for candidate in channel.findall(_atom("link")):
        if candidate.get("rel") == "self":
            atom_link = candidate
            break
    if atom_link is None:
        atom_link = ET.SubElement(channel, _atom("link"))
    atom_link.set("href", rss_url)
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    image = channel.find(_itunes("image"))
    if image is None:
        image = ET.SubElement(channel, _itunes("image"))
    image.set("href", cover_image)

    category = channel.find(_itunes("category"))
    if category is None:
        category = ET.SubElement(channel, _itunes("category"))
    category.set("text", meta.get("category", "Technology"))

    owner = channel.find(_itunes("owner"))
    if owner is None:
        owner = ET.SubElement(channel, _itunes("owner"))
    _ensure_text_element(owner, _itunes("name"), owner_name)
    email = meta.get("email")
    owner_email = owner.find(_itunes("email"))
    if email:
        if owner_email is None:
            owner_email = ET.SubElement(owner, _itunes("email"))
        owner_email.text = email
    elif owner_email is not None:
        owner.remove(owner_email)


def _create_new_feed(meta: dict, base_url: str) -> tuple[ET.ElementTree, ET.Element, ET.Element]:
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    _ensure_channel_metadata(channel, meta, base_url)
    return ET.ElementTree(root), root, channel


def update_feed(
    date_str: str,
    mp3_path: str,
    script: str,
    duration_sec: int,
    srt_path: str | None = None,
    feed_path: str | None = None,
    index_path: str | None = None,
    meta_path: str = "config/podcast_meta.yml",
) -> str:
    """feed.xml に新エピソードを追加して保存する。feed_path を返す。"""
    # feed_path: Noneならデフォルト値を使用
    if feed_path is None:
        feed_path = "docs/feed.xml"
    assert feed_path is not None
    if index_path is None:
        index_path = os.path.join(os.path.dirname(feed_path), "index.html")
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

    podcast = _build_podcast(meta, base_url)
    _ensure_channel_metadata(channel, meta, base_url)

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

    try:
        episodes = _extract_episodes(channel)
        html = generate_index_html(podcast, episodes)
        _write_index_html(html, index_path)
        print(f"[android] index.html updated → {index_path}")
    except Exception as e:
        print(f"[android] index.html generation failed: {e}")

    return feed_path


if __name__ == "__main__":
    # ローカルテスト用
    update_feed(
        date_str="2025-01-01",
        mp3_path="docs/episodes/2025-01-01.mp3",
        script="本日のAIニュースです。テスト用エピソードです。",
        duration_sec=180,
    )
