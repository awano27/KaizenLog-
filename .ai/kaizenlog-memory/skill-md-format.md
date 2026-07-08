# 確認済み仕様: SKILL.md フォーマット

- 置き場所: `<project>/.claude/skills/<name>/SKILL.md`（個人は ~/.claude/skills/）
- frontmatterは**全フィールド任意**。`description` が自動起動のトリガー判定に使われるため実質必須
- 有用なフィールド: `allowed-tools`（スキル実行中に事前承認するツール）, `disable-model-invocation`（手動専用化）
- コマンド名はディレクトリ名で決まる（frontmatterのnameではない）
- 設計方針: スキル本文は短く保つ（daily-kaizenは入力/出力/禁止事項のみで~60行）。巨大化するなら参照ファイルに分離
- 出典: code.claude.com/docs/en/skills.md（2026-07確認）
