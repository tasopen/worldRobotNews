# worldRobotNews パイプライン安定化 実装仕様書

## 1. 目的

GitHub Actions で実行している `worldRobotNews` の Daily AI Podcast
パイプラインについて、外部APIやネットワークの一時的な無応答によって処理が長時間停止する問題を防止する。

今回確認された事象では、通常の実行時間が約4分台であるのに対し、`Run pipeline`
が5時間以上継続した。

本仕様では、以下の3段階で対策する。

1.  **実行時間ログの追加**
2.  **GitHub Actions のジョブタイムアウト設定**
3.  **Gemini API のタイムアウト・リトライ制御**

基本方針は「まず観測可能にする」「次に暴走を止める」「最後にAPI通信を堅牢化する」である。

------------------------------------------------------------------------

## 2. 対象リポジトリ

-   Repository: `tasopen/worldRobotNews`
-   Workflow: `.github/workflows/daily_podcast.yml`
-   Pipeline: `scripts/run_pipeline.py`
-   Editor: `agents/editor.py`
-   Voice: `agents/voice.py`

------------------------------------------------------------------------

## 3. 現状と問題点

### 3.1 通常時の実行時間

過去の成功実行では、おおむね以下の範囲で安定している。

  Run      所要時間
  ------ ----------
  #148      4分17秒
  #147      4分28秒
  #146      4分05秒
  #145      4分29秒
  #144      4分45秒
  #143      4分18秒

6回平均は **約4分24秒**。

### 3.2 今回の異常

Run #149 では `Run pipeline` が
**5時間以上**継続した後、手動キャンセルされた。

GitHub Actions の状態から、以下は正常終了していた。

-   Checkout
-   Install uv
-   Install ffmpeg
-   Install dependencies

異常箇所は `Run pipeline` に限定されている。

### 3.3 現在の構造上の問題

`agents/editor.py` と `agents/voice.py` では Gemini API の
`generate_content()`
を利用しているが、API呼び出しに明示的なタイムアウトを設定していない。

そのため、外部APIが異常状態になった場合に、Pythonプロセスが長時間待機し続ける可能性がある。

特に `agents/voice.py` は、台本を約300文字単位に分割して複数回TTS
APIを呼び出すため、異常発生時の影響が大きい。

------------------------------------------------------------------------

# 4. 実装方針

## Phase 1: 実行時間ログ

### 目的

`Run pipeline` 内のどの処理で時間を消費しているかを明確にする。

### ログレベル

追加するログは通常のGitHub Actionsログに表示する。

形式は以下を基本とする。

``` text
[timing] START @scout
[timing] END   @scout: 12.4s
```

API呼び出しについてはさらに詳細化する。

``` text
[editor] API START
[editor] API END: 18.7s

[voice] segment 1/8 START (285 chars)
[voice] segment 1/8 END: 21.3s
```

### 時刻計測方法

Python標準ライブラリの `time.perf_counter()` を使用する。

`datetime.now()` は表示用とし、経過時間計測には使用しない。

例:

``` python
started = time.perf_counter()
try:
    ...
finally:
    elapsed = time.perf_counter() - started
    print(f"[timing] elapsed={elapsed:.1f}s")
```

### 共通タイマー

`run_pipeline.py`
にローカルなコンテキストマネージャまたはヘルパー関数を追加する。

推奨仕様:

``` python
from contextlib import contextmanager
import time

@contextmanager
def log_timing(name: str):
    started = time.perf_counter()
    print(f"[timing] START {name}", flush=True)
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        print(f"[timing] END   {name}: {elapsed:.1f}s", flush=True)
```

`flush=True` を指定し、GitHub
Actions上でもログが遅延しにくいようにする。

------------------------------------------------------------------------

# 5. Phase 1 詳細

## 5.1 Pipeline全体

`run_pipeline.py` の `run()` を以下のように計測する。

``` text
[pipeline] START

[timing] START @scout
[timing] END   @scout: XX.Xs

[timing] START @editor
[timing] END   @editor: XX.Xs

[timing] START @voice
...
[timing] END   @voice: XX.Xs

[timing] START @android
[timing] END   @android: XX.Xs

[pipeline] TOTAL: XX.Xs
```

## 5.2 Scout

対象:

``` python
articles = collect()
```

ログ:

``` text
[timing] START @scout
[scout] articles=12
[timing] END   @scout: 8.4s
```

記事数も記録する。

## 5.3 Editor

対象:

``` python
headline, body = generate_headline_and_body(articles)
```

ログ:

``` text
[timing] START @editor
[editor] API START
[editor] API END: 18.2s
[editor] Body: 4217 chars
[timing] END   @editor: 18.5s
```

API時間と処理全体時間を分離して記録する。

## 5.4 Voice

Voiceは最重要計測対象。

各セグメントについて以下を記録する。

``` text
[voice] segment 1/14 START (287 chars)
[voice] segment 1/14 END: 20.4s

[voice] segment 2/14 START (295 chars)
[voice] segment 2/14 END: 18.9s
```

記録項目:

-   segment番号
-   全segment数
-   文字数
-   API処理時間
-   成功/失敗
-   リトライ回数

例:

``` text
[voice] segment 4/14 END: 43.7s retries=1
```

### 5.5 Voice集計

Voice終了時に以下を出す。

``` text
[voice] SUMMARY segments=14 total_api_time=267.4s retries=2
```

## 5.6 Android/RSS

対象:

``` python
update_feed(...)
```

ログ:

``` text
[timing] START @android
[timing] END   @android: 0.8s
```

## 5.7 Pipeline総時間

`run()` の開始から終了までを計測する。

成功時:

``` text
[pipeline] TOTAL: 263.8s
```

失敗時も `finally` で出力する。

``` text
[pipeline] TOTAL: 124.7s status=FAILED
```

------------------------------------------------------------------------

# 6. Phase 2: GitHub Actions Timeout

## 6.1 目的

アプリケーション側で予期しない無限待機が発生しても、GitHub
Actionsジョブを有限時間で終了させる。

## 6.2 設定箇所

`.github/workflows/daily_podcast.yml`

現在:

``` yaml
- name: Run pipeline
  env:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
  run: |
    set -x
    uv run python scripts/run_pipeline.py
```

これに `timeout-minutes` を追加する。

推奨:

``` yaml
- name: Run pipeline
  timeout-minutes: 20
  env:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
  run: |
    set -x
    uv run python scripts/run_pipeline.py
```

## 6.3 Timeout値

推奨値: **20分**

理由:

-   過去6回の通常実行平均: 約4分24秒
-   最大実績: 4分45秒
-   20分は最大実績の約4.2倍
-   通常時の遅延を十分許容できる
-   5時間級のハングを防止できる

20分を超える処理は通常ケースでは異常と判断する。

### 変更可能な値

将来的にTTSの文字量が大きくなった場合は、15～30分の範囲で調整する。

------------------------------------------------------------------------

# 7. Phase 3: Gemini API Timeout / Retry

## 7.1 対象

最低限、以下の2箇所を対象とする。

### Editor

`agents/editor.py`

``` python
client.models.generate_content(...)
```

### Voice

`agents/voice.py`

``` python
client.models.generate_content(...)
```

## 7.2 Client設定の共通化

EditorとVoiceで同じタイムアウト方針を使用する。

推奨設定:

-   API timeout: **120秒**
-   最大試行回数: **3回**
-   リトライ対象: 一時的な通信エラー、429、5xx等
-   400系の恒久的な入力エラーは原則リトライしない
-   タイムアウトもリトライ対象
-   最大リトライ時間がGitHub Actionsの20分を圧迫しないようにする

`google-genai` のインストール済みバージョンに対応する `HttpOptions`
のAPI仕様を確認して実装する。

重要: `uv.lock` に固定されている `google-genai`
のバージョンを確認し、実際のバージョンでサポートされるタイムアウト指定方法を使用すること。

------------------------------------------------------------------------

# 8. Retry仕様

## 8.1 基本

最大3回試行する。

``` text
Attempt 1
   ↓ failure
wait
Attempt 2
   ↓ failure
wait
Attempt 3
   ↓ failure
ERROR
```

現在の `voice.py`
にも3回リトライがあるため、これを置き換えるか整理する。

## 8.2 バックオフ

固定2秒待機ではなく、指数バックオフを使用する。

推奨:

``` text
retry 1 → 2秒
retry 2 → 5秒
retry 3 → 終了
```

必要に応じて小さなrandom jitterを加える。

例:

``` text
2～3秒
5～6秒
```

ただし、リトライ回数3回を超えない。

## 8.3 429

HTTP 429の場合は、可能ならAPIが返す `Retry-After` を尊重する。

ただし、極端に長い待機時間は採用せず、上限を設ける。

推奨上限:

``` text
Retry-After > 60秒 → 最大60秒として扱う
```

## 8.4 5xx

以下はリトライ対象:

-   500
-   502
-   503
-   504

## 8.5 Timeout

APIタイムアウトはリトライ対象。

ログ:

``` text
[voice] segment 3/12 API TIMEOUT after 120s
[voice] retry 1/2 in 2.4s
```

## 8.6 恒久的エラー

原則として以下は即時失敗させる。

-   400 Bad Request
-   無効なモデル名
-   無効なAPIキー
-   明らかな入力形式エラー

ただし、Google GenAI
SDKが返す例外型を確認し、実際のHTTPステータスを判定できる実装にする。

------------------------------------------------------------------------

# 9. Retryログ仕様

API呼び出しごとに以下を出力する。

成功:

``` text
[editor] API START attempt=1
[editor] API END attempt=1 elapsed=18.4s
```

リトライ:

``` text
[voice] segment=5/13 API ERROR attempt=1 elapsed=120.0s
[voice] retrying attempt=2 wait=2.3s
```

最終失敗:

``` text
[voice] segment=5/13 API FAILED attempts=3
```

APIキーなどの秘密情報は絶対にログへ出力しない。

------------------------------------------------------------------------

# 10. Voice固有仕様

## 10.1 セグメント単位でタイムアウト

各TTS API呼び出しに120秒のタイムアウトを設定する。

例えば14セグメントの場合、全体で無制限に待つことはない。

ただし、単純計算では最大時間が大きくなり得るため、GitHub
Actionsの20分タイムアウトを最終的な上限とする。

## 10.2 セグメント成功後の処理

成功したsegmentについては従来通り:

1.  WAV保存
2.  WAV時間取得
3.  `srt_segments` へ追加

を行う。

## 10.3 既存の1秒sleep

現在の:

``` python
time.sleep(1)
```

は維持する。

ただし、APIレート制限対策としてのsleepとRetryバックオフを混同しない。

-   通常segment間隔: 1秒
-   API失敗時: Retry Backoff

------------------------------------------------------------------------

# 11. Editor固有仕様

Editorは現在、1回のGemini API呼び出しで台本を生成している。

以下を記録する。

``` text
[editor] API START
[editor] API END elapsed=XX.Xs
[editor] API finish_reason=STOP
[editor] Body: XXXX chars
```

API失敗時:

``` text
[editor] API ERROR attempt=1 ...
```

最終的に失敗した場合は `RuntimeError` 等で処理を終了し、GitHub
Actionsを失敗させる。

------------------------------------------------------------------------

# 12. エラー処理

## 12.1 Pipeline

現在のトップレベル例外処理は維持する。

``` python
try:
    run()
except KeyboardInterrupt:
    sys.exit(0)
except Exception:
    traceback.print_exc()
    sys.exit(1)
```

ただし、失敗時にも総実行時間がログに残るようにする。

## 12.2 Timeout発生時

API timeoutを握り潰さない。

以下のように最終的には明確なエラーとする。

``` text
RuntimeError:
Gemini API timeout after 120s
model=...
operation=voice
segment=5/13
attempts=3
```

APIキーや完全なプロンプト本文はエラーメッセージに含めない。

------------------------------------------------------------------------

# 13. ログ出力の完成形

正常時の例:

``` text
=== AI News Podcast Pipeline ===

[timing] START @scout
[scout] articles=11
[timing] END   @scout: 9.8s

[timing] START @editor
[editor] API START attempt=1
[editor] API END attempt=1 elapsed=17.4s
[editor] API finish_reason=STOP
[editor] Body: 4218 chars
[timing] END   @editor: 17.8s

[timing] START @voice
[voice] segments=14

[voice] segment 1/14 START chars=286
[voice] segment 1/14 API START attempt=1
[voice] segment 1/14 API END elapsed=18.7s
[voice] segment 1/14 END elapsed=19.0s retries=0

[voice] segment 2/14 START chars=294
[voice] segment 2/14 API START attempt=1
[voice] segment 2/14 API END elapsed=20.1s
[voice] segment 2/14 END elapsed=20.4s retries=0

...

[voice] SUMMARY segments=14 retries=0 api_time=271.3s
[timing] END   @voice: 285.1s

[timing] START @android
[timing] END   @android: 0.9s

[pipeline] TOTAL: 313.8s status=SUCCESS
```

------------------------------------------------------------------------

# 14. 異常時の完成形

例えばTTS APIが応答しない場合:

``` text
[voice] segment 4/14 START chars=291
[voice] segment 4/14 API START attempt=1

... 120 seconds ...

[voice] segment 4/14 API TIMEOUT attempt=1 elapsed=120.0s
[voice] retrying attempt=2 wait=2.4s

[voice] segment 4/14 API START attempt=2

... 120 seconds ...

[voice] segment 4/14 API TIMEOUT attempt=2 elapsed=120.0s
[voice] retrying attempt=3 wait=5.3s

[voice] segment 4/14 API START attempt=3

... failure ...

[voice] segment 4/14 API FAILED attempts=3
[pipeline] TOTAL: 260.7s status=FAILED
```

GitHub Actions側では最悪でも `timeout-minutes: 20` が最終防壁となる。

------------------------------------------------------------------------

# 15. 実装時の注意点

## 15.1 SDKバージョン確認

最初に以下を確認する。

``` bash
uv run python -c "import google.genai; print(google.genai.__version__)"
```

または `uv.lock` / `pyproject.toml` を確認する。

`google-genai`
の実際のバージョンに応じて、タイムアウト設定APIを実装する。

## 15.2 Clientの生成回数

可能なら同一モジュール内で不要なClient再生成を避ける。

ただし、現在の構造を大きく変更する必要はない。

## 15.3 ログに秘密情報を出さない

禁止:

``` text
GEMINI_API_KEY=...
Authorization: Bearer ...
```

また、APIレスポンス全体をログ出力する既存処理がある場合も、秘密情報が含まれないことを確認する。

## 15.4 プロンプト全文

通常ログではプロンプト全文を出さない。

現在のEditorにはRaw scriptの出力があるため、GitHub
Actionsログのサイズと個人情報・外部記事内容の扱いを考慮して、必要に応じて短縮する。

------------------------------------------------------------------------

# 16. テスト計画

## Test 1: 通常実行

期待:

-   4～6分程度
-   全segment成功
-   `pipeline TOTAL` が出力される
-   GitHub Actions成功

## Test 2: Editor API一時エラー

意図的にテスト用の例外を発生させる。

期待:

``` text
attempt=1
retry
attempt=2
success
```

## Test 3: Voice API一時エラー

1segmentだけ一時失敗させる。

期待:

``` text
segment N
attempt=1
retry
attempt=2
success
```

## Test 4: API Timeout

テスト用にタイムアウトを短くした環境で無応答を模擬する。

期待:

-   timeoutログが出る
-   リトライされる
-   最大試行回数を超えない
-   最終的に明確なエラーになる

## Test 5: GitHub Actions timeout

テスト用branchで一時的に:

``` yaml
timeout-minutes: 1
```

として、強制終了されることを確認する。

確認後、本番値を20分に戻す。

------------------------------------------------------------------------

# 17. 変更対象ファイル

  --------------------------------------------------------------------------------------------------------
  ファイル                                変更内容                                 優先度
  --------------------------------------- ---------------------------------------- -----------------------
  `scripts/run_pipeline.py`               Pipeline/各agent/segmentの実行時間ログ   必須

  `.github/workflows/daily_podcast.yml`   `timeout-minutes: 20`                    必須

  `agents/editor.py`                      Gemini API timeout/retry、API時間ログ    必須

  `agents/voice.py`                       Gemini API                               必須
                                          timeout/retry、segment時間ログ           

  `pyproject.toml`                        原則変更不要                             確認

  `uv.lock`                               原則変更不要                             確認
  --------------------------------------------------------------------------------------------------------

------------------------------------------------------------------------

# 18. 実装順序

以下の順序で実装する。

### Step 1

`run_pipeline.py` に実行時間ログを追加。

### Step 2

`daily_podcast.yml` に:

``` yaml
timeout-minutes: 20
```

を追加。

### Step 3

`google-genai` のバージョンを確認。

### Step 4

Gemini Clientのtimeout設定を実装。

### Step 5

Editorのretry処理を共通化。

### Step 6

Voiceのretry処理を整理し、既存の3回リトライと二重リトライにならないようにする。

### Step 7

Voice segmentごとの計測ログを追加。

### Step 8

通常実行を1回実施。

### Step 9

ログから各工程の所要時間を確認。

------------------------------------------------------------------------

# 19. 完了条件

以下をすべて満たしたら実装完了とする。

-   [ ] Pipeline総実行時間がログに出る
-   [ ] Scoutの実行時間がログに出る
-   [ ] Editor全体時間がログに出る
-   [ ] Editor API時間がログに出る
-   [ ] Voice全体時間がログに出る
-   [ ] Voice各segmentの時間がログに出る
-   [ ] Voiceのretry回数がログに出る
-   [ ] Android/RSS処理時間がログに出る
-   [ ] API timeoutが設定されている
-   [ ] API timeout時にretryされる
-   [ ] 429/5xx等の一時エラーが適切にretryされる
-   [ ] 恒久的エラーを無限retryしない
-   [ ] GitHub Actionsの`Run pipeline`に20分のtimeoutが設定されている
-   [ ] APIキー等の秘密情報がログに出ない
-   [ ] 通常実行が成功する
-   [ ] 最終ログに`[pipeline] TOTAL`が出る

------------------------------------------------------------------------

# 20. 推奨する最終アーキテクチャ

``` text
GitHub Actions
│
├─ timeout-minutes: 20
│
└─ uv run python scripts/run_pipeline.py
    │
    ├─ @scout
    │   └─ timing log
    │
    ├─ @editor
    │   ├─ API timeout: 120s
    │   ├─ retry: max 3
    │   └─ timing log
    │
    ├─ @voice
    │   ├─ segment 1
    │   │   ├─ API timeout: 120s
    │   │   └─ retry: max 3
    │   ├─ segment 2
    │   │   ├─ API timeout: 120s
    │   │   └─ retry: max 3
    │   ├─ ...
    │   └─ timing / retry summary
    │
    └─ @android
        └─ timing log
```

この構成にすることで、

**「どこが遅いか分からない」** → Phase 1で解消

**「APIがハングすると5時間待つ」** → Phase 2で最大20分に制限

**「一時的なGemini障害で即失敗する」** → Phase 3で自動リトライ

という3段階の防御になる。

------------------------------------------------------------------------

## 21. 今回の障害に対する期待効果

今回のようなケースを想定すると、

### 現在

``` text
Gemini API
   ↓
応答なし
   ↓
Python待機
   ↓
1時間
   ↓
3時間
   ↓
5時間
   ↓
手動キャンセル
```

### 実装後

``` text
Gemini API
   ↓
応答なし
   ↓
120秒 timeout
   ↓
retry
   ↓
120秒 timeout
   ↓
retry
   ↓
失敗
   ↓
明確なエラー
```

さらに万一、SDK側でタイムアウト制御が効かなかった場合でも、

``` text
GitHub Actions
   ↓
20分
   ↓
強制終了
```

という最後の安全弁が存在する。

これにより、今回のような「原因不明の5時間ハング」は防止できる。
