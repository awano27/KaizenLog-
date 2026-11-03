# KaizenLog

**PC の作業記録を、振り返りに使える Markdown 日誌へ。**

ActivityWatch が記録したアプリ名・ウィンドウタイトル・時間を集計する、Windows 向けの個人用ツールです。
日誌・日報の下書き・月次集計は **LLM なし**で作れます。保存先は通常のフォルダーでも Obsidian のボールトでも構いません。

![Windows 10 / 11](https://img.shields.io/badge/Windows-10%20%2F%2011-0451B5)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-0451B5)
![LLM optional](https://img.shields.io/badge/LLM-optional-0E8A91)
[![MIT License](https://img.shields.io/badge/License-MIT-0E8A91)](LICENSE)

[できること](#できること) · [はじめる](#はじめる) · [毎日の使い方](#毎日の使い方) · [AI機能](#ai機能を追加する) · [データの扱い](#データの扱いと計測の限界) · [詳しい使い方](docs/USAGE.md)

## できること

| 目的 | 得られるもの | 必要な入力 |
| --- | --- | --- |
| 今日の作業を振り返る | アプリ・カテゴリ別の時間とタイムライン | ActivityWatch の記録 |
| 日報を下書きする | 取得できた記録に基づく業務・進捗の下書き | 生成済みの日誌・日次統計 |
| 月ごとの時間配分を見る | 作業のつけ先別の時間と成果のサマリー | 工数記録を含む日次統計 |
| 改善を試す | 翌日試す候補、アクションと指標の記録 | 記録・本人の振り返り・設定済みの LLM |
| AI への依頼を見直す | 繰り返し依頼やリトライに費やした時間 | 対応する AI セッションログ |

まずは日誌の生成から始め、必要になったら AI による改善提案やログ分析を追加できます。

### 日誌のイメージ

保存先には、既定で `01 Daily Notes/YYYY-MM-DD.md` と日次統計を作ります。
以下は、開発アプリを60分、ブラウザーを30分使った場合の**架空の出力例**です。実際の利用実績や改善効果を示すものではありません。

```markdown
## 📊 Activity Log

**合計アクティブ時間**: 1h30m / コンテキストスイッチ: 1回

### カテゴリ別

| カテゴリ | 時間 | 割合 |
| --- | ---: | ---: |
| 開発 | 1h00m | 67% |
| ブラウジング | 30m | 33% |

## 振り返り

午前中に実装を進めた。調べた内容を明日のレビューに使いたい。
```

「振り返り」は本人が書く欄です。自動更新するのは管理マーカー（`kaizenlog:…`）で囲んだ区間で、その外側の手書き本文は保持します。
詳しい出力は[日誌サンプル](docs/examples/demo_daily_note.md)で確認できます。

## はじめる

### 必要なもの

- **Windows 10 / 11 と Python 3.11 以上** — 初回インストール時に Python の依存ライブラリも取得します。
- **[ActivityWatch](https://activitywatch.net/)** — 別途インストールし、起動しておきます。対象日の `aw-watcher-window` の記録が必要です。
- **書き込み可能な保存先フォルダー** — Obsidian のボールトも使えます。Obsidian アプリは必須ではありません。

ソース ZIP には Python・ActivityWatch・Obsidian・LLM 本体を含めていません。
ActivityWatch を導入する前の作業は、あとから記録できません。

### 1. ダウンロードしてインストールする

[ソース ZIP](https://github.com/awano27/KaizenLog-/archive/refs/heads/main.zip)を展開し、`pyproject.toml` があるフォルダーで PowerShell を開きます。

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\kaizenlog.exe --help
```

Git を使う場合は `git clone https://github.com/awano27/KaizenLog-.git` で取得したフォルダーから同じ手順を実行します。
仮想環境の有効化や、PowerShell の実行ポリシー変更は不要です。

### 2. 保存先を設定する

日誌用のフォルダーを作り、次の `$vault` をそのパスに変更してください。
ActivityWatch は事前に起動し、通常の接続先 `http://localhost:5600` で記録を確認します。

```powershell
$vault = 'C:\Notes\KaizenLog'  # 作成済みの保存先に変更
.\.venv\Scripts\kaizenlog.exe setup --vault $vault --yes --skip-aw --skip-task --skip-skills
.\.venv\Scripts\kaizenlog.exe doctor
```

この例は設定の保存と診断までを行い、日誌生成・タスク登録・Claude Code スキル配置は行いません。
設定ファイルの既定の場所は `%APPDATA%\kaizenlog\config.toml` です。

新規設定では LLM を使わない `backend = "none"` が選ばれます。既存の設定がある場合はバックエンドを引き継ぐため、日誌だけで始めるなら `[llm]` を次の内容にします。

```toml
[llm]
backend = "none"
fallback_to_local = false
```

`setup --yes` を保存先なしで実行した場合は診断だけで、設定は保存しません。
対話式の `setup` では日誌生成や過去日の補完も案内されます。上の例は、それらを後の操作に分けています。

### 3. 今日の日誌を作る

```powershell
$day = Get-Date -Format 'yyyy-MM-dd'
.\.venv\Scripts\kaizenlog.exe generate --date $day --skip-catch-up

# 日報の下書きも追記する場合
.\.venv\Scripts\kaizenlog.exe report --date $day --no-llm --write
```

保存先の `01 Daily Notes` で当日の `.md` を開いてください。**ここまで LLM の設定・呼び出しは不要です。**

`generate --skip-catch-up` は、過去日の補完や AI 提案を行わず日誌を作ります。ただし、対象日の日誌・統計に加えて**翌日の引き継ぎ欄も更新します**。
日付や `--skip-catch-up` を省略した通常の実行では、過去日の補完が入る場合があります。

以降の `kaizenlog` は、この仮想環境では `.\.venv\Scripts\kaizenlog.exe` に読み替えてください。

## 毎日の使い方

**日中は ActivityWatch で記録 → 夜に日誌を作成 → 自分の振り返りを一言残す。**
日報や改善提案は、必要なときに追加します。

### 画面で確認する

```powershell
kaizenlog home
```

ローカル画面で記録を確認し、日誌生成・日報作成・本人入力を別々に選べます。
起動するだけでは ActivityWatch の取得や日誌生成を始めません。候補を見るだけで、採用・完了に変わることもありません。

### コマンドで使う

| やりたいこと | コマンド例 |
| --- | --- |
| 今日の目標を書く | `kaizenlog goal "提案書の初稿を完成させる"` |
| 日誌を作る | `kaizenlog generate --date YYYY-MM-DD --skip-catch-up` |
| 日報を下書きして追記する | `kaizenlog report --date YYYY-MM-DD --no-llm --write` |
| 月次集計を見る | `kaizenlog monthly --month YYYY-MM` |
| アクションを表示する | `kaizenlog today --no-sync` |
| アクションの完了を記録する | `kaizenlog done KZN-YYYYMMDD-001` |
| 過去の記録を探す | `kaizenlog recall "設計"` |
| 実行履歴・接続状態を調べる | `kaizenlog status` / `kaizenlog doctor` |

日付や ID は実際の値に置き換えてください。
`today` は通常、ノートのチェック状態をアクションの記録へ同期します。表示だけにしたいときは `--no-sync` を付けます。
アクションの実施と、指標が目標に達したかどうかは別々に扱い、観測値がない日は「未判定」と表示します。
月次の工数集計には工数記録が必要です。上の日誌専用手順だけでは工数を作らないため、月次に「工数記録なし」と表示されることがあります。

### 自動実行を使う場合

Windows タスクスケジューラへの登録が別途必要です。[自動実行の手順](docs/USAGE.md#step-7-自動実行の登録)を参照してください。
このソースの登録スクリプトは、新規の日次タスクを日誌専用モードで作成します。改善提案まで実行する場合は `-Mode advice` を指定します。
既存タスクを `Mode` 指定なしで更新すると、そのタスクの実行内容は保持されます。

## AI機能を追加する

### 記録から改善案を作る

Claude Code CLI、GitHub Copilot CLI、OpenAI 互換 API に対応しています。
利用するバックエンドを設定し、必要なログイン・API キー・モデルを用意してください。

```powershell
# 送信予定の文章を確認
kaizenlog advise --date YYYY-MM-DD --dry-run

# 改善候補を生成
kaizenlog advise --date YYYY-MM-DD
```

`advise` は本人の振り返りと観測された記録を材料に、1〜3件の候補を作ります。
`run` は日誌生成と改善提案を続けて実行します。`backend = "none"` で `advise` を実行するとエラーになるため、日誌だけなら `generate` を使います。

### AI への依頼やリトライを振り返る

対応する AI セッションログがある場合、繰り返し依頼やリトライに費やした時間を集計できます。

```powershell
kaizenlog prompts --roi
kaizenlog prompts draft PRM-YYYYMMDD-001
```

`prompts draft` は効果未検証の編集用草案をプレビューします。保存するときだけ `--project PATH --write` を追加します。
`handoff`、`coach`、`abtest` による依頼方法の見直しや効果の記録は、[詳しい使い方](docs/USAGE.md#計測から調教へ第27弾)を参照してください。
取得できないトークン数や費用は「不明」のまま扱います。

### 追加の入力

| 入力 | 用途・条件 |
| --- | --- |
| `aw-watcher-input` | 入力量や集中ブロックの表示。未導入でも日誌は作れます |
| ActivityWatch の `aw-watcher-web` | ブラウザーのサイト別集計 |
| 同梱の Chrome 拡張 | ChatGPT / Claude.ai / Gemini の会話イベントをローカル JSONL に書き出す。[導入手順](browser-extension/README.md) |
| screenpipe | 画面内容を補助情報として利用。既定では無効 |

Chrome 拡張は Python パッケージに含まれないため、ソースの `browser-extension/` を使います。
**M365 Copilot 改善アシストは未実装です。** Microsoft Graph・テナント連携、会話の自動取得、自動送信、カスタム指示の自動変更には対応していません。

## データの扱いと計測の限界

集計・分類・日誌の保存はローカルで行います。AI 機能の送信先は、設定した LLM バックエンドによって変わります。

| `backend` | 送信先 |
| --- | --- |
| `"none"` | LLM へ送信しない |
| `"openai-compatible"` | 設定した endpoint。ローカル・リモートのどちらも指定可能 |
| `"claude-code-cli"` | Claude Code CLI が接続するサービス |
| `"copilot-cli"` | GitHub Copilot CLI が接続するサービス |

`fallback_to_local = true` の場合、CLI が失敗すると OpenAI 互換の endpoint へ切り替わります。名前にかかわらず、その endpoint がローカルとは限りません。
`[privacy] redact_patterns` によるマスクは正規表現による補助です。送信内容は `advise --dry-run` で確認できます。詳しくは[プライバシーの説明](docs/USAGE.md#プライバシーについて)を参照してください。

- 計測対象は主に PC の前景アプリです。スマートフォン・他端末・離席中の行動は既定では測りません。
- 作業時間やカテゴリの変化は、勤務時間・集中力・生産性・目標達成の判定ではありません。
- 日誌のタイムラインは抜粋で、短い作業は「細切れ」にまとめられることがあります。
- 日報や改善案の材料は、取得できたログの範囲に限られます。LLM の提案は本人が確認してください。
- チーム共有・チーム集計の機能はありません。個人の振り返りに使うツールです。

## 困ったとき・配布物について

| 状況 | 確認すること |
| --- | --- |
| ActivityWatch に接続できない | 起動状態と接続先。`kaizenlog doctor` で診断 |
| 対象日の記録がない | `aw-watcher-window` が対象日に動いていたか |
| 日誌を書き込めない | 設定の保存先と、フォルダーへの書き込み権限 |
| AI 提案だけ失敗する | バックエンド・認証・endpoint。日誌は `generate` で作成可能 |
| 全コマンドを見たい | `kaizenlog --help --all` と[使い方手順書](docs/USAGE.md) |

この README は現在のソースを対象にしています。公開済みの配布物に同じ機能が含まれるかは、[GitHub Releases](https://github.com/awano27/KaizenLog-/releases)の案内を確認してください。

<details>
<summary>Windows portable ZIP を入手済みの場合</summary>

ZIP 全体を同じフォルダー構成のまま展開して使います。`gui`、`cli`、`_internal`、`data` の配置を保ち、exe だけを移動しないでください。

```powershell
.\gui\kaizenlog-home.exe
.\cli\kaizenlog.exe --help
.\cli\kaizenlog.exe --config PATH generate --date YYYY-MM-DD --skip-catch-up
```

Python/Tcl/Tk を同梱する設計です。ActivityWatch・Obsidian・LLM は同梱せず、使うものを別途用意します。
Python を別途導入していない Windows や、GUI の全操作を通した動作は未確認です。配布物の提供状況・確認範囲は、各リリースの案内を参照してください。

</details>

## 開発・ドキュメント

リポジトリのルートで、開発用依存関係をインストールしてテストします。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

- [使い方手順書](docs/USAGE.md) — 全コマンド・設定・自動実行・評価
- [設定ファイルの例](config.example.toml) — カテゴリや LLM などの設定
- [日誌サンプル](docs/examples/demo_daily_note.md) — 出力のイメージ
- [ブラウザー拡張](browser-extension/README.md) — AI 会話ログの取得
- [変更履歴](CHANGELOG.md)

[MIT License](LICENSE)。ActivityWatch は別プロセスの REST API 経由で利用し、同梱していません。
