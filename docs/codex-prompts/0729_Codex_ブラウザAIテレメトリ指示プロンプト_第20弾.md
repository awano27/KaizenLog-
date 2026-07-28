# Codex 実装指示: ブラウザAIテレメトリ(拡張+アダプタ)— 第20弾

対象リポジトリ: `C:\develop\KaizenLog\KaizenLog-`(Python / pytest + ブラウザ拡張 JS)

**前提: 第19弾適用済みのワーキングツリー。** これまでより大きい弾。実装順は §B0 → §B2 → §B3 → §B1 → §B4 を推奨(Python側を先に固め、拡張は最後に合わせる)。

## 背景と方針

ChatGPT(ブラウザ/画面時間トップ)がAI作業の質テーブルに一切乗らない非対称を解消する。方式は**ブラウザ拡張がチャットサイトのDOMだけを読み、ローカルJSONLに書き出し、KaizenLogの既存アダプタ層(TelemetryAdapter Protocol)が読む**。キーロガー・画面OCRは採用しない(全キー入力の記録はパスワード等を含み、ボールト同期時のリスクが設計思想に反する)。

原則: **拡張はネットワーク送信を一切しない**(ローカル書出しのみ)/ 対象ドメインはハードコードの許可リスト / 本文の保存は設定でオフ可能 / トークン数は取得不能なので**捏造しない**(文字数を別フィールドで持ち、トークン集計には混ぜない)。

## §B0 [小・先行修正] Codex側 user_turns のラッパー加算非対称

`src/kaizenlog/aiwork_codex.py:137-139`

第19弾レビュー残: Codex側 `note_user_message` はコマンドラッパー文でも `user_turns` を加算する(Claude側は除外)。共通の判定を通して除外し、両アダプタの往復数の意味を揃える。テスト1本。

## §B1 ブラウザ拡張(Manifest V3)

新ディレクトリ `browser-extension/`(manifest.json / background.js / options.html / sites/*.js)

1. **対象ドメイン(ハードコード)**: `chatgpt.com` / `claude.ai` / `gemini.google.com`。content_scripts はこの3ドメインのみに宣言(全サイト権限を要求しない)
2. **捕捉**: 各サイト用モジュールが MutationObserver で会話DOMを監視し、「ユーザー送信」「アシスタント応答完了」イベントを検出。記録: `{ts, site, conversation_id(URLから), role, char_count, text}`。`text` は既定で保存するが、オプションページの「本文を保存しない(メタデータのみ)」でオフ可
3. **セレクタの脆さへの設計**: サイトごとの selector をモジュール先頭に定数集約し、マッチ0件が続く場合は extension console に警告のみ(ページを壊さない・例外を漏らさない)。この設計理由をコメントに明記
4. **書出し**: chrome.storage.local にバッファし、chrome.alarms(5分毎)+ ブラウザ起動時に、chrome.downloads API で `kaizenlog-browser-ai/YYYY-MM-DD.jsonl` へ**追記相当のエクスポート**(同日ファイルは conflictAction: overwrite で全量書き直し。saveAs: false でダイアログなし)。ダウンロード先はブラウザ既定のDownloadsフォルダ配下
5. **ネットワーク権限なし**: manifest の permissions は downloads / storage / alarms のみ。host_permissions は上記3ドメインのみ
6. JSは素のES(ビルド不要)。自動テストは要求しない代わりに、`browser-extension/README.md` に手動確認手順(読み込み方・各サイトでの動作確認・エクスポート確認)を書く

## §B2 Pythonアダプタ(BrowserAIAdapter)

新規 `src/kaizenlog/aiwork_browser.py`、`src/kaizenlog/config.py`、`src/kaizenlog/aiwork.py`(available_adapters)

1. config: `[aiwork] browser_export_dir`(既定 `~/Downloads/kaizenlog-browser-ai`)。ディレクトリ存在+enabled で available_adapters に参加(既存のClaude/Codexと同じゲート方式)
2. 対象日+前日の `YYYY-MM-DD.jsonl` を読み(深夜跨ぎの既存慣行)、`(site, conversation_id)` 単位で AISession に正規化: `user_turns`=user行数、`title`=最初のuser本文から `extract_session_title`(第19弾の共通ヘルパー再利用、redactは既存のQ1配線に乗る)、start/end=min/max ts。`source`=`chatgpt-web` 等
3. **無いものは無いまま**: tool_calls/tool_errors/中断は概念が無いので0でなく**欠損**として扱い、表では `-` 表示(§B3)。`output_tokens` は設定せず、`assistant_chars` を別フィールドで保持(トークン集計・コスト行に混入させない。この理由をコメントに明記)
4. `scan_user_prompts` も実装 → ブラウザのChatGPT依頼文が既存のリトライ連鎖検出・promptmine(プロンプト資産化)に自動参加
5. 本文なし(メタデータのみ)エクスポートでも title 以外の全機能が動くこと(title は「(本文未保存)」)

## §B3 表示と集計の統合

`src/kaizenlog/aiwork.py`(render)、`src/kaizenlog/stats.py`

1. 質テーブルのセッション行にブラウザ由来を追加(プロジェクト列は `chatgpt (web)` 等)。ツール系の列は `-` 表示(0と欠損の区別。既存の「無い指標は無いと言う」原則)
2. ヘッダ行のセッション数内訳に web を追加(例: `セッション: 11回（claude-code 4 / codex 4 / web 3）`)
3. stats v2 の ai セクションに `web_sessions` / `web_user_turns` / `web_assistant_chars` を追加保存(トークン系キーとは分離)。既存キーのセマンティクスは変更しない
4. 依頼文長の層別観察(第18弾 §Q2)にブラウザ依頼文も参加

## §B4 ドキュメントとプライバシー既定

`README.md`、`docs/USAGE.md`、`browser-extension/README.md`

1. README に「ブラウザAIテレメトリ(オプション)」節: 仕組み(ローカルのみ・3ドメイン限定・ネットワーク送信なし)、インストール手順、**本文保存の既定と注意**(ボールト/Downloadsのクラウド同期環境では「本文を保存しない」推奨)を明記
2. ロードマップの「Cursor/Copilot CLI等の他AIツールログ対応」の隣にブラウザ対応済みの旨を反映

---

## 受け入れ条件

- `python -m pytest -q` 全件通過。アダプタは合成JSONLフィクスチャで単体テスト(§B2 の正規化・欠損扱い・本文なしモード・§B3 の表示)
- 拡張のJSに対する自動テストは不要(手動確認手順のドキュメントで代替)。ただし content script のイベント→レコード変換など純関数部分は可能なら分離しておく

作業完了時: 変更ファイル一覧、§B0〜B4 の対応状況、pytest 結果(要約)を報告すること。

## 禁止事項(毎回共通・厳守)

- **git commit / push / branch 操作を絶対にしない(変更はワーキングツリーに残すだけ。前回は遵守できた — 継続すること)**
- ssh / scp / リモートアクセスをしない
- 拡張からKaizenLog本体まで、いかなるネットワーク送信コードも書かない(拡張の permissions に networking 系を含めない)
- DB スキーマ変更をしない
- テスト・実装から実 LLM を呼び出さない
- Pythonに外部ライブラリを追加しない(標準ライブラリのみ)。拡張もビルドツール・npm依存なしの素のJSのみ
- タスクスケジューラへの登録をテストから実行しない
- バージョン番号の変更・CHANGELOG のリリース節の追加をしない
- マーカー区間外のノート内容・手書きテキストを変更するコードを書かない
