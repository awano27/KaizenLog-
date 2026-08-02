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
# 第12弾 日次結果品質改善プロンプト作成（2026-07-26）

## Goal

- 実運用で確認した内部プレースホルダー漏れ、観測期間混在、エラー数の過大な見え方、根拠とアクションの不一致を、既存の第10弾と重複しない実装指示へ落とす。
- 進行中の第10弾・第11弾変更を保持し、アプリコードは変更せず新規プロンプト1ファイルだけを作成する。

## Risks and acceptance

- 第10弾の計測可能指標ガードを再実装させない。
- 実在する関数・ファイル・テスト経路を指定し、曖昧な「品質向上」で終わらせない。
- 内部表現禁止、観測窓表示、エラー分類、提案ゼロ許容、ゴールデンテストを独立した受け入れ条件にする。
- 実LLM、ActivityWatch、Obsidian、タスクスケジューラを自動テストから呼ばせない。

# 現行コードベースUX監査と改善プロンプト作成（2026-07-27）

## Goal breakdown

- 現在の `main`、README、CLI、生成されるObsidianノート、既存UX改善プロンプト、テストから、実装済みの主要ユーザーフローと未解決の摩擦を特定する。
- 初回導入、日次実行、次アクション消化、週次振り返り、障害回復の各導線を、コード証拠と今回取得する実画面・スクリーンショットに結び付けて評価する。
- 構造上の問題と見た目だけの問題、確認済み事実と推測、UX課題とアクセシビリティ上のリスクを分離する。
- 既存の第8弾〜第12弾および最新コミットと重複しない改善候補を優先度順に絞る。
- 実装担当が追加の設計判断をせず着手できるよう、対象ファイル、非対象、変更契約、受け入れ条件、テスト、手動確認を含む日本語の改善指示プロンプトを新規1ファイルとして作成する。

## Dependencies and parallelizable work

- **Sol / integration:** 実リポジトリ、dirty境界、対象ユーザー導線を確定し、ローカルアプリを安全に起動してブラウザ監査を行い、全証拠を統合する。
- **CLI and flow track:** `README.md`、`docs/USAGE.md`、`src/kaizenlog/cli.py`、`src/kaizenlog/doctor.py` から初回導入・日次操作・回復導線を読み取り専用で整理する。
- **Rendered-note track:** `src/kaizenlog/report.py`、テンプレート、表示関連テストから、ユーザーがObsidian上で見る情報階層と操作可能性を読み取り専用で整理する。
- **Regression and overlap track:** 既存のUX改善プロンプト、直近コミット、テストを照合し、すでに解消済みの提案と安全な検証経路を整理する。
- 三つの読取トラックは並列化できる。Solが現行HEADとブラウザ証拠に照らして採否を判断し、重複・過剰スコープを除外する。

## Risks and mitigations

- 既存のdirty treeを混入する: `PLAN.md` の本節と新規プロンプト以外は変更せず、既存未追跡ファイル、`grok-desktop-experiment/`、ユーザーデータ、設定を対象外にする。
- 過去監査を現行事実として扱う: 過去メモは探索起点に限定し、UX判断は今回の現行コード、テスト、実画面だけを根拠にする。
- CLI製品をWebサイトとして誤評価する: READMEの公開ホーム画面、ターミナル出力、Obsidian生成物を別サーフェスとして扱い、主要成果物である日次ノートを中心に評価する。
- 実データや外部サービスへ副作用を出す: ActivityWatch、Ollama、Obsidian実vault、通知、タスクスケジューラを使わず、ヘルプ、デモ、一時ディレクトリ、モック経路だけで確認する。
- 既存改善との重複: 第8弾〜第12弾と直近コミットの変更契約を先に照合し、未解決または新たに露出した摩擦だけを採用する。
- スクリーンショットだけでアクセシビリティ適合を断定する: 視覚証拠、DOM/キーボード確認、コード上のリスク、未確認範囲を分けて記載する。

## Acceptance criteria and tests

- 監査対象の主要導線が番号付きで列挙され、各導線に現行コードまたは今回のスクリーンショット根拠がある。
- 強み、UXリスク、アクセシビリティリスク、確認限界が分離されている。
- 改善候補は重要度・ユーザー影響・実装規模を踏まえて絞られ、既存プロンプトや実装済みコミットと重複しない。
- 新規プロンプトが目的、現状証拠、スコープ、非目標、対象ファイル、具体的変更、互換性、テスト、手動確認、完了報告形式を含む。
- 自動テストは実LLM、ActivityWatch、Obsidian実vault、通知、タスクスケジューラを呼ばない契約になっている。
- 生成プロンプト内のファイル名、関数名、CLI名、テスト経路が現行HEADに実在するか静的照合されている。
- 既存変更を保持し、アプリケーションコード、設定、ユーザーデータ、リモート、Git履歴を変更していない。

# 現行チェックアウト動作確認（2026-07-28）

## Goal breakdown

- `C:\develop\KaizenLog\KaizenLog-` の現行HEADとdirty treeを保護したまま、Pythonコード、全自動テスト、パッケージCLIの起動性を確認する。
- 外部サービスや実ユーザーデータへ書き込まず、ActivityWatch・Ollama・Obsidian設定については診断可能な読取経路まで確認する。
- 失敗があれば、既存変更との関係を含めて再現コマンドと原因範囲を報告する。

## Dependencies and parallelizable work

- Git状態、Python/.venv、設定済みCLIの確認を先に行う。
- `compileall`、CLIヘルプ、全pytestは互いに独立しているが、出力を明確にするため順次実行する。
- 全pytestは既知の権限問題を避けるためOS一時ディレクトリを `--basetemp` に使う。

## Risks and mitigations

- dirty treeを壊す: 既存の変更・未追跡ファイルを編集、削除、stash、stageしない。
- 実データへ副作用を出す: `run`、`generate`、`advise`、スケジューラ登録は行わず、ヘルプと診断の読取経路に限定する。
- キャッシュ生成を混入する: repo外の一時ディレクトリをpytestに指定し、終了後のGit状態を初期状態と比較する。
- 外部サービス到達だけをE2E成功と誤認する: 今回は副作用なしの基本動作確認として報告し、実データE2Eは未実施と明記する。

## Acceptance criteria and tests

- `.venv` のPythonで `compileall` が終了コード0。
- `python -m kaizenlog.cli --help` が終了コード0で主要コマンドを表示する。
- 全pytestが終了コード0で、passed件数と所要時間を取得できる。
- `doctor` が終了コードと診断内容を返し、ハングや未処理例外がない。
- 検証後のGit差分が今回のPLAN追記以外に増えていない。

# 実日誌生成による価値監査（2026-07-28）

## Goal breakdown

- 実設定で当日の日誌を生成し、KaizenLogが提供する成果物の内容と実用価値を現行CLIで確認する。
- 既存ノートの手書き領域を保持し、生成前後の状態、生成マーカー、統計、次に取るべき行動の明確さを評価する。
- 「機能が起動する」と「ユーザーが十分な価値を感じる」を分離し、継続利用に必要な改善を優先順位付きで示す。

## Dependencies and parallelizable work

- README、CLIサブコマンド、空状態関連テストから期待導線を確定する。
- 実設定による日誌生成と、コード・テスト証拠の収集は独立して進められる。
- 最終判断は画面証拠、実行結果、空状態の振る舞いを統合して行う。

## Risks and mitigations

- 実日誌の手書き領域を壊す: 生成前の対象ファイルを記録し、通常の管理マーカー更新経路だけを使用する。
- ActivityWatchデータ不足を価値と誤認する: 収集期間とイベント量を確認し、空データや部分日データを明示する。
- コード上の機能数を価値と誤認する: 初回ユーザーが見える成果、次アクション、待ち時間、エラー回復で評価する。
- 既存dirty treeを混入する: PLAN追記以外の既存変更、設定、ユーザーデータ、Git履歴を変更しない。

## Acceptance criteria and tests

- 初回導線を番号付きで再現し、各段階の健康状態を記録する。
- 生成された日誌、統計、次アクションを読み戻し、内容と構造を確認する。
- 少なくとも1つの実生成画面または成果物を今回取得・確認した証拠として残す。
- 十分な価値があるかを、根拠・限界・改善優先度付きで明確に判定する。
- ActivityWatchは読取、実ボールトは当日日誌の通常生成範囲に限定し、スケジューラや通知へ変更を行わない。

# 改善後の再動作確認・前回比較（2026-07-29）

## Goal breakdown

- `c9b86e0` までの改善後コードを、2026-07-28の確認結果と同じ観点で再検証する。
- 全自動テスト、doctor、実ActivityWatchからの日誌生成、LLM改善提案、翌日アクション引継ぎを確認する。
- 前回の基準値（501 tests、generate 13.7秒、advise 264.0秒）と今回結果を比較し、機能・品質・待ち時間・読者価値の改善有無を判定する。
- Obsidianで生成結果を表示し、今回取得したスクリーンショットを監査証拠として保存する。

## Dependencies and parallelizable work

- Git変更範囲と対象コマンドの現行契約を先に確定する。
- compileall、全pytest、doctorは相互に独立している。
- 実日誌生成後にのみ、改善提案、翌日引継ぎ、Obsidian画面確認を行う。
- 最終比較はCLI終了コード、所要時間、生成ファイル、統計JSON、画面証拠を統合する。

## Risks and mitigations

- 既存日誌の手書き領域を壊す: 通常の管理マーカー更新経路だけを使い、生成前後のハッシュとマーカー数を確認する。
- 当日データが少ない: 収集対象時間とイベント量を明示し、前日の絶対値と単純比較しない。
- LLM処理が長時間化する: プロセスと日誌更新を追跡し、呼出し側タイムアウトと実処理成功を分離する。
- 改善をコミット名だけで判断する: 現行実行結果と生成されたユーザー向け文面だけを比較証拠にする。
- dirty treeを混入する: 本PLAN追記以外を編集・削除・stage・commit・pushしない。

## Acceptance criteria and tests

- `.venv` のPythonでcompileall、全pytest、doctorが終了コード0。
- 2026-07-29の日誌にactivity/adviceマーカーが各1組あり、統計JSONと整合する。
- 新規アクションが機械判定可能なPASS/FAILを持ち、翌日ノートへ引き継がれる。
- Obsidian上のActivity Logと改善提案を今回のスクリーンショットで確認する。
- 前回との差を「改善」「同等」「悪化」「未比較」に分け、数値と成果物で説明する。
- 検証後のGit変更が本PLAN追記以外に増えていない。

# 2026-07-29 センシティブ操作ログの限定削除（2026-07-29）

## Goal breakdown

- 2026-07-29の日誌タイムラインに表示されたセンシティブなブラウザ操作だけを特定する。
- 日誌、統計JSON、ActivityWatch該当イベントを削除前にバックアップする。
- 完全一致するActivityWatchイベントIDだけを削除し、他の日付・アプリ・時間帯・バケットを保持する。
- 元データ削除後に当日の日誌・統計を再生成し、対象タイトルが残っていないことを確認する。

## Dependencies and parallelizable work

- ActivityWatchの現行削除API契約を公式資料または実サーバー仕様で確認する。
- 日誌・統計の対象ブロックとActivityWatchイベントを、時刻、アプリ、タイトルで突合する。
- バックアップ完了と対象ID確定後にのみ削除する。
- 削除後はAPI読戻し、再生成、ファイル検索、非対象イベント件数比較を順に行う。

## Risks and mitigations

- 範囲外イベントを消す: 時間範囲一括削除を避け、完全一致するイベントIDだけを対象にする。
- 復元不能になる: API応答原文、日誌、統計JSON、対象一覧とハッシュを時刻付きバックアップへ保存する。
- 日誌だけ消して再生成で復活する: ActivityWatch元イベントを先に削除し、その後に通常経路で再生成する。
- 既存dirty treeを混入する: ユーザーのPrompt Ledger関連変更を編集・stage・commitせず、外部データとPLAN追記だけを対象にする。
- 改善提案との整合性が崩れる: Activity Log・統計を再生成後、必要に応じて当日adviseの出典整合性を確認する。

## Acceptance criteria and tests

- バックアップに日誌、統計JSON、削除対象イベントJSON、対象ID一覧、SHA-256が揃う。
- 削除対象が2026-07-29、ブラウザアプリ、確認済みセンシティブタイトルに限定される。
- 削除APIが対象IDごとに成功し、再取得で対象IDが0件になる。
- 同一バケットの非対象イベント数と非対象イベントIDが保持される。
- 再生成した日誌と統計JSONに対象タイトル・対象イベントが残らない。
- 全pytestまたは対象回帰テスト、`git diff --check`が成功する。

# GitHub README 再設計（2026-07-29）

## Goal breakdown

- `oil-oil/beautify-github-readme` を Codex スキルとして導入し、手順と制約を確認する。
- 現行コード、CLI、設定、パッケージ情報、既存画像資産を根拠に、KaizenLog の価値が初見で伝わる README 設計を作る。
- ユーザー承認済みの設計に基づいて `README.md` を再作成し、機能・導入手順・安全境界を現行実装と一致させる。
- README のローカルリンク、記載コマンド、Markdown構造、差分を検証し、アプリ実装や公開状態を変更しない。

## Dependencies and parallelizable work

- 実リポジトリ、dirty tree、既存 README/PLAN、直近コミットを先に確認する。
- 上流スキルの調査・導入と、KaizenLog のREADME根拠収集は独立して進められる。
- READMEの構成案、見せ方、情報密度は、現状調査後に2〜3案を比較し、ユーザー承認を得て確定する。
- 編集後のリンク検査、CLIヘルプ照合、Markdown静的検査は並行可能だが、最終レビューで統合する。

## Risks and mitigations

- READMEが実装より先走る: CLIヘルプ、`pyproject.toml`、設定例、現行ソースを一次根拠にし、未公開・未実装の機能を事実として書かない。
- 既存のユーザー作業を壊す: 未追跡 `docs/HANDOFF.md` を変更、stage、commitせず、今回の対象を `README.md`、承認済み設計書、必要なPLAN追記に限定する。
- 装飾過多で可読性を落とす: 外部バッジや画像は情報価値、保守性、アクセシビリティを基準に絞り、長い機能列挙を避ける。
- 導入済みスキルや外部リンクが不正確になる: インストール先、上流README、ライセンス、リンク到達性を確認する。
- ドキュメント作業が公開・実行副作用へ広がる: push、release、PyPI公開、実データ処理、ActivityWatch/LLM/Obsidian実行は行わない。

## Acceptance criteria and tests

- `beautify-github-readme` がローカルのCodexスキルディレクトリへ導入され、`SKILL.md` を読み取れる。
- READMEの設計について、目的、対象読者、構成、ビジュアル方針、非目標がユーザー承認済みである。
- `README.md` が、30秒で価値を理解できる冒頭、最短導入、主要ワークフロー、出力例、安全・プライバシー、開発導線を持つ。
- README内の相対リンクと画像参照がすべてリポジトリ内で解決し、記載CLIが現行 `--help` と矛盾しない。
- `git diff --check` とREADME静的検査が成功し、最終 `git status --short` に対象外ファイルの変更が増えていない。

# 現行コードベース準拠README再作成（2026-07-30）

## Goal breakdown

- 現在のローカルHEAD、dirty tree、CLI、設定、配布メタデータ、ドキュメント、README資産を再調査する。
- 現行READMEの各主張を実装根拠と照合し、古くなった機能・コマンド・安全境界・導入手順を特定する。
- 前回のDaily Ledgerビジュアルを現行プロダクトの中心価値に合わせて再評価し、ユーザー承認後にREADMEと必要なSVGだけを更新する。
- GitHub幅とモバイル幅、リンク、SVG、CLIヘルプ、テストで最終成果物を検証する。

## Dependencies and parallelizable work

- 実リポジトリとdirty treeを確定してから、README、CLIヘルプ、`pyproject.toml`、設定、直近コミットを読む。
- コマンド／配布契約と、プロダクト価値／出力例の根拠収集は独立して確認できる。
- README構成とビジュアル資産は同じ承認済みストーリーに従い、本文確定後に整合させる。
- 静的監査、ローカルリンク、SVG XML、デスクトップ／モバイル表示、全pytestは実装後に統合する。

## Risks and mitigations

- ローカル実装よりREADMEが先走る: READMEの各主張を現行コード、CLIヘルプ、設定、テストへ結び付ける。
- 既存作業を混入する: 開始時の`git status`を記録し、ユーザー所有の変更を編集・stage・commit・pushしない。
- 前回デザインを惰性で流用する: 現在の主要価値、初回成功、実出力が変わっていないかを確認してから採用する。
- 配布状態を誤記する: ローカルのパッケージメタデータと必要に応じた公開状態を分けて確認する。
- 承認前に実装する: brainstormingの設計ゲートを守り、設計書のレビュー後にのみREADMEを変更する。

## Acceptance criteria and tests

- 対象読者、中心価値、一次証拠、最初の成功、ビジュアル方向がユーザー承認済み。
- READMEの主要コマンド、設定、バックエンド、安全境界、バージョンが現行実装と一致する。
- READMEの相対リンク、画像参照、SVG構造がすべて解決する。
- 900pxと360px相当で主要情報が読み取れ、画像がなくても本文だけで意味が通る。
- README監査、`git diff --check`、CLIヘルプ照合、全pytestが成功する。
- 開始時の対象外dirty filesが変更・stage・commit・pushされていない。

# 改善ループ5機能の利用可能化とREADME統合（2026-07-30）

## Goal breakdown

- `main` / `origin/main` のコミット `05a408c` に取り込まれた Loop Tax、Prompt ROI、`handoff`、`coach`、`abtest` を正式な現行機能として扱う。
- 利用確認で再現した `abtest new --help` / `abtest finish --help` のクラッシュを、回帰テストを先に追加して最小修正する。
- READMEの中心メッセージを「AIとの仕事を、実測で調教する。」へ更新し、専門名より先に利用者の変化を説明する。
- M365 Copilot向けChrome拡張は未実装の将来構想として分離し、現行機能と誤認させない。

## Dependencies and parallelizable work

- 設計書を承認内容に合わせて確定し、コミットしてから実装計画へ進む。
- CLI修正は `argparse` のヘルプ文字列だけを対象とし、既存の実験計算や保存契約には触れない。
- README本文と純SVG資産は同じ Measure → Teach → Verify ストーリーで更新する。
- CLI回帰テスト、全pytest、READMEリンク監査、SVG XML検証、表示確認を最後に統合する。

## Risks and mitigations

- すでに正式取り込み済みの機能をPreviewと誤記する: HEADと`origin/main`の一致を根拠にAvailableとして記載する。
- M365対応を実装済みと誤認させる: `Next / Planned`、未実装、自動収集・自動反映なしを近接表示する。
- `abtest`修正で本体挙動を変える: 失敗するヘルプ回帰テストを先に作り、`%`の表示契約だけを直す。
- 既存作業を混入する: 対象ファイルだけを編集・stageし、`.superpowers/`の一時成果物はコミットしない。
- 外部副作用を起こす: ActivityWatch、Obsidian、実LLM、実日誌生成、公開・pushを検証対象に含めない。

## Acceptance criteria and tests

- `abtest new --help`と`abtest finish --help`が終了コード0で、`+N%`の説明を表示する。
- 5機能がREADMEで「何ができるか → コマンド名」の順に説明され、正式な現行機能として扱われる。
- M365 Copilot Chrome拡張が将来構想であり、現在未対応だと同じセクション内で分かる。
- READMEとSVGがClosed Loop方向、Daily Ledger配色、純SVG、モバイル可読性を満たす。
- 対象回帰テスト、全pytest、CLIヘルプ、READMEリンク、SVG XML、`git diff --check`が成功する。

# 日誌指紋と改善提案PASS指標契約の修正（2026-07-30）

## Goal breakdown

- CRLFで保存されたActivity LogとLFで生成された同一内容が同じ`activity_sha256`になるよう、指紋入力を改行コード非依存にする。
- `AdviceEvidence`の可用性フラグから、その日実際に自動判定できるPASS指標一覧を決定論的に作る。
- 日次system promptと契約修復promptへ同じ許可一覧・禁止一覧を渡し、計測不能指標をLLMへ「使用可能」と誤提示しない。
- 保存時の`validate_advice`は最終防衛線として維持し、不完全なアクションをKaizen Memoryへ入れない。

## Dependencies and parallelizable work

- 指紋修正は`src/kaizenlog/stats.py`と既存統計テストだけで独立してRED→GREENできる。
- 指標一覧修正は`AdviceEvidence`の既存フラグと`known_categories` / `observed_sites`を唯一の入力とし、`advisor.py`のprompt準備・修復promptから共有する。
- `src/kaizenlog/cli.py`、`.superpowers/`、実際のObsidian日誌、ActivityWatch、設定ファイルには実装中触れない。
- 単体回帰がGREENになった後だけ、全pytest、`compileall`、`git diff --check`、一時ボールトを使ったCRLF generate→advise dry-run相当を統合確認する。

## Risks and mitigations

- 改行正規化で既存指紋を壊す: LF文字列のハッシュは従来値と一致させ、CRLF/CRだけをLFへ正規化する回帰テストを置く。
- promptとvalidatorの指標集合が再び乖離する: 許可一覧を返す単一helperを作り、system promptとrepair promptの両方から使用する。
- カテゴリ名・サイト名を無制限にpromptへ出す: evidenceで観測済みの値だけを決定論的にソートし、既存のprivacy redactor適用前に組み立てる。
- custom system promptを壊す: 動的指標注記は`requires_daily_contract()`が真の`daily_advisor` / `privacy_safe`だけに追加する。
- ユーザー作業を混入する: 開始時に未追跡だった`.superpowers/`を編集・stage・commitせず、対象ファイルだけを変更する。

## TDD tasks

### Task 1: 改行コード非依存のActivity指紋

- [x] `tests/test_patterns.py`へ、LF/CRLF/CRの同一Activity本文が同じ指紋になり、LFの既存SHA-256値が変わらないテストを追加する。
- [x] 対象テストを実行し、現行実装ではCRLFケースだけFAILすることを確認する。
- [x] `src/kaizenlog/stats.py::activity_fingerprint`でCRLF/CRをLFへ正規化してから`strip()`・SHA-256計算する。
- [x] 対象テストを再実行しGREENを確認する。

### Task 2: evidence準拠のPASS指標一覧

- [x] 新規`tests/test_round32_advice_metric_contract.py`へ、構造化AI・入力・サイト統計が無い場合に該当指標が許可一覧から外れ、基本指標と観測カテゴリだけが残るテストを追加する。
- [x] 同テストを実行し、helper未実装でREDになることを確認する。
- [x] `src/kaizenlog/advisor.py`へ`available_pass_metrics(evidence) -> tuple[str, ...]`を追加し、基本・構造化AI・入力・カテゴリ・サイトを可用性フラグから構築する。
- [x] 対象テストを再実行しGREENを確認する。

### Task 3: 初回promptと修復promptの契約統一

- [x] 新規回帰テストへ、日次system promptに許可・禁止指標の決定論セクションが入り、修復promptにも同じ一覧が入るケースを追加する（custom prompt互換は既存全体テストで確認）。
- [x] 対象テストを実行し、現行の固定「使用可能指標」表示と修復例によりREDになることを確認する。
- [x] `prepare_advice_request`で日次promptだけに動的な許可・禁止注記を追加し、`_contract_repair_prompt`も同じhelperを使用する。
- [x] 計測不能な`ai_tool_errors`を修復例から外し、常に許可される`context_switches`を例にする。
- [x] 対象テストを再実行しGREENを確認する。

## Acceptance criteria and tests

- LF/CRLF/CRの同一Activity Logで`activity_fingerprint`が一致し、既存LFハッシュは不変。
- 2026-07-30相当のCRLF日誌と既存statsの照合で`source_status=verified`になる。
- 構造化AIテレメトリなしの日は`ai_retry_chains` / `ai_tool_errors`が許可一覧に出ず、修復promptにも使用可能例として出ない。
- 構造化AIテレメトリありの日はAI指標が許可され、既存validatorを通過できる。
- `python -m pytest -q`、`python -m compileall -q src`、`git diff --check`が成功する。
- `.superpowers/`と実データ、設定、スケジュール、Memoryを変更しない。

# 最新版再動作確認（2026-07-30）

## Goal breakdown

- `git fetch --prune origin`後の`origin/main`と現在の`HEAD`を比較し、「最新版」を上流同期状態と作業ツリー状態に分けて判定する。
- 現在の未コミット修正を保持したまま、パッケージ版数、CLI、設定診断、全回帰テスト、コンパイル、差分健全性を再確認する。
- 実データを書き換えない`advise --dry-run`で、最新の日誌・統計から改善提案要求を構築でき、計測可能なPASS指標契約が反映されることを確認する。

## Dependencies and parallelizable work

- 上流同期確認、静的検証、CLI診断は相互に独立だが、取得したコミットIDを基準として結果をまとめる。
- pytestはリポジトリ`.venv`とOS一時ディレクトリの`--basetemp`を使う。
- `generate`、通常の`advise`、`run`は日誌・統計・Memoryへ書き込むため、今回の再確認では実行しない。

## Risks and mitigations

- dirty treeを上流更新で壊す: fetchと比較だけを行い、pull、merge、reset、checkout、stashを行わない。
- 未追跡のユーザー成果物を混入する: `.superpowers/`と`Kaizen/`を含む開始時の未追跡ファイルへ触れない。
- 外部サービス状態をアプリ不良と誤認する: `doctor`の必須項目と任意watcher警告を分けて報告する。
- dry-runでも意図しない書き込みが起きる: 実行前後の`git status --short`と対象日誌・statsの更新時刻を比較する。

## Acceptance criteria and tests

- `HEAD...origin/main`のahead/behind件数と両コミットIDを取得できる。
- `.venv\Scripts\kaizenlog.exe --help`と主要サブコマンドのhelpが終了コード0。
- `doctor`が必須経路の状態を診断できる。
- 全pytest、`compileall`、`git diff --check`が成功する。
- `advise --dry-run --date 2026-07-30`が終了コード0で、許可・禁止PASS指標契約を含む要求を生成する。
- 検証前から存在する未コミット・未追跡ファイルを保存し、実日誌・stats・Memoryへ新規書き込みを行わない。

## User-approved real journal regeneration

- ユーザーの追加指示により、非書き込みdry-runだけでなく2026-07-30の実日誌を作り直す。
- 実行直前の日誌、stats、Kaizen Memoryを日時付きバックアップへコピーする。
- `generate --date 2026-07-30`後、Activity Log、統計値、`activity_sha256`をアプリ標準の`extract_section`と`activity_fingerprint`で照合する。
- `advise --date 2026-07-30`後、改善提案マーカー、KZN ID、翌日チェックボックス、Memory記録、status/healthを照合する。
- 同一失敗が2回起きた場合は再実行を止め、プロセス・ファイル・statusを調査する。

# 「今日のアクション」可読性改善（2026-07-31）

## Goal breakdown

- Obsidian日誌の「今日のアクション」を、長い1行の羅列ではなく短時間で走査できる表示へ改善する。
- 既存のチェックボックス同期、KZN ID、PASS/FAIL機械判定、最大3件、古い未完了件数、手書き領域保護を維持する。
- 未コミットの第34弾変更（達成済み指標の分離・計測表現の正直化）を前提にし、上書きや後退を起こさない。

## Dependencies and parallelizable work

- まず現行renderer、実データ出力、既存テスト、未コミット差分を読み、見栄えの選択肢を設計する。
- ユーザー承認後に設計書と実装計画を確定し、テストを先に失敗させてから最小実装を行う。
- renderer変更後、単体テスト、関連回帰、全pytest、実Memoryからの非書き込みプレビュー、実日誌再描画を順に確認する。

## Risks and mitigations

- 長いアクション文字列の分解で情報を落とす: 構造化できる既存形式だけを分割し、旧形式は全文表示へフォールバックする。
- Obsidianチェック同期を壊す: チェックボックス行にはKZN IDを残し、既存の`_CHECKBOX_RE`と`ID_PATTERN`契約を維持する。
- 表形式でモバイル可読性を悪化させる: 横スクロールが必要なMarkdown表は推奨案から外す。
- ユーザー作業を壊す: dirty treeの開始状態を記録し、対象renderer・テスト・承認済み設計文書以外を編集しない。

## Acceptance criteria and tests

- 主要アクションはチェックボックス、行動、PASS判定、提案日・実測を視覚的に分離して読める。
- 1行にPASS/FAIL/ID/日付を詰め込まず、3件並んでも各アクションの境界が明確。
- PASS達成済みの提案は通常アクションと別セクションで表示される。
- チェック済み状態、最大3件、残件案内、`today --all`導線、手書きバイト保護が維持される。
- 関連テスト、全pytest、`compileall`、`git diff --check`が成功する。

# 第35〜38弾 Codexプロンプト改善（2026-07-31）

## Goal breakdown

- `docs/codex-prompts/0731_Codex_日誌情報設計の再構成指示プロンプト_第35弾.md`、第36弾、第37弾、第38弾を、単独実行時の安全性と、35→36→37→38の依存関係が同じ読み方になる指示書へ改善する。
- 各文書の根拠、対象範囲、非目標、実装順、後方互換、失敗時挙動、受け入れテスト、実挙動確認、最終報告形式を再点検し、実装エージェントが行番号のずれや推測で scope を広げない状態にする。
- アプリケーションコード、テストコード、ユーザーのボールト、Memory台帳、設定、リモート、commit/push は変更しない。変更対象は4本のプロンプトと、作業計画としての本 `PLAN.md` に限定する。

## Current baseline and file map

- 実リポジトリ: `C:\develop\KaizenLog\KaizenLog-`
- 開始時ブランチ/HEAD: `main` / `fea5609`
- 開始時に確認した既存の未追跡ファイル: 対象4プロンプト、`.superpowers/`、`docs/2026-07-31-journal-value-proposal.md`。これらのうち対象4本だけを今回の編集対象とする。
- 改善対象:
  - `docs/codex-prompts/0731_Codex_日誌情報設計の再構成指示プロンプト_第35弾.md`: 描画層9件、既存スキーマ不変の前提
  - `docs/codex-prompts/0731_Codex_判定の2段階確定指示プロンプト_第36弾.md`: `verdict_stage` と暫定/確定の同期
  - `docs/codex-prompts/0731_Codex_提案の質と学習ループ再起動指示プロンプト_第37弾.md`: 提案品質、2トラック、因果仮説、PRM
  - `docs/codex-prompts/0731_Codex_提案寿命管理と成果可視化指示プロンプト_第38弾.md`: 提案寿命、digest、git突合

## Dependencies and parallelizable work

- 4本の個別レビューは、対象ファイルの読み取りと契約抽出だけなら相互に独立するため、サブエージェント4体へ並列依頼する。
- 各サブエージェントは担当ファイルだけを編集せず、矛盾、曖昧さ、根拠不足、テスト不足、後続弾への影響を `§ID / file:line / evidence / proposed wording` 形式で返す。
- Sol はレビュー結果を統合し、共通実行契約（前提弾、変更許可、禁止事項、証拠の扱い、テスト、報告）と弾間の依存グラフを決定する。個別レビュー結果を無検証で採用しない。
- 4本の編集は同じディレクトリ内だがファイルが分離しているため、統合方針確定後はファイル単位で編集できる。ただし最終段階では4本を一括で読み直し、契約の相互整合を確認する。

## Risks and mitigations

- **現行実装と行番号がずれる:** 行番号を唯一の識別子にせず、関数名・定数名・CLI名・テスト名を主軸にする。確認できない根拠は `Unknown` と明記し、断定文を作らない。
- **35〜38弾の境界が衝突する:** 35は描画層、36は判定stage、37は提案品質/学習、38は寿命/digest/git突合という境界を固定し、別弾のデータモデルや表示責務を勝手に前倒ししない。
- **先行弾適用前後の読み違い:** 各プロンプトの冒頭に「適用済み前提」「未適用時の扱い」「参照が見つからない場合の停止条件」を揃える。
- **巨大な受け入れ条件が実行不能になる:** 実装前に静的契約、focused test、full regression、実データ確認を分離し、各PhaseのPASS条件を具体的なコマンドと期待結果で書く。テスト件数は未確認なら固定値として断定しない。
- **ユーザーデータや外部副作用の混入:** 4本すべてに、既存データの一括migration禁止、実LLM/外部サービス禁止、リモート禁止、commit/push禁止を一貫して残す。

## Acceptance criteria and verification

- 4本すべてが、目的・前提・対象/非対象・Phase/§ID・受け入れ条件・最終報告を持ち、担当弾の境界と35→36→37→38の依存関係が矛盾しない。
- 実装エージェントが、確認できない実装事実を捏造せず、行番号変更に追随でき、失敗時に停止/報告できる。
- テスト条件が「何を固定するか」「どの境界を含むか」「期待する終了コード/出力/バイト保全」を明記し、単なる「テストを追加する」に留まらない。
- 共通禁止事項、fail-closed、redact、既存データ保全、no commit/push が4本で意味を変えず、より厳しい弾の条件を後続弾が打ち消さない。
- 変更後に以下を実行する:
  - 対象4ファイルの見出し・必須節・弾番号・参照パスの静的契約スキャン
  - `git diff --check`
  - `git status --short` と `git diff --stat --` で対象外の変更がないことを確認
  - 必要に応じて既存のプロンプト関連テストを読み取り専用で確認し、アプリケーション全体テストはコード変更がないため必須証拠と混同しない
- commit、push、外部サービス、実LLM、実ボールト書き込みは行わない。

## Workflow

1. 現行4本と関連提案書・履歴を読み、開始時のdirty baselineを保持する。
2. 4体のサブエージェントへ個別レビューを並列依頼する。
3. Sol が各報告を根拠・重複・弾間整合・保守性の観点でレビューし、採用する改善方針を決める。
4. 設計方針をユーザーへ提示し、承認後に4本だけを編集する。
5. 静的契約、diff check、対象外差分確認を実行し、未確認事項は未確認のまま報告する。

# 第35弾 日誌情報設計の実装計画（2026-08-01）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for the Phase単位実装と、各Phase後の仕様レビュー・品質レビュー。

**Goal:** `docs/codex-prompts/0731_Codex_日誌情報設計の再構成指示プロンプト_第35弾.md` の§A1〜§D2を現行第34弾コードへ実装し、§E1の新規テスト・USAGE追記・隔離fixture検証まで完了する。指示書自体は編集しない。

**Architecture:** 既存の決定論的 renderer/evidence/stats の経路を拡張する。新しい計測源・LLM呼び出し・永続化スキーマ変更は行わず、`DailySummary.ai_tool_minutes`だけをstatsへ加算保存する。共有ファイルをPhase順に触り、各Phaseはテスト先行、focused test、仕様レビュー、品質レビューを通過してから次へ進む。

**Tech Stack:** Python 3、既存の `pytest`、dataclass/Mappingベースのstats、Markdown renderer、既存のprivacy redactor。

## 開始時の証拠と変更境界

- 実リポジトリ: `C:\develop\KaizenLog\KaizenLog-`
- 開始時: `main` / `fea56091cdad` / `705 tests collected`
- 既存dirty/untrackedは保全する。対象変更は `src/`、`tests/`、`docs/USAGE.md`、本 `PLAN.md` に限り、プロンプト・ボールト・台帳・設定・リモート・commit/pushは変更しない。
- 実日誌・実statsの再生成は行わず、§E1は一時fixtureまたは承認済みコピーだけで検証する。

## Task 1: Phase 1 §A1〜§A3 — 空行、ソース分離、画面AIの計測限界

**Files:**

- Modify: `src/kaizenlog/advisor.py` (`render_reader_advice`)
- Modify: `src/kaizenlog/aiwork.py` (`retry_chain_excerpts`, `render_aiwork_markdown`)
- Modify: `src/kaizenlog/stats.py` (`build_stats`の加算キー)
- Modify: `src/kaizenlog/cli.py`（`summary.ai_tool_minutes`のrenderer配線）
- Test: `tests/test_round35_journal_information_design.py`

- [ ] `reader_notes=()`で見出し・本文を出さず、非空notesは従来文面を保つテストを先に追加し、focused testが機能不足で失敗することを確認する。
- [ ] `retry_chain_excerpts`を正規化済み本文・同一project単位で畳み、初出順と畳み込み後`max_chains`を固定する。空本文だけ既存の省略文へ倒す。
- [ ] sourceが2種類以上のときだけmeasurable sessionのエラー/中断/連鎖をsource別に展開し、1 source時は既存文字列を維持する。
- [ ] `render_aiwork_markdown(..., screen_tool_minutes=None)`を追加し、明示テーブルにない画面ツールをログ無し側へ倒す。`DailySummary.ai_tool_minutes`は`build_stats`で`ai_screen_tool_minutes`として丸めて保存し、旧statsにキーが無い場合は注記を増やさない。
- [ ] `ai_stats_valid and telemetry_sessions > 0`の既存条件は変更せず、画面時間30分以上かつ対応`-web`セッション無しの日だけreader noteを追加する。
- [ ] `pytest tests/test_round35_journal_information_design.py -q`で§A1〜§A3がgreenになることを確認する。

## Task 2: Phase 2 §B1〜§B2 — 摩擦、コスト、ループ税

**Files:**

- Modify: `src/kaizenlog/aiwork.py` (`render_aiwork_markdown`, `format_loop_tax_line`)
- Test: `tests/test_round35_journal_information_design.py`

- [ ] 摩擦ワースト0件/1件 fixtureを先に追加し、表の並び順を変えず表直前に3行を挿入する仕様を固定する。
- [ ] cost未登録分岐を、桁区切り・未登録モデル一覧・`[aiwork.pricing]`案内の3行へ置き換え、全モデル単価登録時は旧金額行を保つ。
- [ ] `format_loop_tax_line(..., day_output_tokens=None)`を後方互換に保ち、指定時だけ割合・比較対象を表示する。実計算の重複排除不変条件をfixtureで検証し、不整合入力だけ100%超注記を許す。
- [ ] 最長episodeは`ep.chain.length`、`ep.wasted_tokens`、`ep.has_tool_error`と既存excerptだけを使い、存在しない属性を参照しない。
- [ ] focused test後、`tests/test_round27_loop_tax.py`、`tests/test_round34_journal_quality.py`を回帰実行する。

## Task 3: Phase 3 §C1〜§C2 — Activity説明と前日比

**Files:**

- Modify: `src/kaizenlog/report.py` (`render_markdown`, `render_change_table`)
- Modify: `src/kaizenlog/advice_evidence.py` (`_build_reader_summary`)
- Modify: `src/kaizenlog/cli.py`（section確定後のchange table配線）
- Test: `tests/test_round35_journal_information_design.py`

- [ ] 閾値未満0件、全件閾値未満、eligibleあり/上限超過をテストし、`under_threshold_count > 0`の説明行と上限超過文を別行で出す。閾値は`min_block_minutes`から取得する。
- [ ] `render_change_table(today, prev)`は両辺のstatsに明示キーがある行だけを出し、0埋めしない。前日欠落は空文字とする。
- [ ] 当日の生成済みsectionへchange tableをappendしてから`write_stats(activity_md=section)`へ渡す。既存statsファイルを当日比較元にしない。
- [ ] 履歴のトレンド文は暦日隣接を確認し、欠損日がある場合は「記録のあるN日」へフォールバックする。7/31のように最長でない日は最長断定を出さない。
- [ ] focused test後、`tests/test_report_vault.py`、`tests/test_ux_round13.py`、`tests/test_round34_journal_quality.py`を回帰実行する。

## Task 4: Phase 4 §D1〜§D2 — reader復活と履歴中央値ゲート

**Files:**

- Modify: `src/kaizenlog/advisor.py` (`render_reader_advice`, `_baseline_repair_hint`)
- Modify: `src/kaizenlog/advice_evidence.py` (`_metric_baselines_from_history`、notes)
- Modify: `src/kaizenlog/advice_format.py`（中央値係数）
- Modify: `tests/test_round16_journal_value.py`（既存係数期待値のみ）
- Test: `tests/test_round35_journal_information_design.py`

- [ ] `今日の改善提案`と`AI作業の改善`をJSON/Markdown/reader renderer間で運び、チェックボックス行のPASS/FAIL・ID・`parse_pass_condition`入力を変更しない。因果2行は素のインデント箇条書きにする。
- [ ] `reader_notes`空、`ai_review`空、actions/proposals件数不一致の境界を先にテストする。
- [ ] `_metric_baselines_from_history(stats, history)`で当日除外、暦日・値の有効性、3日未満スキップ、category/siteキー欠損除外を固定する。現在の当日-only関数は置換し、呼び出し側の履歴を渡す。
- [ ] `_MEDIAN_CHALLENGE_LE=0.95` / `_MEDIAN_CHALLENGE_GE=1.05`を導入し、旧1.2/0.8定数の参照がないことを確認する。`tests/test_round16_journal_value.py:51-64`は`>=3.6`境界へ更新する。
- [ ] focused test後、`tests/test_advice_evidence.py`、`tests/test_advice_format.py`、`tests/test_round16_journal_value.py`を回帰実行する。

## Task 5: Phase 5 §E1 — 追加テスト、USAGE、隔離fixture

**Files:**

- Modify: `docs/USAGE.md`
- Test: `tests/test_round35_journal_information_design.py`

- [ ] §A1〜§D2を少なくとも1テストずつ、出す/出さない・空/非空・前日あり/なし・旧stats欠落・冗長化境界を含めて固定する。
- [ ] USAGEへ「計測範囲」「ループ税100%超は不整合入力のみ」「PASS基準は履歴中央値」を追記する。
- [ ] `pytest --collect-only -q -p no:cacheprovider`、focused tests、全`pytest -q`、`compileall`、`git diff --check`を実行する。
- [ ] 実挙動は一時fixtureまたは承認済みコピーのみで7/30・7/31相当を確認し、Activity/Advice/Actions、stats、Memory、実験、run logの変更を許可範囲と照合する。実データ未確認は`Unknown`、未実施は`⚠️`と報告する。

## Delegation and review protocol

- 共有ファイルの競合を避け、実装サブエージェントはTask 1→5を一つずつ順番に担当する。各委譲には対象ファイル、非対象、TDD手順、期待するfocused test、出力形式を明記する。
- 各Task後に同じ実装エージェントへ仕様適合レビューの指摘を返し、別サブエージェントでコード品質レビューを行う。重大/重要指摘は次Taskへ進む前に修正・再レビューする。
- 私は各差分を`git diff`/テスト出力/仕様§IDで独立検証し、サブエージェントの報告だけを根拠に完了扱いにしない。
- 変更はユーザー指定どおり `src/` と `tests/` を中心に実装するが、§E1の`docs/USAGE.md`と計画の`PLAN.md`以外の文書は編集しない。

# 第36弾「判定の2段階確定」実装計画（2026-08-01）

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` for every behavior change and perform the Phase gate verification before starting the next Phase. Do not edit the named prompt document.

**Goal:** `docs/codex-prompts/0731_Codex_判定の2段階確定指示プロンプト_第36弾.md` の §Z1〜§Z6、§A1〜§A4、§B1〜§B3、§C1〜§C3、§D1〜§D2 を、既存の JSONL 追記契約・日誌マーカー保護・後方互換を維持して実装する。

**Architecture:** `MemoryEntry.verdict_stage` を `provisional|confirmed` の2値として Memory の読み書き・再構築点・判定生成・消費側に一貫して運ぶ。判定生成は「当日=暫定、翌日以降の generate 内 backfill=確定」とし、判定 suffix、📌 ACTIONS 転記、測定日ノートを同じ更新集合から再描画する。旧JSONLの stage 欠落は `confirmed`、明示的不正値は行を残したまま `provisional` とする。

**Tech Stack:** Python 3、dataclass、既存の JSONL Memory、Markdown marker renderer、`pytest`、リポジトリ内の `.venv`。外部DB・実LLM・実ボールト・リモートは使わない。

## 開始時の証拠と変更境界

- 実リポジトリは `C:\develop\KaizenLog\KaizenLog-`。開始ブランチは `main`、HEAD は `fea56091cdade0fe61c695e32dc4a18e121ca03d`、基準線は `778 tests collected`。
- 第35弾適用ゲートは `src/kaizenlog/report.py::render_change_table` と `tests/test_round35_journal_information_design.py` の存在で確認済み。これらが失われた場合は実装を止めて報告する。
- 開始時の既存変更（第35弾実装、`PLAN.md`、`docs/USAGE.md`、既存テスト、未追跡のプロンプト等）は保全する。今回の追加変更は `src/`、`tests/`、§D2で指定された `docs/USAGE.md`/`docs/HANDOFF.md`、本 `PLAN.md` に限定する。
- `docs/codex-prompts/0731_Codex_判定の2段階確定指示プロンプト_第36弾.md` 自体は編集しない。`git commit`、`git push`、SSH/scp、外部DB変更、実LLM、実ボールト一括migration、既存 `suggestions.jsonl` 行の書換え・並替え・削除は行わない。
- 各Phaseの実装前に、そのPhaseのテストを先に追加して RED を観測する。各Phase完了時に `./.venv/Scripts/python.exe -m pytest -q` を実行し、前Phaseの失敗を混入させない。

## ファイル責務マップ

- `src/kaizenlog/memory.py`: `MemoryEntry` の stage、JSONL後方互換、手列挙再構築点、統計・一覧・📌行・LLMプロンプト行。
- `src/kaizenlog/verdict.py`: 当日/バックフィルの stage 遷移、stage込み差分抑止、⏳ suffix、ADVICE/ACTIONS の marker 内更新。
- `src/kaizenlog/cli.py`: 実時刻 `today` の判定注入、判定・backfill変更集合の測定日再同期、朝通知の confirmed-only 集計。
- `src/kaizenlog/weekly_context.py` / `src/kaizenlog/decay.py`: confirmed-only の週次集計と風化候補。
- `src/kaizenlog/advisor.py` / `src/kaizenlog/report.py` / `src/kaizenlog/aiwork.py` / `src/kaizenlog/advice_evidence.py`: Phase 0 の第35弾レビュー残件。
- `tests/test_round35_journal_information_design.py`、`tests/test_round16_journal_value.py`、`tests/test_round25_internal_filter.py`: Phase 0 の回帰と不変条件の廃止記録。
- `tests/test_aiwork_adapters.py`: ユーザーの Downloads 配下を拾わないための既存 adapter test fixture 隔離。
- `tests/test_round36_verdict_stage.py`: §A〜§Dの新規 fixture・unit/integration 回帰。
- `docs/USAGE.md` / `docs/HANDOFF.md`: 夜間暫定・翌日以降の generate 内 backfill と当日未確定、§Z6の廃止記録。

## Phase 0 — §Z1〜§Z6（第35弾残件）

### Task Z1: reader advice の interpretation/proposal 分離

**Files:** `src/kaizenlog/advisor.py`、`tests/test_round35_journal_information_design.py`。

- [x] `interpretation。proposal。翌日見る指標` 形式で、読者向け `- なぜ:` が interpretation だけになり、proposal と完全一致しない RED テストを追加する。interpretation 内の句点を含む素材は、既存素材書式に対する実装の決定結果をテストへ固定する。
- [x] 正規表現を `(?P<why>.+?)。(?P<proposal>[^。]+)。翌日見る指標:` 相当の非貪欲分割へ変更し、proposal を context に入れない。
- [x] `pytest tests/test_round35_journal_information_design.py -q -k reader` で RED→GREEN を確認する。

### Task Z2: F-ID除去後の連続空白除去

**Files:** `src/kaizenlog/advisor.py`、`tests/test_round35_journal_information_design.py`。

- [x] F-ID を語間に挿入した `why`、`metric`、`AI作業の改善` 行の出力に `  ` が残らない RED テストを追加する。
- [x] F-ID除去後に `re.sub(r"\\s{2,}", " ", ...)` と trim をサブ行・AI見立て行にも適用し、既存の単一空白出力を保つ。
- [x] Z1/Z2 focused test を再実行する。

### Task Z3: eligible 行が0件のタイムライン説明

**Files:** `src/kaizenlog/report.py`、`tests/test_round35_journal_information_design.py`。

- [x] 全ブロックが `min_block_minutes` 未満の fixture で、説明行は出るが「時刻順に表示。」と表ヘッダが出ない RED テストを追加する。
- [x] `f"{min_label}以上の画面ブロックを時刻順に表示。"` と表ヘッダを `if rows:` に入れる。`under_lines` と overflow の説明は維持する。
- [x] Z3 focused test を再実行する。

### Task Z4: loop tax の最悪例を別行化・重複除去

**Files:** `src/kaizenlog/aiwork.py`、`tests/test_round35_journal_information_design.py`。

- [x] `format_loop_tax_line(..., day_output_tokens=...)` の最悪例が改行された1行になり、連鎖起点と同一 excerpt を二重出力せず、引数省略時は既存の1行文字列と `==` になる RED テストを追加する。
- [x] `return line + "\\n" + f"   — 最悪例: ..."` の形へ変更し、最悪例用 excerpt の候補から直上の retry-chain excerpt を除外する。省略経路の既存文字列は触らない。
- [x] Z4 focused test と `tests/test_round27_loop_tax.py` を実行する。

### Task Z5: トレンドの増加回数とゲート

**Files:** `src/kaizenlog/advice_evidence.py`、`tests/test_round35_journal_information_design.py`。

- [x] 5点・増加4回、4点・増加3回、3点・増加2回、非単調履歴、暦日欠落の fixture を先に追加し、4/3/フォールバックの期待を RED で固定する。
- [x] `N = sum(later > earlier)` 相当の連続増加回数を使い、第1文は `N >= 3`、最長文の既存ゲートは履歴2日以上のままにする。非単調履歴では単調増加文を出さない。
- [x] Z5 focused test を実行する。

### Task Z6: 虚偽テスト名と廃止不変条件の記録

**Files:** `tests/test_round25_internal_filter.py`、`tests/test_round16_journal_value.py`、`docs/HANDOFF.md`。

- [x] `test_s3_token_number_appears_once_on_cost_fallback` を `test_s3_cost_fallback_shows_three_line_guidance` に改名し、3回表示が第35弾 §B2で意図的に採用されたコメントを置く。`tests/test_round16_journal_value.py` の `210,000` 回数アサート削除箇所にも同じ廃止理由コメントを置く。
- [x] `docs/HANDOFF.md` の既知の限界へ「第25弾 §S3 のトークン数値1回表示は第35弾 §B2で廃止」を1行追加する。
- [x] `rg -n "appears_once" tests/` が0件で、Z1〜Z6の focused test が全PASSになることを確認する。

### Phase 0 ゲート

- [x] `./.venv/Scripts/python.exe -m pytest -q` を実行し、全PASS件数を実測記録する。Phase 0の修正・期待値更新による失敗がないことを確認するまで Phase 1 に進まない。実測結果は `786 passed in 78.52s`。
- [x] `git diff --check` と `git status --short` で、プロンプト自体・実データ・DB・リモートに変更がないことを確認する。既存 Downloads を拾う `test_aiwork_adapters.py` は browser export path を tmp_path へ隔離した。

## Phase 1 — §A1〜§A4（MemoryEntry と後方互換）

### Task A: stage の読み書きと再構築保持

**Files:** `src/kaizenlog/memory.py`、`tests/test_round36_verdict_stage.py`。

- [x] 新規テストへ、stage キー無し JSONL が `confirmed`、明示 `provisional` が保持され、明示未知文字列・数値・null は行を破棄せず `provisional` になるケースを追加して RED を観測する。
- [x] `MemoryEntry` に `verdict_stage: str = "confirmed"` を追加し、`_normalize_verdict_stage(raw, key_present)` を `key_present=False -> "confirmed"`、明示値が2値以外 -> `"provisional"` として `load_entries` に接続する。`append_entries` は既存の `asdict` のみで JSONL key を出す。
- [x] provisional entry に対し、`update_statuses_from_note` の x/-、`mark_entry_done`、`mark_entry_skipped` の4再構築経路をそれぞれ通し、4個すべてで stage が `provisional` のままになる個別アサートを追加する。
- [x] A focused test を実行して GREEN にし、旧行の `verdict`/`skip_reason`/値互換が崩れていないことを確認する。

### Phase 1 ゲート

- [x] `./.venv/Scripts/python.exe -m pytest -q` を実行し、Phase 0の実測件数以上が全PASSであることを確認する。実測結果は `792 passed in 69.38s`。

## Phase 2 — §B1〜§B3（暫定生成・確定昇格・差分抑止）

### Task B:判定関数とbackfillのstage遷移

**Files:** `src/kaizenlog/verdict.py`、`src/kaizenlog/cli.py`、`tests/test_round36_verdict_stage.py`。

- [x] `judge_entries` の `today` 未指定=confirmed、`judged_day == today`=provisional、`judged_day < today`=confirmed、confirmed entry の再実行で provisional へ降格しないケースを先に追加する。
- [x] `judge_entries(..., *, today: date | None = None)` とし、既存呼び出しの既定を confirmed にする。CLIの `cmd_generate` では `datetime.now(ZoneInfo(cfg.timezone)).date()` を `today=` として渡す。stageを差分一致条件に加え、生成 MemoryEntry に `verdict_stage=stage` を設定する。現在 confirmed の同ID・同測定日を再判定する場合は stage を confirmed に維持する。
- [x] `backfill_verdicts` は `pass/fail and verdict_stage == "confirmed"` のみ skip し、provisional は同じ値でも `measure_day < as_of` なら confirmed 行を1本追記する。`measure_day == as_of` は provisional、`measure_day > as_of` は従来どおり skip とする。生成行へ stage を設定する。
- [x] 79→181→210 の同一 `judged_day` 3回を再現し、3行が provisional、翌日以降の backfill で confirmed が1行だけ増え、再度の backfill が0行になるテストを追加する。confirmed 後の judge/backfill で provisional へ降格しないことも固定する。
- [x] B focused test、既存 `tests/test_verdict.py`、`tests/test_round10.py` を実行する。

### Phase 2 ゲート

- [x] `./.venv/Scripts/python.exe -m pytest -q` を実行し、全PASSを確認する。夜間21:30相当は provisional、翌日以降の `generate` 内 backfill で confirmed という導線をテスト結果とともに記録する（朝通知だけで昇格するとは記録しない）。実測結果は `797 passed in 68.37s`。

## Phase 3 — §C1〜§C3（表示と測定日同期）

### Task C1/C2: ⏳表示の統一

**Files:** `src/kaizenlog/verdict.py`、`src/kaizenlog/memory.py`、`tests/test_round36_verdict_stage.py`。

- [x] provisional PASS/FAIL の suffix、confirmed PASS/FAIL の既存文字列完全一致、`format_today_action_line` の `⏳暫定`、`_verdict_block_line` の `[⏳暫定PASS/FAIL]`、confirmed の従来表示、`_action_line` の暫定タグを先に文字列固定する。
- [x] `format_verdict_suffix` の provisional 分岐を confirmed 分岐より先に追加し、`parse_pass_condition` の演算子から `目標N以下/以上` を決定論で作る。判定日は `entry.verdict_date` の `M/Dの日締め後に確定` とする。confirmed 文言・`_VERDICT_SUFFIX_RE` は変更しない。
- [x] `memory.py` の `format_today_action_line`、`_verdict_block_line`、`render_actions_section` の `_action_line` へ stage表示を接続する。暫定PASSは `pass_achieved` に入れず、`still_open` の厳密な補集合へ残すため両条件を同時に confirmed-only へ変更する。
- [x] C1/C2 focused test と既存 `tests/test_round10.py`、`tests/test_round12_learning_loop.py`、`tests/test_ux_round8.py` を実行する。新規16件と既存回帰は全PASS。

### Task C3: 測定日 ACTIONS の再同期

**Files:** `src/kaizenlog/verdict.py`、`src/kaizenlog/cli.py`、`tests/test_round36_verdict_stage.py`。

- [x] 一時日誌fixtureで、ACTIONS marker 内に候補上限外・`[x]`済み・📌✅79 の対象ID、Activity相当の210を用意する。判定更新後に同じIDの suffix だけが❌210へ変わり、checkbox・他行・marker外bytesが同一になる RED テストを追加する。marker無しノート、対象IDがmarker内に無いノート、古い日付は書かれないことも固定する。
- [x] `apply_verdicts_to_actions_note(content, updates)` を `verdict.py` に追加する。`ACTIONS_MARKER` の start/end が存在する本文だけを対象に、既存行の KZN ID と既存 checkbox を保持し、行末の既存 handoff tag だけを `format_action_verdict_tag` で置換する。`splitlines(keepends=True)` と文字列sliceで marker外・改行コード・末尾空白を保つ。
- [x] CLI側に `verdict_date` ごとの変更集合を処理する `_resync_measurement_day_actions` を追加する。`judged` と `bf.judged` をID後勝ちで統合し、測定日が `today - ACTIONS_HANDOFF_DAYS` 以上かつ `today` 以下、ノート存在、ACTIONS marker存在、対象ID出現の全ガードを満たす場合のみ `atomic_write_text` する。`backfill` の `as_of` を測定日として使わない。
- [x] `cmd_generate` で judge/backfill のノート更新後、翌日 `target=day+1` の handoff 前に測定日再同期を呼ぶ。D日に暫定判定、D+1日にbackfill確定を行ったfixtureでDだけが更新され、D+1へ誤追記されないことを確認する。

### Phase 3 ゲート

- [x] `./.venv/Scripts/python.exe -m pytest -q` を実行し、全PASSを確認する。実測結果は `802 passed in 67.73s`。
- [x] C3 fixtureの前後bytesを比較し、marker外が完全一致、ACTIONS内の非対象行と checkbox が一致することをテストで固定した（`tests/test_round36_verdict_stage.py` の C3 3テスト）。

## Phase 4 — §D1〜§D2（confirmed-only 消費と文書）

### Task D1: 学習・週次・通知・風化の消費境界

**Files:** `src/kaizenlog/memory.py`、`src/kaizenlog/weekly_context.py`、`src/kaizenlog/decay.py`、`src/kaizenlog/cli.py`、`tests/test_round36_verdict_stage.py`。

- [x] provisional のみの台帳 fixtureで `compute_action_stats().judged == 0`、`metric_pass_rates() == []`、`consecutive_fail_actions() == []`、`detect_kzn_decay() == []`、weeklyの判定/PASS数=0、朝通知の確定数=0、CLI一覧は `⏳` 表示になる RED テストを作る。confirmedへ置換した同じfixtureでは既存の計上が戻ることも追加する。
- [x] `compute_action_stats`、`metric_pass_rates`、`_consecutive_metric_fails`、`consecutive_fail_actions` の verdict判定へ `e.verdict_stage == "confirmed"` を追加する。provisionalは提案/未完了表示から消さず、判定系カウントだけ除外する。
- [x] `decay.detect_kzn_decay` の候補条件、`weekly_context.render_weekly_context` の judged/passed、`cli.build_morning_notification` の done/undone PASS 条件へ confirmed-only を追加する。`summarize_for_prompt` の provisional表示を残す場合は `_verdict_block_line` の `⏳` を使い、PASS/FAIL確定数へ入れない。
- [x] D1 focused test、`tests/test_action_stats.py`、`tests/test_round15_learning_loop_tweaks.py`、`tests/test_round29_decay.py`、`tests/test_ux_round8.py` を実行する（57 passed）。

### Task D2: 新規回帰テストとドキュメント

**Files:** `tests/test_round36_verdict_stage.py`、`docs/USAGE.md`、`docs/HANDOFF.md`。

- [x] 新規テストへ §A1/A3、§B1/B2/B3、§C1/C2/C3、§D1 の各契約を少なくとも1件ずつ含め、confirmed suffix の文字列はスナップショットで固定する。存在しない機構を受け入れ条件へ置かない。
- [x] `docs/USAGE.md` に「夜間21:30の判定は⏳暫定、翌日以降の generate 内 backfill（`as_of` が測定日より後になる実行）で確定に昇格する」を追記する。朝の実行で昇格すると書かない。
- [x] `docs/HANDOFF.md` に「当日中の判定は確定しない」を追記し、Phase 0 §Z6の廃止記録と重複しない短い既知の限界として保つ。

## 最終検証と報告

- [x] `./.venv/Scripts/python.exe -m pytest --collect-only -q -p no:cacheprovider` で最終収集数を取得し、固定値705ではなく実測値を使う。実測は `803 tests collected`。
- [x] `./.venv/Scripts/python.exe -m pytest -q`、`./.venv/Scripts/python.exe -m compileall -q src`、`git diff --check` を実行し、出力と終了コードを読む。全テストは `803 passed in 68.28s`。
- [x] `git diff --name-only` と `git status --short` を開始時のdirty baselineと比較し、プロンプト自体、外部データ、DB、リモート、コミットに変更がないことを確認する。既存dirty変更は保全し、今回の src/tests/docs/PLAN 変更を追加した。
- [x] 実台帳・実ボールト・ActivityWatchを使う実挙動確認は未承認のため行わない。合成fixtureで確認できた範囲だけを✅、実データ確認は⚠️/Unknownとして報告する。
- [x] 最終報告は §Z1〜§Z6、§A1〜§A4、§B1〜§B3、§C1〜§C3、§D1〜§D2 ごとに `✅/⚠️/❌`、`file:line`、追加テスト名を列挙し、Phaseごとの全PASS件数、実データ未実施、commit/push未実施を明記する。

## 2026-08-02 — 空転ブレーカー通知の発生源調査

### 目的

Claude Code 再起動後も表示される `KaizenLog 空転ブレーカー` 通知について、現在有効な実行主体（フック・一時作業コピー・常駐プロセス）を特定し、ユーザーの作業を壊さない最小の停止策を提示する。

### 手順

1. 現在の Claude / Python / KaizenLog プロセスのコマンドラインを確認する。
2. `guard --hook` を含む設定・トランスクリプト・一時作業コピーを時刻付きで照合する。
3. `guard.py` → `notify.py` の通知経路と設定ファイルの `enabled` 値を確認する。
4. 発生源が確認できた場合のみ、該当セッションの終了または該当設定の無効化を行う。未確認の一括削除・強制終了は行わない。

### 受け入れ条件

- [x] 現在の発生源を、ファイルパス・プロセス・時刻の証拠つきで説明できる。
- [x] メイン設定とプロジェクトフックを無効化済みであることを再確認する。
- [x] ユーザーの未保存作業を損なう操作を避け、必要なら安全な手動手順を案内する。

### 調査結果

- [x] `mut40_driver.py` と `scratchpad/repo2/mut2.py` のテスト実行、および親のClaude Code CLIセッション `--resume=6dde7484-53ac-41d8-968d-5c0be6fd4590` を停止した。
- [x] 停止後5秒の確認で、通知PowerShell 0件、対象テスト0件、旧CLIセッション0件。
- [x] リポジトリ/AppData双方の `[guard] enabled = false`、プロジェクト `.claude/settings.json` の `hooks: {}` を確認した。

## 2026-08-02 — 空転ブレーカー通知のユーザー向けノイズ抑止

### 目的

ガードの検知・Claude Codeへの追加コンテキストは維持しつつ、単体テストや設定注入型の検証がWindowsのバルーン通知を表示してユーザー作業を妨げないようにする。

### 実装方針

1. `GuardConfig` に通知表示の独立スイッチを追加し、通知は明示的に有効化した場合だけ表示する。
2. `run_hook(settings=...)` のようなテスト／埋め込み設定では通知を明示しない限りOFFにし、検知結果のstdout JSONは残す。
3. `guard.py` の通知分岐をテストで固定する（デフォルトOFF、明示ON、configのON/OFF）。既存の検知契約は変更しない。
4. 設定テンプレートと利用説明へ、通知を抑止する設定を追記する。

### リスクと対策

- 通知を既定OFFにしても追加コンテキストと状態保存は維持し、検知価値を失わない。
- 通知を抑止しても追加コンテキストまで消すとガードが空転するため、stdout JSONと状態保存は維持する。
- 既存dirty変更を上書きしない。変更対象をguard/config/tests/docsに限定する。

### 受け入れ条件

- [x] `settings` 注入で3回目の検知を実行してもWindows通知関数は呼ばれず、警告JSONは出る。
- [x] `settings={"notify": True}` では通知関数が呼ばれる。
- [x] TOMLの `[guard] notify = false` は通知だけを抑止し、`enabled = true` の検知は維持する。
- [x] 既存のguardテストおよび設定テストが全PASSする（32 passed）。全体回帰も902 passed、compileall・diff checkも成功。
