# 戦略改訂: 主対象を GitHub Copilot に変更

日付: 2026-07-18
前提変更: 主対象AIを Claude Code から **GitHub Copilot** へ切り替える（開発者判断）。
基礎資料: [価値向上戦略レビュー（全15案）](2026-07-18-value-strategy-review.md) / [Copilotテレメトリ調査](2026-07-18-copilot-telemetry-research.md)

## 1. 転換の技術的裏付け（調査結論の要約）

| データ源 | 判定 | 内容 |
|---|---|---|
| Copilot CLI `~/.copilot/session-state/*/events.jsonl` | ◎ 主入力 | 発話・応答・ツール実行・エラー・中断・モデル・出力トークン・コード変更量。Claude Code JSONLとほぼ同等。**過去セッションも遡及可能** |
| Copilot CLI / VS Code の OpenTelemetry JSONL | ◎ 併用 | 呼び出しごとの正確なトークン・ツールtrace・agent編集のaccept/reject。ただし事前opt-inで遡及不可 |
| VS Code `chatSessions/*.jsonl` | △ | mutation-logのリプレイが必要。内部形式依存 |
| 個人 Billing API（AI Credits） | △ | 利用量・費用のみ。品質情報なし |
| インライン補完の受入率 | × | 個人向けには存在しない（組織Metrics APIのみ） |

**製品定義**: 「GitHub Copilot全体」ではなく **「Copilot CLI および VS Code agent/chat における AI協働品質」** を観測対象と明示する。インライン補完は対象外と宣言する（欠損を隠さない）。

## 2. 採用7案の Copilot 版マッピング

| 元の案 | Copilot主対象での変更 |
|---|---|
| 引き算① 書き込み経路CLI一本化 | **変更なし**（ベンダー非依存） |
| 引き算② LLMを任意オプションに降格 | バックエンド2系統の中身を反転: **既定=決定的モード、opt-in時の推奨=Copilot CLI（`copilot -p`）**。Claude Code CLIバックエンドは維持するが推奨から外す |
| 引き算③ NON-GOALS凍結 | 「Windows × Obsidian × **GitHub Copilot（CLI + VS Code agent/chat）**」に改訂。既存のClaude Code解析（aiwork.py）は動作維持のみ・新規投資凍結。JetBrains/VS/Xcode・インライン補完・他AI CLIは明示的NON-GOAL |
| ① `kaizenlog status` | **変更なし**（データ源非依存）。AI系チェックのデータ源がevents.jsonlになるだけ |
| ② 60秒ファーストラン | 読む対象を `~/.claude/projects` → `~/.copilot/session-state/**/events.jsonl` に変更。**過去履歴を遡及できるためOTel方式のccusage Copilot対応に対する優位点になる**。初回出力の末尾でOTel有効化を1行案内（段階的強化） |
| ③ Session Autopsy | events.jsonl のエラー分類・abort.reason・往復数で実装可能。予防策の昇格先を `.github/copilot-instructions.md` / AGENTS.md / Copilot CLI custom agent に変更 |
| ④ プラグイン化（配布） | Claude Codeマーケットプレイス → **(a) Copilot CLI plugin/hooks（1〜3人週）＋ (b) `github/awesome-copilot` 掲載（数日）を先行**。VS Code拡張（4〜8人週）は定着確認後の第2弾。旧Copilot Extensions（GitHub Apps）は廃止済みのため不使用 |

## 3. 新たに必要な技術投資（優先順）

1. **`copilot_cli_events` アダプタ**（aiwork.pyのCopilot版）
   - `session.start` のイベントバージョン＋CLIバージョンを保存、未知イベント無視、CLIバージョン別fixture契約テスト
   - 2025年10月にログ形式の全面刷新歴があるため、バージョン追従を前提に設計
2. **完全性メタデータ**: 各指標に `source / collection_mode(passive|otel) / session_closed_cleanly / metric_completeness(complete|partial|unavailable)` を付与。「エラー0件」が「エラーなし」か「ログに無いだけ」かを区別
3. **`copilot_otel` アダプタ**: `COPILOT_OTEL_FILE_EXPORTER_PATH` のJSONLを解析（トークン精密値・agent編集accept/reject）
4. **`kaizenlog setup copilot`**: OTel有効化・hooks導入・`remoteExport` のプライバシー説明を対話的に実施
5. 表示文言の一般化: nippou.py等の「Claude Code」ハードコードをprovider名に置換

## 4. この転換で失うもの / 得るもの

**失うもの**
- Claude Code版の実装済みテレメトリ資産が主役から降りる（廃棄はしない）
- Claude Codeプラグインマーケットプレイスという配布レバー
- promptmine のスキル昇格先だった Claude Code skills 機構（→ copilot-instructions.md / custom agents で代替）

**得るもの**
- **市場規模**: Copilotユーザー基盤は Claude Code より桁違いに大きく、「Copilot協働品質の観測」は競合調査上ほぼ空白（既存OSSはquota/cost中心。ccusage Copilot対応はOTel事前設定が必要で遡及不可）
- **先行者位置**: events.jsonl を意味的に解析して「質」を測るOSSはまだデファクト不在
- **請求照合**: AI Credits個人APIと突き合わせた「費用対品質」分析という独自軸

## 5. 実行順（改訂版）

1. v1.3.1 信頼性ゲート（変更なし）
2. 引き算3つ（NON-GOALS文言をCopilot版に）
3. `copilot_cli_events` アダプタ＋完全性メタデータ（新規・最優先の技術投資）
4. ① status → ② 60秒ファーストラン（Copilot版）→ ③ Session Autopsy
5. 配布: awesome-copilot掲載 → Copilot CLI plugin → （定着後）VS Code拡張

## 6. リスク

- events.jsonl は「公開された有用な形式」だが第三者パーサー向け長期安定APIの保証はない → バージョン別fixture＋解析品質指標（silent skip禁止）が生命線
- Copilot CLIセッションは既定でGitHubへ同期される（`remoteExport`）→ プライバシー説明とローカル化案内を初回導入に組み込む
- GitHub公式が品質分析を内製するリスク → 防御は従来方針と同じ「提案→台帳→実験→昇格のフルループ」と「ベンダー非依存の改善履歴」
