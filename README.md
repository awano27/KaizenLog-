# KaizenLog

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="KaizenLog — AIとの仕事を、実測で調教する">
</p>

**AIとの仕事を、実測で調教する。**

KaizenLogは、PC作業とAIセッションの記録から、やり直しのムダ、再利用できる依頼方法、改善ルールの効果を測るWindows向けCLIです。ActivityWatchの事実をObsidianへ残し、必要な場合だけLLMを使います。

`Windows` · `Python 3.11+` · `ActivityWatch` · `Obsidian` · `MIT`

[3コマンドで始める](#3コマンドで始める) · [改善ループ](#measure--teach--verify) · [データの扱い](#llmとデータの扱い) · [詳しい使い方](docs/USAGE.md)

---

## まず、何が変わるのか

KaizenLogは「AIをたくさん使ったか」ではなく、次の改善ループを実測します。

| できること | 現行機能 |
| --- | --- |
| やり直しのムダを測る | **Loop Tax** — 最終試行を除くリトライ連鎖の時間・取得可能なトークン・推定費用 |
| 効果の高い依頼方法を見つける | **Prompt ROI** — `kaizenlog prompts --roi` |
| 学んだルールを次のAIへ渡す | **handoff / coach** — `kaizenlog handoff`、`kaizenlog coach` |
| 改善効果を実測する | **A/B test** — `kaizenlog abtest` |

数値が取得できないとき、トークン数や費用は未知のまま扱います。推定値で穴埋めして効果を断定しません。

---

## 実際に残る証拠

<p align="center">
  <img src="./assets/readme/section-loop.svg" width="100%" alt="KaizenLogが実測、ルールの提案、検証をつなぐループ">
</p>

### 表示例

以下は中立的なサンプル値です。ベンチマーク、利用者実績、効果保証ではありません。

```text
Loop Tax
  リトライ連鎖: 2件 / 最終試行を除く時間: 18分
  tokens: 不明 / 推定費用: 不明

Prompt ROI
  PRM-20260730-001  再発: 4回 / skilled効果: 確認待ち

agent-context marker
  <!-- kaizenlog:agent-context:start -->
  - 実測された教訓: 繰り返しの失敗を先に確認する
  <!-- kaizenlog:agent-context:end -->

A/B result card
  予測 +30% / 体感 +20% / 実測: 不成立（baseline不足）
```

Prompt ROIで`skilled`の効果を確定するには、比較対象となる完了済みの観測期間が必要です。`handoff`は自身のマーカー範囲だけを再生成します。`coach`は提案を作るだけで、明示的な`--apply`が必要です。A/B testはbaselineが不足すると、効果を作り出さず「不成立」として返します。

---

## 3コマンドで始める

<p align="center">
  <img src="./assets/readme/section-start.svg" width="100%" alt="KaizenLogの初回導線: setup、doctor、generate">
</p>

### 前提

- Windows 10 / 11
- Python 3.11以上と[pipx](https://pipx.pypa.io/)
- Obsidianボールト
- ActivityWatch（`setup`で検出し、許可した場合だけ導入できます）

現行は`1.5.0rc1`です。GitHubから取得してインストールします。

```powershell
git clone https://github.com/awano27/KaizenLog-.git
cd KaizenLog-
pipx install .
```

最初の成功は、LLMを使わない日誌生成です。`YYYY-MM-DD`を生成したい日付に置き換えてください。

```powershell
kaizenlog setup
kaizenlog doctor
kaizenlog generate --date YYYY-MM-DD
```

`setup`は設定、`doctor`は接続と書き込み先の診断、`generate`はActivityWatchの事実を集計してActivity Logを生成します。裸の`generate`は追いつき処理を行うことがあるため、初回の安全な実行例にはしていません。

---

## Measure → Teach → Verify

実測からルールを作り、そのルールが効いたかを同じデータで確認します。

```powershell
# MEASURE
kaizenlog status
kaizenlog prompts --roi

# TEACH
kaizenlog handoff --dry-run
kaizenlog coach --dry-run

# VERIFY
kaizenlog abtest new --predict +30 --days 28
kaizenlog abtest status
kaizenlog abtest finish --felt +20
```

- `handoff --dry-run`は決定的に計測された教訓をプレビューします。対象は`[handoff] targets`で設定します。
- `coach --dry-run`はレダクション済みの30日コンテキストを表示し、LLMを呼び出しません。
- 通常の`coach`は提案ファイルとdiffを作成します。管理対象のcoach区間へ書き込むのは`coach --apply <proposal-file>`だけです。
- `abtest`は予測・体感・実測効果を比較し、完了時にSVGカードを作成します。

---

## 基本ワークフロー

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="ActivityWatchの記録をKaizenLogが集計してObsidianへ書き、必要な場合だけLLM提案を追加するデータフロー">
</p>

1. **ActivityWatch**がPCの前景アプリを記録します。
2. **KaizenLog**が分類・集計し、Activity Logと機械可読な統計を作ります。
3. **Obsidian**の管理マーカー区間に日誌を残します。
4. 必要な場合だけ、レダクション後の入力をLLMへ渡して1〜3件の提案を作ります。
5. 結果を次の`prompts`、`handoff`、`coach`、`abtest`で検証します。

数値はコードで決定的に計算し、LLMには解釈と提案だけを任せます。LLMがObsidianファイルを直接編集する構成ではありません。

---

## 毎日の記録として使う

```powershell
kaizenlog run
kaizenlog morning
kaizenlog today
```

`kaizenlog run`は`generate`と`advise`を順に実行します。LLMを使わずActivity Logだけを作る場合は`kaizenlog generate --date YYYY-MM-DD`を使ってください。`backend = "none"`の状態で`advise`を呼ぶと、LLM生成を行わずエラーとして終了します。

`morning`は未完了アクションを再表示し、必要な場合は追いつき処理も行います。追いつきを行わない表示だけの確認には`kaizenlog morning --skip-catch-up`を使います。`today`で候補を確認し、実行済みなら`kaizenlog done KZN-…001`で完了にできます。日々のLLM提案は現在の契約どおり1〜3件です。

---

## ブラウザAIとM365 Copilot

### Available — ブラウザAIテレメトリ

現在のChrome拡張はChatGPT、Claude.ai、Geminiの3ドメインだけを対象にし、会話イベントをローカルJSONLへ保存します。

### Next / Planned — M365 Copilot改善アシスト

> **現在未実装です。**

M365 Copilot Chatだけに任意のサイト権限を与え、依頼・回答・往復回数をローカル計測し、改善プロンプトやカスタム指示をコピー可能な形で提案する構想です。会話の自動取得、Microsoft Graph／テナント連携、自動送信、カスタム指示の自動変更にはまだ対応していません。

構想の前提となるMicrosoftの説明は、[Microsoft 365 Copilotの応答のカスタマイズ](https://support.microsoft.com/en-us/microsoft-365-copilot/customize-how-microsoft-365-copilot-responds-to-you)と[declarative agent instructions](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/declarative-agent-instructions)を参照してください。

現行拡張の導入と保存内容は[browser-extension/README.md](browser-extension/README.md)を参照してください。本文保存は拡張オプションで無効化できます。

---

## LLMとデータの扱い

ActivityWatchの読み取り、分類、集計、Obsidianへの保存はローカルで実行します。外部送信の有無は選んだLLMバックエンドで決まります。

| 設定 | データ経路 |
| --- | --- |
| `backend = "none"` | LLMへ送信しない。`advise`はエラーとして終了する |
| `backend = "openai-compatible"` | 設定したendpointへ送信する。endpointはローカルまたはリモートの場合がある |
| `backend = "claude-code-cli"` | ローカルのClaude Code CLIプロセスへプロンプトを渡す |
| `backend = "copilot-cli"` | ローカルのGitHub Copilot CLIプロセスへプロンプトを渡す |

重要な境界:

- `fallback_to_local=true`かつCLIバックエンド失敗時だけ、設定済みのOpenAI互換経路へ切り替えます。そのendpointが常にローカルとは限りません。
- `[privacy] redact_patterns`は、LLMへ送るsystem/userプロンプトの送信直前に適用するベストエフォートの正規表現マスクです。
- `kaizenlog advise --dry-run`で送信予定のマスキング後テキストを確認できます。マスキングはローカルの元日誌を書き換えません。
- KaizenLogは管理マーカー区間だけを更新し、その外側の手書き本文を置換しません。

詳しい境界と注意点は[プライバシー](docs/USAGE.md#プライバシーについて)を確認してください。

---

## 制限とカスタマイズ

- ActivityWatchから取得できるPC前景アプリが中心で、スマートフォン、他デバイス、離席中の行動は既定では測定できません。
- カテゴリ時間の変化は、集中改善ではなく別デバイスへの移行を示すことがあります。
- LLMの提案は事実ではありません。Activity Logと計測上の注意をあわせて判断してください。

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
- [x] Prompt ROI、handoff / coach、A/B testによる改善ループ
- [x] Claude Code／Copilot CLI／OpenAI互換バックエンド
- [x] ブラウザAIテレメトリ
- [ ] M365 Copilot改善アシスト（Next / Planned、現在未実装）
- [ ] Cursorなど追加AIログ、[screenpipe](https://github.com/mediar-ai/screenpipe)連携

## ライセンス

[MIT License](LICENSE)。ActivityWatchは別プロセスとしてREST API経由で利用し、KaizenLogには同梱していません。
