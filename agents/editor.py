"""@editor: 台本生成エージェント

収集記事リストを受け取り、Gemini でラジオ台本（日本語）を生成する。
podcast_meta.yml のテンプレートに従い、任意のニュースカテゴリに対応。
"""
import glob
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone

import yaml
from google import genai
from google.genai import types

from agents.scout import Article

# リトライ対象のHTTPステータスコード
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

# 恒久的エラーとして即時失敗するステータスコード
_PERMANENT_STATUS_CODES = {400, 401, 403, 404}


def _is_retryable_exception(e: Exception) -> bool:
    """例外がリトライ可能か判定する。"""
    status_code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if status_code is not None:
        if status_code in _PERMANENT_STATUS_CODES:
            return False
        if status_code in _RETRYABLE_STATUS_CODES:
            return True
        if 400 <= status_code < 500:
            return False
        if status_code >= 500:
            return True
    exc_name = type(e).__name__
    return any(x in exc_name for x in ("Timeout", "Deadline", "Connection", "Retry"))


def _get_status_code(e: Exception) -> int | None:
    return getattr(e, "code", None) or getattr(e, "status_code", None)


def _load_meta(meta_path: str = "config/podcast_meta.yml") -> dict:
    with open(meta_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_recent_srt(episodes_dir: str = "docs/episodes", max_count: int = 6) -> str:
    """過去の放送SRTファイルからテキスト部分を抽出して返す。"""
    srt_files = sorted(glob.glob(os.path.join(episodes_dir, "*.srt")), reverse=True)
    srt_files = srt_files[:max_count]
    if not srt_files:
        return ""
    past_texts: list[str] = []
    for srt_path in srt_files:
        try:
            with open(srt_path, encoding="utf-8") as f:
                content = f.read()
            # SRT形式からテキスト行のみ抽出（番号行・タイムスタンプ行・空行を除去）
            lines = []
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                if re.match(r'^\d+$', line):
                    continue
                if re.match(r'\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}', line):
                    continue
                lines.append(line)
            filename = os.path.basename(srt_path)
            date_label = filename.replace('.srt', '')
            past_texts.append(f"[{date_label}]\n" + "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            print(f"[editor] Warning: failed to read {srt_path}: {e}")
    count = len(past_texts)
    print(f"[editor] Loaded {count} past SRT(s) for duplicate avoidance")
    return "\n\n".join(past_texts)



def generate_headline_and_body(articles: list[Article], meta_path: str = "config/podcast_meta.yml") -> tuple[str, str]:
    """
    記事リストからPodcast台本のヘッドラインと本文を別々に生成して返す。
    Returns (headline, body)
    """
    meta = _load_meta(meta_path)
    model_id = meta.get("editor_model", "gemini-3-flash-preview")
    tts_model = meta.get("tts_model","gemini-3.1-flash-tts-preview")
    api_key = os.environ["GEMINI_API_KEY"]

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=120_000,  # 120秒（ミリ秒）
        ),
    )

    # メタデータからプロンプトテンプレートを展開
    category = meta.get("category", "Technology")
    short_title = meta.get("short_title", meta.get("title", "ニュース"))
    
    # 当日の日付を JST で取得 (例: 7月27日)
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    date_str = f"{now.month}月{now.day}日"
    
    persona = meta.get("prompt_persona", "あなたは{category}専門のラジオパーソナリティです。").format(
        category=category, short_title=short_title
    )
    greeting = meta.get("prompt_greeting", "おはようございます、{short_title} です。").format(
        category=category, short_title=short_title, date=date_str
    )

    articles_text = "\n\n".join(
        f"【記事{i+1}】\nタイトル: {a.title}\nソース: {a.source}\n概要: {a.summary}"
        for i, a in enumerate(articles)
    )

    # 過去SRTを参照して重複回避
    past_srt_text = _load_recent_srt()
    past_srt_section = ""
    if past_srt_text:
        past_srt_section = f"""\n【過去の放送内容（参考）】\n以下は過去の放送で取り上げた内容です。これらと重複する記事は除外するか、続報がある場合のみ簡潔に触れる程度にしてください。\n{past_srt_text}\n"""

    default_prompt_template = f"""{persona}
以下のニュース記事をもとに、日本語のポッドキャスト台本を生成してください。

【要件】
- **ヘッドライン**: 「{greeting}」から始め、その日のニュースのヘッドラインを1〜2文で手短に紹介してください。
- **本文**: 各記事について、提供された概要をもとに、リスナーが内容を深く理解できるよう、背景情報や重要性を補足しながら、それぞれ300〜400字程度の詳細な解説を加えてください。{tts_model} で読み上げます。一般的な漢字やよく知られた語はそのまま読める前提で、ふりがなは必要最小限にしてください。
- **ふりがな・読み上げルール**:
  - 新しい・珍しい固有名詞、海外企業名や人名など、誤読の可能性が高いものにだけ、「漢字（よみ）」または「英語（よみ）」形式でふりがなを付けてください。
  - 新しい・珍しい固有名詞、海外企業名や人名など、誤読の可能性が高いものにだけ、初出時のみ「漢字（よみ）」または「英語（よみ）」形式でふりがなを付けてください。
  - 一般名詞や既知の用語にはふりがなを付けないでください。
  - 括弧「()」または「（）」は、ふりがな表示以外の用途に使用しないでください。説明・訳語・英語・略語・注釈のために括弧を使わないでください。
  - 括弧内の内容は、ひらがなまたはカタカナだけで表してください。英語・拼音・略語・コロンを含む書き方（例: 「名称（よみ：Reading）」）は絶対に使わないでください。必ず「名称（よみ）」のように、単純な読みだけを入れてください。
  - ラジオであることを考慮し、重複したふりがなや複雑な括弧書きは避け、読みやすい名称一つに統一してください。
- **構成**:
  - 重要な記事から順に紹介してください。
  - 各記事の解説の冒頭には、ニュースソース名を短く入れてください。
  - 記事から次の記事に移る際には、自然なつなぎの言葉を入れてください。
  - 最後にエンディングとして、「本日の{short_title}は以上です。また明日お会いしましょう」で締めくくってください。
- **重複回避**: 過去の放送内容が参考として提供されている場合、すでに取り上げた話題と実質的に同じ内容の記事は省略してください。
- **出力形式**: 以下のフォーマットで出力してください。
ヘッドライン:
（ここにヘッドライン）
本文:
（ここに本文）
{past_srt_section}
【本日の記事】
{articles_text}
"""

    editor_prompt_template = meta.get("editor_prompt_template", default_prompt_template)
    prompt = editor_prompt_template.format(
        persona=persona,
        greeting=greeting,
        tts_model=tts_model,
        short_title=short_title,
        past_srt_section=past_srt_section,
        articles_text=articles_text,
    )

    max_retries = 3
    last_exception: Exception | None = None
    response = None

    for attempt in range(1, max_retries + 1):
        attempt_started = time.perf_counter()
        print(f"[editor] API START attempt={attempt}", flush=True)
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.7,
                    max_output_tokens=8192,
                ),
            )
            elapsed = time.perf_counter() - attempt_started
            print(f"[editor] API END attempt={attempt} elapsed={elapsed:.1f}s", flush=True)

            # finish_reason をログに出力
            if response.candidates:
                finish_reason = response.candidates[0].finish_reason
                print(f"[editor] API finish_reason={finish_reason}", flush=True)

            response_text = response.text
            if response_text is not None:
                break  # 成功

            # response.text が None の場合
            reason = response.candidates[0].finish_reason if response.candidates else "No candidates"
            print(f"[editor] API ERROR attempt={attempt} elapsed={elapsed:.1f}s finish_reason={reason}", flush=True)

        except Exception as e:
            elapsed = time.perf_counter() - attempt_started
            last_exception = e
            status_code = _get_status_code(e)
            status_str = f" status_code={status_code}" if status_code else ""
            print(f"[editor] API ERROR attempt={attempt} elapsed={elapsed:.1f}s{status_str} {type(e).__name__}", flush=True)

            if not _is_retryable_exception(e):
                print("[editor] Non-retryable error, aborting.", flush=True)
                raise

        # リトライ処理
        if attempt < max_retries:
            # 指数バックオフ + jitter
            if attempt == 1:
                wait = 2.0 + random.uniform(0, 1.0)
            else:
                wait = 5.0 + random.uniform(0, 1.0)
            print(f"[editor] retrying attempt={attempt + 1} wait={wait:.1f}s", flush=True)
            time.sleep(wait)

    # 最終結果の確認
    if response is None or response.text is None:
        error_msg = f"Gemini API failed after {max_retries} attempts"
        if last_exception:
            error_msg += f": {last_exception}"
        print(f"[editor] API FAILED attempts={max_retries}", flush=True)
        raise RuntimeError(error_msg)

    print(f"[editor] Raw script from API:\n---\n{response.text}\n---")
    script = response.text.strip()
    # ヘッドラインと本文を抽出
    headline = ""
    body = ""
    if script.startswith("ヘッドライン:"):
        parts = script.split("本文:", 1)
        if len(parts) == 2:
            headline = parts[0].replace("ヘッドライン:", "").strip()
            body = parts[1].strip()
        else:
            headline = script.strip()
    else:
        headline = script[:150].strip()
        body = script.strip()
    print(f"[editor] Headline: {headline[:80]}...")
    print(f"[editor] Body: {len(body)} chars")
    
    # 期待される締め文言が含まれているかチェックし、途切れている場合に警告を出す
    expected_closing = "また明日お会いしましょう"
    if expected_closing not in body:
        print(f"[editor] WARNING: The script seems to be truncated. Expected closing phrase '{expected_closing}' not found.")
        
    return headline, body


if __name__ == "__main__":
    from agents.scout import collect
    articles = collect()
    headline, body = generate_headline_and_body(articles)
    print(f"Headline: {headline}")
    print(f"Body: {body}")