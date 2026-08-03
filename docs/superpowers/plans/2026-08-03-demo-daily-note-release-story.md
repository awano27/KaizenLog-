# KaizenLogリリース候補デモ日誌 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** デモ日誌を「改善効果カードを含むKaizenLog v1.6.0のリリース候補を完成させた1日」という明快で一貫した架空ストーリーへ改訂する。

**Architecture:** 固定Markdownの30秒サマリーをリリース判断の入口にし、改善効果カードをリリースの目玉機能として残す。今日のアクション、モニタリング、Activity Log、日報、未解決、翌日作業を5項目のリリースチェックへ結び付け、公開タグとPyPI公開は未実施として分離する。

**Tech Stack:** Markdown、JSON、PowerShell構造検証

## Global Constraints

- v1.6.0、変更ファイル、テスト、配布物、数値はすべて架空と明記する。
- 「リリース済み」ではなく「リリース候補完成」とし、GitHubタグとPyPI公開は未実施にする。
- 今日の実行checkboxは1件、効果モニタリング内は0件とする。
- 実ActivityWatch、実Vault、LLM、`generate`、`advise`、GitHub Release、PyPIを操作しない。
- `.grok/` と `scripts/self_improve_graph.py` を変更・stageしない。

---

### Task 1: 日誌全体をv1.6.0リリース候補へ統一

**Files:**
- Modify: `docs/examples/demo_daily_note.md`
- Modify: `.kaizenlog/improvement_graph.json`
- Modify: `PLAN.md`
- Create: `docs/superpowers/plans/2026-08-03-demo-daily-note-release-story.md`

**Interfaces:**
- Consumes: 改善効果カードの架空機能デモ、`kaizenlog:actions` 3ブロック、日報ドラフト。
- Produces: v1.6.0リリース候補、5項目リリースチェック、10分マイクロアクション、未公開境界が一貫するMarkdown。

- [x] **Step 1: 30秒サマリーと日次目標をリリース候補へ変更する**

  目標は「改善効果カードを含むKaizenLog v1.6.0のリリース候補を完成させる」。結果は5項目PASS、公開タグ・PyPI未実施とする。

- [x] **Step 2: 今日のアクションをリリース判定へ変更する**

  リリース判定開始前に、version、CHANGELOG、wheel内容を10分で照合する1件のcheckboxを表示する。実行PASSは3/3一致、効果目標はリリース手戻り0件、測定は公開前なので未判定とする。

- [x] **Step 3: モニタリング、Activity Log、日報をリリース作業へ統一する**

  改善効果カードとリリースチェックの状態、作業時間、架空成果、判断、未解決、翌日のタグ作成承認を同じリリース候補へ結び付ける。

- [x] **Step 4: GraphとPLANを更新する**

  release-storyのDesignDecision、Evidence、TestResultを追加し、feature-storyからの `derived-from`、既存Gapへの `improves`、構造検証の `supports` edgeをprovenance付きで保存する。

- [x] **Step 5: 構造と意味を検証する**

  必須語句、5項目チェック、公開未実施、checkbox総数1、モニタリング内0、marker 1組を確認する。GraphのJSON、重複ID、dangling edge、type、provenanceと対象差分の `git diff --check` を検証する。

- [x] **Step 6: 対象4ファイルだけをcommitする**

  commit messageは `docs: make demo journal release goal explicit`。pushはユーザーが明示するまで行わない。
