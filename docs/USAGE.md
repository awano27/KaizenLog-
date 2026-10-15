# KaizenLog 使い方手順書

Windowsの毎日の操作を自動記録し、Obsidianのデイリーノートに日誌として残し、LLMが改善提案（Kaizen）を行うツールのセットアップと日々の使い方です。所要時間は初期セットアップ約10〜30分。

## 最初に知っておくこと

KaizenLog は、次の順で小さく始められます。

1. Python とこのプロジェクトの依存ライブラリを入れる（ソース ZIP / Git clone の導入では Python が必要です）。
2. ActivityWatch を別途インストールして起動し、`aw-watcher-window` が対象日の記録を作る。
3. 保存先のフォルダを `setup` で明示する。
4. 日誌をプレビューして内容と保存先を確認し、本人が生成操作を選ぶ。
5. 必要になったときだけ、日報、本人入力、改善提案、AI 利用ログの分析を追加する。

日誌と `report --no-llm` は LLM なしで動かせます。LLM を設定していても、日誌専用の操作と `report --no-llm` は LLM の探索・送信・fallback を行わない契約です。ActivityWatch が停止中、記録がない、または保存先へ書けない場合は、空の日誌を成功扱いにせず、理由を表示します。

初回画面は、サンプル表示、保存先と対象日・翌日の更新予定、明示的な「日誌を作る」、生成済みノートを開く操作を分けます。閲覧やプレビューだけではノート、台帳、統計、設定を作成・更新しません。`home`、日誌専用生成、`setup --generate-today`、`prompts draft <PRM-ID>` は、下記の明示的な入口から実行します。

本人の成果1行、次に試す1件、表示用のカテゴリ・プロジェクト訂正は、自動計測の原本とは別の本人入力として保存します。日誌へまだ反映できないときは `本人入力は保存済み／日誌反映は未完了` として pending/conflict を残し、推測で別ブロックへ移しません。依頼文の草案は `UNVERIFIED_DRAFT / 効果未検証・編集用` と明記し、既存の採用台帳や `.claude/commands` を自動変更しません。

## 最短セットアップ（開発版）

現行は **1.5.0rc1（RC）** です。PyPI 公開パッケージは未確認のため、GitHub から clone して入れます。

リポジトリ: [https://github.com/awano27/KaizenLog-](https://github.com/awano27/KaizenLog-)

```powershell
git clone https://github.com/awano27/KaizenLog-.git
cd KaizenLog-
pipx install .
kaizenlog setup --vault "C:\path\to\vault" --yes --skip-aw --skip-task --skip-skills
kaizenlog doctor
kaizenlog generate --date 2026-09-06 --skip-catch-up
```

1. `git clone` — 上記リポジトリを取得  
2. `pipx install .` — クローン直下を開発版としてインストール  
3. `kaizenlog setup` — 対話ウィザード（検出優先・不足だけ質問）  
4. `kaizenlog doctor` — 環境診断  
5. `kaizenlog generate --date 2026-09-06` — 日付を今日に置き換え、ActivityWatch 起動後に初回生成（LLM不要）

setup は Obsidian のボールト一覧とデイリーノート保存フォルダを読み取り、候補を確認できます。新規導入では LLM なしから始められます。引数なしの `setup --yes` は診断だけで、設定・日誌・ActivityWatch・タスク・スキルを作りません。`--vault PATH --yes` を明示すると設定だけを保存します。初回日誌を非対話で作るときは、`--generate-today` を追加した操作だけが typed journal 経路を実行します。デイリーノートの既定ファイル名は引き続き `YYYY-MM-DD.md` です。

このソースから作成した配布 wheel にはタスク登録スクリプトを同梱しています。wheel を入手した場合は `pipx install <wheelのパス>` で clone なしの導入が可能です。PyPI 公開は別の承認・検証工程であり、この変更だけでは PyPI からの導入を保証しません。

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
[毎週日曜18:00] claude -p "/weekly-kaizen" → 週次レビュー作成・実験の採否推奨
[4週ごと] claude -p "/kaizen-autopilot" → 自動化コードをPR/提案ノートとして提出
```

### Claude Code連携の2つの使い方（どちらか、または併用）

KaizenLogはClaude Codeを「LLMバックエンドの1つ」としても「自律的に動くエージェント（スキル）」としても使えます。

| 方式 | 何をするか | 向いている場面 |
| --- | --- | --- |
| **A. バックエンド運用** | `kaizenlog advise` がClaude Code CLIを`-p`（ヘッドレス）で1回呼び出し、テキストを受け取ってKaizenLog側がマーカー区間に書き込む。ファイル操作はClaude Codeにさせない | 他のバックエンド（Copilot CLI / Ollama / GitHub Models）と同列に差し替えたいとき。動作が一番シンプルで予測可能 |
| **B. スキル運用** | `daily-kaizen` / `weekly-kaizen` / `kaizen-autopilot` をボールトにインストールし、`/daily-kaizen` などを実行する。日次は同じ `kaizenlog advise` を呼び出し、週次は決定論の集約をもとに考察を書く | 会話から日次処理を起動したいとき、週次の考察や自動化案を作りたいとき |

日次の保存はどちらもKaizenLogのCLIが担当します。週次の考察はweekly-contextマーカーの外に書き、既存の手書きメモを保持します。スキル内のCLI実行には、実行環境で許可されたコマンド実行ツールが必要です。

---

## Step 0: 必要なもの

| 必須/任意 | もの | 備考 |
| --- | --- | --- |
| 必須 | Windows PC | 常時ではなく日中使うPCでOK |
| 必須 | Python 3.11以上 | `python --version` で確認 |
| 必須 | [ActivityWatch](https://activitywatch.net/downloads/) | 無料・オープンソース |
| 任意 | LLM手段のいずれか1つ | 改善提案を使う場合のみ。活動日誌・決定論の集計は設定不要（Step 4参照） |
| 任意 | Claude Code（CLI） | バックエンドとしても、スキル運用（週次分析・オートパイロット・日次エージェント）としても使用可能 |
| ローカル候補の配布形態 | Windows portable ZIP | Python/Tcl/Tk を同梱する設計。build 資材とローカル候補があり、CLI は架空 ActivityWatch データで検証済み。ActivityWatch・Obsidian・LLM は別途必要。実 GUI 操作と Python なし Windows は未確認。公開提供状況は Releases の案内を確認 |

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

### 日次ノートのファイル名を選ぶ

既定の保存先は `vault_dir / daily_notes_dir` です。`[general] daily_note_format` は次の3値だけを受け付けます。

| 値 | 例 |
| --- | --- |
| `YYYY-MM-DD` | `2026-10-15.md` |
| `YYYYMMDD` | `20261015.md` |
| `YYYY/MM/DD` | `2026/10/15.md` |

日付ごとに別の既存ノートを本人が明示して選ぶ場合は `[daily_notes.overrides]` を使います。値は `daily_notes_dir` を基点にした、拡張子 `.md` を含む相対完成パスです。絶対パス、`..` での脱出、symlink/junction 経由の Vault 外参照は拒否されます。

```toml
[general]
daily_notes_dir = "01 Daily Notes"
daily_note_format = "YYYY-MM-DD"

[daily_notes.overrides]
# "2026-10-15" = "2026/10/15.md"
```

候補が複数あるときは自動マージ・削除・改名をせず `conflict` として停止します。既存ノートの移動や一括改名は行いません。機械統計の日付キーは常に ISO 形式のままです。

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

### 安全な明示操作

画面またはサービス層の操作は、読取・プレビューと保存を分けます。保存前に対象日、保存先、翌日への転記を含む更新予定を確認します。生成、日報書込、本人入力、日誌への再投影、外部アプリを開く操作は、それぞれ本人が選んだときだけ実行します。選択や候補表示を、採用・完了・指標達成へ変換しません。

保存は管理マーカー区間を対象にし、マーカー外の手書き本文を保持します。失敗・部分成功・競合は別状態で表示し、生成されていないノートを「開ける」と案内しません。複数ファイルを一括で原子的に保存できるとは扱わず、途中まで成功した保存単位を記録します。

## Step 6: Claude Codeスキルのインストール（Claude Codeを使うなら）

同梱の3スキル（`daily-kaizen` / `weekly-kaizen` / `kaizen-autopilot`）をボールトの`.claude/skills/`に配置します。

```powershell
kaizenlog skill show                              # 同梱スキルの一覧とdescriptionを確認
kaizenlog skill install                           # config.tomlのvault_dirへインストール
kaizenlog skill doctor                            # インストール状態を確認（未導入/更新あり等）
```

本文に加えて週次レビューの参照資料 `weekly-kaizen/references/ai_work_deep_review.md` も配置し、`skill doctor` は両方を確認します。参照資料だけ欠けていて他に変更がなければ、通常のinstallで補います。

既存の配布対象ファイルに変更がある場合は、そのスキル全体を**上書きせずdiffを案内**します。意図的に上書きしたいときだけ `kaizenlog skill install --force` を使います。置換する各ファイルは`.bak`、既存バックアップがあれば`.bak.1`などに退避し、独自に追加したファイルは保持します。別のボールトに入れる場合は `--vault "C:\path\to\vault"` を指定してください。

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

台帳の末尾に改行がなくても、追記時に行を区切って既存の内容を保ちます。同じIDの完了・スキップが複数の日誌で食い違う場合は、日付が新しい日誌の明示チェック（`[x]` / `[-]`）を優先します。未チェックの `[ ]` は完了取り消しにはなりません。

日次提案は生成後に台帳とチェック状態を読み直して採番し、保存から翌日転記までは、完了・スキップなどの協調する更新処理と排他します。Memory配下の `.suggestions.lock` は排他用ファイルです。実行中に削除しないでください。ロックの待機上限は10秒です。保存中に想定外の変更を検出した場合は、他の変更を巻き戻さず対象パスを示して失敗します。外部エディタとの完全な排他や、強制終了・電源断をまたぐ複数ファイルの一括復元は保証しません。

提案保存・縮退セクション保存・翌日転記の失敗は、`advise_health` に `failed` と失敗段階を記録します。翌日転記に失敗した場合は、その前に保存された当日の提案と台帳は残ります。スキル導入では所有ファイルの途中更新に失敗すると元の内容を復元し、復元できないファイルはパスを報告します。既存のバックアップとユーザーが追加したファイルは保持します。

週次集計は、アクション日付が正規の `YYYY-MM-DD` である記録を対象とし、期間指定の風化イベントから日付不明・不正な記録を除外します。実験の基準値や効果量が `NaN` / 無限大になる場合は、数値として報告しません。

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

週次タスクの登録と実行が成功すると、`01 Daily Notes/Weekly Reviews/` にレビューが保存されます。実験の採否は推奨として表示し、最終判断は人間が行います。手動ではボールトでClaude Codeを開いて `/weekly-kaizen`。実験の実測は対象週末まで、台帳の状態は現在値として区別します。

集約のみを保存する場合は `kaizenlog weekly-context --week 2026-W30 --write --strict-write` を使います。`--strict-write` は保存失敗で終了コード1を返すため、失敗時に古いノートでレビューを続けるのを防げます。従来の `--write` だけの呼び出しは、保存失敗を警告・実行ログへ記録して終了コード0を返す動作を維持します。

### 会話から今日の改善提案を生成するとき

Claude Codeでボールトを開いて `/daily-kaizen` を実行します（Step 6の`skill install`が前提）。スキルは対象日と既存設定を確認して `kaizenlog advise` を呼び出します。設定済みのLLMバックエンドによる生成、根拠検証、KZN-ID採番、ノートとKaizen Memoryへの保存が共通の経路で行われます。コマンドを実行できない場合は制約を報告し、直接のノート編集には切り替えません。

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

#### 未検証の依頼文草案

既存の `prompts export` は効果を観測した skilled 済み PRM の出力です。別用途として、`prompts draft <PRM-ID>` は `new` または `skilled` の台帳 snapshot から、効果未検証の編集用草案を決定論的に作る設計です。前後30日の観測、export の再実行、元の会話ログの再収集、LLM 接続は草案作成の条件にしません。

既定はプレビューです。`--write --project PATH` を明示した場合だけ、指定プロジェクトの `.kaizenlog/drafts/<PRM-ID>.md` へ保存します。PRM-ID は台帳の ID として厳密に検証し、パス文字列として扱いません。出力の先頭には `UNVERIFIED_DRAFT / 効果未検証・編集用`、PRM-ID、入力 fingerprint、`目的`・`前提`・`依頼`・`確認条件` の編集欄を置き、入力から分からない項目は「未記入」とします。

```powershell
# プレビュー（台帳やファイルを書き換えない）
kaizenlog prompts draft PRM-20261015-001

# 明示したプロジェクトへ保存
kaizenlog prompts draft PRM-20261015-001 --project "C:\work\my-project" --write
```

プレビュー後に台帳が変わった場合は stale として保存を止めます。同じ内容の再保存は変更なし、異なる既存ファイルは明示の `--force` とバックアップなしには置き換えません。採用・却下などの台帳状態、`.claude/commands`、`AGENTS.md`、`CLAUDE.md` は自動変更しません。

自動化まで任せるなら `claude -p "/kaizen-autopilot"` — 定型作業のスクリプト化や頻出依頼のスキル化が、PRまたは`00 Inbox/`の提案ノートとして提出され、**あなたが承認するまで何も有効化されません**。

---

## コマンド一覧

| コマンド | 何をするか |
| --- | --- |
| `kaizenlog home [--date YYYY-MM-DD] [--config PATH]` | ローカル画面で日誌・計測状態・本人入力を確認し、選んだ操作だけを実行 |
| `kaizenlog run` | ログ生成＋改善提案（毎晩の定期実行と同じ） |
| `kaizenlog generate [--date YYYY-MM-DD] [--skip-catch-up]` | ログ生成。`--skip-catch-up` は対象日のジャーナルだけを明示生成 |
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
| `kaizenlog prompts draft <PRM-ID> [--project PATH] [--write] [--force]` | 未検証の依頼文草案をプレビュー／明示保存 |
| `kaizenlog prompts mark <id> skilled\|dismissed [--skill NAME]` | クラスタをスキル化済み/却下に記録 |
| `kaizenlog skill install [--vault PATH] [--force]` | Claude Codeスキル3種をボールトに配置（既存は上書きせずdiff案内） |
| `kaizenlog skill show` / `skill doctor` | 同梱スキルの一覧／インストール状態の確認 |
| `kaizenlog weekly-context [--week YYYY-Www] [--write] [--strict-write]` | 週次の集約と保存。`--strict-write` は `--write` 必須で、保存失敗を終了コード1で通知 |
| `kaizenlog setup` | 対話式セットアップウィザード（導入の正規経路） |
| `kaizenlog doctor` | 環境の一発診断（AW接続・LLM認証・パス等を✅/⚠️/❌で表示） |
| `kaizenlog status` | 実行履歴の確認（最終成功・直近の失敗理由・partial） |
| `kaizenlog backfill [--days N]` | 欠損日の日誌・統計をまとめて補完 |
| `kaizenlog advise --dry-run` | LLMに送る内容を送信せずに確認（監査用） |
| `kaizenlog init-config [--output PATH]` | 設定ファイルの雛形生成（既定: AppData/XDG） |
| `kaizenlog --config PATH <cmd>` | 明示設定パス（サブコマンドより前） |

### 初回の日誌と毎日の入口

初回は、まず保存先を設定し、対象日と更新予定を確認してから日誌を作ります。引数なしの `setup --yes` は診断だけで、設定・日誌・ActivityWatch・タスク・スキルを書きません。`--vault PATH --yes` を明示すると設定だけを保存します。初回日誌を非対話で作るときは、`--generate-today` を追加した操作だけが typed journal 経路を実行します。

```powershell
# 毎日の確認画面。表示・更新プレビューだけでは保存しない
kaizenlog home --date YYYY-MM-DD --config PATH

# 日誌だけを対象日に作る。過去日の追いつき・retro-advise・LLM/provider探索なし
kaizenlog generate --date YYYY-MM-DD --skip-catch-up

# 設定後に今日の日誌まで作るときは、明示オプションを付ける
kaizenlog setup --vault PATH --yes --skip-aw --skip-task --skip-skills --generate-today
```

`home` の画面では、計測状態や今日の記録を確認し、日誌生成・日報書込・本人入力・外部アプリを開く操作を別々に選びます。候補を見るだけでは採用・完了になりません。ActivityWatch が停止中、記録がない、または保存先へ書けない場合は、空の日誌を成功扱いにしません。

portable Windows ZIP は CLI と GUI を別フォルダに置き、Python/Tcl/Tk を同梱する設計です。このソースの build 資材とローカル候補があり、CLI は架空 ActivityWatch データで検証済みです。ActivityWatch は portable ZIP に含まれず、別途インストール・起動が必要です。実 GUI 操作と Python なし Windows の動作は未確認です。公開ダウンロードの有無は GitHub Releases の案内を確認してください。

portable ZIP を入手済みなら、ZIP 全体を展開したまま次を使います。`gui`、`cli`、`_internal`、`data` は同じ配置に保ち、exe だけ移動・削除しません。

```powershell
.\gui\kaizenlog-home.exe
.\cli\kaizenlog.exe --help
.\cli\kaizenlog.exe --config PATH generate --date YYYY-MM-DD --skip-catch-up
```

ActivityWatch は ZIP とは別にインストールして起動してください。公開 Releases の入手先や Python なしの Windows 動作は、この手順だけでは確認済みとしません。


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
- 発火は `additionalContext` + `memory/live_episodes.jsonl`（検知履歴。トークン会計は夜間ループ税が正）。Windowsトーストは既定で表示せず、必要な場合だけ `[guard] notify = true` を設定する（`false` でも検知と追加コンテキストは残る）
- `run_hook(settings=...)` の設定注入経路は、ホスト環境を汚さないため `notify` を明示しない限りトーストを表示しない（テストや埋め込み実行向け）
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

### 稼働正規化PASS指標と学習表示（第37弾）

- **`context_switches_per_hour`**: 1時間あたりのカテゴリ変更回数。稼働が60分未満の日は測定不能（`None`。0埋めしない）。
- **`ai_tool_errors_per_session`**: AI CLIセッション1回あたりのツールエラー回数。セッション0件の日は測定不能。`ai_tool_error_rate`（÷ツール呼び出し）は `tool_counts` が上位5件しか保存されないため実装しない。
- **判定不成立**: 上記の正規化指標で分母不足のとき、📌 ACTIONS は「判定不成立」と出す（❌ ではない。失敗ではなく測れなかった）。分子キー欠落は「分母不足」と断定しない。
- **実行の有無は問わない指標の挙動**: 提案を実行したかどうかに関係なく、confirmed 判定の指標別PASS率を学習入力に使う。達成・習慣化といった断定ラベルは付けない。実行済みのみを見る従来の指標別PASS実績とは別トラック。

### PASS指標の行動性（第39弾）

- **レートが既定**: `*_per_hour` / `*_per_session` を優先。生カウント（`ai_tool_errors` 等）は**稼働60分以上**の日だけ入口で許可される。
- **計測が薄い日の判定不成立**: 生カウントの判定時、対象日の稼働が直近7日中央値の50%未満なら verdict を書かず「計測が薄い日のため判定不成立」と表示する（既存の pass/fail は書き換えない）。
- **📌欄**: 未完了は既定**1件**表示。全件は `kaizenlog today --all`。完了は `kaizenlog done <ID>`。

### 達成済みの推移再掲とタイムライン被覆（第40弾）

- **達成済みでも指標が戻っているもの**: 📌 の「指標は達成済み N件」の直後に、判定後の実測推移に ❌ があるものだけを最大2件再掲する（未完了1件圧縮は維持）。
- **タイムライン**: 3分未満の細切れは1時間バケット行として表に含め、表末尾に「この表は合計 … の N% を説明しています」と被覆率を明示する。

### 提案の寿命管理と digest / git 突合（第38弾）

- **status 語彙**: `proposed` / `done` / `superseded` / `skipped` に加え、`unmeasurable`（機械PASS無しで3日経過）・`graduated`（測定可能日の直近2日が条件達成）・`retired`（14日経過）を追加。**終了扱いは達成を意味しない**。
- **`kaizenlog:digest`**: advise が frontmatter 直後へ書く決定論30秒サマリ。LLM文・提案本文の再掲はしない。手書き禁止の自動区間。
  - `privacy.redact_patterns` が空で redactor が無い日でも、稼働・AI作業・提案件数など **stats 由来の決定論行は出る**。摩擦ワースト・目標・今日の1手など外部由来文字列を含む行は redactor 無しでは行ごと省略（素通しはしない）。
- **`[aiwork]` の git 突合**（既定ON・読み取り専用）:
  - `outcome_git = true` — false にすると subprocess を呼ばず表示もしない
  - `outcome_git_timeout_seconds = 5.0`
  - `outcome_git_max_repos = 5`
  - 各パスは `git rev-parse --show-toplevel` で root へ正規化してから `git log`（**最大2回/repo**）。subdir の二重計上はしない。
  - コミットメッセージ・著者・ブランチ・絶対パスは日誌に出さない。因果は判定しない並置のみ。

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

## 追加機能の操作例

日常ヘルプは5コマンドに絞っています。専門コマンドは従来の名前で使えます。

```powershell
kaizenlog                         # today と同じ（既存の状態同期も含む）
kaizenlog --help --all
kaizenlog now                     # 今日0時から現在。ノート・統計・実行ログを書かない
kaizenlog recall "設計"            # stats 全期間。日付と一致する記録を表示
kaizenlog backfill --days 30       # 欠損日を古い順に生成。進捗 i/n を表示
kaizenlog monthly --month 2026-09  # プレビュー。SVGも作成しない
kaizenlog monthly --month 2026-09 --write
kaizenlog weekly-context --write  # 集計と「今週の物語」を別マーカーで更新
```

月次アルバムは当月提案の確定 KZN、当月の PRM 昇格件数、判定日のコーチ勝率をそれぞれ条件付きで表示します。採用状況など現在の台帳状態を参照する項目はその旨を明記します。費用・ループ税の SVG は月次フォルダの `assets` に保存し、欠測日は線をつなぎません。日誌末尾の「過去の今日」は1・3ヶ月前を参照し、存在しない月末日はその月の最終日に合わせます。

分類候補は直近14日で120分以上使い、名前だけではルールに一致しないアプリ・サイトです。doctor と週次にも表示します。タイトルを含む分類全体の誤りを断定するものではありません。

```powershell
kaizenlog rules list
kaizenlog rules add "設計作業" --pattern "my-editor"        # 追記内容のプレビュー
kaizenlog rules add "設計作業" --pattern "my-editor" --write
```

既存の TOML とコメントは保持します。過去の分類を更新したい日だけ `generate --date` で再生成してください。

Grok は `~/.grok/sessions/**/updates.jsonl` の検証済み形式を読みます。Cursor はネイティブ履歴を推測せず、`[aiwork] cursor_sessions_dir` に明示した raw stream-json キャプチャのみ対象です。ユーザーイベントに `timestamp_ms` がなければ日次に計上しません。両方とも未取得のトークン・コストを推測しません。[形式と制約](user-delight-20260906/adapter-evidence.md) を参照してください。

```powershell
kaizenlog prompts --roi
kaizenlog prompts export PRM-20260801-001 --project "C:\work\my-project"
kaizenlog prompts export PRM-20260801-001 --project "C:\work\my-project" --write
```

export は skilled 済み PRM の前後30日の再発減少を条件にし、毎日の依頼ログが揃わない期間は欠損として停止します。日数だけ経過した状態を観測完了とは扱いません。`.claude/commands` の編集用下書きには元の依頼例をマスクして掲載します。出力だけで採用台帳は変更しません。同じ内容は変更なし、異なる既存ファイルは停止し、`--force --write` の場合だけバックアップ付きで置き換えます。

```powershell
kaizenlog preflight                         # 事前文脈のプレビュー
kaizenlog guard install --preflight         # フック設定のプレビュー
kaizenlog guard install --preflight --project --write
kaizenlog guard watch --once
kaizenlog task install                      # 登録内容のプレビュー
kaizenlog task install --weekly --guard-watch --write
```

`task install` は日次タスクを登録・更新します。`--weekly` は日曜の `weekly-context --write`、`--guard-watch` は5分毎の `guard watch --once` も登録します。設定ファイルのパスを各タスクへ渡します。watch は Codex とブラウザの新規依頼を読み、Claude Code は既存フックで扱います。トーストは `[guard] notify = true` のときだけ出し、推定不能な浪費額は不明のままです。ライブ通知履歴は夜間の費用集計へ加算しません。

preflight は未チェックの候補・昨日の確定判定・今日の目標の存在する項目だけを最大360文字に収め、リポジトリごとに1日1回注入します。PRM 台帳にリポジトリ所属の証拠がないため、PRM本文の自動注入は行いません。実行ログには注入文字数と概算トークン数だけを残し、本文を保存しません。

LLM 縮退日は読者向けの短い案内へ切り替えます。診断資料はマスクして `.kaizenlog/logs/advice-evidence-YYYY-MM-DD.md` に分け、`doctor` は契約違反の分類と修復導線を表示します。

```toml
[privacy]
private_title_patterns = ["個人用", "private-project"]
```

標準の私的タイトル判定に上の正規表現を追加できます。履歴検索・タイムカプセル・日報などの表示で適用します。保存済みの raw stats を削除・書き換える設定ではありません。

補助コマンドの用途も `--help` で確認できます: `rehumanize` は過去の提案区間を平文化、`screenpipe-probe` は接続診断、`excavate` は繰り返しの遡及調査、`handoff` はリポジトリ向け引き継ぎ、`coach` は承認付きルール改善、`abtest` は予測・体感・実測の比較です。

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
