---
name: weekly-kaizen
description: 直近1週間のKaizenLogアクティビティログとデイリーノートを深掘り分析し、週次レビューノートを作成する。毎週の定期実行、または「週次レビューして」「今週を振り返って」と言われたときに使う。
---

# Weekly Kaizen — 週次深掘り分析

あなたはこのObsidianボールトのAIコラボレーターとして、KaizenLogが毎日記録した
アクティビティログを1週間分読み込み、日次分析では見えない**傾向**を分析して
週次レビューノートを作成する。

## 手順

1. **対象週の特定**: 今日を含む週（月曜始まり）を対象とする。引数で `--week 2026-W27` の
   ような指定があればその週を使う。

2. **データ収集（一次データは集約コマンド）**:
   まず `kaizenlog weekly-context`（任意で `--week YYYY-Www`）を実行し、その stdout を
   **一次データ**とする（日別カテゴリ・AI推移・消化率/PASS率・実験サマリー・記録なし日が
   決定論で揃う）。トークンと幻覚面積を削るため、ここを主材料にする。

   デイリーノートの生読みは次に限定する:
   - 手書きの Today's Focus / Tasks / Reflections
   - 提案本文の確認（`<!-- kaizenlog:advice:start -->` が必要なときだけ）

   `weekly-context` が使えない場合は従来どおり `01 Daily Notes/` の7日分と
   `Kaizen/Memory/suggestions.jsonl` を直接読む（記録なし日はレビューに明記）。

3. **関連コンテキスト**: `02 Projects/` に進行中プロジェクトのノートがあれば読み、
   今週の作業がどのプロジェクトに対応するか突き合わせる。
   `03 Areas/Kaizen Experiments/` の実験ノート（`tags: [type/kaizen-experiment]`）も
   すべて読む（Measurementsテーブルに日次実測値が自動記録されている）。

4. **分析** — 日次では出せない以下の観点を重視する:
   - **傾向**: カテゴリ別時間の曜日変動、集中できた日とできなかった日の違い
   - **AI作業の質のトレンド**: 往復数・細切れセッション・ツールエラー・中断の週次推移。
     改善しているか悪化しているか
   - **提案の追跡**: 今週の日次提案のうち、翌日以降のログに行動変化が表れたものと
     無視されたもの。同じ提案が繰り返されていればその原因。
     `suggestions.jsonl` から対象週の**消化率**（done / 提案）と
     **PASS率**（verdict==pass / 判定済み）を算出し、
     「効いた提案 / 効かなかった提案」に数値で記載する
   - **繰り返しパターン**: 毎日ほぼ同じ時間帯に発生している手作業。自動化
     （スクリプト・Claude Codeスキル・スケジュール実行）の候補として具体的に挙げる
   - **繰り返しプロンプト**: `kaizenlog prompts --days 7` を実行し（使えなければ省略可）、
     頻出依頼があればテンプレ化/スキル化候補としてレビューに1〜3件記載する

4b. **実験の判定**: 実験ノートのうち `status: expired`（期限切れ）のものは、
   Measurementsの達成率から判定して frontmatter の status を書き換える:
   - 測定日の過半数で目標達成 → `status: adopted`（採用。習慣として定着したとみなす）
   - 達成率が低い → `status: rejected`（棄却。ノートのNotes欄に棄却理由を1-2行追記）
   `status: running` の実験は途中経過にコメントするだけで書き換えない。
   **退行**: `status: adopted` のうち、直近7日の実測が3点以上ありその過半数が
   目標未達のものは「今週の実験結果」に退行として記載し、frontmatter を
   `status: running` に戻して `deadline` を今日+14日にする（再計測サイクルへ復帰）。

5. **週次レビューノートの作成**: `01 Daily Notes/Weekly Reviews/YYYY-Www.md` に書く
   （フォルダがなければ作成）。既に存在する場合は全体を書き直してよい（このノートは
   本スキルが所有する）。フォーマット:

```markdown
---
title: "YYYY-Www Weekly Review"
date: YYYY-MM-DD
tags: [type/weekly-review]
---

# YYYY-Www 週次レビュー

## 週間サマリー
（カテゴリ別合計時間の表。前週のレビューノートがあれば増減も）

## 計画と実績
（Today's Focusの達成傾向）

## AI作業の質のトレンド
（往復数・細切れ・エラー・中断の推移と解釈）

## 効いた提案 / 効かなかった提案
（日次Kaizen提案の追跡結果。対象週の消化率・PASS率を数値で先に書く）

## 今週の実験結果
（採用/棄却した実験と、実行中の実験の途中経過）

## 来週の実験（最大3つ）
- [[EXP YYYY-MM-DD タイトル]]: 仮説を1行で
（それぞれ翌週のログで検証できる形にする）

## Related
- [[YYYY-MM-DD]] 〜 [[YYYY-MM-DD]]（対象週のデイリーノート）
```

6. **来週の実験の起票**: 「来週の実験」に挙げたものは、実際に
   `03 Areas/Kaizen Experiments/EXP YYYY-MM-DD タイトル.md` として作成する。
   `kaizenlog experiment new --title "..." --metric <指標> --target "<= N" --days 7`
   コマンドが使えればそれを使い、使えなければ既存の実験ノートと同じ
   frontmatter形式（title / date / tags: [type/kaizen-experiment] / status: running /
   metric / target / baseline（空欄） / deadline）で直接書く。
   使える指標: context_switches, total_active_minutes, ai_activity_blocks,
   ai_sessions（旧名・互換用）, ai_cc_sessions,
   ai_fragmented_sessions, ai_tool_errors, ai_interruptions, ai_avg_turns,
   category_minutes:<カテゴリ名>

7. **リンク**: 対象週の各デイリーノートの末尾に週次レビューへの `[[リンク]]` を
   追加する（既にあれば追加しない）。

## 注意

- デイリーノートの手書き部分・kaizenlogマーカー区間は編集しない（読むだけ）
- 提案は必ずログ上の事実を根拠にする。憶測で断定しない
- 「来週の実験」は多くても3つ。翌週に検証不能な曖昧な目標は書かない
