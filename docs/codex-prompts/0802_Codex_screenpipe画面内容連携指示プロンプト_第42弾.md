# 第42弾: screenpipe 画面内容連携（内容層の追加・read-only・fail-closed）

## 依存と適用順

**第41弾（日誌可読性の抜本改善）の実装後に着手すること。** 本弾は第41弾 §B1 の「（ログなし）」AIブロックと §C2 の日報事実化の上に内容層を足す。第41弾が未適用ならこの指示は保留。

## 背景

ActivityWatch はウィンドウタイトルまで、AI CLI アダプタはセッションログのあるツールまでしか見えない。実日誌 2026-08-02 の盲点:
- ChatGPT デスクトップアプリ約25分が「ログなし」（既知の限界#4）
- タイムラインの3分以上ブロックは合計時間の12%しか説明しない
- ブラウジング 1h15m（36%）の中身が不明

screenpipe（https://github.com/screenpipe/screenpipe）は画面を常時ローカル記録し、**accessibility優先+OCRフォールバックの画面テキスト**を REST API で返す。これを **read-only の追加ソース**として組み込み、「ログなし」ブロックに画面テキスト由来の内容を補完する。

**ユーザー前提作業（Codex は行わない・確認もスキップ可）**: screenpipe のインストールと常駐はユーザーが実施する。本弾の実装はサービス不在でも全機能が既存挙動へ完全フォールバックすること（fixtures 駆動で開発・テストする）。

## screenpipe API 契約（openapi.yaml から抽出済み・Codex はネット不要）

- ベース URL: `http://localhost:3030`（既定）
- `GET /health` → `HealthCheckResponse`: `status, status_code, last_frame_timestamp, last_audio_timestamp, frame_status, audio_status, message, version` ほか
- `GET /search` クエリ: `q, limit, offset, content_type, start_time, end_time, app_name, window_name, min_length, max_length, browser_url, focused, max_content_length` ほか
  - `content_type` enum: `all | ocr | audio | input | accessibility | memory`
  - 応答 `SearchResponse`: `{"data": [ContentItem...], "pagination": {"limit","offset","total"}}`
  - `OCRContent`: `frame_id, text, timestamp, file_path, offset_index, app_name, window_name, tags, frame_name, browser_url, focused, device_name`
  - `UiContent`(accessibility): `id, text, timestamp, app_name, window_name, initial_traversal_at, file_path, offset_index, frame_name, browser_url`
  - **注意**: `data[i]` は `{"type": "OCR", "content": {...}}` の入れ子形の可能性がある。入れ子/フラット両対応のパーサにし、§S7 の probe で実形状を確認できるようにする
- `GET /activity-summary` クエリ: `start_time, end_time, app_name` → `{apps: [{name, frame_count, minutes, first_seen, last_seen}], recent_texts: [{text, app_name, timestamp}], audio_summary, total_frames, time_range}`
- `timestamp` は ISO8601（UTC 想定）。**ローカル TZ への変換を必ず行い、テストで固定する**（第32弾で TZ 変換バグの前例あり）

**使用禁止エンドポイント/型（恒久）**: `content_type=input`（キー入力由来）・`content_type=memory`・`/raw_sql`・`/add`・削除/設定系すべて・`include_cloud=true`。使用可能は `GET /health` `GET /search` `GET /activity-summary` の3つのみ（read-only）。

## 設計原則（遵守）

- 画面テキストは**参考層**であり指標ではない。**KZN 提案の PASS/FAIL 指標に screenpipe 由来の値を使うことを禁止**する（第39弾レート契約を汚染しない）
- 無い指標は無いと言う: 画面テキストで往復数・エラー数を推定しない。ラベルは「画面テキスト」と明示
- redact してから外に出す: vault・advisor 入力へ出す前に必ず `title_redactor` を通す
- fail-closed: 未導入・停止中・タイムアウト・不正 JSON のすべてで既存出力に一切影響しない
- localhost 以外への HTTP 禁止

---

## §S1 設定と config

対象: `src/kaizenlog/config.py`（雛形 config.py:311-347 相当箇所にも追記）

```toml
[screenpipe]
enabled = false          # 既定 OFF
base_url = "http://localhost:3030"
timeout_seconds = 3.0
max_lines = 3            # 1ブロックあたり採用する画面テキスト行数
max_excerpt_chars = 120  # 1行あたりの上限
```

- `enabled=false` 時は import 副作用含め一切のネットワークアクセスが発生しないこと。
- base_url は `http://localhost` / `http://127.0.0.1` 始まりのみ許可（それ以外は起動時に警告して disabled 扱い）。

## §S2 クライアントモジュール

新規: `src/kaizenlog/screenpipe_source.py`（stdlib `urllib` のみ・新規依存禁止）

- `ScreenText(ts_local, app_name, window_name, text, browser_url)` dataclass。
- `class ScreenpipeClient`: `health() -> dict | None`、`search_text(app_name, start_local, end_local, min_length=8, limit=50) -> list[ScreenText]`。
  - `content_type=accessibility` を先に照会し、0件なら `ocr` にフォールバック。
  - `start_time`/`end_time` はローカル時刻→UTC ISO8601 に変換して送る。応答 timestamp は UTC→ローカルへ変換。
  - タイムアウト・接続拒否・HTTP エラー・不正 JSON はすべて空リストを返し、**1回の generate 実行につき警告ログは最大1行**（ブロックごとに繰り返さない）。リトライなし。
  - `max_content_length` を指定して応答サイズを抑制する。

## §S3 画面テキスト要約（決定論）

同モジュール内 `summarize_screen_texts(items, max_lines, max_chars) -> list[str]`:

1. `text` を行分割し正規化（連続空白圧縮・前後trim）
2. 6文字未満の行・数字/記号のみの行を除外
3. UI 定型句の除外リスト（例: "New chat", "Copy", "送信", "設定", "ファイル 編集 表示" 等。モジュール定数 `UI_CHROME_STOPLIST` として保持し、テストで固定）
4. 残行を出現頻度×長さでランク付けし、重複（完全一致・包含）を除去して上位 `max_lines` 行
5. 各行 `max_chars` で切詰め
- 純関数・決定論・ネットワーク非依存で単体テスト可能にする。

## §S4 タイムライン「（ログなし）」補完

対象: 第41弾 §B1 の突合結果を組み立てる箇所（cli.py の generate 経路）

- `screenpipe.enabled` かつ AI ブロック（`Block.ai == True`）がセッション未突合（第41弾で「（ログなし）」になるケース）のとき: `search_text(app_name=正規化したアプリ名, start, end)` → 要約1行目を採用し、内容列を `（画面テキスト: {要約}）` にする。
  - アプリ名正規化: `ChatGPT.exe` → `ChatGPT` のように `.exe` を外し大文字小文字非依存で照合する小関数（テスト付き）。
  - 取得0件なら従来どおり `（ログなし）` のまま。
- **クエリを発行してよいのは ai=True ブロックのみ**。私的・エンタメ・その他カテゴリのブロックに対して screenpipe を照会してはならない（テストで担保）。
- 採用テキストは内容列へ入れる前に redact（第41弾のセッション label と同じ扱い）。

## §S5 AI作業の質・日報への補完（小）

- 「🧠 AI作業の質」: 計測範囲注記の直後に、enabled かつ補完があった場合のみ小節を追加:
  `🖥 ログなしAI画面の内容（screenpipe・参考）` — `- {HH:MM}-{HH:MM} {アプリ}: {要約}`（最大3行・redact済み）。往復・エラー・トークンは不明のままにする（推定禁止）。
- 「📝 日報ドラフト」【本日の業務】: セッション digest の無い AI 画面ブロック（例 ChatGPT デスクトップ）が合計10分以上あるとき、`- {アプリ}: 「{要約}」（画面テキストより・約{分}分）` を1行追加（SHOULD）。
- `stats` JSON へ `stats["screenpipe"] = {"queried_blocks": n, "filled_blocks": m}` を保存（数のみ・テキストは保存しない）。

## §S6 advisor 証拠への追加（参考層として）

対象: `src/kaizenlog/advice_evidence.py`、`src/kaizenlog/prompts/daily_advisor.md`

- enabled かつ補完があった日のみ、advisor 入力に節を追加:
  `## screenpipe画面観測（参考・推定）` — §S5 と同じ最大3行（redact済み・合計600字上限）。
- `daily_advisor.md` に1行追記: 「screenpipe画面観測は参考情報であり、提案の PASS/FAIL 指標に使ってはならない（指標は既存の計測値のみ）」。
- 入口ガード（第17/32弾系の PASS 構文検証）に「screenpipe」由来メトリック名が現れたら reject する必要は**ない**（そもそもメトリックとして存在させない）。advisor プロンプト側の禁止文言のみでよい。

## §S7 doctor と probe（実挙動確認の材料）

- `kaizenlog doctor` に項目追加: `screenpipe: disabled` / `OK（version・最終フレーム: N分前）` / `unreachable（enabled だが応答なし）`。unreachable でも他項目・終了コードに影響しない。
- 新サブコマンド `kaizenlog screenpipe-probe [--minutes 30] [--app NAME]`: health の要約1行 + 直近 N 分の `search_text` サンプル最大3件（redact済み・各120字）を stdout に出す。日誌・stats には一切書かない。レビュー時の実挙動確認に使う。

## §S8 テスト

新規 `tests/test_round42_screenpipe.py`（最低12ケース・ネットワークは全て monkeypatch/フェイク）:

1. enabled=false（既定）で HTTP 層が一切呼ばれない（urlopen をモックし call 0 を assert）
2. 接続拒否 / タイムアウト / 不正 JSON → 空結果・既存出力不変・警告1行のみ
3. `data[i]` 入れ子形（`{"type":"OCR","content":{...}}`）とフラット形の両方をパースできる
4. UTC timestamp → ローカル変換（JST 固定 fixture で時刻ズレなし）
5. accessibility 0件 → ocr フォールバック
6. summarize: UI 定型句除外・短行除外・重複除去・max_lines/max_chars・決定論（同入力同出力）
7. ai=True 以外のブロックでクエリが発行されない
8. 「（ログなし）」ブロックが `（画面テキスト: …）` に置換される / 0件なら `（ログなし）` のまま
9. redact 適用（秘匿パターンを含む fixture テキストがマスクされる）
10. `content_type=input` / `include_cloud` がリクエストに決して現れない（URL を capture して assert）
11. base_url が localhost 以外 → disabled 扱い+警告
12. doctor 3状態の表示

fixtures: `tests/fixtures/screenpipe_search_accessibility.json` / `_ocr.json` / `_health.json`（上記契約どおりの形で作成）。

既存テストの変更は不要のはず（enabled=false 既定のため）。既存テストを変更する実装になった場合は設計を見直すこと。

## 受け入れ条件（全体）

- 既定（enabled=false）で全既存テスト・全出力が完全不変
- enabled=true かつサービス不在でも出力不変（フォールバック文言含め第41弾と同一）
- 新規テスト12+ 全パス、`./.venv/Scripts/python.exe -m pytest -q` が基準線（第41弾適用後の passed 数）から減少しない
- 日誌・advisor 入力へ出る screenpipe 由来文字列はすべて redact 済み・「画面テキスト」「参考」ラベル付き
- KZN 提案の PASS/FAIL 指標に screenpipe 由来値が使われない

## 検証コマンド（完了報告に出力を添付）

```
./.venv/Scripts/python.exe -m pytest -q
```

screenpipe が起動している環境でのみ（起動していなければスキップと明記）:

```
./.venv/Scripts/python.exe -m kaizenlog.cli --config kaizenlog.toml screenpipe-probe --minutes 30
```

## 禁止事項（毎回同じ・厳守）

- **git commit / push 禁止**
- ssh / scp / リモートアクセス禁止（localhost:3030 への GET のみ許可）
- DB・台帳スキーマ変更禁止
- 実 LLM 呼び出し禁止（テストはフェイクのみ）
- 実 vault（`C:/develop/obsidian`）への書き込み禁止（検証は一時ディレクトリ）
- screenpipe 本体のインストール・起動・設定変更・データ削除の実行禁止（ユーザー作業）
- マーカー区間外の編集禁止

## 完了報告様式

1. §S1〜§S8 の実装状況表（完了/未完了 + 主要変更 file:line）
2. `pytest -q` の結果（passed 数）
3. probe 実行結果（screenpipe 稼働時のみ・なければ「サービス未稼働のためスキップ」と記載）
4. 変更ファイル一覧
