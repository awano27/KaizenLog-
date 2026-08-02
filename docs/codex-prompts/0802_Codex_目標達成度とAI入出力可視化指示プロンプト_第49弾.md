# 第49弾: 日誌5要件の完成 — 縮退バグ修正・目標達成度・AI入出力可視化 指示プロンプト

対象リポジトリ: `C:\develop\KaizenLog\KaizenLog-`（判定は HEAD + ワーキングツリー）

## 0. 背景と目的

日誌だけで次の5点が分かる状態にする（ユーザー要求）:

1. 1日の作業記録がわかる
2. 目標が登録できる
3. 目標の達成度がわかる
4. AI作業のインプット・アウトプットがわかる
5. AI作業への改善案がわかりやすく出力される

現状ギャップ（2026-08-02 の実ノート・実 stats で確認済み）:

- 【P0】`cmd_generate` が stats のフィンガープリントを `_finalize_note_layout`（cli.py:856）の**前**に保存する（cli.py:581-599）。finalize の `consolidate_disclaimers` が ACTIVITY 本文の ※ 行を `[^n]` に書き換えるため、`advise` 側の照合（cli.py:1523-1539）が必ず mismatch になり当日 stats が捨てられ、縮退文「当日の確定統計がないため、作業状況を評価できません」（advice_evidence.py:60-61, 700-723）が出る。digest も `digest_skipped`（cli.py:913-916、runs.jsonl 2026-08-02T12:35Z で実証）。**要件5がこれで死んでいる。**
- 【P0】`consolidate_disclaimers`（vault.py:180-271）が既存 `[^n]:` 定義を無条件 seed（vault.py:190-201）し重複除去しない（vault.py:266）ため、実行のたびに脚注が同文4件ずつ増殖。実ノートは [^1]..[^16] で本文参照は [^13]..[^16] のみ（orphan 12件）。
- 目標は `kaizenlog goal`（cli.py:1756, goal.py:136）で登録可能だが、**達成度の概念が皆無**（weekly_context.py:445「観察のみ・達成判定なし」、prompts/daily_advisor.md:33 で断定禁止）。要件3が未実装。
- AI作業は入力が40字タイトルのみ（aiwork.py:348-361, SESSION_TITLE_MAX）、**アシスタント出力は完全破棄**（aiwork.py:537-565 は usage/tool_use しか読まない）。要件4が「何を依頼し何が返ったか」レベルで読めない。
- 日報は手動 `kaizenlog report --write` のみ（cli.py:3484-3487, 2391）で夜間 `run` に含まれず、書き忘れた日は要件1の記録が欠ける。

## §A 統計フィンガープリント整合（P0）

- **A1**: `cmd_generate` で stats に保存する `activity_sha256` を、`_finalize_note_layout` 実行**後**の実ノート上の ACTIVITY 区間本文から計算する（`write_stats` を finalize 後に移す、または finalize 後に再読込して stats を更新する。どちらでも可だが、digest の verified 判定（cli.py:830-843）も同じ最終本文と整合させること）。
- **A2**: `consolidate_disclaimers` を冪等にする（同一ノートに2回適用しても本文・脚注が変化しない）。§B の前提。
- **A3**: 受け入れ: tmp ボールトで `generate` → `advise` を連続実行して (a) source_status == "verified"、(b) ADVICE に縮退文「当日の確定統計がない」が含まれない、(c) runs.jsonl に digest_skipped が出ない。

## §B 脚注の増殖停止（P0）

- **B1**: `consolidate_disclaimers` は脚注ブロックを毎回ゼロから再構築する: (a) 現在の本文に参照が残る注のみ保持（orphan 定義は破棄）、(b) 同一文面は1定義に統合して複数参照で共有、(c) 番号は 1 から振り直し。
- **B2**: 受け入れ: `generate` + `advise` を2周しても脚注は各ユニーク文面ちょうど1件・orphan `[^n]` 定義ゼロ。既存の汚染ノート（[^1]..[^16] 状態）も次の実行で自動修復される。

## §C 目標の達成度（P1・要件2/3）

- **C1**: `kaizenlog goal --achieved <0-100>`（`--date` 併用可）を追加。GOAL 区間の目標行に「達成度: NN%（自己申告）」を追記し、stats に `goal_achieved` を保存（`goal_stats_fields` 拡張: goal.py:165-180 → stats.py:265-269。後方互換の追加キーのみ）。
- **C2**: digest の目標行（digest.py:173-180）を拡張: `目標: <text> ｜ 達成度: NN%（自己申告）`。未申告なら `達成度: 未申告（kaizenlog goal --achieved N で記録）`。目標カテゴリがあれば実測分数を併記（advice_evidence.py:441-451 `_goal_category_minutes` 相当をヘルパー共有で）。
- **C3**: nippou の目標行（nippou.py:617-620）にも同じ達成度を出す。【成果・進捗】（nippou.py:424 `_outcome_lines`）に「目標カテゴリ実測: <カテゴリ> N分」の対応1行を追加（目標カテゴリがある日のみ）。
- **C4**: weekly の目標トレース（weekly_context.py:445-470）に達成度列（自己申告 NN% / 未申告は —）と「申告あり日の平均達成度」を追加。
- **C5**: prompts/daily_advisor.md:33 と prompts/privacy_safe.md:28 の断定禁止を更新: 「自己申告の達成度は転記可。AI 自身による達成/未達の断定は引き続き禁止」。
- **C6**: `morning`（cli.py:1259）実行時、当日ノートに GOAL 区間が無ければプレースホルダ `🎯 今日の目標: （未設定 — kaizenlog goal "..." で登録）` を書く。実テキストの唯一のライターは引き続き `cmd_goal`（プレースホルダは上書きされる前提。vault.py:70 のコメントも更新）。

## §D AI入出力の可視化（P1・要件4/5）

- **D1**: aiwork.py のアシスタント分岐（537-565）で text ブロックを読み、セッション**最後**のアシスタント本文を `last_reply_digest` として AISession に保持。順序は **redact → 先頭120字切詰め**（切詰め後 redact の境界漏れリスク HANDOFF.md:97 を再現しないこと）。
- **D2**: 「主なセッションの中身」（aiwork.py:1837-1881）を拡張: 対象を往復上位3 → 「編集>0 または往復上位」の最大5セッションに。各セッションを `依頼:`（first prompt digest 80字）と `成果:`（last_reply_digest + 変更ファイル最大5 + テスト実行有無）の**入出力対**で表示。ブラウザ計測（aiwork_browser.py）は `出力 N字（本文ログなし）` と明示。
- **D3**: stats の `session_digests`（aiwork.py:1319-1372）に `last_reply_digest` を追加保存。
- **D4**: advice evidence に摩擦ワーストセッションの 依頼/成果 digest 行（[F] 行追加、advice_evidence.py）を加え、「AI作業の見立て」が具体的セッションを引用して改善案を1つ出せる材料にする。redact 済みのもののみ。

## §E 日次記録の完全性（P2・要件1）

- **E1**: `kaizenlog run`（cli.py:4150-4163）の advise 後に nippou 決定論版を自動書き込み（`cmd_report --write` 相当。LLM 変種は呼ばない）。config `[nippou] auto_write = true` 既定 ON・OFF 可。config.example.toml を同期（既定 config で機能が死なないこと）。

## テスト

- 新規 `tests/test_round49_goal_achievement_ai_io.py`（最低16ケース）。命名は項目ID対応: `test_a1_...`, `test_c2_...` 形式。
- 各項目は実装の1行を無効化するとテストが落ちること（ミューテーション確認）。
- tmp_path ベースの一時ボールトを使用。実 LLM 呼び出しなし（決定論経路のみ）。
- 回帰: `pytest -q` 全 green（基線 1007 passed）。仕様変更で既存テスト（test_round26_goal_trace.py / test_round46_session_io.py / test_round48_read_value.py 等）を更新する場合は、変更理由をテスト内コメントに1行残す。

## 禁止事項（恒常）

- **git commit / push 禁止**（変更はワーキングツリーに残すだけ。過去に違反7回）
- 実 LLM 呼び出し禁止（実装確認・テストとも決定論経路のみ)
- 実ボールト `C:\develop\obsidian` への書き込み禁止（テストは tmp_path）
- ssh / scp / リモートアクセス禁止
- stats JSON・マーカー仕様の破壊的変更禁止（後方互換のキー追加のみ可）
- マーカー区間外の手書きテキスト不可侵・外部に出す文字列は redact 後のもののみ

## 完了報告フォーマット

- 項目ID（A1〜E1）ごとに 実装済み/未実装 と主要変更点1行
- 変更ファイル一覧
- `pytest -q` の結果（passed 数を明記）
- 自己判定は書いてよいが、最終判定はレビュー側（HEAD + ワーキングツリーで再検証）が行う
