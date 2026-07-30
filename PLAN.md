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
