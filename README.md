# worldRobotNews — 多言語 RSS Podcast Generator

複数の言語からなる最新のニュース（AI、技術、自動車など）を RSS から自動収集・日本語で要約・音声化して、GitHub Pages 上で Podcast として配信するシステムです。RSS と Gemini Grounding Search を活用した自動メンテナンス機能を備えています。

サンプルとしてロボット関連のニュースを集めた Podcast、[朝刊ワールドロボットニュース](https://tasopen.github.io/worldRobotNews/index.html) を公開しています：

## 🏗 アーキテクチャ

```
毎朝 6:00 JST (GitHub Actions)
    │
    ├─ @scout    agents/scout.py   → 多言語 RSS (日・中・英) → 上位 7 記事
    ├─ @editor   agents/editor.py  → Gemini 3 Flash → カテゴリ別・動的台本生成
    ├─ @voice    agents/voice.py   → Gemini TTS(gemini-2.5-flash-preview-tts) → MP3 & SRT (字幕)
    └─ @android  agents/android.py → Podcast RSS (feed.xml) 更新
                       │
毎週日曜 12:00 JST
    └─ @maintenance scripts/maintain_feeds.py → Gemini Grounding Search
                                             → 生存確認・URL復旧・新規発見
```

## ⚙️ 独自の内容に変える手順

このテンプレートは、設定ファイルを書き換えるだけで別ジャンルの Podcast に流用できます。新しいテーマで使うときは、以下の順で変更すると手戻りが少なくなります。

1. **配信名と公開URLを決める**
   - GitHub リポジトリ名を変える場合は、この段階で変更
   - `config/podcast_meta.yml` の `base_url` を `https://<GitHubユーザー名>.github.io/<リポジトリ名>` に更新
   - `scripts/publish_to_github.py` は現在の Git 情報から owner/repo を自動判定するので、通常は追加修正不要
2. **番組メタ情報を変える**
   - `config/podcast_meta.yml` の `title`, `short_title`, `description`, `author`, `category` を更新
   - Apple Podcasts / Spotify 向けに `name` と `email` を設定すると、RSS に `itunes:owner` と `atom:link rel="self"` が入る
   - `prompt_persona`, `prompt_greeting` を新テーマ向けの語り口に合わせる
   - 配信先URLが変わったら、既存の `docs/feed.xml` と `docs/index.html` は再生成される前提で扱う
3. **ニュースソースを入れ替える**
   - `config/sources.yml` の `keywords` を対象テーマ向けに変更
   - `rss_feeds` を収集したい媒体に入れ替える
4. **見た目を差し替える**
   - `docs/podcast_cover.jpg` を新しいカバー画像に差し替える
   - 推奨サイズは 1400x1400px、500KB 以下
5. **動作確認する**
   - `uv run python scripts/run_pipeline.py` を実行
   - 生成された `docs/feed.xml` と `docs/index.html` を確認
   - 問題なければ GitHub Pages と Podcast アプリで購読テスト

補足:
- `pyproject.toml` の `name` は Python プロジェクト名です。リポジトリ名と一致していなくても動作はしますが、配布名や環境表示も揃えたい場合は変更してください。
- `pyproject.toml` の `name` を変えた場合は `uv sync` または `uv lock` をやり直すのが安全です。

## 🔑 必要な Secrets

GitHub リポジトリの Settings → Secrets and variables → Actions に登録：

| 名前 | 取得元 | 役割 |
|------|--------|------|
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) | 台本生成, 音声合成, Grounding Search |

## ⚙️ セットアップ

1. このリポジトリを fork / clone
2. `config/podcast_meta.yml` の `base_url` を自分の GitHub Pages URL に変更
3. GitHub Secrets (`GEMINI_API_KEY`) を設定
4. リポジトリの Settings → Pages → Source を `docs/` フォルダに設定
5. Actions タブから `daily_podcast.yml` を手動実行してテスト

## 🛠 フィード自動メンテナンス

`scripts/maintain_feeds.py` は以下の機能を提供します：
- **生存確認**: 応答がないフィードを検知（3回連続で自動削除）
- **自動修復**: URL が変わった場合、Gemini Grounding Search で新しいアドレスを検索・更新
- **自動発見**: 人気の RSS フィードを Grounding Search で定期的に検索し、リストに追加

## 📱 アプリでの購読

Podcast アプリ（AntennaPod, Apple Podcasts 等）を開き、以下の URL を登録：
```
https://<あなたのユーザー名>.github.io/<あなたのリポジトリ名>/feed.xml
```

## 📦 依存関係・環境構築

[uv](https://docs.astral.sh/uv/) を使用します。

```bash
# 依存パッケージのインストール
uv sync

# パイプラインの手動実行
uv run python scripts/run_pipeline.py

# フィードメンテナンスの手動実行
uv run python scripts/maintain_feeds.py --auto-add
```

## 🛠 開発とデバッグ

### デバッグモードの利用
パイプライン実行時に `--debug` フラグを付けることで、デバッグ情報が出力され、中間データが保存されます。

```bash
uv run python scripts/run_pipeline.py --debug
```

- **PCMデータの保存**: `docs/episodes/` 内に、TTSから返された生の音声データ (`.pcm`) が保存されます。音声品質の確認に利用できます。
- **詳細なエラーログ**: APIレスポンスの詳細（finish_reason や安全しきい値の状態など）が出力されます。

### 便利な環境変数
ローカルでの開発・テスト時に以下の環境変数を設定することで、動作をカスタマイズできます。

- `GEMINI_API_KEY`: 必須。
- `TEST_OUTPUT_PATH`: 出力先（通常は `docs/`）を一時的に別の場所に変更したい場合に使用します。

> ffmpeg が必要です。GitHub Actions には標準搭載されています。
