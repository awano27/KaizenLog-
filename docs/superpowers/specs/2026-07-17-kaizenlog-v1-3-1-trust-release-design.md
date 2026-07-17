# KaizenLog v1.3.1 信頼性とリリースの設計

**ステータス:** ユーザーレビュー向け提案版
**日付:** 2026-07-17
**ベースライン:** `main` の `8242927a6ec83d356282f84d4ecbcbf1294583de`（`v1.3.0`）
**リリース方針:** 信頼性を回復するパッチリリース。成長施策向け機能は含めない

## 1. 決定事項の概要

`v1.3.1` は、より広く宣伝する前に、既存の製品上の約束を確実に守れる状態にする最小のリリースである。手書きメモを保護し、プレビューモードを真に読み取り専用とし、カレントディレクトリが誤って vault として使われることを防ぎ、新規設定では LLM の使用をオプトインにする。また、`backend = "none"` と `doctor` の挙動を文書どおりに揃え、公開パッケージをクリーンな環境からインストールできることを実証する。

このリリースには、次の6つの必須成果がある。

1. 不正な形式の KaizenLog マーカーに対して、自動修復、追記、置換、削除を一切行わない。元ファイルをバイト単位で完全に変更せずに維持し、コマンドは復旧方法を示すメッセージとともに失敗する。
2. `advise --dry-run` と `run --dry-run` は、永続化書き込み、LLM 呼び出し、通知、自動バックフィルを一切行わない。
3. 設定が存在しない、または不完全な場合、設定依存コマンドで `vault_dir = "."` へフォールバックしない。
4. 新規設定は `llm.backend = "none"` から開始する。`none` 以外のバックエンドを選択することを、文書化されたデータフローに対するユーザーの明示的なオプトインとする。
5. `backend = "none"` でも LLM なしで `generate` と `run` を実行でき、`doctor` は `claude-code-cli` を含むすべての対応バックエンドを認識する。
6. wheel とソースディストリビューションにインストールガイドで使用するすべてのアセットを含め、Windows と Linux でビルド、検査、クリーンインストールを行う。PyPI のインストール手順を有効なものとして扱う前に、不変な `v1.3.1` リリースとして公開する。

この設計を承認することは、安全性を重視した次の2つの挙動変更を明示的に受け入れることを意味する。LLM バックエンドを明示していない設定では LLM を無効化し、vault パスを明示していない設定では設定エラーで停止する。

## 2. 検証済みベースラインと問題提起

ベースラインのリポジトリには有用な製品コアがあり、ローカルテスト86件が成功している。しかし現在のリリース面では、実装が保証できる内容よりも強い約束をしている。

- `src/kaizenlog/vault.py::upsert_section` は、片方のマーカータグしか存在しない場合に、2つ目の管理ブロックを追記する。その後の更新では、最初の start タグから後方の end タグまでを置換し、間にある手書き内容を削除する可能性がある。
- `src/kaizenlog/cli.py::cmd_advise` は、dry-run のガードより前に `append_entries` を呼び出す。トップレベルの `run --dry-run` パスでも、`cmd_generate` をスキップする前に自動バックフィルを呼び出す可能性がある。
- `src/kaizenlog/config.py::load_config` は、設定が存在しない場合に `Config(vault_dir=Path("."))` を返す。そのため、設定依存コマンドが起動ディレクトリ配下を読み書きする可能性がある。
- `src/kaizenlog/advisor.py` は `claude-code-cli` に対応している一方、`src/kaizenlog/doctor.py::_check_llm` はこれを不明なバックエンドとして報告する。
- `config.example.toml` は `backend = "none"` を「ログ生成のみ」と説明しているが、現在のコマンドパスでは `AdvisorError` が発生し、`run` が失敗する。
- README は活動データが PC 外へ出ないとしているが、クラウドバックエンドを使用する `advise` と LLM レポートモードでは、レンダリング済みの活動、意図、実験、Memory のコンテキストを選択したプロバイダーへ送信する。
- README はインストール済みユーザーに `scripts/register-task.ps1` と `templates/Kaizen Experiments.base` の使用を案内しているが、現在の wheel にはいずれのトップレベルパスも含まれていない。`pipx` ユーザーは、インストール済み製品だけでは文書化された手順を完了できない。
- 2026-07-17 時点の観測では、公開 GitHub リポジトリにはコミットが1件だけで公開済みリリースがなく、README が `pipx install kaizenlog` を推奨しているにもかかわらず、`https://pypi.org/pypi/kaizenlog/json` は 404 を返す。
- `.github/workflows/tests.yml` は editable install のみをテストしている。実際の wheel またはソースディストリビューションをビルドしてインストールしてはいない。

これらは機能不足ではなく、信頼を損なう問題である。そのため、本設計ではオンボーディングの洗練やマーケティングよりも、データ保全、誠実な挙動、再現可能な配布を優先する。

## 3. 目標と対象外

### 3.1 目標

- 単一の有効な管理マーカーペアの外側にあるすべての内容を保持する。
- 管理マーカーが不完全、重複、逆順、不一致、ネスト、または重なっている場合はフェイルクローズする。
- 文書化されたすべての dry-run パスを、観測可能な形で読み取り専用にする。
- 設定依存の操作を行う前に、意図して指定された vault の保存先を必須とする。
- LLM 無効モードを正式に対応する第一級のモードとし、新規設定または暗黙の LLM 設定に対する安全なデフォルトにする。
- 何がローカルに留まり、何が LLM へ送信され得るか、どこに redact が適用されるかを正確に説明する。
- このリリースで対象とする既存のファイル全体永続化パスに、クラッシュセーフな置換を使用する。
- インストール可能なアーティファクトを公開し、アーティファクト、バージョン、リソース、またはスモークチェックに失敗した場合はリリースを阻止する。
- 既存のコマンド名、マーカー名、保存済みデータ形式、明示的なバックエンド設定を維持する。

### 3.2 対象外

- セットアップウィザード、GUI、Web ダッシュボード、ブラウザ拡張、デモ vault、スクリーンショット、テレメトリーサービス、分析データ収集は追加しない。
- ActivityWatch の収集、分類ルール、助言品質、プロンプト文言、実験指標、Obsidian レイアウトは変更しない。
- 不正な形式のノートを自動修復せず、既存のすべてのノートを書き換える移行も行わない。
- 日次ノート、stats ファイル、実験ノート、Memory、run log をまたぐクロスファイルトランザクションは実装しない。
- マルチプロセスロックを保証しない。このパッチでは、スケジュール実行と手動実行による同時書き込みは引き続き非対応とする。
- JSONL スキーマを再設計せず、既存の Memory 履歴のコンパクションも行わない。
- 導入事例、Issue テンプレート、コントリビューションガイド、リポジトリのブランディング、ローンチキャンペーンなど、コミュニティ成長施策は行わない。
- stars、ダウンロード数、レビューを直接改善すると約束しない。このリリースは、ユーザーにツールの試用を依頼するための前提条件を整える。

## 4. 検討したアプローチ

### 4.1 アプローチA: 狭い範囲のバグ修正のみ

不正な形式のマーカーに関するケースを修正し、dry-run のガードを移動したうえで `1.3.1` をリリースする。

これは最速だが、無効な PyPI 手順、安全でない設定フォールバック、プライバシー説明の矛盾、非対応の `none` の挙動が残る。より広いユーザー層は、依然としてインストール時や初回実行時に失敗へ遭遇する。却下する。

### 4.2 アプローチB: 信頼性とリリースに絞った範囲

データ安全性とプレビューのセマンティクスを修正し、安全でない設定デフォルトを排除し、LLM の使用をオプトインにし、バックエンド診断を整合させ、ファイル全体のアトミック置換を追加する。また、実際のデータ境界を文書化し、実効性のあるパッケージリリースゲートを強制する。

このアプローチを採用する。各項目は1つの受け入れ境界を共有する。すなわち、新規ユーザーがアーティファクトをインストールし、LLM なしでローカル実行でき、変更を加えずに予定される LLM ペイロードを確認でき、不正な形式の管理セクションが書き換えられないことを信頼できる状態である。

### 4.3 アプローチC: 製品として整った再ローンチ

信頼性修正と、オンボーディングウィザード、デモデータ、スクリーンショット、リポジトリメタデータ、コントリビューション／セキュリティポリシー、公開実績を組み合わせる。

これは正確性、リリースエンジニアリング、UX、成長施策を1つのパッチに混在させ、安全性修正を遅らせる。`v1.4` 以降へ延期する。

## 5. システム境界とデータフロー

### 5.1 ローカル生成パス

```text
ActivityWatch localhost API
  -> メモリ上で収集および分類
  -> 管理対象の活動セクションをレンダリング
  -> 対象ノート内のすべての KaizenLog マーカーを検証
  -> そのノートをアトミックに置換
  -> 日次 stats と変更された実験ノートをアトミックに置換
  -> 運用 run log を追記／置換
```

`generate` では、KaizenLog が運用するリモートエンドポイントは関与しない。ActivityWatch は引き続き、設定されたベース URL を介してアクセスされる別個のローカルプロセスである。

### 5.2 助言パス

```text
日次ノート + カテゴリーのみの lookback + 手書きの意図
  + 実行中の実験コンテキスト + Kaizen Memory の要約
  -> メモリ上で system prompt と user prompt を構築
  -> 送信するすべてのプロンプト文字列に設定済み redact を適用
  -> 選択された LLM バックエンド（none または dry-run を除く）
  -> action ID を割り当て
  -> マーカーを検証し、advice セクションをアトミックに置換
  -> 新しい Memory レコードを追記
```

LLM がファイルハンドルを受け取ることはなく、vault へ直接書き込むこともない。LLM が受け取るのはテキストである。そのテキストには、ウィンドウタイトル、プロジェクト名、手書きの計画、実験タイトル、アクション履歴が含まれる可能性があるため、クラウドバックエンドを使用する助言を文書で「ローカルのみ」と呼んではならない。

### 5.3 Dry-run パス

Dry-run は、実際の助言呼び出しで使用するものと同じ読み取りを行い、新たに検出した完了済みアクションのメモリ内投影を含め、同一の実効プロンプトを構築する。redact 済みの system prompt と user prompt を出力するが、ネットワーク境界または永続化境界の前で停止する。

## 6. 詳細設計の契約

### 6.1 管理マーカーの完全性

`src/kaizenlog/vault.py` は、対象マーカーと人間が読める理由を保持する `VaultFormatError` を定義する。既存のマーカー文字列は変更しない。

`upsert_section` または `extract_section` が動作する前に、内容を解析し、`<!-- kaizenlog:<name>:start -->` および `<!-- kaizenlog:<name>:end -->` に完全一致する KaizenLog タグを検出する。

有効な状態は次のとおりである。

- 対象マーカーのペアが存在しない: `upsert_section` は新しいブロックを1つ追記し、`extract_section` は `None` を返す。
- マーカーごとに、順序が正しく重なりのない start/end ペアがちょうど1つ存在する: 対象セクションを読み取りまたは置換できる。

その他のすべての状態を無効とする。

- start はあるが end がない、または end はあるが start がない。
- 1つのマーカーに複数の start または複数の end がある。
- end が start より前にある。
- 別の管理セクションが開いている間に start タグが出現する。
- 現在開いているマーカーとは異なるマーカーの end タグが出現する。

置換する `section_md` に、対象名を問わず KaizenLog の管理マーカータグが含まれる場合も拒否する。これにより、LLM の応答または呼び出し元が提供する本文によって管理境界のネストしたコピーが作成されることを防ぐ。

内容が無効な場合は、次のとおりとする。

- `upsert_section` と `extract_section` は `VaultFormatError` を送出する。
- 呼び出し元は、正確なファイル、マーカー、理由、およびマーカーを手動で修復または復元するための指示を報告する。
- マーカーの自動挿入、削除、正規化、バックアップへの置換を一切行わない。
- 元ファイルと、当該操作におけるその他すべての永続化対象は変更しない。

内容が有効な場合、置換範囲は対象 start タグの最初の文字から、それと対応する end タグの最後の文字までに限定する。接頭部と接尾部の内容はバイト単位で完全に保持する。`DailyNoteStore` は改行変換を無効にして読み取り、既存の LF または CRLF の規約を検出し、その規約で管理ブロックをレンダリングする。新規ファイルは LF を使用する。既存のマーカー名（`activity`、`advice`、`nippou`、`measurements`）およびレンダリング済みセクション本文との互換性を維持する。

### 6.2 クラッシュセーフな単一ファイル永続化

共通の `src/kaizenlog/storage.py` モジュールで、次を提供する。

```python
atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None
```

このヘルパーは親ディレクトリを作成し、同じディレクトリ内に一意な名前の一時ファイルを書き込み、flush した後に `os.fsync` を呼び出し、`os.replace` でコミットする。コミットされていない一時ファイルは `finally` で削除する。書き込みまたは置換に失敗した場合は `StorageError` を送出し、既存の保存先を変更せずに維持する。対応プラットフォームではディレクトリメタデータの sync をベストエフォートで行ってよいが、Windows 上のディレクトリ `fsync` を正しさの前提にしてはならない。

次のファイル全体の置換では、このヘルパーの使用を必須とする。

- `DailyNoteStore.write_section`。
- `stats.write_stats`。
- `runlog.log_run`。
- `experiments.record_measurement`。
- 新規実験および生成設定の作成。ただし、既存の「すでに存在してはならない」というチェックの後に行う。

`memory.append_entries` は JSONL 履歴を維持し、同時 read-modify-replace の契約を避けるために、追記専用のままとする。メモリ上でバッチを構築し、1回の追記操作を発行し、flush と `fsync` を行わなければならない。ローダーは引き続き、不正な末尾行を無視する。マルチプロセスの順序保証と、中断された最後の Memory レコードの復旧は明示的な対象外とする。

アトミック性はコマンド単位ではなく、ファイル単位である。`generate` が日次ノートのコミットに成功し、その後 stats または実験の書き込みに失敗した場合、コミット済みの日次ノートはそのまま残る。コマンドは失敗を返し、run log の記録が引き続き利用可能であれば失敗したパスを記録する。

`cmd_generate` は最初の書き込みを行う前に、予定される日次ノートと実行中の実験の内容を構築し、マーカーを検証する。これにより、既知のマーカー破損は、いずれかの保存先が変更される前に失敗する。`record_measurement` は更新後の measurements、baseline、status、ファイル内容をコピー上で計算し、アトミックなファイルコミットに成功した後にのみ、渡された `Experiment` オブジェクトを変更する。その後の複数ファイルコミット中に予期しない I/O 障害が発生した場合、文書化された部分更新状態が生じる可能性は残る。

ファイル単位および全体の成功メッセージは、そのメッセージが表すコミットに成功した後にのみ出力する。特に、`cmd_generate` は必須の stats または実験のコミットがまだ保留中の段階で、Activity Log の成功を通知してはならない。

### 6.3 厳格な書き込みなしの dry-run

`advise --dry-run` と `run --dry-run` は、1つの共有プレビューパスを使用する。契約は次のとおりである。

- 許可する読み取り: 設定、対象ノートと lookback ノート、実験、Memory、同梱またはカスタムの system prompt。
- 許可する出力: redact 済みの system prompt、redact 済みの user prompt、および明確な「未送信／未書き込み」の通知を stdout に出力すること。
- 禁止する呼び出し: すべての LLM バックエンド、ActivityWatch 収集、通知、`cmd_backfill`、`cmd_generate`、ノート書き込み、stats 書き込み、実験書き込み、Memory 追記、run-log 書き込み、skill 書き込み、設定書き込み。
- プレビューを構築できる場合の exit code は `0`、入力データが存在しない、または無効な場合は `1`。
- ディレクトリまたは一時ファイルを一切作成しない。

最近のノートから検出した完了済み action status は、プレビューを次回の実際のプロンプトと一致させる目的に限り、メモリ内コピーへ適用する。dry-run が false の場合にのみ永続化する。

実際の助言実行では、検出した status 更新を、LLM が成功し advice ノートがコミットされるまでメモリ内に保持する。その後、新規 action entry と status 更新を1つの Memory バッチとして追記する。日次ノートと Memory のコミットはクロスファイルトランザクションではない。後続の Memory 障害が発生した場合は再実行が必要であり、既存の action ID を変更せずに収束しなければならない。

後方互換性のため、`run --dry-run` は引き続き受け付ける。ただし help と文書では、すでに生成済みの日次ノートから advice ペイロードをプレビューするものと明記する。新しい ActivityWatch の1日分をシミュレーションまたは収集することはない。対象ノートが存在しない場合は `1` で終了し、先に `generate` を実行するようユーザーへ伝える。

### 6.4 設定の探索と検証

設定の優先順位は次のまま維持する。

1. 明示的な `--config PATH`。
2. 設定されている場合は `KAIZENLOG_CONFIG`。
3. 既存のデフォルト検索場所。

明示的なパスまたは環境変数で指定されたパスが存在しない場合はエラーとし、別の場所へフォールスルーしてはならない。探索された設定には、vault を必要とするすべてのコマンド向けに `general.vault_dir` が含まれていなければならない。相対 vault パスは、プロセスの作業ディレクトリではなく、設定ファイルのディレクトリを基準に解決する。

CLI は、設定が存在しない、無効、または不完全な場合に exit code `2` を使用し、トレースバックなしで、実行可能な対処を1つのメッセージとして stderr へ出力する。`doctor` だけは同じ問題を診断結果として stdout へ出力し、`1` を返す。ActivityWatch、LLM、vault、Memory、stats、実験、run log、通知のいずれの処理よりも前にチェックを行う。

利用可能な設定がない場合のコマンド挙動を、次のように固定する。

| コマンド | 挙動 | Exit |
| --- | --- | ---: |
| `kaizenlog --help`、サブコマンドの help | argparse の出力。設定を検索しない | 0 |
| `init-config` | CWD に `kaizenlog.toml` が存在しない場合にのみ作成。既存なら変更しない | 作成時0、既存時1 |
| `skill show` | パッケージ化されたリソースのみを読み取る | 0 |
| `assets export --destination PATH` | 明示的な保存先へのみパッケージ化されたアセットをエクスポート | 成功／同一内容時0、保護競合／失敗時1 |
| `skill install/doctor --vault PATH` | 明示的な vault を使用。設定は不要 | 既存の成功／失敗の契約 |
| `doctor` | 設定の欠落／無効をエラーとして報告し、依存する probe をスキップ | 1 |
| その他すべてのコマンド | 設定エラーと `init-config` の案内を出力。運用 I/O は行わない | 2 |

`Config.vault_dir` の初期値は `None` とし、運用上のフォールバックとして `Path(".")` を持ってはならない。`LLMConfig.backend` の初期値は `"none"` とする。`[llm]` セクションがない場合、または `llm.backend` がない場合は `none` として解決する。既存の明示値は変更しない。

生成される設定と `config.example.toml` は、編集が必要だと明確に分かる `vault_dir = 'C:/path/to/your/ObsidianVault'` と `backend = "none"` を使用する。`claude-code-cli`、`copilot-cli`、またはリモートの `openai-compatible` エンドポイントを選択すると、外部へのプロンプト送信が有効になることを説明する。

### 6.5 LLM 無効時の挙動と診断

`backend = "none"` は、Advisor の失敗ではなく、コマンドレベルで対応する状態である。

| コマンド | `backend = "none"` の挙動 |
| --- | --- |
| `generate` | 通常のローカル生成 |
| `advise` | stdoutへ「LLM は無効です。改善提案をスキップしました。」と出力。ノートも Memory も変更しない。0 を返す |
| `advise --dry-run` | 予定される redact 済みペイロードを構築して出力。0 を返す |
| `run` | `generate` を完了し、stdout の案内とともに advice をスキップし、全体の成功を記録。0 を返す |
| `run --dry-run` | 厳格な dry-run 契約に従う advice プレビューのみ |
| `report --no-llm` | 通常の決定論的レポート |
| `report`（`--no-llm` なし） | バックエンドを選択するか `--no-llm` を追加するよう案内し、1 を返す |
| `doctor` | LLM 機能が無効であることを warning／information として表示。それ自体はエラーとしない |

`advisor.generate_text` は、`none` を指定して直接呼び出された場合に引き続き `AdvisorError` を送出してよい。CLI は、これを呼び出す前に対応済みのスキップケースを捕捉しなければならない。

dry でない `advise` では、日次ノート、実験、Memory を読み取る前に `none` によるスキップを決定する。したがって、無効なバックエンドでは対象ノートが存在する必要がなく、付随的に action state を検出または永続化することもない。dry-run は意図的に、読み取り専用のプレビューパスを進行する。

`doctor._check_llm` に、Copilot のチェックと同様に `shutil.which(cfg.llm.claude_command)` を使う `claude-code-cli` の分岐を追加する。検証するのは実行可能ファイルの探索のみとし、有料またはテスト用のプロンプト呼び出しは行わない。不明なバックエンド名は引き続きエラーとする。

利用可能な設定がない場合、`doctor` は最初にその事実を報告し、CWD から導出したデフォルトを診断するのではなく、vault、ActivityWatch、LLM、AI 作業履歴、run history のチェックをスキップする。

### 6.6 プライバシーと外部送信の契約

文書と doctor の出力では、次の用語を一貫して使用する。

- **ローカル収集／保存:** ActivityWatch の読み取り、分類、Markdown／stats／Memory の保存、patterns、prompt mining、決定論的レポート、`backend = "none"` は、LLM プロバイダーへデータを送信しない。
- **ローカル LLM エンドポイント:** loopback host（`localhost`、`127.0.0.1`、または `::1`）を使用する `openai-compatible` は、ローカルとして説明する。KaizenLog は、マシン全体またはモデルスタックにリスクがないとは主張しない。
- **クラウドまたは不明な外部パス:** Claude Code CLI、Copilot CLI、および loopback でない `openai-compatible` URL は、解決済みの system prompt と user prompt を第三者へ送信する可能性がある。カスタムの企業内エンドポイントはプライベートと仮定せず、「remote or unknown」と表示する。

`advise` の user prompt には、次の内容が含まれる可能性がある。

- アプリ／ウィンドウ由来のタイムラインテキストを含む、現在レンダリング済みの Activity Log。
- 設定された lookback 期間のカテゴリー要約。
- 日次ノートから抽出した手書きの focus および task テキスト。
- 実行中の実験のタイトル、仮説、metrics、直近の measurements。
- 未完了および直近で完了した Kaizen Memory の actions。

LLM レポートのプロンプトには、現在の Activity Log と、手書きの focus／task テキストが含まれる可能性がある。カスタムの system-prompt ファイルも、system prompt テキストとして送信される。

設定済みの redact は、送信直前に、解決済みの system prompt と user prompt の両方へ適用する。Dry-run は同じ redact 後の文字列を出力する。redact は、機密情報がすべて削除される保証ではなく、ベストエフォートの正規表現置換として文書化する。

設定で `none` 以外のバックエンドを明示的に選択することを、`v1.3.1` のオプトイン手段とする。追加の同意データベースまたは対話型プロンプトは導入しない。明示的なクラウドバックエンドが設定済みの既存設定では、その選択を維持する。アップグレードノートでは、`advise --dry-run` を実行して redact を確認するよう案内する。

`doctor` は、選択されたデータパスのカテゴリー、エンドポイントまたは CLI の種類、設定済み redact pattern の数、dry-run の監査コマンドを出力する。外部バックエンドで pattern が0件なら警告するが、実行自体は禁止しない。API key またはその値は一切出力しない。

プライバシーガイドには、その保証の外側にある2つの境界も記載する。Claude／Copilot CLI アダプターは現在、プロンプトテキストをプロセス引数として渡すため、十分な検査権限を持つ別のローカルプロセスから見える可能性がある。Dry-run の出力は、ターミナルのスクロールバック、shell capture、またはリダイレクト先のファイルに残る可能性がある。さらに、インストール済みの Claude Code skill の実行は、より広い vault コンテキストを読み取り得る別個の agent path である。`advise` の redact と dry-run の契約は、その外部 agent session を統制しない。

### 6.7 パッケージとリリースのゲート

バージョン `1.3.1` は、`pyproject.toml`、`src/kaizenlog/__init__.py`、changelog の見出し、Git tag `v1.3.1`、GitHub Release で一致しなければならない。バージョンの不一致はハードエラーとする。

scheduler script と Obsidian Bases template を、`src/kaizenlog/assets/scripts/` および `src/kaizenlog/assets/templates/` 配下のインストール済みリソースとする。`kaizenlog assets export --destination PATH` は、両方を同じディレクトリ構造でエクスポートする。保存先を必須とするため、このコマンドは設定なしで動作し、vault を推測しない。同一ファイルは no-op として扱い、`--force` がない限り内容が異なるファイルの置換を拒否し、強制置換の前には `.bak` コピーを書き込む。README と usage guide は、リポジトリ相対パスではなく、このコマンドを使用する。

`pyproject.toml` は、これらのアセットを package data として宣言し、インストール済みメタデータに Project（`https://github.com/awano27/KaizenLog-`）、Issues（`https://github.com/awano27/KaizenLog-/issues`）、Changelog（`https://github.com/awano27/KaizenLog-/blob/main/CHANGELOG.md`）の URL を追加する。`pipx` は wheel をインストールするため、ソース専用の `MANIFEST.in` による解決では不十分である。

CI は現在の Windows／Linux および Python 3.11／3.12 のテストマトリクスを維持し、さらに次のアーティファクトゲートを追加する。

1. クリーンな checkout から `python -m build` を使用して wheel とソースディストリビューションの両方をビルドする。
2. `python -m twine check --strict dist/*` でメタデータ検証を実行する。
3. wheel と sdist を検査し、4つすべての prompt template、3つすべての同梱 `SKILL.md`、scheduler script、Bases template、license／readme メタデータ、CLI package が含まれることを確認する。
4. tests、cache、bytecode、ローカル設定、`PLAN.md`、build output、無関係な workspace ディレクトリなどが wheel に含まれていれば拒否する。sdist には tests と distribution verifier を含めてよいが、ローカル／生成データは除外しなければならない。
5. editable install ではないクリーンな virtual environment を作成する。
6. wheel をインストールし、別途 sdist からビルドした package もインストールし、それぞれで `python -m pip check` を実行する。
7. README の主要経路であるため、Windows と Ubuntu でローカル wheel の `pipx install` スモークを実行する。
8. 一時作業ディレクトリから、`kaizenlog --help`、`kaizenlog skill show`、`kaizenlog assets export --destination <temp>`、`kaizenlog init-config` を実行する。
9. `kaizenlog` を import し、`__version__ == "1.3.1"` を検証し、パッケージ化された各 prompt、skill、asset を解決し、生成済み設定が `backend = "none"` を使用することを確認する。
10. CWD または `PYTHONPATH` がスモーク環境へ漏れたことだけを理由にソースツリーを import できている場合は失敗させる。

tag をトリガーとする `.github/workflows/release.yml` は、tag の commit 上で同じテストとアーティファクトゲートを再実行し、新たにビルドして検証した同一アーティファクトを使用する。tag のバージョンが一致しない場合、または commit が `main` に含まれない場合は停止する。GitHub Environment の手動承認後に PyPI Trusted Publishing を通じて公開し、開発者のマシンでビルドしたアーティファクトをアップロードしてはならない。この workflow は wheel と sdist の SHA-256 manifest を保存し、PyPI への公開成功後、manifest と `1.3.1` の changelog を含む GitHub Release を作成する。

次の条件をすべて満たすまで、リリースは完了していない。

- `https://pypi.org/pypi/kaizenlog/json` が `200` を返し、`1.3.1` を報告する。
- 新しいクリーン環境で `pipx install kaizenlog` とスモークコマンドを実行できる。
- GitHub に不変な tag `v1.3.1` と、それに対応する release が表示される。
- README と `docs/USAGE.md` が、検証済みのインストール経路と実際のプライバシー境界のみを説明している。

tag を push する前に、Trusted Publishing で使用するリポジトリ／workflow／environment に対して、リモートの PyPI project を設定しなければならない。公開用の認証情報を commit してはならない。

## 7. エラーモデルとユーザー向けメッセージ

想定される運用エラーは CLI 境界で捕捉し、デフォルトではトレースバックを表示しない。

| 失敗クラス | Exit | メッセージに必須の内容 |
| --- | ---: | --- |
| 設定の欠落／無効 | 2 | パスまたは検索結果、不足している key、`init-config` の案内 |
| 管理マーカーの不正 | 1 | 対象ファイル、マーカー、完全性を満たさない理由、手動修復の案内、「file not changed」 |
| ストレージのコミット失敗 | 1 | 対象パス、操作、元のエラークラス、旧ファイルが保持されたかどうか |
| ActivityWatch／LLM／プライバシーの runtime failure | 1 | 既存の実行可能な対処説明 |
| 意図的な LLM スキップ（`none`） | 0 | バックエンドが無効であることと、どのローカル処理が完了したか、または完了しなかったか |

失敗のロギングはベストエフォートとする。run-log の書き込み失敗を出力しなければならず、一次エラーを隠してはならない。通知の失敗が一次コマンド結果を置き換えてはならない。

## 8. 互換性と移行

- 一括データ移行は不要である。既存の有効な notes、stats、experiments、Memory JSONL、run logs は引き続き読み取り可能である。
- 既存の明示的なバックエンド選択は引き続き動作する。`llm.backend` を省略していた設定は LLM 無効となり、アップグレード案内を受け取る。
- `general.vault_dir` を省略していた設定は、CWD 配下へ書き込むのではなく停止する。
- 相対 `vault_dir` 値は設定ファイルのディレクトリを基準に解決する。以前の CWD 相対の挙動とは異なるパスを選択する可能性があるため、この変更を文書化する。
- 既存の不正な形式のノートは変更せずに残す。コマンドは正確なマーカー問題を特定し、ユーザーが手動で修復した後に再実行する。
- `run --dry-run` は引き続き受け付けるが、完全な生成シミュレーションではなく、advice ペイロードのプレビューとして文書化する。
- JSON スキーマとマーカーテキストは変更しないため、有効なファイルではダウングレードが可能なままである。ダウングレードすると新しい安全性保証が失われるため、推奨しない。

## 9. 想定される実装範囲

実装計画では、ファイルが不要であることを実証できた場合にこの一覧を狭めてもよいが、設計の改訂なしに製品範囲を拡大してはならない。

### Runtime と永続化

- `src/kaizenlog/storage.py` — 新しい atomic-write primitive と `StorageError`。
- `src/kaizenlog/vault.py` — 厳格な管理マーカーパーサー、`VaultFormatError`、アトミックな日次ノート書き込み。
- `src/kaizenlog/cli.py` — dry-run 境界、設定のコマンドマトリクス、`none` の挙動、エラーマッピング、安全なテンプレート。
- `src/kaizenlog/config.py` — 必須設定／vault のセマンティクス、パス解決、安全な LLM デフォルト。
- `src/kaizenlog/doctor.py` — 設定欠落時の短絡、Claude CLI のチェック、データパス要約。
- `src/kaizenlog/advisor.py` と `src/kaizenlog/nippou.py` — 最終的な system／user dispatch 境界での redact。
- `src/kaizenlog/memory.py` — 純粋なメモリ内 status 投影と、flush するバッチ追記。
- `src/kaizenlog/stats.py`、`src/kaizenlog/runlog.py`、`src/kaizenlog/experiments.py` — ファイル全体のアトミック置換。

### 製品面とリリース面

- `config.example.toml`、`README.md`、`docs/USAGE.md`、`CHANGELOG.md`。
- `pyproject.toml`、`src/kaizenlog/__init__.py`。
- `.github/workflows/tests.yml`、新規の `.github/workflows/release.yml`、および shell の重複によってゲートが分かりにくくなる場合は小さな package-smoke script。
- `src/kaizenlog/assets/` および asset-export helper／CLI 分岐 — 正規のパッケージ化済み scheduler および Bases リソース。リポジトリ相対の重複物は削除するか、内容がずれ得ない文書へ置き換える。
- `tests/` 配下の対象を絞ったテスト。unit test では、実際の LLM、実際の PyPI publish、実際の ActivityWatch service を使用しない。

## 10. テストと受け入れマトリクス

### 10.1 マーカーとストレージのテスト

- 有効なペアがない場合は正確に1回だけ追記し、冪等性を維持する。
- 有効なペアの置換では、接頭部と接尾部を正確に保持する。
- start のみ、end のみ、重複 start、重複 end、逆順、ネスト、マーカー間の重なりの各ケースで `VaultFormatError` を送出する。
- 置換本文に KaizenLog の管理マーカータグが含まれる場合、書き込み前に拒否する。
- 不正な形式の各ケースで、処理前後のディスク上のバイトを比較し、差分がないことを確認する。
- 有効な CRLF および LF のノートは元の改行規約を維持し、マーカー外の領域はバイト単位で同一に保つ。
- `os.replace` より前の fault injection では、古い保存先をそのまま維持し、一時ファイルを削除する。
- 置換成功後は一時ファイルが残らず、有効な UTF-8 内容になる。
- 実験 measurement には、日次ノートと同じマーカー完全性の挙動を適用する。
- 実行中のいずれかの実験に壊れたマーカーがある場合、日次ノート、stats、その他の実験を変更する前に `generate` の preflight で失敗する。
- 実験のコミットに失敗した場合、ファイルとメモリ内 `Experiment` フィールドの両方を変更しない。

### 10.2 Dry-run のテスト

- 新たにチェック済みとなった Memory action を持つ fixture では、プレビューに更新後の action state を反映するが、Memory のバイトは変更しない。
- バックフィル対象日がある状態での `run --dry-run` は、`cmd_backfill`、ActivityWatch、`cmd_generate`、LLM、通知、writer のいずれも呼び出さない。
- 両方の dry-run コマンドの前後で再帰的な filesystem snapshot が同一である。
- ノート欠落および不正マーカーによるプレビュー失敗は、run-log entry またはディレクトリを作成せずに `1` を返す。
- system prompt と user prompt の出力は redact 後のものであり、backend mock の呼び出し回数は0である。

### 10.3 設定、バックエンド、doctor のテスト

- 設定用の環境変数がない空の一時 CWD では、すべての設定依存コマンドが `2` を返し、何も作成しない。
- 存在しない明示的な `--config` と、存在しない `KAIZENLOG_CONFIG` のパスは、別の設定へフォールスルーせず失敗する。
- 無効な TOML および `general.vault_dir` のない設定は、同一の I/O なし設定エラー契約に従う。
- 設定なしの `doctor` は `1` を返し、ActivityWatch または LLM の probe を行わない。
- `skill show` と `skill ... --vault PATH` は設定なしのマトリクスに従う。
- `vault_dir` の欠落は拒否し、相対値は設定ディレクトリを基準に解決する。
- 既存の明示的なバックエンド値は load 後も維持し、バックエンドがない場合は `none` として解決する。
- Claude の doctor は `shutil.which` の結果に応じて成功／失敗し、不明なバックエンドとして報告されない。
- `none` での `run` は生成を1回行い、advice をスキップし、`0` で終了し、成功を記録する。
- `none` での `advise` は日次ノートも Memory も変更せず、`0` で終了する。
- `none` での LLM report は `--no-llm` の案内とともに失敗し、決定論的 report は成功する。

### 10.4 プライバシーのテスト

- カスタムの system prompt テキストと、user-prompt のすべての構成要素の両方に redact を適用する。
- Dry-run の出力と、mock した実際の backend dispatch が受け取る引数は、redact 後に同一である。
- 無効な redact 式は、LLM、ノート、Memory、stats、実験、run log、通知のいずれの処理よりも前に失敗する。
- Doctor は loopback の OpenAI-compatible URL を local、loopback でない URL を remote or unknown と分類する。
- Doctor の出力は Claude と Copilot CLI を cloud path として特定し、secret の値を一切含まない。
- README、usage guide、生成済み template、example config、CLI help、doctor は同じ local／cloud／none の用語を使用する。

### 10.5 パッケージとリリースのテスト

- すべての unit test と integration test が、Python 3.11 および 3.12 の Windows と Ubuntu で成功する。
- wheel と sdist の build、metadata check、resource inspection、clean install、CLI smoke、version parity がすべて成功する。
- fixture／test build で prompt、skill、scheduler、template のいずれかの resource を意図的に省略すると、artifact gate が失敗する。
- クリーンな wheel install で scheduler と template をエクスポートできる。再エクスポートは冪等であり、内容が異なるファイルは保護され、強制エクスポートでは backup が作成される。
- release workflow は、tag のない commit、または package metadata とバージョンが異なる tag から公開できない。
- 公開後の検証で、PyPI `1.3.1`、クリーンな `pipx` install、GitHub Release `v1.3.1` を確認する。

## 11. リリース手順とロールバック

1. 専用ブランチ上で TDD により実装し、無関係な untracked file は stage しない。
2. 完全なテストマトリクスとアーティファクトゲートを、リリース対象の正確な commit 上で実行する。
3. 代表的な機密文字列を使い、プライバシー文言と dry-run の transcript を手動レビューする。
4. リポジトリへ token を保存せず、PyPI Trusted Publishing を設定して検証する。
5. レビュー済みのリリース commit を merge し、annotated tag `v1.3.1` を作成し、tag workflow に build と publish を実行させる。
6. 提供開始を告知する前に、PyPI JSON、クリーンな `pipx` installation、パッケージ化された resources、GitHub Release を検証する。

PyPI のバージョンは不変である。公開したアーティファクトに欠陥がある場合、ファイルを置き換えたり tag を再利用したりしてはならない。`1.3.1` を yank し、GitHub Release に目立つ警告を付け、証拠を保持し、`1.3.2` として修正を進め、インストール案内を更新する。PyPI がバージョンを受理する前に公開が失敗した場合は、必要に応じて未公開の GitHub draft／tag のみを削除または修正し、レビュー済み commit から再実行する。公開利用された tag を黙って移動してはならない。

## 12. リリース成功条件と後続作業

すべての受け入れチェックが成功し、新規ユーザーが文書と矛盾することなく、クリーンインストール、ローカルのみの生成、読み取り専用のペイロード監査、任意かつ明示的な LLM 有効化を実行できる場合、`v1.3.1` は成功とする。

安定したパッチリリースの後、別個の `v1.4` 設計で、オンボーディング、デモコンテンツ、スクリーンショット、リポジトリメタデータ、サポート／セキュリティポリシー、導入事例、測定可能な採用拡大を扱える。これらは評判向上に有用だが、ここで確立した検証済みの信頼境界の上に構築しなければならない。
