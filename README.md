# KaizenLog

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="KaizenLog — 一日のPC作業を、明日の改善1つに変えるWindows向けCLI">
</p>

**一日のPC作業を、明日の改善1つに変える。**

KaizenLogは、[ActivityWatch](https://activitywatch.net/)の記録を整理して[Obsidian](https://obsidian.md/)のデイリーノートへ残し、必要な場合だけLLMを使って「明日試すこと」を1つ提案するWindows向けCLIです。

> ヒーロー内の時間と提案は**表示例**です。利用者の実績値や効果を保証するものではありません。

`Windows` · `Python 3.11+` · `ActivityWatch` · `Obsidian` · `MIT`

[最初の日誌を作る](#3コマンドで最初の日誌を作る) · [データの扱い](#llmとデータの扱い) · [詳しい使い方](docs/USAGE.md)

---

## まず、何が残るのか

長いアプリ履歴を読み返す代わりに、KaizenLogは取得できた事実を集計し、次のようなMarkdownをデイリーノートへ追記します。

```markdown
## 📊 Activity Log
- 開発: 2時間02分
- コミュニケーション: 38分

## 🚀 Kaizen（AIからの改善提案）
### 今日の結論
午前は開発時間をまとめて確保できました。

### 明日試すこと
- [ ] KZN-20260729-001: 9:00に25分の集中枠を入れる

### 計測上の注意
ActivityWatchで取得できた範囲をもとにしています。
```

Activity Logは決定的な集計、KaizenはLLMを有効にした場合の提案例です。提案には`KZN-…` IDと判定条件を持たせ、翌日に実行・完了・保留を追跡できます。

---

## 3コマンドで最初の日誌を作る

<p align="center">
  <img src="./assets/readme/section-start.svg" width="100%" alt="KaizenLogの初回導線: setup、doctor、generate">
</p>

### 前提

- Windows 10 / 11
- Python 3.11以上と[pipx](https://pipx.pypa.io/)
- Obsidianボールト
- ActivityWatch（`setup`で検出し、許可した場合だけ導入できます）

現行は`1.5.0rc1`です。PyPIの公開パッケージは未確認のため、GitHubから取得してインストールします。

```powershell
git clone https://github.com/awano27/KaizenLog-.git
cd KaizenLog-
pipx install .
```

インストール後、まずLLMなしでも確認できる日誌生成まで進めます。

```powershell
kaizenlog setup
kaizenlog doctor
kaizenlog generate
```

1. **`setup`** — ActivityWatch、Obsidianボールト、LLM、任意機能を対話形式で設定
2. **`doctor`** — 接続、設定、書き込み先、選択したLLM経路を診断
3. **`generate`** — ActivityWatchを分類・集計し、最初のActivity Logを生成

設定は通常`%APPDATA%\kaizenlog\config.toml`へ保存されます。日次タスクは、セットアップ中に許可した場合、または非対話実行で`--register-task`を指定した場合だけ登録されます。`--yes`だけでは登録されません。

手動設定とトラブルシュートは[詳しい使い方](docs/USAGE.md)を参照してください。

---

## 毎日どう使うか

<p align="center">
  <img src="./assets/readme/section-loop.svg" width="100%" alt="夜に日誌を作り、朝に確認し、日中に改善を1つ試すKaizenLogのループ">
</p>

### 1. 夜 — 計測を日誌へ

```powershell
kaizenlog run
```

`run`は`generate`と`advise`を順に実行します。LLMバックエンドが`none`なら、Activity Logだけを生成して提案はスキップします。

### 2. 朝 — 昨日を確認

```powershell
kaizenlog morning
```

未完了アクションを再描画して通知します。必要な場合はActivityWatchの追いつき、LLM提案、ノート書き込みを含みます。追いつきを行わない場合は`--skip-catch-up`を使います。

### 3. 日中 — 改善を1つ試す

```powershell
kaizenlog today
kaizenlog done KZN-20260729-001
```

`today`は未完了アクションを表示し、既定ではノートのチェック状態をMemoryへ同期します。実行したアクションは`done`で完了できます。

そのほかの運用コマンド:

```powershell
kaizenlog status
kaizenlog backfill --days 7
kaizenlog patterns --days 30
```

---

## 仕組み

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="ActivityWatchの記録をKaizenLogが集計してObsidianへ書き、任意でLLM提案を追加するデータフロー">
</p>

1. **ActivityWatch**が前景アプリを記録
2. **KaizenLog**が分類・集計し、Activity Logと機械可読な統計を作成
3. **Obsidian**の管理マーカー区間へ日誌を書き込み
4. **任意のLLM**が、マスキング後のプロンプトをもとに改善案を返す
5. **KaizenLog**が提案を検証し、Markdownとして管理マーカー区間へ追記

数値はコードで決定的に計算し、LLMには解釈と提案だけを任せます。LLMがObsidianファイルを直接編集する構成ではありません。

---

## LLMとデータの扱い

KaizenLogはLLMなしでもActivity Logを生成できます。外部送信の有無は、選択したバックエンドで決まります。

| 設定 | データ経路 |
| --- | --- |
| `backend = "none"` | LLMへ送信せず、活動ログだけを生成 |
| `backend = "openai-compatible"` + Ollama | 設定したローカルAPIへ送信 |
| `backend = "claude-code-cli"` | ローカルのClaude Code CLIプロセスへプロンプトを渡す |
| `backend = "copilot-cli"` | ローカルのGitHub Copilot CLIプロセスへプロンプトを渡す |
| `backend = "openai-compatible"` + リモートAPI | 設定した外部APIへ送信 |

重要な境界:

- ActivityWatchの読み取り、分類、集計、Obsidianへの保存はローカルで実行します
- `fallback_to_local=true`の場合だけ、選択したCLIバックエンドが失敗した際に設定済みのOpenAI互換ローカル経路へ切り替えます
- `[privacy] redact_patterns`は、LLMへ渡すsystem/userプロンプトへ送信直前に適用するベストエフォートの正規表現マスクです
- マスキングはローカルの元日誌を書き換える機能ではありません。`kaizenlog advise --dry-run`で送信予定のマスキング後テキストを確認できます
- KaizenLogは管理マーカー区間だけを更新し、その外側にある手書き本文を置換しません

より詳しい境界と注意点は[プライバシー](docs/USAGE.md#プライバシーについて)を確認してください。

---

## 必要に応じて広げる

### Claude Codeスキル

```powershell
kaizenlog skill install
claude -p "/daily-kaizen"
claude -p "/weekly-kaizen"
```

日次・週次レビューをClaude Codeから呼び出す任意機能です。インストールしたスキルは、KaizenLog CLI本体とは別のagent実行経路になります。

### ActivityWatch watcher

| watcher | 追加できる指標 |
| --- | --- |
| [aw-watcher-web](https://github.com/ActivityWatch/aw-watcher-web) | サイト別時間、`site_minutes` |
| [aw-watcher-input](https://github.com/ActivityWatch/aw-watcher-input) | 集中ブロック、`focus_blocks` |

未導入でも動作します。取得できない指標は生成されず、`doctor`が状態を案内します。

### ブラウザAIテレメトリ

ローカル専用拡張を使うと、ChatGPT、Claude.ai、Geminiの会話イベントをJSONLへ書き出し、`🧠 AI作業の質`へ集計できます。

- DOM監視対象は3ドメイン
- 出力先は既定で`Downloads/kaizenlog-browser-ai/YYYY-MM-DD.jsonl`
- 本文保存は拡張オプションで無効化可能
- トークン数は取得不能なため、文字数だけを別集計

導入手順は[browser-extension/README.md](browser-extension/README.md)を参照してください。

---

## 制限とカスタマイズ

### 計測の限界

- ActivityWatchから取得できるPC前景アプリが中心です
- スマートフォン、他デバイス、離席中の行動は既定では測定できません
- カテゴリ時間の減少が、集中改善ではなく別デバイスへの移行を示す場合があります
- LLMの提案は事実ではありません。Activity Logと計測上の注意をあわせて判断してください

### 分類ルール

アプリ名やタイトルに合わせてカテゴリを追加できます。

```toml
[[categories.rules]]
name = "AI作業"
ai = true
patterns = ["dify", "自社チャットボット"]
```

実験、レポート、プロンプト台帳、評価ハーネスを含む全コマンドは[docs/USAGE.md](docs/USAGE.md)にまとめています。

---

## 開発

```powershell
git clone https://github.com/awano27/KaizenLog-.git
cd KaizenLog-
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

変更履歴は[CHANGELOG.md](CHANGELOG.md)を参照してください。

## ロードマップ

- [x] ActivityWatchからのMarkdown日誌生成
- [x] KZN ID、Memory、PASS/FAILを使った改善ループ
- [x] Claude Code／Copilot CLI／OpenAI互換バックエンド
- [x] ブラウザAIテレメトリ、実験、レポート
- [ ] Cursorなど追加AIログ、[screenpipe](https://github.com/mediar-ai/screenpipe)連携

## ライセンス

[MIT License](LICENSE)。ActivityWatchは別プロセスとしてREST API経由で利用し、KaizenLogには同梱していません。
