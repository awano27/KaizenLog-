# Codex 実装指示: UX改善5件（朝の到達性・配置・可読性・追いつき・北極星の日次表示）— 第8弾

対象リポジトリ: `C:\develop\KaizenLog\KaizenLog-`（Python / pytest）

**前提: 第1〜7弾適用済みの HEAD（5e74751 以降）。** 実装順は §U2 → §U5 → §U1 → §U4 → §U3 を推奨（U1 が U2/U5 の出力を使い、U4 が U1 に統合されるため。U3 は独立だが影響範囲が広いので最後）。

## 背景

夜間の全自動ループは完成したが、UX 上のボトルネックが残っている: (1) 朝の到達性がノート開封頼み、(2) テンプレート併用時に「📌 今日のアクション」が末尾に付く、(3) `[F1]` や `PASS: context_switches <= 40` が開発者向けの見た目、(4) 21:30 前に PC を落とした日は提案・採点が翌晩まで欠落、(5) 消化率/PASS率が CLI でしか見えない。

設計原則（従来どおり厳守）: LLM にファイルを触らせない / マーカー区間の外は変更しない / 夜間・朝の無人実行を止めない（失敗は縮退して続行し、実行ログに残す）。

---

## §U2 「📌 今日のアクション」を frontmatter 直後に挿入

`src/kaizenlog/vault.py`、`src/kaizenlog/config.py`、呼び出し元 `cli.py`

1. `upsert_section(content, marker, section_md, position="bottom")` に `position` 引数を追加
   - `"top"`: **区間が未存在の場合のみ**、frontmatter（先頭が `---` 行で始まり次の `---` 行で閉じるブロック）の直後に挿入する。frontmatter が無ければファイル先頭。**既存区間がある場合は現在位置で置換**（区間を移動しない — 区間外のテキストとの相対位置をユーザーが前提にしている可能性があるため。この理由をコメントに明記）
   - `"bottom"`: 従来挙動
2. `DailyNoteStore.write_section(...)` にも `position` を追加して素通しする
3. 設定: config に `actions_position`（値 `"top"` / `"bottom"`、既定 `"top"`）を追加。適切な既存セクションに置き、不正値は ConfigError。`init-config` のテンプレートにコメント付きで追記
4. ACTIONS_MARKER への書き込み箇所（`cmd_generate` / `cmd_advise` / §U1 の morning）はすべて設定値を渡す。ACTIVITY / ADVICE は従来どおり bottom 固定

テスト: frontmatter あり/なしでの top 挿入位置 / 既存区間は位置を維持したまま置換 / bottom 設定で従来挙動 / 不正値で ConfigError。

## §U5 消化率/PASS率を「📌 今日のアクション」に常時表示

`src/kaizenlog/memory.py` `render_actions_section`

1. 見出し直下の説明行の次に1行追加: `直近14日: 消化率 42%（12件中5件）/ 自動判定 8件 / PASS率 75%`
   - `compute_action_stats(entries, target_day)` を再利用。分母0の率は `-`。`proposed == 0` の場合はこの行自体を省略
2. アクション0件で統計だけある場合の扱い: 従来どおりセクション自体を書かない（None）— 統計のためだけにセクションを作らない

テスト: 統計行の出現と文言 / proposed 0 で行省略 / アクション0件で None 維持。

## §U1 朝の到達性: `kaizenlog morning` コマンド＋朝タスク登録

`src/kaizenlog/cli.py`、`src/kaizenlog/notify.py`、`scripts/register-task.ps1`、セットアップウィザード（`src/kaizenlog/setup.py` のタスク登録箇所）

1. 新サブコマンド `kaizenlog morning [--date YYYY-MM-DD]`（省略時は今日）。処理順:
   1. **昨日の追いつき**（§U4 のヘルパーを呼ぶ。LLM 失敗・AW 未起動でも警告して続行）
   2. **今日のノートに 📌 セクションを保証**: `load_entries` → `render_actions_section(entries, today, 現ノート)` → None でなければ `write_section(..., position=設定値)`（夜間に作成済みでも最新の Memory 状態で再描画。チェック保持は既存実装が担保）
   3. **Windows トースト通知**: `今日のアクション {未完了n}件 / 昨日の判定 ✅{pass数} ❌{fail数}`。**アクション本文は通知に載せない**（ロック画面に固有名詞を出さないため。この理由をコメントに明記）。昨日の判定 = `verdict_date == 昨日` のエントリ集計。n=0 かつ判定0件なら通知しない（静かに終了）
   4. コンソールにも同内容を出力し、`runlog.log_run` に `morning` として記録
2. `notify.py` の既存トースト機構を汎用化して再利用する（現在は失敗通知専用なら、タイトル・本文を取る汎用関数に切り出す。既存の失敗通知の挙動は変えない）
3. タスク登録: `scripts/register-task.ps1` に `-MorningTime`（例 `"08:30"`、未指定なら登録しない）を追加し、タスク名 `KaizenLog Morning` で `kaizenlog morning` を登録。セットアップウィザードにも同じ選択肢を追加（既定 ON・時刻 08:30、既存の 21:30 登録処理の実装様式に合わせる）
4. README: 朝の流れ（morning コマンドと通知）を1段落追記

テスト: 通知内容の組み立て（件数・判定集計、0件で通知なし）/ 📌 再描画でチェック保持 / runlog 記録。トースト送出自体はモック。

## §U4 前夜に走らなかった日の追いつき（retro-advise）

`src/kaizenlog/cli.py`

1. ヘルパー `catch_up_yesterday(cfg, today) -> None` を新設:
   1. 昨日の stats が無ければ `cmd_generate(昨日)` を実行（既存 `missing_days` を利用。**対象は昨日のみ** — それ以前の深い欠損は従来の backfill に任せる。generate 内で一昨日分の判定・実験計測も自動で走る）
   2. 昨日のノートに ACTIVITY 区間があり ADVICE 区間が無い場合のみ `cmd_advise(昨日)` を実行（retro-advise）。この提案の「明日の最小アクション」は今日向けなので朝でも価値がある（この理由をコメントに明記）。`llm.backend == "none"` ならスキップ
   3. 各ステップの失敗は警告出力＋runlog 記録にとどめ、呼び出し元の処理を止めない
2. 呼び出し箇所: §U1 の `morning` 冒頭、および毎晩の `run` の自動キャッチアップ処理（既存の backfill 呼び出し箇所の直後）。同日二重実行しても冪等（ADVICE 区間が既にあれば何もしない）
3. `advise` の dry_run とは無関係（retro-advise は常に実書き込み）

テスト: 昨日 stats 欠損→generate が呼ばれる / ACTIVITY あり ADVICE なし→advise が呼ばれる / ADVICE あり→何もしない / advise 失敗でも例外が伝播しない（LLM はモック）。

## §U3 提案表示の可読性向上（機械トークンを裏方に下げる）

`src/kaizenlog/advice_format.py`、`src/kaizenlog/advisor.py`（`advice_contract_errors`）、`src/kaizenlog/verdict.py`、`skills/daily-kaizen/SKILL.md`

**不変条件**: JSON 層の検証（fact_ids・数値再掲禁止・意味ガード・機械構文）は一切緩めない。変えるのは**レンダリング後の見た目**だけ。KZN ID 付与（`assign_action_ids`）・PASS 解析（`parse_pass_condition`）・判定書き戻し・チェック検出は引き続き動くこと。

1. `render_advice_markdown` の出力を次に変更:
   - 今日の改善提案: `1. {interpretation}。{proposal}。翌日見る指標: {next_metric}`（**先頭の `[F#]` を表示しない**。根拠は JSON 層で検証済み。daily-kaizen スキルが先行して採用した「保存する文章には F-ID を表示しない」方針に CLI 側も揃える）
   - AI作業の改善: `- {text}`（`[F5]` 等を表示しない）
   - 明日の最小アクション: `- [ ] {action}｜PASS: {pass}{注記}｜FAIL: {fail}`
     - `{pass}` が機械構文のとき `{注記}` = `（{METRIC_DESCRIPTIONS の説明を短くした日本語ラベル}）` を付ける。例: `｜PASS: context_switches <= 40（コンテキストスイッチ回数）｜FAIL: 41回以上`。自由文 PASS には注記なし
     - ラベルは `experiments.METRIC_DESCRIPTIONS` から導出する共通関数にする（`category_minutes:エンタメ` → `エンタメの時間（分）` のようにサフィックスを埋める）
2. `verdict.parse_pass_condition`: PASS セグメント末尾の全角括弧注記 `（…）` を除去してからマッチする（レンダラが付けた注記で機械判定が壊れないように）。半角 `(...)` も同様に許容
3. `advice_contract_errors`（レンダラのインバリアント兼スキル出力仕様）を新形式に更新:
   - 削除: アクション行の `[F#]` 開始要求、各項目の F-ID 引用要求、「AI作業の改善」の F4/F5 引用要求、観測数値再掲の禁止ルール（いずれも JSON 層で同等以上の検証が済んでおり、レンダリング後のテキストでは検証不能になったため。削除理由をコメントに明記）
   - 維持: 見出し集合・回数・順序、番号付き1〜3件、チェックボックスは最小アクションのみ、PASS/FAIL の存在と数値条件、機械構文の既知指標チェック（**注記括弧を許容**するよう更新）、KZN 禁止、コードフェンス禁止
   - `render_advice_markdown` のインバリアント（意味violation→AdviceContractError / 構造→renderer bug の分類）は維持
4. `skills/daily-kaizen/SKILL.md`: 出力フォーマット例を新形式（F-ID 非表示・PASS 注記あり）に同期。「分析中は F-ID で照合、保存文には表示しない」の既存方針と矛盾しないこと
5. 既存ノートとの互換: 判定書き戻し・done 検出は KZN ID で行を特定するため旧形式の行にもそのまま作用する（変更不要だが、旧形式行を含むフィクスチャで回帰テストを1本追加）

テスト: 新形式のゴールデン出力 / ラウンドトリップ（render → assign_action_ids → parse_pass_condition が注記付き PASS を解析 → judge_entries 判定）/ 注記付き・注記なし・自由文 PASS の3系統 / 旧形式行への判定書き戻し回帰 / インバリアント分類の維持。

---

## 受け入れ条件

- `python -m pytest -q` 全件通過（現状 329 passed からの増加のみ、既存デグレなし）
- §U3 のラウンドトリップテストで「読みやすい表示のまま機械ループ（ID付与→PASS解析→判定→書き戻し）が壊れていない」ことを実証
- `kaizenlog morning` を LLM モック環境で通し実行するテスト（追いつき→📌再描画→通知組み立て）が存在する

作業完了時: 変更ファイル一覧、§U1〜U5 それぞれの対応状況、pytest 結果（要約）を報告すること。

## 禁止事項（毎回共通）

- git commit / push / branch 操作をしない（変更はワーキングツリーに残すだけ）
- ssh / scp / リモートアクセスをしない
- DB スキーマ変更をしない
- テスト・実装から実 LLM・ネットワークを呼び出さない
- 外部ライブラリを追加しない（標準ライブラリのみ。トースト通知は notify.py の既存方式を踏襲）
- タスクスケジューラへの登録をテストから実行しない（登録スクリプト・ウィザードのロジックはドライラン/モックで検証）
- バージョン番号の変更・CHANGELOG のリリース節の追加をしない（README への機能説明追記は可）
- マーカー区間外のノート内容・手書きテキストを変更するコードを書かない
