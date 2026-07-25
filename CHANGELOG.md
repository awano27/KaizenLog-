# Changelog

## Unreleased

### Added
- **`kaizenlog setup`**: 対話式セットアップウィザード（ボールト / LLM / ActivityWatch / スキル / 日次タスクを検出優先で構成し、末尾で `doctor` を実行）
- 設定の既定先を AppData/XDG（`%APPDATA%\kaizenlog\config.toml`）に変更。`init-config --output` で任意パスにも出力可能
- `doctor` の ActivityWatch 接続失敗時に `kaizenlog setup` への修復案内。CWD 設定のみ使用時は AppData 移行を警告

## 1.5.0-rc1 (2026-07-20) — Watchers + 介入ループ + 信頼性強化（リリース候補）

### 記録の解像度（オプションwatcher）
- **aw-watcher-web 連携**: ブラウザ時間をタブURL粒度に分割。ドメイン別分類・サイト別集計・`site_minutes:<ドメイン>` 実験指標。ブラウザごとの別バケットを全マージ、WebView2ホストは除外
- **aw-watcher-input 連携**: 集中ブロック（25分以上入力が続いた区間）を検出。`focus_blocks` / `focus_minutes` / `input_keypresses` 実験指標
- `doctor` が両watcherの導入状況を検出。未導入でも従来どおり動作

### 介入ループ（LeechBlock連携）
- `kaizenlog block`: 時間泥棒を検出し [LeechBlock NG](https://github.com/proginosko/LeechBlockNG) のインポートファイルと効果測定実験を生成。検出→介入→計測→判定の閉ループ。適用は人間のブラウザインポート（承認ゲート）。段階的介入（時間帯上限/日次上限）、深夜跨ぎの時間帯も有効な形式に分割、既存セット（1-19）は非破壊

### LLMバックエンドの堅牢化
- 指定バックエンド不可時に Ollama へ自動フォールバック（`fallback_to_local`）
- Windows: npm製CLIの `.CMD` 解決、プロンプトのstdin渡し（コマンドライン長制限・cmd.exe再解釈の回避）、Claude CLIの認証エラー（stdout JSONの401）を即フォールバック
- OpenAI互換API: null content・タイムアウト・非JSON応答をリトライ/フォールバック網に載せる
- cp932コンソールでの絵文字出力クラッシュを修正

### 信頼性（無人夜間実行）
- 実行ログ・実験・統計・ノートの書き込みをアトミック化（クラッシュ/電源断での破損防止）
- 不正UTF-8を含むログ/メモリ/統計ファイルでもクラッシュせず継続
- 設定値のtypo（タイムゾーン・数値・型違い）を明確なエラーで報告し、夜間実行の無音死を防止
- `--dry-run` の書き込み副作用を排除、`backfill`/`skill doctor` の終了コード修正

### 計測精度
- AI作業テレメトリ: 出力トークン・API呼び出し数の2-3倍過大計上を修正（message.id重複排除）、サブエージェント/コンパクション要約の混入除外、深夜跨ぎセッションの誤カウント修正
- パターン検出・プロンプト資産化のUTC/ローカル時刻ずれ、日報の私的時間混入を修正

### テスト
- 累計176件（回帰テスト90件超を新規追加）。修正前後の挙動差を一時worktreeで実証済み

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
