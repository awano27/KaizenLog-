# KaizenLog

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="KaizenLog — PCの作業記録をObsidianの日誌にし、改善を実測する">
</p>

**PCの作業記録を、Obsidianの日誌にして残す。必要なときだけ、明日試す改善を1件出す。**

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

## 毎日の使い方

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="ActivityWatch → KaizenLog → Obsidian（必要なら LLM 提案）">
</p>

### 1日の流れ

1. **朝** — 目標を1行書く（任意）／未完了アクションを確認  
2. **日中** — ActivityWatch が自動記録（KaizenLog は常駐しない）  
3. **夜** — 日誌を生成。必要なら改善提案も  
4. **翌日** — やってみたアクションを `done` する  

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

`backend = "none"` のまま `advise` / `run` の提案部分を呼ぶと、LLM 無しのためエラーになります。日誌だけなら `generate` を使ってください。

### 手書きは消えない

```markdown
## 振り返り

午前は執筆に集中できた。午後は会議後の再開に時間がかかった。
```

この外側の本文は置換しません。`advise` を使う日は、`## 振り返り` を本人の文脈として優先します。

---

## 発展: AIの使い方を実測で直す

日誌が安定してからで十分です。  
「やり直しのムダ」「効く依頼の型」「ルールが効いたか」を**同じ計測データ**で見ます。

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
| **未実装** | M365 Copilot 改善アシスト（構想のみ。自動取得・Graph 連携・指示の自動変更は非対応） |

---

## LLMとデータの扱い

集計・分類・Obsidian への保存はローカルです。外部送信の有無は LLM バックエンド次第です。

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
