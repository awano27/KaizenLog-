# 確認済み仕様: Claude Code ヘッドレスモード

- `-p` と `--print` は完全に同義。`--output-format json` の最終テキストは **`result`** フィールド（`session_id`, `total_cost_usd` 等のメタデータ付き）
- エラーはstderr＋非ゼロ終了コード。認証切れは "Not logged in" 等の文言
- ツール許可フラグ（--allowedTools等）を渡さなければ、`-p` はツールを実行しない → KaizenLogのバックエンドはフラグなしで安全（LLMにファイルを触らせない設計と整合）
- `claude --version` でインストール検出。`claude -p "/skill-name"` でスキル起動可
- 実装: `advisor._call_claude_code_cli` はJSONパース→失敗時プレーンテキストfallbackで新旧CLI両対応
- 出典: code.claude.com/docs/en/headless.md, cli-reference.md（2026-07確認）
