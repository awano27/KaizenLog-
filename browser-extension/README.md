# KaizenLog Browser AI Telemetry

ChatGPT / Claude.ai / Gemini の会話イベントを**ローカル JSONL だけ**に書き出す
Manifest V3 拡張です。ネットワーク送信・全サイト権限はありません。

## インストール（手動・開発者モード）

1. Chrome / Edge で `chrome://extensions`（Edge は `edge://extensions`）を開く
2. 「デベロッパーモード」をオン
3. 「パッケージ化されていない拡張機能を読み込む」→ この `browser-extension/` フォルダを選択
4. オプションページで「本文を保存する」可否を設定  
   - Downloads / ボールトをクラウド同期しているなら **本文オフ推奨**

## 動作確認

| 手順 | 期待 |
| --- | --- |
| chatgpt.com で1往復送る | 拡張の service worker コンソールにエラーが出ない |
| オプション「今すぐエクスポート」 | `Downloads/kaizenlog-browser-ai/YYYY-MM-DD.jsonl` ができる |
| JSONL 1行 | `key`, `ts`, `site`, `conversation_id`, `role`, `char_count`（任意 `text`） |
| **長い応答のストリーミング中〜完了後** | 同一アシスタントメッセージは **JSONL 1件のまま**（途中長の行が増えない） |
| **5分アラームを2回（またはエクスポート2回）** | 当日ファイルが **全量** を含む（2回目で先頭イベントが消えない） |
| claude.ai / gemini.google.com | 同様に記録（セレクタ不一致時は console.warn のみ） |
| KaizenLog | `kaizenlog.toml` の `[aiwork] browser_export_dir` が上記フォルダを指す（既定で Downloads 配下）→ `kaizenlog generate` で 🧠 表に `web` が載る |

### データ構造メモ

- バッファは日付マップ `{ "YYYY-MM-DD": { recordKey: record } }`（当日分はエクスポート後も保持）
- レコードキーは内容非依存（`site|conversation_id|role|messageRef`）。ストリーミングは同一キー上書き

## 権限

- `storage` / `alarms` / `downloads` のみ
- host: `chatgpt.com` / `claude.ai` / `gemini.google.com` のみ

## 制限

- サイト DOM 変更でセレクタが死ぬことがある（警告のみ・ページは壊さない）
- トークン数は取得しない（文字数のみ）
- ツールエラー・中断はブラウザ会話では概念が無い（KaizenLog 表では `-`）
