# Codex 実装指示: 実験設計v2・LLM評価ハーネス・PC外盲点の明記（I1〜I3）— 第11弾

対象リポジトリ: `C:\develop\KaizenLog\KaizenLog-`（Python / pytest）

**前提: 第10弾適用済みの HEAD（1b5c026 以降）。** ワーキングツリーの進行中変更は取り消さず、その上に積むこと。

**非目標（今回はやらない）**: 隔日介入（alternating-day）デザイン、ActivityWatch Android の統合本体、月次トレンドビュー。

## 背景

10弾分の機能は揃ったが、(1) 実験判定が曜日・仕事量の交絡に無防備で偽陽性/偽陰性を出しうる、(2) プロンプトを大改造してきたのに実LLMでの契約合格率を測る手段がなく回帰が検知できない、(3) PC外時間（スマホ移行）が不可視なため介入実験が構造的に偽の成功を報告しうる。

---

## §I1 実験設計 v2 — 曜日マッチド基準と効果量

`src/kaizenlog/experiments.py`、`cli.py`、`skills/weekly-kaizen/SKILL.md`、weekly-context 出力

1. `Experiment` に `start: date | None` を追加（frontmatter の `date` フィールドから解析。不正・欠落は None）
2. **同曜日基準**: ヘルパー `weekday_baseline(metric, day, stats_list) -> float | None` を追加
   - 実験開始日**より前**の統計（`load_stats` で開始前28日分）から、`day` と同じ曜日の `metric_from_stats` 値を集めて中央値を返す。サンプル2日未満は None
3. `record_measurement` 呼び出し側（`cmd_generate` の実験計測ループ）で同曜日基準を算出し、Measurements テーブルに列を追加:
   `| 日付 | 値 | 目標達成 | 同曜日基準 |`（None は `-`）。既存3列のノート（旧形式）を読んだ場合も壊さずパースできること（`_parse_measurements` は先頭2列しか見ていないため互換のはず。テストで固定）
4. **効果量**: `effect_size(exp) -> float | None` を追加（実測中央値 vs baseline の変化率%。baseline が None・0 は None）
   - `render_experiments_context`（LLM向け）と `kaizenlog experiment list`・weekly-context の実験サマリーに `効果量 -32%` を併記
5. `weekly-kaizen` SKILL.md 手順4b を更新: expired の採用/棄却は「達成率の過半数」に加えて効果量と同曜日基準を判定材料にする。達成率が過半数でも効果量が僅少（例: baseline比 ±10%未満）なら「効果薄」として棄却を検討する、という指針を明記
6. 曜日交絡の注意書き: 実験テンプレート（EXPERIMENT_TEMPLATE）の Notes に「開始曜日・週内の仕事量変動が交絡し得る。同曜日基準列と効果量で判断する」を1行追加

テスト: weekday_baseline のサンプル数境界・開始前データのみ使用 / 4列テーブルの upsert と旧3列ノートの互換 / effect_size の None 系 / コンテキスト・list 出力の効果量表記。

## §I2 LLM評価ハーネス — プロンプト回帰の実測（`kaizenlog eval`）

`src/kaizenlog/cli.py`、`advisor.py`、新規 `src/kaizenlog/evalharness.py`

**目的**: 実バックエンド（Claude CLI / Copilot / Ollama）での日次契約の一発合格率・修復後合格率・縮退率を、記録済みケースで再現可能に測る。プロンプトを変えるたびに前後比較できるようにする。

1. **生成ループの計測可能化**: `generate_advice` 内の「生成→解析→検証→修復1回→レンダリング」ループを、結果と計測レポートを返す内部関数に抽出する（例: `_run_daily_pipeline(cfg, system_prompt, prompt, evidence) -> tuple[str | None, PipelineReport]`。`PipelineReport` = parse成功/初回違反リスト/修復実行有無/最終合否/所要秒）。`generate_advice` は従来どおりの外部挙動（例外含む）を維持し、この関数を内部で使う
2. **ケース形式**: `eval/cases/*.json` — advise 入力のスナップショット（当日 stats JSON・過去 stats・intent・experiments_ctx・memory_ctx）。評価時は `build_advice_evidence` → `prepare_advice_request` で本番と同一のプロンプトを組む
3. **記録コマンド**: `kaizenlog eval record [--date]` — 対象日の実入力をケース化して保存する。**保存前に privacy redactor を適用**し、`eval/cases/` は `.gitignore` に追加（個人ログのコミット防止）。完全合成の同梱サンプルを `eval/samples/` に2〜3件コミットする（日本語カテゴリ・機械構文PASS が出やすい構成に）
4. **実行コマンド**: `kaizenlog eval run [--cases DIR] [--repeat N] [--min-pass-rate X]`
   - 各ケース×N回を現在の cfg.llm で実行し、ケース別と集計（一発合格率 / 修復後合格率 / 縮退率 / 平均所要秒）を表で表示
   - `--min-pass-rate` 指定時、修復後合格率が下回れば exit 1（未指定は常に 0）
   - 実LLMを呼ぶのはこのコマンドの実行時のみ。ネットワーク不可環境では即座に分かるエラーを出す
5. **ドキュメント**: docs/USAGE.md（あれば）と README に開発者向け節を1つ: 「プロンプトを変更したら `kaizenlog eval run --repeat 3` で前後比較する」
6. pytest はモックバックエンドでハーネス自体（レポート集計・record の redaction・exit code）を検証する。**実LLM呼び出しをテストに入れない**

テスト: PipelineReport の各フィールド（一発合格/修復合格/縮退の3系統をモックで）/ record の redaction 適用と gitignore / run の集計と --min-pass-rate の exit code / generate_advice の外部挙動が不変（既存テストが緑のまま）。

## §I3 PC外時間の測定限界を体系的に明記する（風船効果）

`src/kaizenlog/advice_evidence.py`、`intervention.py`、prompts、README

1. `advice_evidence` の測定限界に新しい L 項目を追加（既存の最終番号の次を使う）:
   「測定対象はこのPCの前景アクティビティのみ。スマホ・他デバイス・離席中の行動は含まれない。数値の減少はデバイス移行の可能性を排除できない」
2. `intervention.render_plan` の末尾注意書きに追記: 「PCでのブロックはスマホ等への移行（風船効果）を測定できない。実験が成功して見えても体感と合わない場合はデバイス移行を疑うこと」。`cmd_block` が起票する実験の hypothesis にも同趣旨を1文追加
3. `prompts/daily_advisor.md`・`privacy_safe.md` の分析ルールに1行: 「カテゴリ時間の減少を行動改善と断定しない。PC外への移行は測定できない（該当のL項目を引用）」。`weekly_review.md`・weekly-context の実験サマリーにも同様の注意を1行
4. README の測定限界の説明に「PC外は未測定。ActivityWatch Android + 同期で拡張可能（未対応）」を追記
5. daily-kaizen SKILL.md の L 対応表に新 L 項目を追記

テスト: 新 L 項目が evidence markdown に常時出現する / render_plan・実験テンプレートの文言。

---

## 受け入れ条件

- `python -m pytest -q` 全件通過（既存デグレなし。実LLM・ネットワーク不使用）
- §I1: 旧3列 Measurements ノートを読み書きしても壊れない互換テスト
- §I2: `kaizenlog eval run` がモック環境で集計表を出す統合テスト。`generate_advice` の外部挙動（例外・戻り値）が不変であることを既存テストで確認
- 作業完了時: 変更ファイル一覧、§I1〜I3 の対応状況、pytest 結果（要約）を報告

## 禁止事項（毎回共通）

- git commit / push / branch 操作をしない（変更はワーキングツリーに残すだけ。進行中の未コミット変更を取り消さない）
- ssh / scp / リモートアクセスをしない
- DB スキーマ変更をしない
- **テストから実 LLM・ネットワークを呼び出さない**（実LLMは `kaizenlog eval run` の手動実行時のみ。Codex は eval run を実行しないこと）
- 外部ライブラリを追加しない（標準ライブラリのみ）
- 実セッションログ・実活動ログの本文をコード・テスト・同梱サンプルへ転記しない（eval/samples は完全合成）
- バージョン番号の変更・CHANGELOG のリリース節の追加をしない（README / USAGE への追記は可）
- マーカー区間外のノート内容・手書きテキストを変更するコードを書かない
