# KaizenLog 使い方手順書

Windowsの毎日の操作を自動記録し、Obsidianのデイリーノートに日誌として残し、LLMが改善提案（Kaizen）を行うツールのセットアップと日々の使い方です。所要時間は初期セットアップ約30分。

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

Obsidianボールトとは別の場所にクローンして構いません（`vault_dir`設定でボールトの場所を指定します）。PowerShellで:

```powershell
git clone https://github.com/awano27/KaizenLog- kaizenlog
cd kaizenlog
pip install -e .
kaizenlog --help    # ヘルプが出れば成功
```

## Step 3: 設定ファイルの作成

```powershell
kaizenlog init-config          # カレントに kaizenlog.toml の雛形ができる
notepad kaizenlog.toml         # 編集する
```

最低限確認するのは2箇所:

```toml
[general]
vault_dir = 'C:/develop/obsidian/2026'   # あなたのボールトのパス

[llm]
backend = "copilot-cli"   # Step 4 で選んだものに合わせる
```

編集したら所定の場所に配置:

```powershell
mkdir $env:APPDATA\kaizenlog -Force
copy kaizenlog.toml $env:APPDATA\kaizenlog\config.toml
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
```

> CPU推論は遅い（応答に数分）ですが夜間バッチなので問題ありません。

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

### 翌朝: 最小アクションにチェックを付ける（10秒）

提案の「### 明日試すこと」にはID付きチェックボックスが並びます：

```markdown
- [ ] KZN-20260707-001: 開発開始時に25分タイマーを1回設定する｜PASS: 25分以上の集中ブロックが1回以上｜FAIL: 0回
```

内部のF-IDは保存前に除去されます。PASS/FAILは翌日の判定条件です。実行したら `[x]` にするだけ。**完了として記録され（Kaizen Memory）、LLMは同じ提案を繰り返さなくなります**。放置した提案も記録され、蒸し返しではなく「（継続）」として扱われます。

Kaizen Memoryの実体は `Kaizen/Memory/suggestions.jsonl`（1行1提案、ID・状態・日付）です。設定の`[general] memory_dir`で保存先を変更できます。中身を直接見たり編集したりする必要は基本ありません — `kaizenlog advise`が読み書きを自動でやります。

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
kaizenlog prompts --days 14   # 「5回/5日: ニュースを要約して… → スキル化を強く推奨」
```

自動化まで任せるなら `claude -p "/kaizen-autopilot"` — 定型作業のスクリプト化や頻出依頼のスキル化が、PRまたは`00 Inbox/`の提案ノートとして提出され、**あなたが承認するまで何も有効化されません**。

---

## コマンド一覧

| コマンド | 何をするか |
| --- | --- |
| `kaizenlog run` | ログ生成＋改善提案（毎晩の定期実行と同じ） |
| `kaizenlog generate [--date YYYY-MM-DD]` | ログ生成のみ（実験計測・統計蓄積を含む） |
| `kaizenlog advise [--date YYYY-MM-DD]` | 改善提案のみ |
| `kaizenlog experiment new --title ... --metric ... --target ...` | 実験の起票 |
| `kaizenlog experiment list` | 実験一覧（使える指標のヘルプも） |
| `kaizenlog patterns [--days N]` | 繰り返しパターン検出レポート |
| `kaizenlog report [--no-llm] [--write]` | 提出用の日報ドラフト生成 |
| `kaizenlog prompts [--days N] [--min-count N]` | Claude Codeへの繰り返し依頼の発掘 |
| `kaizenlog skill install [--vault PATH] [--force]` | Claude Codeスキル3種をボールトに配置（既存は上書きせずdiff案内） |
| `kaizenlog skill show` / `skill doctor` | 同梱スキルの一覧／インストール状態の確認 |
| `kaizenlog doctor` | 環境の一発診断（AW接続・LLM認証・パス等を✅/⚠️/❌で表示） |
| `kaizenlog status` | 実行履歴の確認（最終成功・直近の失敗理由） |
| `kaizenlog backfill [--days N]` | 欠損日の日誌・統計をまとめて補完 |
| `kaizenlog advise --dry-run` | LLMに送る内容を送信せずに確認（監査用） |
| `kaizenlog init-config` | 設定ファイルの雛形生成 |

## トラブルシューティング

**まず `kaizenlog doctor` を実行してください。** 接続・認証・パスの問題を✅/⚠️/❌で一発診断します。夜間実行が動いているか不安なときは `kaizenlog status` で最終成功日時と失敗理由を確認できます（失敗時はWindows通知も出ます）。PCを使わなかった日の歯抜けは、翌晩の実行が自動で補完します（手動なら `kaizenlog backfill`）。

| 症状 | 対処 |
| --- | --- |
| `ActivityWatchに接続できません` | タスクトレイにAWがいるか確認。http://localhost:5600 が開くか確認 |
| `ウィンドウウォッチャーのバケットが見つかりません` | AWを再起動。インストール直後は数分待つ |
| `Claude Code CLI が見つかりません` | https://claude.com/claude-code の手順でインストール後、新しいPowerShellを開き直す |
| `Claude Code CLI がエラーを返しました`＋認証切れの案内 | `claude` を対話起動して `/login` し直す |
| `Copilot CLI が見つかりません` | `npm install -g @github/copilot` 後、新しいPowerShellを開き直す |
| Ollamaでタイムアウト | 初回はモデルロードが遅い。`timeout_seconds` を900に上げる。それでも遅ければ `qwen3:4b` に変更 |
| 提案の質が低い（ローカルLLM） | モデルを大きくするか、backend をCopilot/GitHub Modelsに切り替え |
| 分類がおかしい（仕事のツールがエンタメ扱い等） | `config.toml` の `[[categories.rules]]` にルール追加（デフォルトより優先される） |
| デイリーノートの日誌だけ消したい | ノート内の `<!-- kaizenlog:activity:start -->` 〜 `end -->` を削除（次回また生成される） |
| 「AI作業の質」が出ない | Claude Code未使用の日は出ません。`~/.claude/projects` の場所が違う場合は `[aiwork] claude_projects_dir` を設定 |

## プライバシーについて

- 活動データ・統計はすべてPC内（ボールト内）に保存。外部送信はLLM呼び出し時のプロンプトのみ
- **レダクション**: `config.toml` の `[privacy] redact_patterns` に正規表現を書くと、LLM送信前に該当箇所が `[REDACTED]` にマスクされます（日誌本体は原文のまま）。`kaizenlog advise --dry-run` でマスク後の送信内容を確認できます。さらに送信を絞るなら `[llm] system_prompt = "privacy_safe"` を併用してください
- 完全オフラインにしたい場合はOllamaバックエンドを選択
- ウィンドウタイトルには機密が含まれ得ます。ボールトをGitHub同期している場合はプライベートリポジトリにしてください
- 特定アプリを記録から外したい場合はActivityWatch側の設定でも、`min_block_minutes` を上げてタイムラインへの表示を減らすことでも調整できます
