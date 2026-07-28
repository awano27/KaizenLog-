# Codex 修正指示: 自由文PASSの遮断(機械構文の強制)— 第17弾

対象リポジトリ: `C:\develop\KaizenLog\KaizenLog-`(Python / pytest)

**前提: 第16弾適用済みのワーキングツリー(コミット 2ededb4 以降)。**

## 背景(実物からの証拠)

第15・16弾でPASS指標の実在・計測可否・挑戦性ガードを入れた直後、2026-07-28 の advise 再生成で LLM が**全ガードを迂回する自由文PASS**を出力した:

> PASS: ChatGPT履歴に[タグ]付きセッションが翌朝2件以上ある｜FAIL: [タグ]付きセッションが0件

- `parse_pass_condition` が解析できない自由文 → 指標が特定できず、実在・計測可否・挑戦性の全ガードがスキップされる
- 数字(「2件」「0件」)を含むため既存の `_is_measurable` 系チェックは通過する
- 対象(ChatGPT履歴)はKaizenLogが計測できず、**永久に人間判定=事実上未判定**
- ガードを厳格化するほどLLMは検証の緩い経路に流れる、という定石どおりの挙動

## §P1 [High] PASS条件の機械構文を契約で強制する

`src/kaizenlog/advice_format.py`、`src/kaizenlog/verdict.py`(parse_pass_condition 流用)、`src/kaizenlog/advisor.py`(修復プロンプト)

1. 提案受入検証で、各アクションの PASS/FAIL 条件を `parse_pass_condition` に通し、**解析不能(None)なら契約違反**とする(修復ループ対象)。これにより既存の実在・計測可否・挑戦性ガードが必ず適用される入口に一本化される
2. 修復プロンプトに機械構文の形式を明示: 「PASS条件は `指標名 演算子 数値`(例: `ai_tool_errors <= 60`、`category_minutes:エンタメ <= 35`)のみ。使用可能な指標は根拠セクションに列挙されたもののみ」
3. プロンプトテンプレート(daily_advisor.md / privacy_safe.md / daily-kaizen SKILL.md)にも同じ制約を明記し、修復前に正しく書かれる確率を上げる
4. **縮退リスクへの配慮**: この強制で契約違反率が上がり得るため、advise_health の violations に `pass_not_machine_readable` 種別を追加し、ヘルスレジャーで発生率を観測できるようにする(第11弾 §T3 の classify_violation_kind に追加)

テスト: 自由文PASSが契約違反になる / 機械構文は通過する / FAIL側も同様に検査される / violations 種別の記録。

## §P2 [Low] 決定論結論の文面調整

`src/kaizenlog/advice_evidence.py`(_build_reader_summary)

**問題**: 「カテゴリでは「AI作業」が59.1分と最多が記録されています。」— 文法が不自然(「最多が記録」)。

1. 「カテゴリ別では「AI作業」が最多(59.1分)でした。」程度の自然な文面に調整(数字はevidence由来のみ、断定禁止の既存原則は維持)

テスト: 既存の文面テストの追随のみ。

---

## 受け入れ条件

- `python -m pytest -q` 全件通過(既存デグレなし)

作業完了時: 変更ファイル一覧、§P1〜P2 の対応状況、pytest 結果(要約)を報告すること。

## 禁止事項(毎回共通・厳守)

- **git commit / push / branch 操作を絶対にしない(変更はワーキングツリーに残すだけ。コミットするかはユーザーが決める)**
- ssh / scp / リモートアクセスをしない
- DB スキーマ変更をしない
- テスト・実装から実 LLM・ネットワークを呼び出さない
- 外部ライブラリを追加しない(標準ライブラリのみ)
- タスクスケジューラへの登録をテストから実行しない
- バージョン番号の変更・CHANGELOG のリリース節の追加をしない
- マーカー区間外のノート内容・手書きテキストを変更するコードを書かない
