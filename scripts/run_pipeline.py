"""パイプライン統合エントリポイント

@scout → @editor → @voice → @android の順に実行し、
毎日の AI ニュースエピソードを生成して docs/ に保存する。
"""
from __future__ import annotations

# リポジトリルートを sys.path に追加（GitHub Actions での実行対応）
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.android import update_feed
from agents.editor import generate_headline_and_body
from agents.scout import collect, save_seen_urls
from agents.voice import (
    _clean_text_for_tts,
    _sanitize_text_for_tts,
    _tts_input_diagnostics,
    get_audio_duration,
    synthesize,
)


def _format_srt_time(ms: int) -> str:
    h = ms // 3600000
    ms_rem = ms % 3600000
    m = ms_rem // 60000
    s = (ms_rem % 60000) // 1000
    ms_part = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms_part:03d}"


def _write_srt(segments: list[tuple[str, int]], srt_path: str) -> None:
    os.makedirs(os.path.dirname(srt_path), exist_ok=True)
    parts: list[str] = []
    current_ms = 0
    for i, (seg_text, duration_ms) in enumerate(segments):
        start = _format_srt_time(current_ms)
        end = _format_srt_time(current_ms + duration_ms - 1)
        # 連続した短いセグメントなどで duration が 0 の場合を考慮
        if duration_ms == 0:
             end = start
        parts.append(f"{i+1}\n{start} --> {end}\n{seg_text}\n")
        current_ms += duration_ms
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"[voice] Saved SRT {srt_path}")


def run() -> None:
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    date_str = now.strftime("%Y-%m-%d %H:%M:%S")
    timestamp = now.strftime("%Y-%m-%d_%H%M%S")
    test_output_root = os.environ.get("TEST_OUTPUT_PATH")
    if test_output_root:
        feed_path = f"{test_output_root}/feed.xml"
        output_dir = f"{test_output_root}/episodes"
    else:
        feed_path = "docs/feed.xml"
        output_dir = "docs/episodes"
    os.makedirs(output_dir, exist_ok=True)
    mp3_path = f"{output_dir}/{timestamp}.mp3"
    wav_path = f"{output_dir}/{timestamp}.wav"

    # コマンドライン引数で--debugを受け付ける
    debug = getattr(run, "debug", False)

    print(f"=== AI News Podcast Pipeline: {date_str} ===")

    # @scout: ニュース収集
    print("\n--- @scout: 収集中 ---")
    articles = collect()
    if not articles:
        print("[pipeline] 記事が見つかりませんでした。アナウンスを生成します。")
        headline = "本日は新しいニュースがありませんでした。"
        body = "また明日お会いしましょう。"
        full_script = headline + "\n" + body

        # 音声合成
        print("\n--- @voice: 音声合成中 ---")
        synthesize(full_script, mp3_path, debug=debug, output_format="mp3")

        # RSS フィード更新
        print("\n--- @android: RSS 更新中 ---")
        duration_sec = get_audio_duration(mp3_path)
        update_feed(
            date_str=date_str,
            mp3_path=mp3_path,
            script=headline,
            duration_sec=duration_sec,
            feed_path=feed_path,
        )

        print("[pipeline] アナウンスを生成して終了しました。")
        sys.exit(0)

    # @editor: 台本生成（ヘッドラインと本文を分離）
    print("\n--- @editor: 台本生成中 ---")
    headline, body = generate_headline_and_body(articles)
    if not body:
        print("[pipeline] 台本生成に失敗しました。終了します。")
        sys.exit(1)

    full_script = headline + "\n" + body


    # @voice: 音声合成（2分ごとに分割生成＆結合）
    print("\n--- @voice: 音声合成中 ---")
    # 2分=120秒, Gemini TTSは24kHz/16bit/monoなので1秒=48000byte程度
    # 句点・改行で分割し、各セグメントの合計文字数で近似的に2分ごとに分割
    import re

    from agents.voice import _wav_exact_duration_ms
    from agents.voice_concat import concat_wav
    # 分割バッファは300文字ごととする
    max_chars = 300
    # 句点・改行で分割
    segments = [s.strip() for s in re.split(r'(?<=[。！？\!\?\n])', full_script) if s.strip()]
    seg_groups = []
    buf = ""
    for seg in segments:
        if len(buf) + len(seg) > max_chars and buf:
            seg_groups.append(buf.strip())
            buf = seg
        else:
            buf += seg
    if buf:
        seg_groups.append(buf.strip())
    
    wav_parts = []
    srt_segments = []
    for i, seg_text in enumerate(seg_groups):
        part_path = wav_path.replace('.wav', f'_part{i+1}.wav')
        print(f"  [voice] Converting segment {i+1}/{len(seg_groups)} ({len(seg_text)} chars)...")
        
        try:
            # synthesizeでWAV出力
            synthesize(seg_text, part_path, debug=debug, output_format="wav")
            wav_parts.append(part_path)
            
            # 正確なミリ秒を取得してSRT配列に追加
            with open(part_path, "rb") as f:
                b = f.read()
                dur_ms = _wav_exact_duration_ms(b)
                srt_segments.append((seg_text, dur_ms))
        except Exception:  # noqa: BLE001
            clean = _sanitize_text_for_tts(_clean_text_for_tts(seg_text))
            print(f"\n[pipeline] ERROR at segment {i+1}:")
            print(f"Text content: \"{seg_text}\"")
            print(f"[pipeline] TTS input diagnostics: {_tts_input_diagnostics(clean)}")
            raise
        
        # クォータ（RPM）制限を避けるため十分な待機を入れる
        if i < len(seg_groups) - 1:
            time.sleep(1)

    if len(wav_parts) == 1:
        os.rename(wav_parts[0], wav_path)
    else:
        concat_wav(wav_parts, wav_path)
        for p in wav_parts:
            os.remove(p)
    # WAV→MP3変換
    from agents.voice import _convert_wav_to_mp3
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()
    _convert_wav_to_mp3(wav_bytes, mp3_path)
    os.remove(wav_path)

    # @android: RSS フィード更新
    print("\n--- @android: RSS 更新中 ---")
    duration_sec = get_audio_duration(mp3_path)
    srt_path = mp3_path.replace('.mp3', '.srt')
    _write_srt(srt_segments, srt_path)
    # feed.xml出力先を環境変数で切り替え
    update_feed(
        date_str=date_str,
        mp3_path=mp3_path,
        srt_path=srt_path,
        script=headline,
        duration_sec=duration_sec,
        feed_path=feed_path,
    )

    # 使用済み URL を記録（次回以降の重複排除）
    save_seen_urls([a.url for a in articles])

    print("\n=== 完了 ===")
    print(f"  MP3: {mp3_path}")
    print("  RSS: docs/feed.xml")


if __name__ == "__main__":
    debug = "--debug" in sys.argv
    setattr(run, "debug", debug)  # noqa: B010
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
