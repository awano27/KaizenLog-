# KaizenLog 使い方手順書

Windowsの毎日の操作を自動記録し、Obsidianのデイリーノートに日誌として残し、LLMが改善提案（Kaizen）を行うツールのセットアップと日々の使い方です。所要時間は初期セットアップ約10〜30分。

## 最短セットアップ（開発版）

現行は **1.5.0rc1（RC）** です。PyPI 公開パッケージは未確認のため、GitHub から clone して入れます。

リポジトリ: [https://github.com/awano27/KaizenLog-](https://github.com/awano27/KaizenLog-)

```powershell
git clone https://github.com/awano27/KaizenLog-.git
cd KaizenLog-
pipx install .
kaizenlog setup
kaizenlog doctor
kaizenlog run
```

1. `git clone` — 上記リポジトリを取得  
2. `pipx install .` — クローン直下を開発版としてインストール  
3. `kaizenlog setup` — 対話ウィザード（検出優先・不足だけ質問）  
4. `kaizenlog doctor` — 環境診断  
5. `kaizenlog run` — ActivityWatch 起動後に初回実行  

設定の既定先は **`%APPDATA%\kaizenlog\config.toml`**（Windows）です。CWD の `kaizenlog.toml` は移行期間のフォールバックです。設定が無い通常コマンドは終了コード 2 で止まります（診断だけは `kaizenlog doctor`）。

### `kaizenlog setup` フラグ

| フラグ | 意味 |
| --- | --- |
| `--config PATH` | 読み書きする設定パス（省略時は AppData/XDG） |
| `--vault PATH` | Obsidian ボールトのパス |
| `--yes` | 安全な既定提案を確認なしで採用（非対話向け） |
| `--force` | 既に OK のフェーズも再確認 |
| `--skip-aw` | ActivityWatch フェーズをスキップ |
| `--skip-task` | 日次タスク登録をスキップ |
| `--skip-skills` | スキル導入をスキップ |
| `--install-aw` | 非対話でも winget で ActivityWatch 導入を許可 |
| `--register-task` | 非対話でも日次タスク登録を許可 |
| `--time HH:MM` | 日次タスク時刻（既定 `21:30`） |

例（CI / スモーク）:

```powershell
kaizenlog setup --vault "C:\path\to\vault" --yes --skip-aw --skip-task --skip-skills
```

### `kaizenlog init-config`（互換・雛形のみ）

```powershell
kaizenlog init-config                 # 既定: AppData/XDG の config.toml
kaizenlog init-config --output PATH   # 任意パスへ雛形を出力
```

既存ファイルは上書きしません。再構成は `kaizenlog setup` を使ってください。

## 全体像（何が毎日起きるか）

```
[常駐] ActivityWatch がウィンドウ操作を記録
[毎晩21:30] kaizenlog run が自動実行
   ├─ 📊 Activity Log をデイリーノートに書き込み（カテゴリ別時間・タイムライン・AI作業の質）
   ├─ 🧪 実行中の実験に実測値を追記（✅/❌判定）
   ├─ 📈 統計JSONを蓄積（パターン検出の材料）
   └─ 🚀 LLMが改善提案を追記（計画vs実績・AI使い方の改善・Kaizen Memoryで重複提案を回避）
[毎週日曜18:00] claude -p "/weekly-kaizen" → 週次レビュー作成・実験の採用/棄却
[4週ごと] claude -p "/kaizen-autopilot" → 自動化コードをPR/提案ノートとして提出
```

### Claude Code連携の2つの使い方（どちらか、または併用）

KaizenLogはClaude Codeを「LLMバックエンドの1つ」としても「自律的に動くエージェント（スキル）」としても使えます。

| 方式 | 何をするか | 向いている場面 |
| --- | --- | --- |
| **A. バックエンド運用** | `kaizenlog advise` がClaude Code CLIを`-p`（ヘッドレス）で1回呼び出し、テキストを受け取ってKaizenLog側がマーカー区間に書き込む。ファイル操作はClaude Codeにさせない | 他のバックエンド（Copilot CLI / Ollama / GitHub Models）と同列に差し替えたいとき。動作が一番シンプルで予測可能 |
| **B. スキル運用** | `daily-kaizen` / `weekly-kaizen` / `kaizen-autopilot` スキルをボールトにインストールし、`claude -p "/daily-kaizen"` のようにClaude Code自身にログを読ませて提案を書かせる | ボールト全体の文脈（CLAUDE.md・過去ノート・プロジェクトノート）を踏まえた深い提案が欲しいとき。週次・自動化提案は元々この方式 |

どちらもマーカー区間だけを更新し、手書きメモは壊しません。迷ったらAで始めて、物足りなくなったらBのスキルを足すのがおすすめです。

---

## Step 0: 必要なもの

| 必須/任意 | もの | 備考 |
| --- | --- | --- |
| 必須 | Windows PC | 常時ではなく日中使うPCでOK |
| 必須 | Python 3.11以上 | `python --version` で確認 |
| 必須 | [ActivityWatch](https://activitywatch.net/downloads/) | 無料・オープンソース |
| 必須 | LLM手段のいずれか1つ | Claude Code / Copilotサブスク / Ollama / GitHub PAT（Step 4参照） |
| 任意 | Claude Code（CLI） | バックエンドとしても、スキル運用（週次分析・オートパイロット・日次エージェント）としても使用可能 |

## Step 1: ActivityWatch のインストール

1. https://activitywatch.net/downloads/ からWindows版をダウンロードしてインストール
2. 起動する（タスクトレイに常駐します）
3. ブラウザで http://localhost:5600 を開き、ダッシュボードが表示されれば成功
4. **スタートアップ登録を確認**: ActivityWatchの設定で「Start on boot」を有効にする（PCを再起動しても記録が続くように）

> 記録はすべてローカル（あなたのPC内）に保存されます。クラウド送信はありません。

## Step 2: KaizenLog のインストール

```powershell
git clone https://github.com/awano27/KaizenLog-.git
cd KaizenLog-
pipx install .
kaizenlog --help
kaizenlog setup
```

開発用クローンは Obsidian ボールトとは別ディレクトリで構いません（`vault_dir` でボールトを指定）。`pip install -e .` も可です。

## Step 3: 設定ファイル（手動が必要なとき）

通常は `kaizenlog setup` で十分です。雛形だけ欲しい場合:

```powershell
kaizenlog init-config                 # %APPDATA%\kaizenlog\config.toml
kaizenlog init-config --output PATH   # 任意パス
# または環境変数 KAIZENLOG_CONFIG / CLI --config でパス指定
```

最低限確認するのは2箇所:

```toml
[general]
vault_dir = 'C:/develop/obsidian/2026'   # あなたのボールトのパス

[llm]
backend = "copilot-cli"   # Step 4 で選んだものに合わせる
```

## Step 4: LLMバックエンドの設定（4択から1つ）

### A. Claude Code CLI（スキル運用（Step 6）とコマンドを共用できるので推奨）

```powershell
# https://claude.com/claude-code の手順でインストール
claude --version   # バージョンが出ればOK
claude             # 初回はこれで対話起動し /login でログイン
```

設定:
```toml
[llm]
backend = "claude-code-cli"
[llm.claude_code_cli]
command = "claude"
extra_args = []   # 例: ["--model", "claude-sonnet-5"]
```

`kaizenlog advise`は`claude -p "<プロンプト>" --output-format json`をヘッドレスで1回呼び出すだけで、ファイル操作はさせません（`--allowedTools`等は付与しないので、ツール実行の承認プロンプトも出ません）。Step 6でスキル運用（週次レビュー・オートパイロット・日次エージェント）も併用する場合、同じ`claude`コマンドをそのまま使えます。

### B. GitHub Copilot CLI（Copilotサブスクがある人）

```powershell
npm install -g @github/copilot
copilot          # 初回起動でGitHubログイン
```

設定: `backend = "copilot-cli"`

### C. Ollama（GPUなしPCでも完全ローカル）

1. https://ollama.com からインストール
2. `ollama pull qwen3:8b`（RAM 16GB推奨。8GBのPCなら `qwen3:4b`）

設定:
```toml
[llm]
backend = "openai-compatible"
[llm.openai_compatible]
base_url = "http://localhost:11434/v1"
model = "qwen3:8b"
reasoning_effort = "none"  # thinkingを無効化し、改善提案の本文を確実に返す
```

> CPU推論は遅い（応答に数分）ですが夜間バッチなので問題ありません。
> thinkingが必要な場合だけ `low`、`medium`、`high`へ変更してください。

### D. GitHub Models（無料API）

1. GitHub → Settings → Developer Settings → Fine-grained PAT を `models:read` 権限で発行
2. 環境変数に設定: `setx KAIZENLOG_API_KEY "github_pat_..."`

設定:
```toml
[llm]
backend = "openai-compatible"
[llm.openai_compatible]
base_url = "https://models.github.ai/inference"
model = "openai/gpt-4o"
```

## Step 5: 初回実行と確認

ActivityWatchを起動した状態でしばらくPC作業をしてから:

```powershell
kaizenlog generate    # まずログ生成だけ試す
```

`✅ Activity Log を書き込みました: ...` と出たら、Obsidianで今日のデイリーノート（`01 Daily Notes/YYYY-MM-DD.md`）を開いて確認。次に:

```powershell
kaizenlog advise      # LLMの改善提案を追記
kaizenlog run         # 上記2つをまとめて実行（毎晩の定期実行と同じ）
```

> 既にそのノートに手書きメモがあっても消えません。KaizenLogは `<!-- kaizenlog:... -->` マーカーで囲まれた区間だけを更新します。何度実行しても同じ区間が置き換わるだけです。

## Step 6: Claude Codeスキルのインストール（Claude Codeを使うなら）

同梱の3スキル（`daily-kaizen` / `weekly-kaizen` / `kaizen-autopilot`）をボールトの`.claude/skills/`に配置します。

```powershell
kaizenlog skill show                              # 同梱スキルの一覧とdescriptionを確認
kaizenlog skill install                           # config.tomlのvault_dirへインストール
kaizenlog skill doctor                            # インストール状態を確認（未導入/更新あり等）
```

既にボールト側にスキルがあり内容が異なる場合は**上書きせずdiffを案内**します。意図的に上書きしたいときだけ `kaizenlog skill install --force`（既存ファイルは`.bak`にバックアップされます）。別のボールトに入れる場合は `--vault "C:\path\to\vault"` を指定してください。

## Step 7: 自動実行の登録

```powershell
cd C:\path\to\kaizenlog   # Step 2 でクローンしたフォルダ

# 毎晩21:30に日次処理（時刻は -Time で変更可）
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Time "21:30"

# ＋Claude Codeがあるなら: 毎週日曜18時の週次レビューも登録
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Weekly -VaultDir "C:\develop\obsidian\2026"

# ＋4週ごとの自動化提案（オートパイロット）も登録する場合
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Autopilot -VaultDir "C:\develop\obsidian\2026"
```

PCがその時刻にスリープしていた場合は、次回起動時に実行されます。解除はいつでも `-Unregister` で。

---

## 日々の使い方

### 朝（30秒・任意だが強く推奨）

デイリーノートの **Today's Focus** に今日やることを書く。書いておくと夜の提案に「計画と実績」の差分分析が加わります。

```markdown
## Today's Focus
- KaizenLogの分類ルールを調整する
- AI-NEWSのスクレイパー修正
```

### 夜（0秒）

何もしなくてOK。21:30に自動で日誌と改善提案が書き込まれます。翌朝デイリーノートの「🚀 Kaizen」セクションを読むだけ。

### 朝 CLI（追いつき → 📌 → 通知）

```powershell
kaizenlog morning                 # 既定: 昨日の追いつき（AW/LLM・書き込みの場合あり）→再描画→件数トースト
kaizenlog morning --skip-catch-up # 追いつきなし（表示と通知のみ）
```

### 日中: 候補と消化

```powershell
kaizenlog today              # 今日の候補（最大3件）+ 保留件数。既定でノート [x] を Memory 同期
kaizenlog today --no-sync    # 同期せず表示のみ
kaizenlog today --all        # 直近7日 / 8〜30日前 / 31日以上 を全件表示
kaizenlog done KZN-…001      # ターミナルから消化（末尾でも可・一意時）
```

提案の「### 明日試すこと」にはID付きチェックボックスが並びます：

```markdown
- [ ] KZN-20260707-001: 開発開始時に25分タイマーを1回設定する｜PASS: 25分以上の集中ブロックが1回以上｜FAIL: 0回
```

内部のF-IDは保存前に除去されます。PASS/FAILは翌日の判定条件です。実行したら `[x]` にするか `kaizenlog done` するだけ。**完了として記録され（Kaizen Memory）、LLMは同じ提案を繰り返さなくなります**。

判定のタイミング: 夜間21:30の判定は⏳暫定、翌日以降の `generate` 内 `backfill`（`as_of` が測定日より後になる実行）で確定に昇格します。

Kaizen Memoryの実体は `Kaizen/Memory/suggestions.jsonl`（1行1提案、ID・状態・日付）です。設定の`[general] memory_dir`で保存先を変更できます。

### 提案に納得したら実験にする（1コマンド）

提案の末尾に付いてくるコマンド例をそのまま実行:

```powershell
kaizenlog experiment new --title "エンタメ30分以内" `
    --metric "category_minutes:エンタメ" --target "<= 30" --days 14
```

以後、毎晩自動で実測値が✅/❌つきで記録されます。進捗は:
- `kaizenlog experiment list`（コマンド）
- `03 Areas/Kaizen Experiments/Kaizen Experiments.base`（Obsidianダッシュボード）

### 日報を出す会社の人は（10秒）

```powershell
kaizenlog report          # 提出用の日報下書きを生成（コピペして提出）
kaizenlog report --write  # デイリーノートにも残す場合
kaizenlog report --no-llm # LLMを待たず事実ベースの箇条書きで即生成
```

エンタメ・YouTube等の私的コンテンツは自動で除外されます。朝Tasksにチェックボックスを書いておくと、チェック済み→【成果・進捗】、未チェック→【明日の予定】に反映されます。

### 週次（読むだけ）

日曜夜に `01 Daily Notes/Weekly Reviews/` に週次レビューができています。期限切れの実験は採用/棄却が判定済み。手動で今すぐやりたいときはボールトでClaude Codeを開いて `/weekly-kaizen`。

### 今日の分をClaude Codeに深掘りしてほしいとき

`kaizenlog advise`（バックエンド運用）の代わりに、Claude Codeでボールトを開いて `/daily-kaizen` を実行すると、CLAUDE.mdやプロジェクトノートも踏まえた提案を書いてもらえます（Step 6の`skill install`が前提）。書き込み先・マーカー区間・Kaizen Memoryとの連携は同じです。

### 3日以上溜まったら: パターン検出

```powershell
kaizenlog patterns --days 14
```

「毎日9時台にchromeで25分の定型作業」のような繰り返しが検出されます。Claude Codeを使っているなら、繰り返している依頼も発掘できます：

```powershell
kaizenlog prompts --days 14   # PRM-ID 付きで台帳 upsert。「5回/5日: … → スキル化を強く推奨」
kaizenlog prompts --unhandled # status=new のみ（autopilot 入力）
kaizenlog prompts mark PRM-20260729-001 skilled --skill ai-news-summary
kaizenlog prompts mark 001 dismissed
```

台帳は `<vault>/Kaizen/Memory/prompt_clusters.jsonl`（追記型後勝ち）。代表文は `[privacy] redact_patterns` 適用後に保存します。

自動化まで任せるなら `claude -p "/kaizen-autopilot"` — 定型作業のスクリプト化や頻出依頼のスキル化が、PRまたは`00 Inbox/`の提案ノートとして提出され、**あなたが承認するまで何も有効化されません**。

---

## コマンド一覧

| コマンド | 何をするか |
| --- | --- |
| `kaizenlog run` | ログ生成＋改善提案（毎晩の定期実行と同じ） |
| `kaizenlog generate [--date YYYY-MM-DD]` | ログ生成のみ（実験計測・統計蓄積を含む） |
| `kaizenlog advise [--date YYYY-MM-DD]` | 改善提案のみ |
| `kaizenlog morning [--skip-catch-up]` | 朝: 追いつき→📌再描画→通知 |
| `kaizenlog today [--no-sync] [--all]` | 未完了一覧（既定でノートチェック同期） |
| `kaizenlog goal ["目標 @カテゴリ"]` | 今日の作業目標を設定/表示（goal マーカー専用） |
| `kaizenlog done <id>` | アクションを消化 |
| `kaizenlog experiment new --title ... --metric ... --target ...` | 実験の起票 |
| `kaizenlog experiment list` | 実験一覧（効果量付き） |
| `kaizenlog eval record [--date]` | 対象日の advise 入力をケース化（redact 済み） |
| `kaizenlog eval run [--cases DIR] [--repeat N] [--min-pass-rate X]` | 契約合格率の集計（開発者向け） |
| `kaizenlog patterns [--days N]` | 繰り返しパターン検出レポート |
| `kaizenlog report [--no-llm] [--write]` | 提出用の日報ドラフト生成 |
| `kaizenlog prompts [--days N] [--min-count N] [--unhandled]` | 繰り返し依頼の発掘＋台帳 upsert |
| `kaizenlog prompts mark <id> skilled\|dismissed [--skill NAME]` | クラスタをスキル化済み/却下に記録 |
| `kaizenlog skill install [--vault PATH] [--force]` | Claude Codeスキル3種をボールトに配置（既存は上書きせずdiff案内） |
| `kaizenlog skill show` / `skill doctor` | 同梱スキルの一覧／インストール状態の確認 |
| `kaizenlog setup` | 対話式セットアップウィザード（導入の正規経路） |
| `kaizenlog doctor` | 環境の一発診断（AW接続・LLM認証・パス等を✅/⚠️/❌で表示） |
| `kaizenlog status` | 実行履歴の確認（最終成功・直近の失敗理由・partial） |
| `kaizenlog backfill [--days N]` | 欠損日の日誌・統計をまとめて補完 |
| `kaizenlog advise --dry-run` | LLMに送る内容を送信せずに確認（監査用） |
| `kaizenlog init-config [--output PATH]` | 設定ファイルの雛形生成（既定: AppData/XDG） |
| `kaizenlog --config PATH <cmd>` | 明示設定パス（サブコマンドより前） |


### 空転ブレーカー（第31弾）

セッション中のドゥームループを Claude Code フックで遮断する（リアルタイム）。

```powershell
kaizenlog guard install                 # 登録用 JSON を表示のみ
kaizenlog guard install --write --project   # .claude/settings.json に冪等マージ（.bak 作成）
kaizenlog guard status
```

- 登録するのは **UserPromptSubmit** と **Stop** のみ
- **PostToolUse には登録しない**（ツール毎の Python 起動はレイテンシ税が大きく非受容。リトライはユーザー発話、ツールエラー連続は Stop で足りる）
- フックは内部エラーでも **exit 0**（セッションを壊さない）。transcript には書かない
- 発火はトースト + `additionalContext` + `memory/live_episodes.jsonl`（通知履歴。トークン会計は夜間ループ税が正）
- 状態キャッシュ `%LOCALAPPDATA%/kaizenlog/guard/` で増分 tail・デバウンス（既定30秒。`debounce_seconds` は前回完全実行時の値を一段目ゲートに使う＝設定変更は1回遅れで反映）
- リトライ検知の正規化・類似度は**夜間の `detect_retry_chains` と同一**（`promptmine.normalize` + 類似度0.85・窓30分）

### 発掘監査と風化センチネル（第29弾）

| コマンド | 説明 |
| --- | --- |
| `kaizenlog excavate [--days 90] [--write] [--card]` | 過去ログを読み取り専用で走査し、空転税・最悪ループ日を即時表示。stats/日誌は書かない。`--write` で `memory/excavate/` へ冪等レポート、`--card` で SVG |

**風化センチネル**（夜間 `run`/`generate` に自動配線）:

- skilled 済み PRM の再発、採用実験の退行、KZN PASS 後の再悪化を検知
- `kaizenlog status` に「⚠️ 風化した改善: N件（直近7日）」（0件なら非表示）
- 週次コンテキストに「⚠️ 風化した改善」小節（イベント0なら省略）
- advise の確定事実に `[F17]` として列挙（自動再オープンはしない）

### 計測から調教へ（第27弾）

| コマンド | 説明 |
| --- | --- |
| `kaizenlog handoff [--target PATH ...] [--dry-run]` | 実測教訓（リトライ傾向・ツールエラー・連続FAIL・skilled待ちPRM）を CLAUDE.md / AGENTS.md の `kaizenlog:agent-context` 区間へ冪等注入。`--target` 未指定時は config `[handoff] targets`。注入時に `handoff_ledger.jsonl` へ first_injected を記録 |
| `kaizenlog handoff roi [--target PATH] [--suppress ID] [--unsuppress ID] [--promote ID]` | 申し送りROI: レッスン行ごとに概算家賃（tok×sess）と注入前後効果を対照。効果なし×30日経過は「→ 抑制候補」。2+ target で効いている行は「→ 昇格候補」。`--suppress`/`--unsuppress`/`--promote` は明示CLI=承認（自動では CLAUDE.md を変えない）。昇格は config `[handoff] global_target` 必須 |
| `kaizenlog prompts --roi` | プロンプト資産ROIランキング（再発30日・推定tokens・skilled削減は後30日完了後のみ確定） |
| `kaizenlog coach [--dry-run] [--apply FILE]` | 30日実測から CLAUDE.md 追記案を diff 提案（自動適用しない）。`--dry-run` はコンテキストのみ。`--apply` で承認適用（`kaizenlog:coach` 区間）。適用後は `coach_ledger.jsonl` で7日後に機械判定し、FAIL ならロールバック提案を生成（再適用も承認ゲート）。勝率は weekly/status/F18 に表示 |
| `kaizenlog abtest new --predict +30 [--days 28]` | パーソナルMETR実験の開始（予測%） |
| `kaizenlog abtest finish --felt +20` | 体感入力・実測効果量・SVGカード生成・終了日ADVICE区間へ1行。baseline不足時は不成立 |
| `kaizenlog abtest status` | 実験一覧 |

設定例:

```toml
# [handoff]
# targets = ["C:/develop/myrepo/CLAUDE.md"]
# global_target = "C:/Users/you/CLAUDE.md"  # handoff roi --promote の書き込み先

# [aiwork]
# usd_jpy = 150.0
# loop_tax_alert_usd = 1.0
```

ループ税は `generate` / `status` に1行表示されます（最終試行を除くリトライ連鎖の浪費。**エピソード間で同一セッションは1回のみ計上**。部分不明時は tokens不明/金額不明とし部分合計しない。金額不明時は `$-.--` ではなく「金額不明」表記）。

### 計測の注記（第34弾）

- **システム注入 XML**（`<task-notification>`・`<in-app-browser-context>`・`<scheduled-task>` 等）はユーザー発話・リトライ連鎖・guard から除外
- **ツールエラー**: Codex 由来セッションが含まれる日は「（codexは文字列判定・過大計上の可能性）」を付記（構造化フィールド優先は今後）
- **結論の合計時間**: Activity Log と同じ「N時間M分」形式（分の小数表記をやめる）
- **計測範囲（§A3）**: 日誌のAI作業の数値は、セッションログのあるAI CLI / ブラウザ拡張のみが対象です。画面分類でAI時間があっても、対応するwebセッションのないツールは往復・エラー・トークンを計測できません。未知の画面ツールもログなしとして扱います。画面時間だけでAI利用全体の質は判断できません。
- **ループ税の100%超（§B2）**: 通常の実データで100%を超えても成果指標を意味しません。重複・不整合入力への防御的な上限表示で、表示時は「入力不整合のため上限」と注記されます。通常は日次output tokensに対する既知の浪費の割合です。
- **PASS閾値ベースライン（§D2）**: PASSの挑戦性ゲートは当日値ではなく、当日を除く有効な過去3日以上の履歴中央値を使用します。履歴不足・欠損・測定不能は0扱いにせず検査をスキップします。減らす目標は0.95倍、増やす目標は1.05倍です。

## 開発者向け: プロンプト変更後の評価

プロンプト（`prompts/*.md`）や契約検証を変えたら、前後比較のために:

```powershell
kaizenlog eval run --repeat 3             # 初回は同梱サンプル(eval/samples)で動作確認
kaizenlog eval record --date 2026-07-21   # 自分のデータは redact 済みで cases へ
kaizenlog eval run --repeat 3             # ユーザーケースがあればそちらを優先
kaizenlog eval run --cases path\to\cases --min-pass-rate 0.8
```

**初回は同梱サンプル（`eval/samples/`）で動作確認し、自分のデータでは `eval record`。**  
ユーザーケースが無いとき `eval run` は自動でサンプルへフォールバックします。pytest はモックのみで、**実 LLM は `eval run` の手動実行時のみ**呼び出します。

## トラブルシューティング

**まず `kaizenlog doctor` を実行してください。** 接続・認証・パスの問題を✅/⚠️/❌で一発診断します。夜間実行が動いているか不安なときは `kaizenlog status` で最終成功日時と失敗理由を確認できます（失敗時はWindows通知も出ます）。PCを使わなかった日の歯抜けは、翌晩の実行が自動で補完します（手動なら `kaizenlog backfill`）。

| 症状 | 対処 |
| --- | --- |
| `ActivityWatchに接続できません` | タスクトレイにAWがいるか確認。http://localhost:5600 が開くか確認。修復: `kaizenlog setup` |
| `ウィンドウウォッチャーのバケットが見つかりません` | AWを再起動。インストール直後は数分待つ |
| `Claude Code CLI が見つかりません` | https://claude.com/claude-code の手順でインストール後、新しいPowerShellを開き直す |
| `Claude Code CLI がエラーを返しました`＋認証切れの案内 | `claude` を対話起動して `/login` し直す |
| `Copilot CLI が見つかりません` | `npm install -g @github/copilot` 後、新しいPowerShellを開き直す |
| Ollamaでタイムアウト | 初回はモデルロードが遅い。`timeout_seconds` を900に上げる。それでも遅ければ `qwen3:4b` に変更 |
| 提案の質が低い（ローカルLLM） | モデルを大きくするか、backend をCopilot/GitHub Modelsに切り替え |
| 分類がおかしい（仕事のツールがエンタメ扱い等） | `config.toml` の `[[categories.rules]]` にルール追加（デフォルトより優先される） |
| デイリーノートの日誌だけ消したい | ノート内の `<!-- kaizenlog:activity:start -->` 〜 `end -->` を削除（次回また生成される） |
| 「AI作業の質」が出ない | Claude Code / Codex / ブラウザ拡張いずれも未使用の日は出ません。CLI は `claude_projects_dir` / `codex_sessions_dir`、ブラウザは `browser_export_dir` を確認 |
| ブラウザの ChatGPT が表に乗らない | 拡張を読み込み、対象サイトで1往復後にオプション「今すぐエクスポート」。`Downloads/kaizenlog-browser-ai/*.jsonl` と `[aiwork] browser_export_dir` が一致しているか確認 |

### ブラウザ AI テレメトリ（オプション）

ChatGPT・Claude.ai・Gemini をブラウザで使う場合、リポジトリの `browser-extension/` を Chrome/Brave/Edge に読み込むと、会話イベントが
`Downloads/kaizenlog-browser-ai/YYYY-MM-DD.jsonl` にローカル保存されます（**ネットワーク送信なし・3ドメイン限定**）。

```toml
[aiwork]
browser_export_dir = "~/Downloads/kaizenlog-browser-ai"
```

- 詳細・手動確認手順: [browser-extension/README.md](../browser-extension/README.md)
- 本文保存の既定はオン。**Downloads やボールトをクラウド同期している場合は拡張オプションで本文オフを推奨**
- トークン数は取得しません（文字数のみ。コスト行に混ぜません）
- ツールエラー列はブラウザ会話では `-`（欠損）表示

## プライバシーについて

- 活動データ・統計はすべてPC内（ボールト内）に保存。外部送信はLLM呼び出し時のプロンプトのみ
- **レダクション**: `config.toml` の `[privacy] redact_patterns` に正規表現を書くと、LLM送信前に該当箇所が `[REDACTED]` にマスクされます（日誌本体は原文のまま）。`kaizenlog advise --dry-run` でマスク後の送信内容を確認できます。さらに送信を絞るなら `[llm] system_prompt = "privacy_safe"` を併用してください
- セッション「内容」列の依頼抜粋も同じ redact を通します（ブラウザ JSONL に本文がある場合）
- **プロンプト代表文は redact 適用後に** `prompt_clusters.jsonl` 台帳へ保存します（ボールト同期での逐語漏れ防止。日誌原文主義の意図的例外）
- 完全オフラインにしたい場合はOllamaバックエンドを選択
- ウィンドウタイトルには機密が含まれ得ます。ボールトをGitHub同期している場合はプライベートリポジトリにしてください
- 特定アプリを記録から外したい場合はActivityWatch側の設定でも、`min_block_minutes` を上げてタイムラインへの表示を減らすことでも調整できます
