# KaizenLog v1.3.1 Trust and Release Design Plan

**Goal:** 前回の評判改善監査で確認した信頼・配布・初回体験の問題を、実装判断に曖昧さが残らない `v1.3.1` 修正仕様へ落とし込み、ユーザー承認後にTDD実装計画へ移行できる状態にする。

**Current baseline:** `C:\develop\KaizenLog\KaizenLog-` / `main` / `8242927a6ec83d356282f84d4ecbcbf1294583de`。既存の未追跡 `grok-desktop-experiment/` は対象外とし、閲覧・変更・ステージを行わない。

**Scope boundary:** この段階では仕様書と計画資料だけを作る。アプリケーションコード、テスト、設定、依存関係、GitHub設定、PyPI、タグ、Release、リモートは変更しない。実装計画は仕様書をユーザーがレビュー・承認した後に作る。

## Goal breakdown

- `v1.3.1` を「評判向上機能」ではなく、安心して配布・試用できる最小の信頼回復リリースとして定義する。
- 壊れたマーカー、dry-run副作用、設定欠落、Claude doctor不整合、LLMなし運用、プライバシー説明、配布物検証を具体的な契約と失敗時挙動に変換する。
- 原子的永続化や公開配布など範囲が広い項目を、同一リリースに必須なものと後続リリースへ送るものに分離する。
- 各要件に、対象ファイル、インターフェース、データフロー、エラー処理、互換性、テスト・受け入れ条件を付ける。
- ユーザー承認後、仕様を小さなTDDタスクへ分解し、依存順・並列化・コミット境界まで含む実装計画を作る。

## Dependencies and parallelizable work

- **Sol / architecture:** リリース境界、互換性判断、CLI契約、仕様統合、優先順位、最終レビューを担当する。
- **Persistence safety track:** `vault.py`、`memory.py`、`stats.py`、`runlog.py`、`experiments.py` の書き込み契約と故障時挙動を読み取り専用で整理する。
- **CLI and privacy track:** `cli.py`、`config.py`、`doctor.py`、`advisor.py`、README/USAGEの外部送信・dry-run・初回設定契約を読み取り専用で整理する。
- **Packaging and test track:** `pyproject.toml`、CI、テスト構成、package-data、タスク登録スクリプト、Basesテンプレート、リリース経路を読み取り専用で整理し、clean-wheel検証の要件を提案する。
- 三トラックは同じファイルを変更せず、仕様候補と証拠だけを返すため並列化できる。Solが重複、矛盾、過剰スコープを解消する。

## Design workflow

- [x] **1. Confirm approved direction and live baseline**
  - 前回提示した `信頼 → 配布 → 初回成功 → 実績公開` の順序に対するユーザー承認を確認する。
  - 現在のHEAD、ブランチ、作業ツリー、既存成果物を再確認する。
  - 時点依存の配布状況は必要最小限だけ再検証する。

- [x] **2. Build decision-complete requirement tracks**
  - 各トラックに明確な範囲、非目標、期待出力、成功条件を渡す。
  - 既存実装・テストを根拠に、API/CLI契約、例外、互換性、検証方法を抽出する。
  - `v1.3.1必須`、`同リリースでは行わない`、`後続候補` を分ける。

- [x] **3. Write the approved design specification**
  - `docs/superpowers/specs/2026-07-17-kaizenlog-v1-3-1-trust-release-design.md` を作成する。
  - アーキテクチャ、コンポーネント、データフロー、エラー処理、テスト、移行、配布ゲート、ロールバックを具体化する。
  - プレースホルダー、矛盾、二義的要件、範囲過大を自己レビューし、その場で修正する。

- [x] **4. Commit and hand off the spec for user review**
  - 仕様書と今回の `PLAN.md` だけを明示的にステージし、既存未追跡ファイルを含めない。
  - 差分とコミット対象を確認してから、仕様書コミットを作成する。
  - 仕様書の要点、未採用事項、レビューしてほしい判断点をユーザーへ提示する。

- [ ] **5. Create the implementation plan after approval**
  - ユーザー承認後に `superpowers:writing-plans` を使用する。
  - 正確な対象ファイル、関数・型・CLI契約、失敗テスト、実行コマンド、期待結果、コミット単位を含める。
  - 依存順を固定し、並列化可能なタスクと統合ゲートを明記する。

## Risks and mitigations

- **仕様が一リリースに広がりすぎる:** ユーザーデータ保護、no-write契約、正しい配布、既存の宣言済みCLI整合だけを `v1.3.1` の必須範囲にする。セットアップウィザード、デモ、成長施策は後続へ送る。
- **後方互換性を壊す:** 既存CLI名と設定キーは維持し、厳格化で挙動が変わる箇所は明示エラーと移行案内を仕様化する。
- **プライバシー説明だけ直して実装が追いつかない:** 文書、初回設定、doctor、実送信経路の同一契約を一つの受け入れ条件として扱う。
- **原子的書き込みの横断実装が過大になる:** 共有ヘルパーの境界を設計し、JSONL追記と全体置換を区別する。全ストアを一度に再設計しない。
- **配布状況が変化する:** PyPI/GitHubの現行状態は仕様作成日に再確認し、結果ではなく再現可能なrelease gateを要件にする。
- **dirty treeの混入:** `git add -A` を使わず、仕様書と `PLAN.md` の正確なパスだけを対象にする。

## Acceptance criteria

- 仕様書が対象HEADと確認済みの再現事実に紐付いている。
- `v1.3.1` の必須範囲と非目標が明確で、一つのリリースとして実行可能である。
- マーカー不整合、dry-run、設定欠落、Claude doctor、`backend=none`、外部送信同意、atomic write、wheel clean-installについて、期待挙動とテストが具体化されている。
- 既存の正常系、設定キー、日誌形式、手書き領域との互換性が明記されている。
- 実装判断を保留する仮置き文言や、挙動を特定しない表現が残っていない。
- 仕様書と `PLAN.md` 以外に変更がなく、`grok-desktop-experiment/` がステージ・コミットされていない。
- 実装計画の作成は、ユーザーが書面仕様をレビュー・承認するまで開始しない。
# qwen3.6:27b KaizenLog機能確認（2026-07-26）

## Goal breakdown

- インストール済みモデル、Ollama、コミット済みreasoning制御を確認する。
- ユーザーデータを使わず、代表的な日次統計からKaizenLogの構造化改善提案を実生成する。
- JSON契約検証、Markdownレンダリング、根拠・最小アクション・PASS/FAILを確認する。
- 回帰テストとGit差分を確認し、外部作業中のdirtyファイルを変更・ステージしない。

## Risks and mitigations

- `run` / `generate` / `advise` CLIは実ボールトへ書き得るため使用しない。
- ActivityWatch、通知、Memory、実験、スケジューラへの書き込みを行わない。
- ローカルOllama以外のネットワークへ接続しない。
- 27B CPU推論は数分かかり得るため、KaizenLog既定の600秒内で判定する。

## Acceptance criteria

- `qwen3.6:27b`がOllamaに存在する。
- `generate_advice()`が空でないKaizen Markdownを返す。
- 出力が根拠、15分以内の行動、翌日のPASS/FAILを含み、保存契約を通過する。
- 全pytestが失敗なしで完了する。
# Ollama恒久化・実データE2E計画（2026-07-26）

## Goal breakdown

- 既定のOllamaフォールバックモデルを、この端末で実在し動作確認できるモデルへ更新する。
- Ollamaが返す修復済みJSONで `proposals` と `actions` の件数だけがずれる場合、安全に1対1へ正規化する。
- 正規化後も根拠ID、測定可能性、短時間データの上限など既存の意味検証は必ず維持する。
- 単体テスト、全テスト、ActivityWatch→Obsidian→Ollamaの実データE2Eで確認する。

## Dependencies and parallelizable work

- 現行のJSON解析、修復、検証、Markdownレンダリングの順序を確認する。
- 先に失敗テストを追加して現象を固定し、その後に最小実装を行う。
- 設定変更の検証と契約正規化の単体テストは独立して確認できる。
- 全テスト合格後にのみ実データE2Eを実行する。

## Risks and mitigations

- 不正な提案を黙って保存する: 正規化対象を件数超過の切り詰めだけに限定し、既存の全意味検証を後段で再実行する。
- 対応関係を誤る: 配列順を保持し、先頭から同数だけ採用する。0件の場合は従来どおり失敗させる。
- ユーザー設定を過度に固定する: 既定値と同梱設定のみ実在モデルへ合わせ、任意の明示設定は尊重する。
- 実データを壊す: マーカー更新前後のハッシュと構造を確認し、対象日のみ再実行する。

## Acceptance criteria and tests

- 件数だけがずれた有効JSONを正規化するテストが修正前に失敗し、修正後に成功する。
- 根拠不一致など件数以外の契約違反は正規化後も拒否される。
- 既定設定と `kaizenlog.toml` が導入済みOllamaモデルを指す。
- 全pytest、構文検査、diff checkが成功する。
- 実データE2Eで正式なKaizen提案が保存され、縮退メッセージが残らない。

# 実環境書き込みE2E（2026-07-26）

## Goal breakdown

- 実ActivityWatchへ専用テストバケットとイベントを書き込み、読戻し後に専用バケットだけを削除する。
- 実ボールトの日次ノート、統計、実験、Kaizen Memoryへ本番コード経路で書き込み、構造と読戻しを検証する。
- KaizenLogの通知経路から識別可能なWindowsテスト通知を実際に送る。
- 専用名のWindowsスケジュールタスクを登録し、オンデマンド実行結果を確認してから専用タスクだけを削除する。
- `qwen3.6:27b`を指定した一時設定で、ActivityWatch収集から助言・Memory保存までを確認する。

## Dependencies and parallelizable work

- 書き込み前に実設定、対象日、既存ノート／Memory／統計／実験のパスとハッシュを記録する。
- ActivityWatch専用バケット試験、通知試験、スケジューラ試験は相互に独立している。
- 実ボールト更新はActivityWatch読取確認後、27B助言保存は日次統計生成後に実施する。
- 全試験後にGit差分、専用外部リソースの削除、保存成果物の読戻しを統合確認する。

## Risks and mitigations

- 実データを上書きする: 対象ファイルを事前バックアップし、KaizenLogのマーカー更新経路だけを使って手書き領域を保持する。
- ActivityWatchを汚す: 衝突しない専用バケットIDを使い、読戻し後に完全一致するIDだけを削除する。
- スケジューラへ不要な常設タスクを残す: 専用タスク名を事前確認し、実行結果確認後に完全一致するタスクだけを削除する。
- 通知を誤認する: タイトルと本文にE2E試験・日時を明記する。
- 27B推論が長時間化する: Ollamaの600秒タイムアウト内で待ち、プロセスと保存結果を確認する。
- dirty treeを混入する: 既存変更へ触れず、コミットする場合もユーザーが明示した対象だけを個別ステージする。

## Acceptance criteria and tests

- ActivityWatch専用イベントがAPIで読戻せ、専用バケットが試験後に存在しない。
- 実ボールトの対象日ノートに生成マーカーがあり、統計・実験・Memoryが各ローダーで読める。
- 27B助言がフォールバック文ではなく、根拠・最小行動・PASS/FAILを含む。
- Windows通知コマンドが成功終了する。
- 専用スケジュールタスクが登録・実行され、`LastTaskResult = 0`を確認後に削除される。
- 既存の外部変更を含めず、対象試験以外のActivityWatchバケット／スケジュールタスクを変更しない。
