# Codex 実装指示: AIテレメトリの stats 永続化と週次スコアカードの永続化 — 第13弾

対象リポジトリ: `C:\develop\KaizenLog\KaizenLog-`(Python / pytest)

**前提: 第1〜12弾適用済みのワーキングツリー(HEAD 5c698b3 以降)。** 実装順は §W1 → §W2(W2 のトークン週計行が W1 の保存キーを使うため)。

## 背景

(1) AIセッションから収集済みの `output_tokens` / `api_calls` / `tool_counts` / `models`(aiwork.py の AISession)が stats JSON に保存されておらず、週次トレンド・実験指標・パターン検出に一切使えない。また `ai_avg_turns` は daily-kaizen スキルが PASS 指標として提示しているのに `metric_from_stats` が復元不能(experiments.py の docstring に明記)で、baseline 算出も遅延判定バックフィル(第10弾 §M2)も効かない非対称がある。
(2) `kaizenlog weekly-context` は決定論の週次集約を出力するが **stdout 限りで永続しない**。weekly-kaizen スキルが実行されなかった週は数値が一切残らない。「数値・判定はコード、LLM は解釈のみ」の設計原則に従い、スコアカードをノートに永続化してスキルは考察に専念する分業へ移行する。

設計原則(従来どおり厳守): マーカー区間の外は変更しない / atomic_write / 冪等 / 失敗は縮退して続行し runlog に残す。

---

## §W1 AIテレメトリの stats 永続化と ai_avg_turns 復元

`src/kaizenlog/stats.py`、`src/kaizenlog/experiments.py`、`src/kaizenlog/aiwork.py`(参照のみ)

1. stats JSON の `ai` セクションに以下を追加保存し、`version` を 2 に上げる(既存キーは全て維持):
   - `turns_total`(Σ user_turns)、`avg_turns`(turns_total ÷ セッション数、セッション0なら null)
   - `output_tokens`(Σ)、`api_calls`(Σ)
   - `tool_counts`(全セッション合算の上位5ツール `{name: count}`)
   - `models`(使用モデル名の重複除去リスト)
2. **後方互換**: version 1 の既存 stats を読む全経路(experiments / weekly_context / advice_evidence 等)はキー欠損を許容する(既存のフォールバック様式に従う)。`version` キーで分岐しない(キー存在で判定)
3. `metric_from_stats` の `ai_avg_turns` 分岐を復元実装に変更: `ai.avg_turns` があれば採用、無ければ `turns_total ÷ sessions` で導出、v1 stats では `projects[*].turns` 合計 ÷ セッション数で近似復元(それも不能なら従来どおり None)。docstring の「復元不能」記述を更新
4. `baseline_median_from_stats` が `ai_avg_turns` で機能することをテストで確認(第10弾 §M2 のバックフィルにも自動で乗る)
5. 新指標 `ai_output_tokens` を METRIC_DESCRIPTIONS / compute_metric / metric_from_stats に登録(説明例: 「AI応答トークン量」)。第10弾 §M1c の `ai_*` 計測可否ガードの対象に自動で含まれることを確認

テスト: v2 stats の保存キー / v1 stats 読込のデグレなし / ai_avg_turns の3経路復元(avg_turns / 導出 / v1近似)/ baseline 算出 / ai_output_tokens の登録と判定。

## §W2 週次スコアカードの永続化(weekly-context --write)

`src/kaizenlog/cli.py`、`src/kaizenlog/weekly_context.py`、`src/kaizenlog/vault.py`(流用)、`src/kaizenlog/skills/weekly-kaizen/SKILL.md`

1. `kaizenlog weekly-context` に `--write` オプションを追加: stdout 出力に加えて、`<daily_notes_dir>/Weekly Reviews/YYYY-Www.md` の新マーカー区間 `<!-- kaizenlog:weekly-context:start / end -->` へ upsert する(既存の upsert_section + atomic_write 経路を流用)。ノートが無ければ新規作成。**マーカー外(スキルが書く考察・来週の実験提案)は不変**
2. 期限切れ(expired)実験の扱いを確認し、無ければ追加: 直近実測の過半数達成で「✅採用推奨 / ❌棄却推奨」を表示する。**frontmatter の status 書換はしない**(判定表示のみ。簡易パーサでの自動書換はリスクが高く、採否の最終決定は人間/スキルに残す。この理由をコメントに明記)
3. §W1 のキーを使い、週次コンテキストに AI テレメトリ週計を1行追加(例: `AIトークン: 週計 123k / 日平均 18k`)。v1 stats しか無い日は分母から除外し、全日欠損なら `-`
4. `--write` 実行を runlog に記録(command は既存命名に合わせる)。失敗は警告+runlog で続行(無人実行を止めない)
5. weekly-kaizen SKILL.md を分業型に改訂: 手順を「`kaizenlog weekly-context --write` を実行し、書き込まれた区間を一次データとして読む。**数値の再計算・採否判定の独自計算はしない**。考察・来週の実験提案はマーカー区間の外に書く」に変更(既存のフォールバック手順は残す)

テスト: --write で区間が upsert され再実行が冪等 / マーカー外テキスト保持 / ノート新規作成 / 採否推奨の表示(達成・未達・実測不足) / トークン週計行(v1混在・全欠損)。

---

## 受け入れ条件

- `python -m pytest -q` 全件通過(既存デグレなし)
- v1 stats ファイルを読ませる後方互換テストが §W1 に存在する

作業完了時: 変更ファイル一覧、§W1〜W2 の対応状況、pytest 結果(要約)を報告すること。

## 禁止事項(毎回共通・厳守)

- **git commit / push / branch 操作をしない(第11・12弾で2度違反。変更はワーキングツリーに残すだけ)**
- ssh / scp / リモートアクセスをしない
- DB スキーマ変更をしない
- テスト・実装から実 LLM・ネットワークを呼び出さない
- 外部ライブラリを追加しない(標準ライブラリのみ)
- タスクスケジューラへの登録をテストから実行しない
- バージョン番号の変更・CHANGELOG のリリース節の追加をしない
- マーカー区間外のノート内容・手書きテキストを変更するコードを書かない
