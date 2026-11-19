#!/usr/bin/env python3
"""既存エピソードの SRT から MP3 を再生成するスクリプト

使い方:
  python scripts/regenerate_episode.py docs/episodes/2026-10-26_063250.srt

やること:
  1. SRT から各セグメントのテキストを抽出
  2. 文字列置換（TEXT_OVERRIDES）を適用
  3. セグメントごとに TTS 再合成（WAV）
  4. 言語チェック（Gemini）で日本語以外を検出したら該当セグメントを再試行
  5. 結合して MP3 を上書き、SRT のタイムスタンプを再計算して上書き
  6. feed.xml の enclosure length / itunes:duration を更新
     （guid / pubDate / ファイル名は変えないため、ポッドキャストクライアントは
       同一エピソードとして修正済み MP3 を再ダウンロードできる）

10/27 など他のエピソードには一切触れない。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.voice import (  # noqa: E402
    _convert_wav_to_mp3,
    _wav_exact_duration_ms,
    get_audio_duration,
    synthesize,
)
from agents.voice_concat import concat_wav  # noqa: E402
from scripts.run_pipeline import _write_srt  # noqa: E402

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

# セグメント番号（1始まり）ごとの文字列置換。
# 10/26 第7セグメントは TTS が英語化したため、文頭の英字表記をカタカナへ
# 変えて日本語読み上げを誘導する。
TEXT_OVERRIDES: dict[int, list[tuple[str, str]]] = {
    7: [("The Guardian", "ガーディアン")],
}

# 言語チェックで日本語以外と判定された場合のセグメント再試行回数
MAX_LANGUAGE_RETRIES = 3


def parse_srt(srt_path: str) -> list[str]:
    """SRT からセグメント本文（1始まりの順序）を抽出する。"""
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    blocks = [b for b in re.split(r"\n\s*\n", content.strip()) if b.strip()]
    texts: list[str] = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            raise ValueError(f"SRTブロックが不正です: {block!r}")
        texts.append("\n".join(lines[2:]).strip())
    if not texts:
        raise ValueError(f"SRTにセグメントがありません: {srt_path}")
    return texts


def apply_overrides(seg_no: int, text: str) -> str:
    for old, new in TEXT_OVERRIDES.get(seg_no, []):
        if old in text:
            text = text.replace(old, new)
            print(f"[regen] seg {seg_no}: '{old}' -> '{new}'")
    return text


def check_language(mp3_path: str, start_ms: int, end_ms: int) -> str:
    """MP3 の指定区間を書き出し、話されている言語を1語で返す。"""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    start = start_ms / 1000
    # 冒頭15秒だけ判定すれば言語は十分分かる
    end = min(end_ms / 1000, start + 15)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                "-i", mp3_path,
                "-ar", "16000", "-ac", "1", tmp_path,
            ],
            check=True,
        )
        with open(tmp_path, "rb") as f:
            audio = f.read()
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=audio, mime_type="audio/wav"),
                "この音声で話されている言語を1語だけで答えてください。"
                "例: Japanese / English",
            ],
        )
        return (resp.text or "").strip()
    finally:
        os.unlink(tmp_path)


def synthesize_segment(
    text: str, wav_path: str, seg_no: int, total: int, debug: bool
) -> int:
    """1セグメントを TTS し、WAV を wav_path に保存して長さ(ms)を返す。"""
    print(f"[regen] synthesizing segment {seg_no}/{total} ({len(text)} chars)...")
    synthesize(text, wav_path, debug=debug, output_format="wav")
    with open(wav_path, "rb") as f:
        return _wav_exact_duration_ms(f.read())


def update_feed_entry(feed_path: str, mp3_path: str) -> None:
    """feed.xml の該当エピソード項目の length / duration を更新する。

    guid・pubDate・URL は変えないため、ポッドキャストクライアントは
    同一エピソードとして修正済みファイルを取得できる。
    """
    tree = ET.parse(feed_path)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("feed.xml に channel がありません")
    filename = os.path.basename(mp3_path)
    size = os.path.getsize(mp3_path)
    duration_sec = get_audio_duration(mp3_path)

    updated = False
    for item in channel.findall("item"):
        link = item.findtext("link") or ""
        guid = item.findtext("guid") or ""
        if filename not in link and filename not in guid:
            continue
        enclosure = item.find("enclosure")
        if enclosure is not None:
            enclosure.set("length", str(size))
        dur = item.find(f"{{{ITUNES_NS}}}duration")
        if dur is None:
            dur = ET.SubElement(item, f"{{{ITUNES_NS}}}duration")
        h, rem = divmod(duration_sec, 3600)
        m, s = divmod(rem, 60)
        dur.text = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        updated = True
        print(
            f"[regen] feed updated: length={size}, duration={dur.text}"
        )
        break

    if not updated:
        raise RuntimeError(f"feed.xml に {filename} の項目が見つかりません")

    ET.indent(tree, space="  ")
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="unicode", xml_declaration=False)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    debug = "--debug" in sys.argv
    if len(args) != 1:
        print("使い方: python scripts/regenerate_episode.py <episode.srt> [--debug]")
        sys.exit(1)

    srt_path = args[0]
    if not srt_path.endswith(".srt") or not os.path.exists(srt_path):
        print(f"SRT が見つかりません: {srt_path}")
        sys.exit(1)
    mp3_path = srt_path[:-4] + ".mp3"
    if not os.path.exists(mp3_path):
        print(f"対応する MP3 が見つかりません: {mp3_path}")
        sys.exit(1)
    feed_path = "docs/feed.xml"

    original_texts = parse_srt(srt_path)
    texts = [apply_overrides(i + 1, t) for i, t in enumerate(original_texts)]
    total = len(texts)
    print(f"[regen] {srt_path}: {total} segments -> {mp3_path}")

    workdir = tempfile.mkdtemp(prefix="regen_ep_")
    try:
        durations: list[int] = []
        for attempt in range(1, MAX_LANGUAGE_RETRIES + 1):
            durations = []
            for i, text in enumerate(texts):
                wav_path = os.path.join(workdir, f"part{i + 1}.wav")
                durations.append(
                    synthesize_segment(text, wav_path, i + 1, total, debug)
                )

            # 結合 & MP3 変換
            wav_parts = [
                os.path.join(workdir, f"part{i + 1}.wav") for i in range(total)
            ]
            full_wav = os.path.join(workdir, "full.wav")
            if len(wav_parts) == 1:
                shutil.copyfile(wav_parts[0], full_wav)
            else:
                concat_wav(wav_parts, full_wav)
            with open(full_wav, "rb") as f:
                _convert_wav_to_mp3(f.read(), mp3_path)
            print(f"[regen] MP3 written: {mp3_path}")

            # 言語チェック: 日本語以外のセグメントがあれば再試行
            bad: list[int] = []
            pos = 0
            for i, dur in enumerate(durations):
                lang = check_language(mp3_path, pos, pos + dur)
                print(f"[regen] seg {i + 1} language: {lang}")
                if "japanese" not in lang.lower():
                    bad.append(i + 1)
                pos += dur
            if not bad:
                break
            if attempt == MAX_LANGUAGE_RETRIES:
                raise RuntimeError(
                    f"セグメント {bad} が日本語になりませんでした。"
                    "TTS プロンプト/原稿を確認してください。"
                )
            print(f"[regen] 非日本語セグメント {bad} を再試行 "
                  f"(attempt {attempt + 1}/{MAX_LANGUAGE_RETRIES})")
            for seg_no in bad:
                wav_path = os.path.join(workdir, f"part{seg_no}.wav")
                if os.path.exists(wav_path):
                    os.remove(wav_path)

        # SRT を実測タイムスタンプで上書き（テキストは置換後を記録）
        _write_srt(list(zip(texts, durations)), srt_path)

        # feed.xml の size / duration 更新
        update_feed_entry(feed_path, mp3_path)

        print("=== 再生成完了 ===")
        print(f"  MP3: {mp3_path} ({os.path.getsize(mp3_path)} bytes)")
        print(f"  SRT: {srt_path}")
        print(f"  RSS: {feed_path}")
        print("  ※ 公開には docs/ の変更を GitHub へ push してください")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
