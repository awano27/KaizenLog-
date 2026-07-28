# KaizenLog Browser AI Telemetry

ChatGPT / Claude.ai / Gemini の会話イベントを**ローカル JSONL だけ**に書き出す
Manifest V3 拡張です。ネットワーク送信・全サイト権限はありません。

## インストール(5分・管理者権限不要)

Chrome と Brave の両方で使う場合は、それぞれのブラウザで同じ手順を繰り返します。

1. アドレスバーに拡張機能ページのURLを入力して開く
   - Chrome: `chrome://extensions`
   - Brave: `brave://extensions`
   - Edge: `edge://extensions`
2. 右上(Edgeは左下)の「**デベロッパーモード**」をオンにする
3. 「**パッケージ化されていない拡張機能を読み込む**」ボタンを押し、
   フォルダ選択で `C:\develop\KaizenLog\KaizenLog-\browser-extension` を選ぶ
4. 一覧に「KaizenLog Browser AI Telemetry」が表示されれば読み込み完了
5. (推奨)本文保存の設定を確認する:
   拡張カードの「**詳細**」→「**拡張機能のオプション**」を開く
   - Downloads フォルダや Obsidian ボールトをクラウド同期しているなら
     「本文を保存する」を**オフ**(メタデータのみ)にするのが安全です

## 動作確認(1分)

1. [chatgpt.com](https://chatgpt.com) か [claude.ai](https://claude.ai) を開き、何か1往復だけ会話する
2. 拡張のオプションページで「**今すぐエクスポート**」を押す
3. `ダウンロード\kaizenlog-browser-ai\` フォルダに今日の日付の `.jsonl` ファイルが
   できていれば成功です

あとは放置で構いません(5分ごとに自動エクスポート)。**翌日の朝ノートの
🧠 AI作業の質テーブルに `web` セッションが載り始めます**(夜間の
`kaizenlog generate` が読み込みます)。

### うまくいかないとき

| 症状 | 確認すること |
| --- | --- |
| フォルダ/ファイルができない | 対象3サイトで会話してから「今すぐエクスポート」を押したか。会話イベントが1件もないとファイルは作られません |
| 日誌に `web` が出ない | `kaizenlog.toml` の `[aiwork] browser_export_dir` が上記フォルダと一致しているか(既定は `~/Downloads/kaizenlog-browser-ai`)。`kaizenlog doctor` でも確認できます |
| サイトの見た目が変わった後、記録されない | サイトのDOM変更でセレクタが古くなった可能性(ページは壊しません)。issueとして報告してください |
| `git pull` で拡張を更新した | 拡張機能ページで KaizenLog カードの「🔄 更新」(リロード)を押す |

## 権限(最小構成)

- `storage` / `alarms` / `downloads` のみ — ネットワーク送信の権限はありません
- 対象サイト: `chatgpt.com` / `claude.ai` / `gemini.google.com` のみ

## 制限

- トークン数は取得しない(文字数のみ。KaizenLogのコスト集計には混ぜない)
- ツールエラー・中断はブラウザ会話では概念が無い(KaizenLog 表では `-`)
- ChatGPTデスクトップアプリは対象外(ブラウザで使ってください)

## 開発者向け: 受け入れ確認チェックリスト

| 手順 | 期待 |
| --- | --- |
| chatgpt.com で1往復送る | service worker コンソールにエラーが出ない |
| JSONL 1行 | `key`, `ts`, `site`, `conversation_id`, `role`, `char_count`(任意 `text`) |
| 長い応答のストリーミング中〜完了後 | 同一アシスタントメッセージは JSONL 1件のまま(途中長の行が増えない) |
| 5分アラームを2回(またはエクスポート2回) | 当日ファイルが全量を含む(2回目で先頭イベントが消えない) |
| claude.ai / gemini.google.com | 同様に記録(セレクタ不一致時は console.warn のみ) |

データ構造: バッファは日付マップ `{ "YYYY-MM-DD": { recordKey: record } }`(当日分は
エクスポート後も保持)。レコードキーは内容非依存(`site|conversation_id|role|messageRef`)で、
ストリーミングは同一キー上書き。
