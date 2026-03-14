"""@voice: 音声合成エージェント

Gemini TTS (gemini-2.5-flash-preview-tts) を使って台本テキストを MP3 に変換する。
出力フォーマット: PCM → WAV → MP3 (wave + ffmpeg subprocess)
pydub は Python 3.13 で audioop が削除されたため使用しない。
"""
from __future__ import annotations

import io
import os
import struct
import subprocess
import tempfile
import time
import wave

import yaml
from google import genai
from google.genai import types


def _load_meta(meta_path: str = "config/podcast_meta.yml") -> dict:
    with open(meta_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _pcm_to_wav_bytes(pcm_data: bytes, channels: int = 1, rate: int = 24000, sample_width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def _wav_duration_sec(wav_bytes: bytes) -> int:
    """WAV バイト列から再生時間（秒）を計算する。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return int(wf.getnframes() / wf.getframerate())


def _wav_exact_duration_ms(wav_bytes: bytes) -> int:
    """WAV バイト列から再生時間（ミリ秒）を正確に計算する。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return int((wf.getnframes() / wf.getframerate()) * 1000)


def _convert_wav_to_mp3(wav_bytes: bytes, output_path: str, bitrate: str = "128k") -> None:
    """WAV バイト列を ffmpeg で MP3 に変換して output_path に保存する。"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_path, "-b:a", bitrate, output_path],
            check=True,
            capture_output=True,
        )
    finally:
        os.unlink(tmp_path)


def synthesize(script: str, output_path: str, meta_path: str = "config/podcast_meta.yml", debug: bool = False, output_format: str = "mp3") -> str:
    """台本テキストを音声合成して音声ファイルに保存する。output_formatでmp3/wav選択可。output_path を返す。debug=True でPCMも保存。"""
    meta = _load_meta(meta_path)
    tts_model = meta.get("tts_model", "gemini-2.5-flash-preview-tts")
    voice_name = meta.get("voice", "Kore")
    title = meta.get("title", "ニュース")
    category = meta.get("category", "Technology")
    short_title = meta.get("short_title", title)
    api_key = os.environ["GEMINI_API_KEY"]

    persona_instruction = meta.get(
        "persona_instruction",
        "指示: {short_title} の明るく情熱的なラジオパーソナリティとして、はつらつと感情を込めて日本語で読み上げてください。指示内容は読み上げず、以下の台本部分のみを音声にしてください。\n\n---\n台本:\n",
    ).format(title=title, category=category, short_title=short_title)
    client = genai.Client(api_key=api_key)

    tts_prompt = f"{persona_instruction}{script}"

    response = None
    last_exception = None
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=tts_model,
                contents=tts_prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    safety_settings=[
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
                            threshold=types.HarmBlockThreshold.BLOCK_NONE,
                        ),
                    ],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice_name,
                            )
                        )
                    ),
                ),
            )
            if response.candidates and response.candidates[0].content:
                break
            
            reason = response.candidates[0].finish_reason if response.candidates else "No candidates"
            print(f"[voice] TTS attempt {attempt+1} failed (Reason: {reason}). Retrying...")
            time.sleep(2)
        except Exception as e:
            last_exception = e
            print(f"[voice] TTS attempt {attempt+1} error: {e}. Retrying...")
            time.sleep(2)

    # PCM バイナリを取得
    # 極めて慎重に階層をチェック
    if (not response or 
        not response.candidates or 
        len(response.candidates) == 0 or 
        not response.candidates[0].content or 
        not response.candidates[0].content.parts or 
        len(response.candidates[0].content.parts) == 0 or
        not response.candidates[0].content.parts[0].inline_data or
        not response.candidates[0].content.parts[0].inline_data.data):
        
        reason = response.candidates[0].finish_reason if (response and response.candidates and len(response.candidates) > 0) else "Unknown"
        print(f"[voice] ERROR: TTS failed to return valid audio data after {max_retries} attempts.")
        if last_exception:
            print(f"[voice] Last exception: {last_exception}")
        print(f"[voice] Script segment: \"{script[:100]}...\"")
        print(f"[voice] Finish reason: {reason}")
        if response:
            print(f"[voice] Full Response: {response}")
            if response.candidates and len(response.candidates) > 0:
                if response.candidates[0].safety_ratings:
                    print(f"[voice] Safety ratings: {response.candidates[0].safety_ratings}")
        
        raise RuntimeError(f"TTS API returned no audio data. Reason: {reason}. Exception: {last_exception}")

    pcm_data = response.candidates[0].content.parts[0].inline_data.data

    if debug:
        pcm_path = output_path + ".pcm"
        with open(pcm_path, "wb") as f:
            f.write(pcm_data)
        print(f"[voice][debug] PCM saved: {pcm_path} ({len(pcm_data)} bytes)")

    # PCM → WAV or MP3
    wav_bytes = _pcm_to_wav_bytes(pcm_data)
    if output_format == "wav":
        with open(output_path, "wb") as f:
            f.write(wav_bytes)
        size_kb = os.path.getsize(output_path) // 1024
        duration_sec = _wav_duration_sec(wav_bytes)
        print(f"[voice] Saved {output_path} ({size_kb} KB, {duration_sec}s)")
        return output_path
    else:
        _convert_wav_to_mp3(wav_bytes, output_path)
        size_kb = os.path.getsize(output_path) // 1024
        duration_sec = _wav_duration_sec(wav_bytes)
        print(f"[voice] Saved {output_path} ({size_kb} KB, {duration_sec}s)")
        return output_path


def get_audio_duration(mp3_path: str) -> int:
    """ffprobe で MP3 ファイルの長さを秒で返す。"""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            mp3_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(float(result.stdout.strip()))


if __name__ == "__main__":
    import sys
    script = sys.argv[1] if len(sys.argv) > 1 else "こんにちは、テストです。"
    debug = "--debug" in sys.argv
    synthesize(script, "docs/episodes/test.mp3", debug=debug)
