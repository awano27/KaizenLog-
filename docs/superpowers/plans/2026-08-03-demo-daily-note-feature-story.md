# KaizenLog追加機能デモ日誌 改訂計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** デモ用日誌を「KaizenLogの改善効果カード（Before / After）を追加した1日」という一貫した架空ストーリーへ改訂する。

**Architecture:** 固定Markdownの表示見本として、追加機能・数値・変更ファイルが架空であることを冒頭に明示する。30秒サマリー、今日の1アクション、効果モニタリング、Activity Log、日報ドラフトを同じ機能開発へ結び付け、永続Graphへ改訂判断と構造検証を追記する。

**Tech Stack:** Markdown、JSON、PowerShellによる構造検証

## Global Constraints

- 実ActivityWatch、実Vault、LLM、`generate`、`advise`を実行しない。
- checkboxは今日の実行候補1件だけに付け、効果モニタリングには付けない。
- 未観測値を推測せず、`未判定` と因果の範囲を残す。
- `.grok/`、`scripts/self_improve_graph.py`、その他の利用者所有差分を変更・stageしない。
- 架空の追加機能を現行実装済み機能として表現しない。

---

### Task 1: 改善効果カード機能のデモストーリーへ統一

**Files:**
- Modify: `docs/examples/demo_daily_note.md`
- Modify: `.kaizenlog/improvement_graph.json`
- Modify: `PLAN.md`
- Create: `docs/superpowers/plans/2026-08-03-demo-daily-note-feature-story.md`

**Interfaces:**
- Consumes: 現行の `kaizenlog:actions` 3ブロック表示、1件のcheckbox、最大2件の効果モニタリング。
- Produces: 追加機能名、実装目標、10分のマイクロアクション、開発結果、テスト、翌日確認が一貫する固定Markdown。

- [x] **Step 1: サンプル日誌の主語をKaizenLog追加機能へ変更する**

  `今日の目標` を「改善効果カードを日誌へ自動挿入し、Before / After・観測日数・証拠強度を表示する」にする。結果、ムダ上位、AIの質、明日のフォーカスも同機能の実装日に合わせる。

- [x] **Step 2: 今日のアクションとモニタリングを機能開発へ接続する**

  実装開始前に期待Markdownと失敗テストを書く10分アクションを1件だけ表示する。効果モニタリングはAI平均往復数とカテゴリ切替を扱い、checkboxなし、最新値、目標、観測範囲を保持する。

- [x] **Step 3: Activity Logと日報を架空の実装成果へ統一する**

  作業時間、洞察、架空変更ファイル、テスト結果、未解決事項、翌日の実日誌fixture確認を改善効果カードの開発ストーリーに揃える。冒頭で機能・ファイル・数値がすべて架空だと明示する。

- [x] **Step 4: GraphとPLANへ改訂証拠を永続化する**

  DesignDecision、Evidence、TestResultを追加し、旧サンプル判断からの `derived-from`、既存Gapへの `improves`、検証の `supports` edgeをprovenance付きで保存する。

- [x] **Step 5: 構造検証を実行する**

  PowerShellで必須見出し、追加機能名、checkbox総数1、モニタリング内checkbox 0、marker 1組、架空データ注記を確認する。GraphはJSON parse、ID重複、dangling edge、type、provenanceを検証し、対象差分へ `git diff --check` を実行する。

- [x] **Step 6: 今回の対象だけをcommitする**

  `docs/examples/demo_daily_note.md`、`.kaizenlog/improvement_graph.json`、`PLAN.md`、本計画ファイルだけを明示stageし、`docs: align demo journal with KaizenLog feature story` でcommitする。pushは行わない。
