# Codex 実装指示: 消化率/PASS率の北極星化＋適応投与（A3）— 第2弾

対象リポジトリ: `C:\develop\KaizenLog\KaizenLog-`（Python / pytest）

**前提: 第1弾（PASS/FAIL自動判定＋朝の引き継ぎ）適用済みのコードベース。** `MemoryEntry` に verdict / verdict_value / verdict_date が存在すること。未適用なら作業を中断して報告する。

## 背景と目的

KaizenLog はカイゼンツール自身の効果（提案が実行されたか・効いたか）を測っていない。北極星指標として**消化率**（done率）と**PASS率**（自動判定の合格率）を集計し、status と LLM プロンプトに供給する。消化率が低いときは提案量を自動的に絞る（適応投与）。

## §A3-1 集計関数（`src/kaizenlog/memory.py`）

```python
@dataclass(frozen=True)
class ActionStats:
    window_days: int
    proposed: int      # 窓内に提案されたアクション数
    done: int          # うち status == "done"
    judged: int        # うち verdict が pass/fail
    passed: int        # うち verdict == "pass"
    # done_rate / pass_rate プロパティ（分母0なら None）
```

- `compute_action_stats(entries, today, window_days=14) -> ActionStats`
  - 対象: 提案日（entry.date）が `today - window_days` 〜 `today - 1日` のエントリ（当日提案は実行機会がないため除外）
  - 不正な date は無視（クラッシュさせない）

## §A3-2 `kaizenlog status` への表示（`src/kaizenlog/cli.py`）

status コマンドの出力（`render_status(load_runs(...))` の表示箇所）の後に1ブロック追加:

```
📈 Kaizen実績（直近14日）: 提案 12件 / 消化 5件（42%）/ 自動判定 8件 / PASS 6件（75%）
```

- 提案0件なら `📈 Kaizen実績（直近14日）: まだ提案がありません`
- 分母0の率は `-` と表示
- memory の読み込み失敗は status 全体を落とさない（load_entries は既に安全）

## §A3-3 プロンプトへの供給＋適応投与（`src/kaizenlog/memory.py` `summarize_for_prompt`）

`summarize_for_prompt(entries, today, ...)` の先頭に「## 提案の実績（直近14日）」ブロックを追加:

- `提案N件 / 消化率x% / 自動判定M件 / PASS率y%`（compute_action_stats を使用。数値は丸めて整数%）
- **適応投与ルール**: `proposed >= 6 かつ done_rate < 0.4` のとき、次の指示行を追加する
  `⚠️ 消化率が低いため、今回は「今日の改善提案」と「明日の最小アクション」を1件だけにし、最も小さく始められるものを選ぶこと。`
- エントリが1件も無い場合の従来動作（空文字を返す）は維持。ただし実績ブロックだけ存在するケース（過去エントリはあるが未完了/完了リストが空）でも実績ブロックは返す

しきい値 `0.4` / 最低サンプル `6` はモジュール定数として定義（`_DOSING_MIN_PROPOSED = 6`、`_DOSING_DONE_RATE = 0.4`）。

注意: 出力契約（advisor.advice_contract_errors）は 1〜3 件を許容しているため、コード側の変更は不要。指示はプロンプト経由のソフト制御でよい。

## §A3-4 プロンプト・スキルの整合

- `prompts/daily_advisor.md`: 「実験とMemory」節に1行追加 —「『提案の実績』に消化率低下の指示がある場合は提案を1件に絞る」
- `skills/weekly-kaizen/SKILL.md`: 手順2のデータ収集に `Kaizen/Memory/suggestions.jsonl` を追加し、手順4「提案の追跡」で対象週の消化率・PASS率（verdict フィールド集計）を「効いた提案 / 効かなかった提案」セクションに数値で記載するよう指示を追記
- `prompts/weekly_review.md`: 観点3に「消化率・PASS率の数値評価」を追記

## 受け入れ条件（テスト）

`python -m pytest -q` 全件通過。追加テスト:

1. `compute_action_stats`: 窓の境界（today-14 は含む / today は含まない）、done/judged/passed の集計、不正 date の無視、分母0で rate None
2. `summarize_for_prompt`: 実績ブロックの出現、適応投与行の条件分岐（proposed 5件では出ない / 6件+消化率39%で出る / 41%で出ない）
3. status 表示: 提案あり/なしの両ケースの出力文字列
4. 既存テストのデグレなし

作業完了時: 変更ファイル一覧と pytest 結果（要約）を報告すること。

## 禁止事項（毎回共通）

- git commit / push / branch 操作をしない（変更はワーキングツリーに残すだけ）
- ssh / scp / リモートアクセスをしない
- DB スキーマ変更をしない
- テスト・実装から実 LLM・ネットワークを呼び出さない
- バージョン番号の変更・CHANGELOG のリリース節の追加をしない（README への機能説明追記は可）
- マーカー区間外のノート内容・手書きテキストを変更するコードを書かない
