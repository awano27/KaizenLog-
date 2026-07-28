# Codex 修正指示: 第11弾の補完 — eval 合成サンプルの同梱（1件のみ）

対象リポジトリ: `C:\develop\KaizenLog\KaizenLog-`（第11弾実装済み HEAD = b6056ea 以降）

## 欠落項目（§I2-3 の後半）

eval ハーネス本体（record / run / --min-pass-rate / redaction / gitignore）は実装済みだが、**完全合成の同梱サンプルケースが存在しない**（`eval/` ディレクトリ自体が無い）。このままだと新規環境で `kaizenlog eval run` を実行してもケース0件で、ユーザーが自分で record するまでハーネスが使えない。

## 修正

1. `eval/samples/` に完全合成のケース JSON を3件コミットする（`kaizenlog eval record` が書く形式と同一）:
   - case1: 標準的な1日（複数カテゴリ・AIテレメトリあり・機械構文 PASS が自然に出る構成・日本語カテゴリ名 `AI作業`/`ブラウジング` を含む）
   - case2: データ薄の日（統計欠落気味・watcher なし → 測定不能系 F 行が多い構成）
   - case3: 実験・Memory 文脈あり（experiments_ctx / memory_ctx / intent が埋まっている構成）
   - 固有名詞・実ログ由来の文字列を含めない（プロジェクト名は `demo-app` 等の架空名）
2. `kaizenlog eval run` の `--cases` 既定を「`eval/cases/`（または設定済みの既定ディレクトリ）にケースがあればそれ、無ければ `eval/samples/` にフォールバック」にする。フォールバック時はその旨を1行表示
3. README / USAGE の該当節に「初回は同梱サンプルで動作確認、自分のデータでは `eval record`」と1行追記

## 受け入れ条件

- `python -m pytest -q` 全件通過
- サンプル3件が `eval record` 形式として読める（ハーネスのローダーテストに追加）
- ケース無し環境で `eval run` がサンプルへフォールバックする分岐のテスト

## 禁止事項（毎回共通・抜粋）

- git commit / push をしない（ワーキングツリーに残す。進行中変更を取り消さない）
- テストから実 LLM・ネットワークを呼ばない
- 実ログ本文をサンプルに転記しない（完全合成のみ）
