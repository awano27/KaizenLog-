# KaizenLog

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="KaizenLog: Windows activity becomes an Obsidian daily note and small measurable improvement actions">
</p>

**Windows の操作ログを毎晩まとめて、翌日に試せる改善アクションにする。**  
ActivityWatch + Obsidian + LLM。ローカルファースト。マーカー区間だけを更新するので手書きノートは残り続けます。

> Automatic Windows activity journal in Markdown, with LLM-powered daily improvement suggestions. Built on ActivityWatch. Local-first.

---

<p align="center">
  <img src="./assets/readme/section-loop.svg" width="100%" alt="Daily loop: measure at night, surface in the morning, complete during the day">
</p>

| 夜 | 朝 | 日中 |
| --- | --- | --- |
| `kaizenlog run` が日誌と提案を書く | `morning` が 📌 と件数トースト | `today` / `done` で消化 |

- **計測は ActivityWatch に委譲** — 収集エンジンを再発明しない
- **提案は言いっぱなしにしない** — `KZN-…` ID、PASS/FAIL、消化率
- **LLM はテキストだけ** — ノート書き込みは常に KaizenLog（マーカー区間のみ）
- **ローカルファースト** — 活動データは PC 内。Ollama なら完全オフライン可
- **意味ガードは日本語同梱プロンプト向け** — 英語プロンプト追加時はキーワード対訳が必須（沈黙劣化防止）

詳細な機能一覧・設定は [docs/USAGE.md](docs/USAGE.md) を参照してください。

---

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="ActivityWatch to generate to advise to morning to today and done to Memory">
</p>

### 仕組み（要約）

1. **ActivityWatch** が前景アプリを常駐記録  
2. **`generate`** が分類・集計し、デイリーノートの Activity Log を更新  
3. **`advise`** が機械可読統計を根拠に JSON 提案 → 読みやすい Markdown へ  
4. **`morning` / `today`** が未完了アクションを届ける  
5. **`done`** とノートのチェックで消化率・翌日の自動判定へつなぐ  

---

<p align="center">
  <img src="./assets/readme/section-start.svg" width="100%" alt="Quick start with four commands">
</p>

### 最短セットアップ（開発版）

現行は **1.5.0rc1（RC）** です。PyPI への公開パッケージは未確認のため、GitHub から clone して入れます。

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
3. `kaizenlog setup` — ボールト・LLM・AW・スキル・夜間/朝タスク（設定は `%APPDATA%\kaizenlog\config.toml`）  
4. `kaizenlog doctor` — 環境診断（設定なしでは通常コマンドは動きません）  
5. `kaizenlog run` — ActivityWatch 起動後に初回実行  

手動設定・トラブルシュートは [docs/USAGE.md](docs/USAGE.md)。

### 日々のコマンド

```powershell
kaizenlog run
kaizenlog morning
kaizenlog today
kaizenlog done KZN-…001
kaizenlog status
```

- **run** — 夜: 収集 + 提案（タスクスケジューラと同じ）  
- **morning** — 朝: 追いつき（AW/LLM・書き込みを含む場合あり）→ 📌 再描画 → 件数トースト（`--skip-catch-up` で追いつきなし）  
- **today** — 日中: 今日の候補と保留件数（既定でノートのチェックを Memory へ同期。`--no-sync` / `--all` あり）  
- **done** — ターミナルから消化（末尾 `001` でも可・一意時）  
- **status** — 実行ログ・提案ヘルス・消化率  


### LLM バックエンド

指定バックエンドが使えない場合は **Ollama へ自動フォールバック**します。

| バックエンド | 向いている人 | 準備 |
| --- | --- | --- |
| **Claude Code CLI** | 提案の質を優先 | [Claude Code](https://claude.com/claude-code) ログイン |
| **GitHub Copilot CLI**（既定） | Copilot 利用者 | `npm i -g @github/copilot` → ログイン |
| **Ollama** | ローカル / フォールバック | `ollama pull qwen3:8b` など |
| **GitHub Models** | 無料 API | `KAIZENLOG_API_KEY`（`models:read` PAT） |

いずれも **テキスト生成のみ**。ファイル書き込みは KaizenLog がマーカー区間に限定して行います。

### Claude Code スキル（任意・推奨）

```powershell
kaizenlog skill install
claude -p "/daily-kaizen"
claude -p "/weekly-kaizen"
```

---

## 出力イメージ

ノートに載るのは「事実の Activity Log」と「読める提案」です。

```markdown
## 📊 Activity Log
**合計アクティブ時間**: 6h42m / コンテキストスイッチ: 23回
### カテゴリ別
| カテゴリ | 時間 | 割合 |
| 開発 | 3h10m | 47% |
| AI作業 | 1h25m | 21% |

## 🚀 Kaizen（AIからの改善提案）
### 今日の結論
…
### 明日試すこと
- [ ] KZN-20260722-001: 始業時に25分枠を1件入れる｜PASS: …｜FAIL: …
### 計測上の注意
…
```

F-ID などの機械トークンは検証層に留め、ノート本文は平易な日本語と実行アクション中心です。

---

## できること（圧縮）

| 領域 | コマンド / 機能 |
| --- | --- |
| 日誌・提案 | `run` / `generate` / `advise` / `morning` / `today` / `done` |
| 実験・介入 | `experiment`（同曜日基準・効果量） · `block`（LeechBlock ルール案） |
| 運用 | `doctor` · `status` · `backfill` · 失敗通知 · ヘルスレジャー |
| AI 作業の質 | Claude Code / Codex CLI テレメトリ、リトライ連鎖 |
| 拡張 | `report`（日報）· `prompts`（台帳 PRM-ID・`mark` / `--unhandled`）· `patterns` · `/kaizen-autopilot` |
| 開発 | `eval record` / `eval run`（プロンプト回帰。初回は同梱 `eval/samples/`、自分のデータは `eval record`） |
| 安全 | プライバシーマスク（プロンプト代表文も redact 後に台帳保存）· `advise --dry-run` · マーカー外不変 |

### 測定の限界

- **PC 前景のみ** — スマホ・他デバイス・離席中の行動は未測定。カテゴリ時間の減少はデバイス移行（風船効果）の可能性を排除できない
- **ActivityWatch Android + 同期で拡張可能**（本ツールは未対応）

### 分類のカスタム

```toml
[[categories.rules]]
name = "AI作業"
ai = true
patterns = ["dify", "自社チャットボット"]
```

### 解像度を上げる watcher（任意）

| watcher | 追加されるもの |
| --- | --- |
| [aw-watcher-web](https://github.com/ActivityWatch/aw-watcher-web) | サイト別時間 · `site_minutes` |
| [aw-watcher-input](https://github.com/ActivityWatch/aw-watcher-input) | 集中ブロック · `focus_blocks` |

未導入でも動作します（該当指標が出ないだけ）。`doctor` が案内します。

---

## 開発

```bash
pip install -e ".[dev]"
pytest
```

### ブラウザ AI テレメトリ（オプション）

ChatGPT / Claude.ai / Gemini をブラウザで使う日は、**ローカル専用の拡張**で会話イベントを JSONL に書き出し、`🧠 AI作業の質` に載せられます。

- **仕組み**: DOM 監視 → `Downloads/kaizenlog-browser-ai/YYYY-MM-DD.jsonl`（ネットワーク送信なし・3 ドメイン限定）
- **導入**: [browser-extension/README.md](browser-extension/README.md)（デベロッパーモードで読み込み）
- **設定**: `[aiwork] browser_export_dir`（既定は上記 Downloads 配下）
- **本文保存**: 拡張オプションでオフ可。**ボールトや Downloads をクラウド同期している場合は「本文を保存しない」を推奨**（依頼逐語は画面タイトルより機密性が高い）
- トークン数は取得不能のため捏造しません（文字数のみ別集計）

## ロードマップ（要約）

- [x] AI テレメトリ · 実験ループ · Memory/PASS · 運用パック · Claude/Codex 連携  
- [x] ブラウザ AI（ChatGPT / Claude.ai / Gemini · 拡張 + アダプタ）  
- [ ] Cursor 等その他 AI ログ · [screenpipe](https://github.com/mediar-ai/screenpipe) 連携  

変更履歴は [CHANGELOG.md](CHANGELOG.md) を参照。

## ライセンス

MIT（本ツール）。ActivityWatch は別プロセスとして REST API 経由で利用し、同梱していません。
