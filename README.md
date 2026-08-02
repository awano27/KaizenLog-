# KaizenLog

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="KaizenLog — PCの作業を日誌に残し、AIのやり直しを測って次に活かす">
</p>

**PCの作業を日誌に残し、AIのやり直しを測って次に活かす。**  
ActivityWatch の記録を Obsidian のデイリーノートへ書き、必要なときだけ「明日試す改善」を1件出します。

KaizenLog は Windows 向け CLI です。  
[ActivityWatch](https://activitywatch.net/) が記録した前景アプリを集計し、Obsidian のデイリーノートへ書き込みます。数値の計算はコードが行い、LLM は任意（提案が欲しいときだけ）です。

`Windows` · `Python 3.11+` · `ActivityWatch` · `Obsidian` · `MIT` · 現行 `1.5.0rc1`

[3コマンドで始める](#3コマンドで始める) · [毎日の使い方](#毎日の使い方) · [データの扱い](#llmとデータの扱い) · [詳しい手順](docs/USAGE.md)

---

## これは何をするツールか

| やること | やらないこと |
| --- | --- |
| その日の PC 作業をカテゴリ・タイムライン付きで日誌にする | 生産性の「点数」や人格評価を出す |
| 未完了の改善アクションを翌朝ノートに出す | 手書きメモを上書きする |
| （任意）LLM で「明日試す1〜3件」を提案する | LLM に事実の集計やファイル全体の編集を任せる |
| （発展）AI セッションの空転・依頼の再利用を測る | 効果を保証する・未知の数値を埋めて断定する |

**いちばん最初に欲しい成果**は、LLM なしでも動く **Activity Log（活動日誌）** です。  
AI 改善ループ（Prompt ROI / handoff / A/B など）は、日誌が回り始めてからの**発展機能**です。

---

## ノートにこう残る（イメージ）

> 数値・文言は説明用の架空例です。利用者実績や効果保証ではありません。

```markdown
## 📌 今日のアクション
今日の実験: セッション終了時に git diff --stat を見る（目安1分）
- [ ] KZN-20260801-001: …
完了したら: `kaizenlog done KZN-20260801-001`

🎯 今日の目標: 提案書の初稿を出す

## 📊 Activity Log
合計 5h37m / 集中ブロック 3h02m
AI作業 39% · ブラウジング 29% · …

## 📝 日報ドラフト
【本日の業務】プロジェクト別の依頼要約とセッション数
【成果・進捗】コミット件数と主な内容（取得できた場合）
```

- **管理マーカー区間**（`kaizenlog:…`）だけを自動更新します。その外側の手書きは残ります。
- 「振り返り」「明日の目標」は本人が書く欄です。自動では書きません。

---

## 3コマンドで始める

<p align="center">
  <img src="./assets/readme/section-start.svg" width="100%" alt="setup → doctor → generate">
</p>

### 前提

- Windows 10 / 11
- Python 3.11 以上と [pipx](https://pipx.pypa.io/)
- Obsidian ボールト
- ActivityWatch（`setup` が検出し、許可したときだけ導入を案内）

```powershell
git clone https://github.com/awano27/KaizenLog-.git
cd KaizenLog-
pipx install .

kaizenlog setup
kaizenlog doctor
kaizenlog generate --date 2026-08-02   # 日付は自分の観測日に
```

| コマンド | 役割 |
| --- | --- |
| `setup` | 設定ウィザード（ボールト・タイムゾーンなど） |
| `doctor` | ActivityWatch 接続・書き込み先の診断 |
| `generate --date …` | **その日の日誌だけ**作る（LLM 不要・初回向き） |

設定の既定場所は `%APPDATA%\kaizenlog\config.toml` です。  
日付なしの `generate` や `run` は追いつき処理を含むことがあるため、**初回は日付付き `generate` を推奨**します。

---

## 毎日の記録と振り返りに使う

AIを使わない日でも、日誌と工数の下書きは作れます。  
**朝に目標を書く → 日中は自動記録 → 夜に振り返る → 必要な場合だけAI提案**。

数値と文章はすべて**架空の例**です。実在する利用者のデータ、導入実績、改善効果ではありません。  
「自分の振り返り」と「明日の目標」は本人が書くもので、自動生成ではありません。  
Activity LogはActivityWatchが観測したPC前景活動の記録です。生活全体の記録でもありませんし、勤務時間、目標達成、集中力、生産性を判定するものでもありません。  
スマートフォン、他デバイス、離席中の行動は既定では測定できません。  
入力watcherから統計を取得できる場合は、25分以上入力が続いた区間を「集中ブロック」として表示します。

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="ActivityWatch → KaizenLog → Obsidian（必要なら LLM 提案）">
</p>

### 1日の流れ

1. **朝** — 目標を1行書く（任意）／未完了アクションを確認  
2. **日中** — ActivityWatch が自動記録（KaizenLog は常駐しない）  
3. **夜** — 日誌を生成。必要なら改善提案も  
4. **翌日** — やってみたアクションを `done` する  

#### 1. ソフトウェア開発者

**自動記録（Activity Log）**  
6時間12分｜実装 3時間05分｜レビュー 1時間20分｜会議 50分  

**自分の振り返り（手書き）**  
会議後に開発へ戻るまで時間がかかった  

**明日の目標（自分で設定）**  
午前中にレビュー対応を終える  

#### 2. 企画・営業

**自動記録（Activity Log）**  
5時間48分｜顧客会議 2時間10分｜提案書 1時間45分｜調査 1時間05分  

**自分の振り返り（手書き）**  
会議が続き、提案書の作成が夕方に偏った  

**明日の目標（自分で設定）**  
最初の会議までに提案書の骨子を作る  

#### 3. ライター・研究者

**自動記録（Activity Log）**  
5時間30分｜執筆 2時間35分｜調査 1時間50分｜推敲 45分  

**自分の振り返り（手書き）**  
午前の調査が長引いたが、午後は執筆に集中できた  

**明日の目標（自分で設定）**  
調査を90分で区切り、初稿へ進む  

各例のカテゴリ時間は主な内訳であり、合計時間の完全な内訳ではありません。

```powershell
# 朝（任意）
kaizenlog goal "提案書の初稿を完成させる @執筆・ノート"
kaizenlog morning --skip-catch-up    # 表示だけ。追いつき無し

# 夜 — 日誌のみ（LLM 不要）
kaizenlog generate --date YYYY-MM-DD

# 夜 — 日誌 + 改善提案（LLM 設定が必要）
kaizenlog run

# アクションの確認・完了
kaizenlog today
kaizenlog done KZN-20260801-001
```

| コマンド | いつ | メモ |
| --- | --- | --- |
| `goal "…"` | 朝 | ノートに今日の目標を書く |
| `morning` | 朝 | 未完了アクション再掲。`--skip-catch-up` で安全確認向け |
| `generate` | 夜 | Activity Log / 日報ドラフトなど（決定的） |
| `run` | 夜 | `generate` のあと `advise`（LLM） |
| `today` / `done` | 随時 | 候補一覧と完了記録 |

`kaizenlog run`は`generate`と`advise`を順に実行します。  
`backend = "none"`の状態で`advise`を呼ぶと、LLM生成を行わずエラーとして終了します。日誌だけなら `generate` を使ってください。  
日々のLLM提案は現在の契約どおり1〜3件です。  
`generate --date YYYY-MM-DD`による日誌生成と、本人が書く振り返りにはLLMが不要です。  
振り返りを翌日の改善提案へ反映するときだけ、設定済みのLLMバックエンドを使います。

### 手書きは消えない

管理マーカー区間だけを更新し、その外側の手書き本文を置換しません。

```markdown
## 振り返り

午前は執筆に集中できた。午後は会議後の再開に時間がかかった。
```

この外側の本文は置換しません。`advise` を使う日は、`## 振り返り` を本人の文脈として優先します。

---

## 発展: AIのやり直しを測り、次に活かす

日誌が安定してからで十分です。  
やり直しのムダを測る · 効果の高い依頼方法を見つける · 学んだルールを次のAIへ渡す · 改善効果を実測する — を**同じ計測データ**で見ます。

<p align="center">
  <img src="./assets/readme/section-loop.svg" width="100%" alt="実測 → ルール → 検証">
</p>

```powershell
# 見る
kaizenlog status
kaizenlog prompts --roi

# 教える（まず dry-run）
kaizenlog handoff --dry-run
kaizenlog coach --dry-run

# 効いたか確かめる
kaizenlog abtest new --predict +30 --days 28
kaizenlog abtest status
kaizenlog abtest finish --felt +20
```

| 機能 | ひとこと |
| --- | --- |
| **Loop Tax** | リトライ連鎖のうち、最終試行を除くムダ（時間が主。トークンは取れたときだけ） |
| **Prompt ROI** | 繰り返し依頼の再発と、スキル化後の変化 |
| **handoff** | 実測された教訓をエージェント用コンテキストへ（マーカー範囲のみ再生成） |
| **coach** | 提案ファイル作成。書き込みは `coach --apply <file>` のみ |
| **abtest** | 予測・体感・実測。baseline 不足なら「不成立」（数字を作らない） |

`kaizenlog prompts --roi` / `kaizenlog handoff` / `kaizenlog coach` / `kaizenlog abtest` が中心コマンドです。  
通常の`coach`は提案ファイルとdiffを作成します。管理対象のcoach区間へ書き込むのは`coach --apply <proposal-file>`だけです。

取得できないトークンや費用は「不明」のままにします。推定で穴埋めして効果を断定しません。

### その他の補助

```powershell
kaizenlog rehumanize --days 30          # 過去ノートの機械構文を平文に（既定は差分のみ、--write で反映）
kaizenlog excavate                      # 過去の空転をさかのぼって監査
kaizenlog guard install --write         # セッション中の空転をフックで警告（明示時のみ）
kaizenlog screenpipe-probe --minutes 30 # screenpipe 疎通（有効化時・既定 OFF）
```

---

## ブラウザ AI と M365 Copilot

| 状態 | 内容 |
| --- | --- |
| **利用可** | Chrome 拡張で ChatGPT / Claude.ai / Gemini の会話イベントをローカル JSONL へ（詳細は [browser-extension/README.md](browser-extension/README.md)） |
| **Next / Planned** | M365 Copilot（現在未実装） |

### Next / Planned — M365 Copilot改善アシスト

> **現在未実装です。**

会話の自動取得、Microsoft Graph／テナント連携、自動送信、カスタム指示の自動変更にはまだ対応していません。  
参考: [customize-how-microsoft-365-copilot-responds-to-you](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/customize-how-microsoft-365-copilot-responds-to-you) / [declarative-agent-instructions](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/declarative-agent-instructions)

---

## LLMとデータの扱い

集計・分類・Obsidian への保存はローカルです。外部送信の有無は LLM バックエンド次第です。  
`openai-compatible` は設定したendpointへ送信する。endpointはローカルまたはリモートの場合がある点に注意してください。

| `backend` | 送信先 |
| --- | --- |
| `"none"` | 送らない（`advise` はエラー） |
| `"openai-compatible"` | 設定した endpoint（ローカル／リモートどちらもありうる） |
| `"claude-code-cli"` | ローカル Claude Code CLI |
| `"copilot-cli"` | ローカル GitHub Copilot CLI |

- `fallback_to_local=true` で CLI 失敗時だけ OpenAI 互換へ切り替えます。その endpoint が常にローカルとは限りません。
- `[privacy] redact_patterns` は送信直前のベストエフォートな正規表現マスクです。
- `kaizenlog advise --dry-run` でマスク後の送信予定文を確認できます（元ノートは書き換えません）。
- 更新するのは管理マーカー区間だけです。

詳細は [プライバシー](docs/USAGE.md#プライバシーについて) を参照してください。

---

## 制限とカスタマイズ

- 測れるのは主に **PC 前景アプリ**です。スマホ・他端末・離席は既定では測りません。
- カテゴリ時間の増減は「集中が良くなった」ではなく、別画面への移行を示すことがあります。
- LLM の提案は事実ではありません。Activity Log と注記を見て判断してください。
- 日誌のタイムラインは抜粋です。短いブロックの多くは「細切れ」にまとまります。

カテゴリは設定で足せます。

```toml
[[categories.rules]]
name = "AI作業"
ai = true
patterns = ["dify", "自社チャットボット"]
```

全コマンド・実験・評価は [docs/USAGE.md](docs/USAGE.md) にあります。

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

変更履歴は [CHANGELOG.md](CHANGELOG.md)。

## ロードマップ

- [x] ActivityWatch から Markdown 日誌
- [x] 改善提案（advise）とアクション転記
- [x] Prompt ROI / handoff / coach / A/B
- [x] Claude Code・Copilot CLI・OpenAI 互換
- [x] ブラウザ AI テレメトリ
- [x] [screenpipe](https://github.com/mediar-ai/screenpipe) 連携（既定 OFF・参考層）
- [ ] M365 Copilot 改善アシスト
- [ ] Cursor など追加 AI ログ

## ライセンス

[MIT License](LICENSE)。ActivityWatch は別プロセスとして REST API 経由で使い、同梱しません。
