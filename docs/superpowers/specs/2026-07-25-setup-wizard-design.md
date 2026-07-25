# KaizenLog 導入ウィザード（`kaizenlog setup`）設計

**ステータス:** ユーザーレビュー向け提案版  
**日付:** 2026-07-25  
**ベースライン:** `main` @ `1.5.0rc1`（`34fc1b4` 近傍）  
**決定の根拠:** 動作確認で ActivityWatch 未導入・設定パスのズレ・Ollama モデル不一致・スキル未配置により E2E が止まった。ユーザー方針は「対話ウィザード」「検出優先＋不足だけ質問」「winget 等で AW 自動インストール試行」「`kaizenlog setup` 新コマンド」。

## 1. 概要

新規ユーザーが次の一本で、設定の確定から依存関係の充足、初回 `doctor` まで到達できるようにする。

```text
pipx install kaizenlog   # または pip install -e .
kaizenlog setup
kaizenlog doctor         # setup 末尾でも自動実行
```

検出できるものは自動提案し、未確定項目だけ対話する。外部ソフト（ActivityWatch）のインストールとタスクスケジューラ登録は、ユーザー確認後にのみ実行する。`--yes` でも winget / タスク登録は別フラグ無しでは走らせない。

## 2. 目標と非ゴール

### 2.1 目標

1. **`kaizenlog setup`** が導入の正規経路になる。
2. 設定の既定書き込み先を **`%APPDATA%\kaizenlog\config.toml`**（非 Windows: `~/.config/kaizenlog/config.toml`）にする。
3. ボールト・LLM・ActivityWatch・Claude スキル・夜間タスクを **フェーズ順** に処理し、各フェーズは冪等。
4. Ollama 利用時は **インストール済みモデルを検出し config に書く**（存在しない `qwen3:8b` 固定を避ける）。
5. ActivityWatch 未導入時は、確認後に **winget インストール試行 → 起動 → API 待機**。失敗しても config 等の部分成功は保持する。
6. 終了時に `doctor` 相当を実行し、残課題と次コマンド（`kaizenlog generate` / `run`）を明示する。
7. `init-config` は互換維持しつつ、同じ設定 writer を共有し既定先を AppData にする。

### 2.2 非ゴール（本仕様の範囲外）

- ActivityWatch なしでの「JSONL 即価値」オンボーディング（別イテレーション）。
- GUI / Web ウィザード。
- PyPI 公開や配布パイプラインの変更。
- macOS / Linux での ActivityWatch 自動インストール（検出・手動案内のみ。winget は Windows 限定）。
- 週次 / Autopilot タスクの自動登録（日次 `KaizenLog Daily` のみ。週次は手動案内）。
- `grok-desktop-experiment/` への変更。

## 3. CLI 契約

### 3.1 `kaizenlog setup`

```text
kaizenlog setup
    [--config PATH]        # 読み書きする設定パス（省略時は default_config_path()）
    [--vault PATH]         # vault を対話スキップ
    [--yes]                # 安全な既定提案を確認なしで採用
    [--force]              # OK 済みフェーズも再確認
    [--skip-aw]            # AW フェーズをスキップ
    [--skip-task]          # タスク登録フェーズをスキップ
    [--skip-skills]        # skill install をスキップ
    [--install-aw]         # 非対話でも winget 試行を許可（--yes と併用時）
    [--register-task]      # 非対話でも日次タスク登録を許可（--yes と併用時）
    [--time HH:MM]         # 日次タスク時刻（既定 21:30）
```

**終了コード**

| 条件 | code |
| --- | --- |
| 必須フェーズ（設定先・ボールト）成功、かつ doctor に ❌ なし | 0 |
| 部分成功（例: AW 失敗）だが設定は書けた | 1 |
| 必須フェーズ失敗（ボールト不正・設定書き込み失敗等） | 2 |

### 3.2 `kaizenlog init-config`（互換）

```text
kaizenlog init-config [--output PATH]
```

- 既定出力: `default_config_path()`（AppData）。
- 既存ファイルは上書きしない（exit 1、メッセージで setup を案内）。
- 雛形本文は setup と同じ `CONFIG_TEMPLATE` 生成器を使う。

### 3.3 フラグと危険操作

| 操作 | 対話モード | `--yes` のみ | `--yes` + 明示フラグ |
| --- | --- | --- | --- |
| 設定の作成・vault/LLM の書込 | Y/n | 自動 | 自動 |
| skill install | Y/n | 自動 | 自動 |
| winget で AW インストール | Y/n | **しない** | `--install-aw` で実行 |
| タスク登録 | Y/n | **しない** | `--register-task` で実行 |
| 既存 config の破壊的再作成 | 明示確認必須 | **しない**（`--force` + 対話） | 対話必須のまま |

## 4. 設定パスの信頼境界

現状の問題: `init-config` が CWD に `kaizenlog.toml` を書き、探索も CWD 優先のため、手動実行とタスクスケジューラで別設定を拾う。

### 4.1 探索優先順位（`find_config_file`）

1. 明示 `--config`（存在しなければ `FileNotFoundError`）
2. 環境変数 `KAIZENLOG_CONFIG`（指定されて存在しなければ fail-closed）
3. `default_config_path()`（AppData / XDG）
4. **移行期間のみ**: CWD の `kaizenlog.toml` または `config.toml` があれば **使用しつつ doctor/setup で警告**

`config.toml`（CWD ルートの一般名）は誤検出リスクがあるため、移行期間後の削除候補として docs に明記する。本仕様では後方互換のため残す。

### 4.2 書き込み

- `default_config_path()` を追加。親ディレクトリを作成する。
- setup / init-config は **原子的書き込み**（一時ファイル → `os.replace`）を使う。
- 既存 TOML がある場合は **キー単位 merge**（ユーザー追記の `[[categories.rules]]` 等を消さない）。LLM/general のウィザード管理キーのみ更新。

### 4.3 テンプレート既定値

setup が書く新規設定の推奨初期値:

| キー | 値 | 理由 |
| --- | --- | --- |
| `general.vault_dir` | ユーザー確定パス | 必須 |
| `llm.backend` | 検出結果（下記） | サイレント cloud 送信を避けるため、検出ゼロなら `none` |
| `llm.openai_compatible.model` | 検出した実在モデル、またはプレースホルダ | 未 pull モデル固定をやめる |
| `llm.fallback_to_local` | `true` | 既存挙動 |

検出ゼロで backend を `none` にするのは、信頼リリース設計（v1.3.1 系）と整合する。ウィザードが明示的に CLI/Ollama を選んだ場合のみ cloud/local 生成を有効化する。

## 5. フェーズ仕様

各フェーズは `(status, messages, actions)` を返す。status は `ok` / `skipped` / `changed` / `failed`。

### Phase 0 — 前提

- OS、Python 版（情報表示のみ）。
- 既存 config の有無:
  - なし → Phase 1 で新規作成。
  - あり → 「この設定を更新する」が既定。`--force` 時のみ「再作成」を選択肢に出す（再作成はバックアップ `.bak` を残す）。

### Phase 1 — 設定先

- 書き込み先を表示（AppData 既定、または `--config`）。
- 雛形が無ければ作成。あれば merge モードで続行。

### Phase 2 — ボールト

**検出候補（存在し書込可能なディレクトリ）**

1. 既存 config の `vault_dir`
2. 環境・既知パス例: `~/Documents/Obsidian Vault`, `C:/develop/obsidian/*` の直下フォルダ（Windows）
3. Obsidian の `obsidian.json` から vault 一覧が読める場合はそれも候補（読めなければスキップ。失敗しても致命ではない）

**対話**

- 候補が1つ → 既定採用（`--yes` で無確認）。
- 複数 → 番号選択。
- ゼロ → パス入力。存在しない / 書込不可なら再入力。

**副作用:** config に `vault_dir` を書き、`daily_notes_dir` 等はテンプレ既定のまま（既存値は維持）。

### Phase 3 — LLM

**検出（副作用なし）**

| 信号 | 意味 |
| --- | --- |
| `shutil.which("claude")` | Claude Code CLI |
| `shutil.which("copilot")` | Copilot CLI |
| `GET {base_url}/models` 成功 | Ollama 等 OpenAI 互換 |
| モデル id 一覧 | 実在モデル |

**自動選択優先順（上から最初に使えるもの）**

1. `claude-code-cli`
2. `copilot-cli`
3. `openai-compatible`（検出モデルから推奨を1つ選ぶ）
4. `none`

**Ollama モデル推奨ヒューリスティック**

1. 一覧に config 既存 model があればそれを維持。
2. なければ優先トークンを順に部分一致: `qwen3`, `qwen`, `gemma`, `llama`, `mistral`, `phi`。
3. それでも無ければ chat 向けでない `nomic-embed` / `embed` を除外した先頭。
4. 全部 embed のみ → backend は `none` 相当扱いで警告（埋め込み専用では advise 不能）。

対話モードでは提案を表示し変更可。`--yes` は提案を採用。

### Phase 4 — ActivityWatch

**検出順**

1. `GET {aw_base_url}/api/0/buckets/` 成功 → ok。
2. 一般的な exe パス / PATH の `aw-qt` を探索。見つかれば「起動しますか？」→ 起動後ポーリング（既定 60 秒、2 秒間隔）。
3. 未インストール（Windows）:
   - 対話: winget で入れますか？
   - 非対話: `--install-aw` があるときのみ実行。
   - package id は実装時に `winget search activitywatch` で確認し、定数化（例: `ActivityWatch.ActivityWatch`）。id 解決失敗時は公式 URL のみ表示。
4. install 後に起動 + ポーリング。
5. 失敗: 手動手順（公式 DL、起動、doctor 再実行）を表示。status=`failed` だが setup は継続。

**非 Windows:** winget は使わない。接続失敗時はインストール URL と起動確認のみ。

**オプション watcher（aw-watcher-web / input）:** 本ウィザードでは **案内のみ**（必須にしない）。doctor の既存警告を流用。

### Phase 5 — スキル

- 既存 `skill_manager` / `cmd_skill` の doctor 相当で未導入・差分を検出。
- 未導入なら確認後 `install`（`--force` はユーザーが差分上書きを望んだときのみ）。
- vault 未確定ならこのフェーズは failed/skipped。

### Phase 6 — 夜間タスク（Windows）

- タスク名 `KaizenLog Daily` の有無を検出。
- 未登録なら確認後、パッケージ同梱または `scripts/register-task.ps1` 相当を実行。
- **作業ディレクトリ:** 設定が AppData のみに依存するよう、タスクの WorkingDirectory は任意でよいが、**必ず `kaizenlog` が PATH で解決できること**を事前チェック。editable 開発環境では venv の Scripts を使う必要があり得る → setup は「どの `kaizenlog` を登録するか」を `sys.executable` / `shutil.which` から表示し、register スクリプトに渡す。
- 週次タスクは登録しない。成功メッセージ末尾で `-Weekly` の手動例を1行出す。

### Phase 7 — 検証と次の一手

- `run_doctor(cfg, config_path)` を実行し全文表示。
- サマリ:
  - 変更した項目一覧
  - ❌ が残る場合の最短修復
  - 次: `kaizenlog generate` →（データが溜まったら）`kaizenlog advise` / `kaizenlog run`

## 6. モジュール分割

| モジュール | 責務 | 副作用 |
| --- | --- | --- |
| `src/kaizenlog/setup.py` | フェーズ orchestration、CLI 入口 `cmd_setup`、プロンプト I/O | あり（各フェーズ経由） |
| `src/kaizenlog/setup_detect.py` | vault / LLM / AW / winget / タスク / Obsidian 候補の検出 | **なし**（起動・install は setup 側） |
| `src/kaizenlog/config.py` | `default_config_path`, find/load 優先順位、atomic write/merge helper | ファイル I/O |
| `src/kaizenlog/cli.py` | `setup` サブコマンド登録、`init-config` 引数拡張 | なし |
| 既存 `doctor.py` | Phase 7 | なし |
| 既存 `skill_manager.py` | Phase 5 | ファイル配置 |
| `scripts/register-task.ps1` または resources 版 | Phase 6 | タスク登録 |

**プロンプト I/O 抽象:** `SetupUI` プロトコル（`confirm(msg, default=True) -> bool`, `choose(msg, options) -> int`, `ask_path(msg) -> Path`, `print`）。テストは FakeUI を注入。TTY でない stdin で対話が必要な場合は、不足フラグを示すエラーで exit 2（`--yes` と必要な明示フラグで非対話完走可能にする）。

## 7. エラー処理と部分成功

- Phase 1–2 失敗 → 以降スキップ、exit 2。
- Phase 3 で backend=`none` は失敗ではない（警告）。
- Phase 4 失敗 → 続行、最終 exit は 1（doctor に ❌ が残るため）。
- Phase 5–6 失敗 → 続行、メッセージに手動コマンド。
- winget / 外部プロセス: タイムアウト（install 最大 10 分）、stdout/stderr を要約表示。ユーザー中断（Ctrl+C）は途中まで書いた config を残し exit 130 相当。

## 8. テスト計画

| テスト | 内容 |
| --- | --- |
| `test_setup_detect.py` | PATH / HTTP モックで LLM 優先順・モデル推奨・AW URL |
| `test_setup_config_path.py` | default path、init-config 出力先、CWD 警告、merge が rules を消さない |
| `test_setup_wizard.py` | FakeUI で vault 選択・`--yes` 経路・winget がフラグ無しで呼ばれない |
| `test_setup_partial.py` | AW 失敗でも vault が書かれ exit 1 |
| 既存 doctor / skill / cli contracts | 回帰 |

実 winget インストールは CI で行わない。

## 9. ドキュメント変更

- **README セットアップ:** 最短経路を `install → setup → doctor →（AW 稼働後）run` に置換。
- **USAGE.md:** 手動ステップを「ウィザードが失敗したとき」節に再配置。
- **config.example.toml / CONFIG_TEMPLATE:** コメントで AppData 既定と `kaizenlog setup` を案内。model の例は「setup が実在モデルを書く」旨を追記。
- doctor の AW エラー文に `kaizenlog setup` への誘導を1行追加。

## 10. 受け入れ基準

1. クリーンな環境（または config 削除後）で `kaizenlog setup --vault <writable> --yes --skip-aw --skip-task --skip-skills` が exit 0 または doctor 上の LLM/AW 以外で致命なし、かつ AppData（または `--config`）に vault が書かれる。
2. Ollama に `gemma4:latest` のみある環境で setup が model に実在 id を書く（デフォルト `qwen3:8b` のまま残さない）。
3. `--yes` だけで winget とタスク登録が走らない。
4. 対話で AW インストールを拒否しても config / vault は保持され exit 1。
5. `init-config` が既定で AppData に書き、既存を壊さない。
6. 単体テストがモックのみで上記契約を固定する。
7. README の最短導入が setup を指す。

## 11. 実装順序（計画フェーズへの引き継ぎ）

1. config: `default_config_path` / find 順序 / atomic write / init-config 出力先
2. setup_detect（純粋関数 + テスト）
3. setup orchestration + CLI
4. AW winget / 起動（Windows 分岐）
5. skill + task 連携
6. doctor メッセージと README/USAGE
7. 全体回帰 `pytest`

## 12. リスクと緩和

| リスク | 緩和 |
| --- | --- |
| winget package id の変更・ロケール差 | 検索フォールバック + 手動 URL |
| タスクが別 config を読む | AppData 既定 + 登録時に which kaizenlog を表示 |
| クラウド LLM を黙って有効化 | 検出ゼロは `none`。CLI 検出時はユーザー環境に既にある前提 |
| Obsidian パス誤検出 | 候補提示のみ。確定はユーザーまたは `--vault` |
| CWD 設定の移行 | 警告表示。削除は将来バージョン |

## 13. 決定ログ（ブレインストーミング）

| 項目 | 決定 |
| --- | --- |
| ゴール | 対話ウィザード |
| 自動化 | 検出優先、不足だけ質問 |
| AW | winget 等で自動インストール試行 |
| 形 | `kaizenlog setup` 新コマンド |
| 危険操作 | `--yes` でも winget/タスクは明示フラグ必須 |
| 週次タスク | 自動登録しない |

---

**次のステップ:** 本仕様のユーザー承認後、`writing-plans` に従い実装計画を作成する。
