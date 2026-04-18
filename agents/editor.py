"""@editor: 台本生成エージェント

収集記事リストを受け取り、Gemini でラジオ台本（日本語）を生成する。
podcast_meta.yml のテンプレートに従い、任意のニュースカテゴリに対応。
"""
from __future__ import annotations

import glob
import os
import re
from typing import TYPE_CHECKING

import yaml
from google import genai
from google.genai import types

if TYPE_CHECKING:
    from agents.scout import Article


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
            filename = str(os.path.basename(str(srt_path)))
            date_label = filename.replace('.srt', '')
            past_texts.append(f"[{date_label}]\n" + "\n".join(lines))
        except Exception as e:
            print(f"[editor] Warning: failed to read {srt_path}: {e}")
    count = len(past_texts)
    print(f"[editor] Loaded {count} past SRT(s) for duplicate avoidance")
    return "\n\n".join(past_texts)



def generate_headline_and_body(articles: list["Article"], meta_path: str = "config/podcast_meta.yml") -> tuple[str, str]:
    """
    記事リストからPodcast台本のヘッドラインと本文を別々に生成して返す。
    Returns (headline, body)
    """
    meta = _load_meta(meta_path)
    model_id = meta.get("editor_model", "gemini-3-flash-preview")
    tts_model = meta.get("tts_model","gemini-3.1-flash-tts-preview")
    api_key = os.environ["GEMINI_API_KEY"]

    client = genai.Client(api_key=api_key)

    # メタデータからプロンプトテンプレートを展開
    category = meta.get("category", "Technology")
    short_title = meta.get("short_title", meta.get("title", "ニュース"))
    persona = meta.get("prompt_persona", "あなたは{category}専門のラジオパーソナリティです。").format(
        category=category, short_title=short_title
    )
    greeting = meta.get("prompt_greeting", "おはようございます、{short_title} です。").format(
        category=category, short_title=short_title
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

    prompt = f"""{persona}
以下のニュース記事をもとに、日本語のポッドキャスト台本を生成してください。

【要件】
- **ヘッドライン**: 「{greeting}」から始め、その日のニュースのヘッドラインを1〜2文で手短に紹介してください。
- **本文**: 各記事について、提供された概要をもとに、リスナーが内容を深く理解できるよう、背景情報や重要性を補足しながら、それぞれ300〜400字程度の詳細な解説を加えてください。{tts_model} で読み上げます。一般的な漢字やよく知られた語はそのまま読める前提で、ふりがなは必要最小限にしてください。新しい・珍しい固有名詞、海外企業名や人名、中国語由来の名称など、誤読の可能性が高いものにだけ初出時のみ「漢字（よみ）」形式で付けてください。一般名詞や既知の用語にはふりがなを付けないでください。同じ語に何度もふりがなを繰り返さないでください。重要な記事から順に紹介してください。各記事の解説の冒頭には、ニュースソース名を短く入れてください(例: 「TechCrunchによりますと…」「36Krが報じたところでは…」）。記事から次の記事に移る際には、自然なつなぎの言葉を入れてください。最後にエンディングとして、「本日の{short_title}は以上です。また明日お会いしましょう」で締めくくってください。
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

    response = client.models.generate_content(
        model=model_id,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.7,
            max_output_tokens=8192,
        ),
    )
    response_text = response.text
    print(f"[editor] Raw script from API:\n---\n{response_text}\n---")
    if response_text is None:
        raise RuntimeError(f"Editor API returned no text. Full response: {response}")
    script = response_text.strip()
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
    return headline, body


if __name__ == "__main__":
    from agents.scout import collect
    articles = collect()
    headline, body = generate_headline_and_body(articles)
    print(f"Headline: {headline}")
    print(f"Body: {body}")
