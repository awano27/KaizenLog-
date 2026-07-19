# KaizenLog

**Windowsの毎日の操作を自動記録 → Markdown日誌 → LLMが改善提案（Kaizen）。**

[ActivityWatch](https://activitywatch.net/) が記録したアクティビティを毎晩集計してObsidianのデイリーノートに書き込み、GitHub Copilot CLI やローカルLLM（Ollama）が「明日からできる改善提案」を追記します。**AIツール（Claude / ChatGPT / Copilot / Cursor…）の使い方の改善**に特に重点を置いています。

> Automatic Windows activity journal in Markdown, with LLM-powered daily improvement suggestions. Built on ActivityWatch. Local-first.

## 仕組み

```
ActivityWatch ──REST API──> kaizenlog generate ──> 01 Daily Notes/YYYY-MM-DD.md
（自動記録・常駐）           （分類・集計・Md生成）         │  📊 Activity Log
                                                        ▼
                            kaizenlog advise  <── Copilot CLI / Ollama / GitHub Models
                            （改善提案を追記）      🚀 Kaizen（AIからの改善提案）
```

- **収集はActivityWatchに委譲**: 実績ある収集エンジンをそのまま使い、本ツールは「分類→Md日誌→LLM分析」に集中
- **AI Work Telemetry (v0.2)**: Claude Codeがローカルに残すセッションログ（`~/.claude/projects/**/*.jsonl`）を解析し、**AI作業の"質"**（往復数・細切れセッション・ツールエラー・中断/拒否・トークン量）を日誌に記録。LLMはこの指標を使って「依頼の粒度を上げる」「CLAUDE.mdを整備する」「プランモードを使う」といった具体的な改善提案を行う
- **計画vs実績の差分分析 (v0.3)**: デイリーノートに手書きした「Today's Focus」「Tasks」を自動で拾い、計画と実際の時間配分のズレ・未達の要因をLLMが分析する
- **週次のエージェント深掘り分析 (v0.3)**: 同梱の `/weekly-kaizen` スキル（`src/kaizenlog/skills/weekly-kaizen/`、`kaizenlog skill install` で配置）を使うと、Claude Codeが週1回、7日分のログ・提案の追跡結果・繰り返しパターンを分析して週次レビューノートを自動作成する
- **Kaizen実験ループ (v0.4)**: 改善提案を「言いっぱなし」にせず、**検証可能な実験**として追跡する。`kaizenlog experiment new` で仮説・指標・目標・期限つきの実験ノートを起票すると、毎晩の `generate` が実測値を自動追記し、目標達成を✅/❌で判定。LLMは実行中の実験と実測値を見た上で提案する（重複提案の防止・進捗コメント）。期限切れの実験は週次レビューが採用/棄却を判定し、一覧はObsidian Basesダッシュボード（`templates/Kaizen Experiments.base`）で見られる
- **Claude Code連携が一級機能 (v1.3)**: `daily-kaizen` / `weekly-kaizen` / `kaizen-autopilot` の3スキルをパッケージに同梱し `kaizenlog skill install` で安全に配置（既存ファイルは上書きせずdiff案内）。LLMバックエンドにも `claude-code-cli`（ヘッドレス実行）を追加。プロンプトテンプレート4種（daily_advisor / weekly_review / ai_work_deep_review / privacy_safe）同梱で `[llm] system_prompt` から差し替え可能
- **Kaizen Memory＋Action Ledger (v1.3)**: 提案アクションに安定ID（`KZN-YYYYMMDD-NNN`）を自動付与し `Kaizen/Memory/` に記録。デイリーノートでチェックを付けると完了として追跡され、LLMは過去の提案の記録を見て**同じ提案を繰り返さない**。提案は「今日の改善提案（根拠つき）／明日の最小アクション（チェックボックス）／AI作業の改善」の3セクション構成
- **プライバシーレダクション (v1.3)**: `[privacy] redact_patterns`（正規表現）でLLM送信前に顧客名・案件名等をマスク。**日誌本体は原文のまま**、送信プロンプトだけがマスクされる。`advise --dry-run` でマスク後の送信内容を事前監査できる
- **運用強化パック (v1.2)**: 無人の夜間実行を「静かな故障」から守る。全実行を記録する実行ログ＋`kaizenlog status`、失敗時のWindows通知、環境を一発診断する`kaizenlog doctor`、PCオフ日の日誌を自動で埋める欠損補完（`backfill`＋毎晩の自動キャッチアップ）、LLM一時エラーの自動リトライ、送信内容を事前確認できる`advise --dry-run`（監査用）
- **日報ドラフトの自動生成 (v1.1)**: `kaizenlog report` が活動ログから**提出用の日報下書き**（【本日の業務】【成果・進捗】【明日の予定】【所感】）を生成。LLMで自然な文章に仕上げるモードと、LLM不要の事実ベースモード（`--no-llm`）の2択。エンタメやYouTube等の私的コンテンツは自動で除外。Tasksのチェック状態が成果・明日の予定に反映される
- **プロンプト資産化 (v1.1)**: `kaizenlog prompts` がClaude Codeへの依頼文を解析し、**繰り返している依頼**を類似クラスタリングで発掘（「5回/5日 ai-news: ニュースを要約して…→スキル化を強く推奨」）。`/kaizen-autopilot` はこれを入力に、頻出依頼を実際に `.claude/skills/` のスキルとして生成し、対応表を `04 Resources/Prompt Library.md` に記録する
- **自己実装するカイゼン (v1.0)**: 毎晩の `generate` が機械可読な日次統計（`.kaizenlog/stats/`）を蓄積し、`kaizenlog patterns` が**繰り返しパターン**（毎日の時間泥棒・定時ルーチン・AI作業の慢性的な摩擦）を決定的に検出する。同梱の `/kaizen-autopilot` スキルはその検出結果から自動化コード（スクリプト・CLAUDE.md改善・スキル）を実際に実装し、**ブランチ＋PRまたは提案ノートとして提出**する。勝手に有効化はせず、必ず人間の承認で止まる。実装した自動化には効果測定の実験が自動起票され、カイゼンが複利で回る
- **ローカルファースト**: 活動データはPCから出ない。LLMもOllamaを選べば完全オフライン
- **既存ノートを壊さない**: デイリーノート内のマーカー区間（`<!-- kaizenlog:... -->`）だけを更新。手書きのメモはそのまま残る

## セットアップ

### 1. ActivityWatch のインストール

[公式サイト](https://activitywatch.net/downloads/)からWindows版をインストールして起動しておく（タスクトレイに常駐）。

### 2. KaizenLog のインストール

```powershell
pipx install kaizenlog       # または pip install kaizenlog
kaizenlog init-config        # kaizenlog.toml の雛形を生成
```

生成された `kaizenlog.toml` を編集して `%APPDATA%\kaizenlog\config.toml` に置く（最低限 `vault_dir` を自分のObsidianボールトに変更）。

### 3. LLMバックエンドの設定

**自動フォールバック機構搭載** — 指定されたバックエンドが利用できない場合、自動的にローカル LLM（Ollama）にフォールバック。すべて失敗時のみエラー。

| バックエンド | 向いている人 | 準備 |
| --- | --- | --- |
| **Claude Code CLI** | Claude Pro/Maxサブスク保有者・提案の質最優先 | [Claude Code](https://claude.com/claude-code) をインストール → `claude` で一度ログイン |
| **GitHub Copilot CLI**（デフォルト） | Copilotサブスク保有者 | `npm install -g @github/copilot` → `copilot` で一度ログイン |
| **Ollama**（自動フォールバック先） | 環境がない場合の自動選択肢 / 完全ローカル | `ollama pull qwen3:8b`（16GB RAM推奨、8GBなら `qwen3:4b`） |
| **GitHub Models** | 無料APIで済ませたい人 | `models:read` 権限のPATを発行し環境変数 `KAIZENLOG_API_KEY` に設定 |

**セットアップ不要の最小構成**（最初は Copilot CLI を試すが、なければ Ollama が起動していれば自動利用）:

```toml
# kaizenlog.toml
[llm]
backend = "copilot-cli"  # 見つからなければ自動的に Ollama を試す
```

いずれのバックエンドも**テキスト生成のみ**を行い、ノートへの書き込みは常にKaizenLogがマーカー区間に対して行います（LLMにファイルを直接触らせません）。

### 4. 毎晩の自動実行

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Time "21:30"
```

### 5. Claude Code連携（オプション・推奨）

Claude Codeユーザーは2つの使い方を選べます（併用も可）：

**方式A: スキル運用** — Claude Codeエージェントがボールト全体の文脈を読んで直接分析・追記する：

```powershell
kaizenlog skill install    # 同梱スキル3種を <vault>/.claude/skills/ に配置
claude -p "/daily-kaizen"  # 日次: Activity Logを読み改善提案をマーカー区間に追記
claude -p "/weekly-kaizen" # 週次: 7日分の傾向分析・週次レビュー作成
# 週次の定期実行を登録:
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Weekly -VaultDir "C:\develop\obsidian\2026"
```

`kaizenlog skill install` は既存スキルと差分がある場合は上書きせずdiffを表示します（`--force`で.bak退避後に上書き）。状態確認は `kaizenlog skill doctor`、一覧は `kaizenlog skill show`。

**方式B: バックエンド運用** — `kaizenlog advise` の生成エンジンとしてClaude Codeを使う（書き込みはKaizenLog側）：

```toml
[llm]
backend = "claude-code-cli"
```

方式Aはボールト文脈込みの深い提案、方式Bは決定的で監査しやすい運用（`--dry-run`で送信内容を事前確認可能）が利点です。

## 使い方

```powershell
kaizenlog run                    # 今日のログ収集 + 改善提案（毎晩の定期実行と同じ）
kaizenlog generate --date 2026-07-04   # 指定日のログだけ生成
kaizenlog advise                 # 改善提案だけ再生成

# カイゼン実験: 提案を数値で検証する
kaizenlog experiment new --title "エンタメ30分以内" `
    --metric "category_minutes:エンタメ" --target "<= 30" --days 14 `
    --hypothesis "夕方のYouTubeを昼休みに移せば作業時間の分断が減る"
kaizenlog experiment list        # 実験の一覧と直近の実測値（指標の一覧もここで表示）
```

実験ノートは `03 Areas/Kaizen Experiments/` に作られ、毎晩 `generate` が実測値を追記します：

```markdown
## Measurements（自動計測）
| 日付 | 値 | 目標達成 |
| --- | ---: | :-: |
| 2026-07-06 | 45 | ❌ |
| 2026-07-07 | 25 | ✅ |
```

`templates/Kaizen Experiments.base` をボールトにコピーすると、実行中/判定待ち/全実験のダッシュボードがObsidian Basesで表示されます。

```powershell
# 提出用の日報ドラフト（stdoutに出力。--write でノートにも書き込み）
kaizenlog report --write          # LLMで文章に仕上げる
kaizenlog report --no-llm         # LLM不要・事実ベースの箇条書き（0秒）

# Claude Codeへの繰り返し依頼を発掘（プロンプト資産化）
kaizenlog prompts --days 14

# 繰り返しパターンの検出（3日分以上の蓄積が必要）
kaizenlog patterns --days 14

# 検出結果から自動化を実装させる（Claude Code）
claude -p "/kaizen-autopilot"
# または4週ごとの定期実行を登録
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Autopilot -VaultDir "C:\develop\obsidian\2026"
```

## 出力例

```markdown
## 📊 Activity Log

**合計アクティブ時間**: 6h42m / コンテキストスイッチ: 23回

### カテゴリ別
| カテゴリ | 時間 | 割合 |
| --- | ---: | ---: |
| 開発 | 3h10m | 47% |
| AI作業 | 1h25m | 21% |
...

### 🤖 AI作業の内訳
セッション数: 9回
| ツール | 時間 |
| --- | ---: |
| claude | 52m |
| copilot | 33m |

### 🧠 AI作業の質（Claude Code）
セッション: 5回 / ユーザー発話: 21回（平均 4.2回/セッション、2往復以下の細切れ: 2回）
ツールエラー: 3回 / ユーザー中断・拒否: 1回 / 出力トークン: 48,200

| 時刻 | プロジェクト | 往復 | ツール実行 | エラー | 中断 |
| --- | --- | ---: | ---: | ---: | ---: |
| 09:12-09:58 | ai-news | 8 | 14 | 2 | 0 |

## 🚀 Kaizen（AIからの改善提案）
### 改善提案
1. 14時台にClaudeとVS Codeを9回往復しています。プロンプトを...
### AI活用の次の一手
- 毎朝手動で行っているニュース要約は Copilot CLI のスケジュール実行に...
```

## カスタマイズ

分類ルールは `kaizenlog.toml` に正規表現で追加できます（デフォルトルールより優先）:

```toml
[[categories.rules]]
name = "AI作業"
ai = true
patterns = ["dify", "自社チャットボット"]
```

## オプションのwatcher（入れると解像度が上がる）

| watcher | 追加されるもの | 導入 |
| --- | --- | --- |
| [aw-watcher-web](https://github.com/ActivityWatch/aw-watcher-web) | サイト別集計・ドメイン分類・`site_minutes:<ドメイン>` 実験指標 | ブラウザ拡張をストアから追加（Chrome/Edge/Firefox） |
| [aw-watcher-input](https://github.com/ActivityWatch/aw-watcher-input) | 集中ブロック（25分以上入力が続いた区間）・`focus_blocks` / `focus_minutes` / `input_keypresses` 実験指標 | `pip install git+https://github.com/ActivityWatch/aw-watcher-input.git` → `aw-watcher-input` を常駐起動 |

どちらも導入状況は `kaizenlog doctor` が検出して案内します。未導入でも従来どおり動作します（該当指標・セクションが出ないだけ）。

## ロードマップ

- [x] **v0.2 — AI Work Telemetry**: Claude CodeのJSONLログから「AI作業の質」を集計
- [x] **v0.3 — 意図の差分分析**（Today's Focusと実績の比較）＋週次のClaude Codeエージェント分析（`/weekly-kaizen`スキル）
- [x] **v0.4 — Kaizen実験ループ**（実験ノートの起票・毎晩の自動計測・週次判定・Basesダッシュボード）
- [x] **v1.0 — 自己実装するカイゼン**（日次統計の蓄積・`kaizenlog patterns`・`/kaizen-autopilot`スキル）
- [x] **v1.1 — 日報ドラフト自動生成**（`kaizenlog report`）＋**プロンプト資産化**（`kaizenlog prompts`・スキル自動生成）
- [x] **v1.2 — 運用強化パック**（`doctor` / `status` / `backfill` / 失敗通知 / LLMリトライ / `--dry-run`）
- [x] **v1.3 — Product Ready**（Claude Code一級対応・スキル同梱と`skill install`・Kaizen Memory/Action Ledger・プライバシーレダクション・CI）
- [x] ブラウザ拡張watcher（aw-watcher-web）連携でURL粒度の分析 ＋ 入力watcher（aw-watcher-input）連携で集中ブロック計測
- [ ] Cursor / Copilot CLI などClaude Code以外のAIツールログ対応
- [ ] [screenpipe](https://github.com/mediar-ai/screenpipe) 連携（OCRで画面内容まで分析）

## 開発

```bash
pip install -e ".[dev]"
pytest
```

## ライセンス

MIT（本ツール自体）。ActivityWatchは別プロセスとしてREST API経由で利用しており、同梱していません。
