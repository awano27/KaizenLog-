# Changelog

## 1.3.0 (2026-07-07) — Product Ready

### Claude Code連携（一級機能）
- LLMバックエンドに `claude-code-cli` を追加（`claude -p --output-format json` のヘッドレス実行、JSON非対応CLIへのプレーンテキストフォールバック、未インストール/認証切れの明確なエラー）
- `daily-kaizen` スキルを新規同梱（マーカー区間のみ更新・重複提案禁止・根拠/最小アクション/AI改善の3点構成）
- スキル3種（daily-kaizen / weekly-kaizen / kaizen-autopilot）をpipパッケージに同梱し、`kaizenlog skill install / show / doctor` で安全に配置（既存ファイルは上書きせずdiff案内、`--force`時のみ.bak退避後に上書き）
- プロンプトテンプレート4種を同梱（daily_advisor / weekly_review / ai_work_deep_review / privacy_safe）。`[llm] system_prompt` で差し替え可能

### 改善ループ
- **Kaizen Memory**: 提案アクションに安定ID（`KZN-YYYYMMDD-NNN`）を自動付与し `Kaizen/Memory/suggestions.jsonl` に記録。ノートのチェックボックス（`- [x] KZN-...`）からdoneを検出し、LLMには未完了/完了済みの要約を渡して重複提案を防止
- **Action Ledger**: 改善提案の出力を「今日の改善提案／明日の最小アクション（チェックボックス）／AI作業の改善」の3セクション構成に

### プライバシー
- `[privacy] redact_patterns` でLLM送信前に機密パターンをマスク（日誌本体は原文のまま）。`advise --dry-run` はマスク適用後の送信内容を表示

### その他
- GitHub Actions CI（ubuntu/windows × Python 3.11/3.12）
- テスト86件

## 1.2.0 — 運用強化パック
- `kaizenlog doctor` / `status` / `backfill`、実行ログ、失敗時Windows通知、LLM自動リトライ、`advise --dry-run`、UTF-8出力強制

## 1.1.0 — 日報とプロンプト資産化
- `kaizenlog report`（日報ドラフト、LLM/決定的の2モード、私的コンテンツ除外）
- `kaizenlog prompts`（Claude Codeへの繰り返し依頼のクラスタリング発掘）

## 1.0.0 — 自己実装するカイゼン
- 日次統計の蓄積（`.kaizenlog/stats/`）、`kaizenlog patterns`（時間泥棒/定時ルーチン/AI摩擦の検出）、`/kaizen-autopilot` スキル

## 0.4.0 — Kaizen実験ループ
- 実験ノート（指標・目標・期限）、毎晩の自動計測、Obsidian Basesダッシュボード

## 0.3.0 — 計画vs実績・週次分析
- Today's Focus/Tasksの差分分析、`/weekly-kaizen` スキル

## 0.2.0 — AI Work Telemetry
- Claude CodeセッションログJSONLから「AI作業の質」（往復数・細切れ・エラー・中断）を集計

## 0.1.0 — MVP
- ActivityWatch収集→分類→デイリーノート出力→LLM改善提案（Copilot CLI / OpenAI互換）
