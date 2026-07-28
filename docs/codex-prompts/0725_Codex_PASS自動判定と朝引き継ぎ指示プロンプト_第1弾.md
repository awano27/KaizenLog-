# Codex 実装指示: PASS/FAIL自動判定（A1）＋ 朝の引き継ぎ（A2）— 第1弾

対象リポジトリ: `C:\develop\KaizenLog\KaizenLog-`（Python / pytest）

## 背景と目的

日次改善提案の「明日の最小アクション」行
`- [ ] KZN-YYYYMMDD-NNN: [F#] 行動｜PASS: 数値条件｜FAIL: 数値条件`
には数値条件を強制しているが、翌日に判定するコードが存在しない。また提案は前日ノートに残るだけで翌朝に想起されない。次の2機能で「提案→行動→検証」のループを閉じる。

- **A1**: PASS 条件を機械可読な `指標 演算子 数値` 構文に寄せ、翌晩の `kaizenlog generate` が自動判定して ✅/❌ をノートと Kaizen Memory に書き戻す
- **A2**: `generate` / `advise` が翌日のデイリーノートへ「📌 今日のアクション」セクション（未完了アクションの転記）を書き込む

設計原則（厳守）:
- LLM にファイルを触らせない。書き込みは常に KaizenLog がマーカー区間へ行う
- マーカー区間の外（手書き部分）は1文字も変更しない
- 夜間無人実行を止めない（ノート欠損・旧形式データはスキップし、クラッシュさせない）
- 既存コードのコメント規約（日本語・「なぜ」を書く）に合わせる

## 用語

- **提案日 / 判定日**: 提案日 D のアクションの PASS 条件は「翌日 D+1」の実績を指す。判定は D+1 の `kaizenlog generate`（D+1 の統計計算後）で行う
- **機械構文**: `<metric> <op> <number>`（例: `context_switches <= 40`）。metric は `experiments.METRIC_DESCRIPTIONS` の指標

---

## §A1 PASS/FAIL 自動判定

### A1-1 新モジュール `src/kaizenlog/verdict.py`

- `parse_pass_condition(action_text: str) -> tuple[str, str, float] | None`
  - アクション行テキストから `PASS:` セグメントを抽出（区切りは全角 `｜` と半角 `|` の両対応。次の区切りまたは `FAIL:` の手前まで）
  - セグメント全体が `<metric> <op> <number>`（op ∈ `<= >= < > == =`、空白は任意）に一致し、かつ metric が既知なら `(metric, op, value)` を返す。それ以外は None（自由文条件＝人間判定のまま）
  - op の正規化（`=`→`==`）と数値解釈は `experiments.parse_target` を再利用する
- `is_known_metric(metric: str) -> bool`
  - `METRIC_DESCRIPTIONS` の固定キーに一致（`category_minutes:<カテゴリ名>` / `site_minutes:<ドメイン>` のプレースホルダキー自体は除外）、
    または `category_minutes:` / `site_minutes:` プレフィックス＋非空サフィックス（空白を含まない。日本語カテゴリ名を許容）
- `judge_entries(entries, proposal_day, summary, cc_sessions, input_stats, judged_day) -> list[MemoryEntry]`
  - `entry.date == proposal_day.isoformat()` のうち PASS が機械構文のエントリを判定する
  - 実測値: `experiments.compute_metric(metric, summary, cc_sessions, input_stats)`。None（watcher 未導入等で未計測）はスキップ
  - 合否: `experiments.target_met(value, op, target_value)`
  - 既存 entry と verdict・verdict_value が同一なら返さない（再実行時の JSONL 追記増殖を防ぐ）
  - 返り値は verdict（"pass"/"fail"）・verdict_value・verdict_date（= judged_day）を設定した新 MemoryEntry。status / done_date 等の既存フィールドは必ず保持する

### A1-2 `MemoryEntry` 拡張（`src/kaizenlog/memory.py`）

- フィールド追加: `verdict: str | None = None`、`verdict_value: float | None = None`、`verdict_date: str | None = None`
- `load_entries`: フィールドが無い旧形式 JSONL 行を None として読めること（後方互換）
- `update_statuses_from_note` が done 化する際、既存エントリの verdict 系フィールドを引き継ぐこと（現状は id/date/action/status/done_date のみで再構築しているため、verdict が消える）

### A1-3 `cmd_generate` への組み込み（`src/kaizenlog/cli.py`）

実験の自動計測ループ（`for exp in load_experiments(...)`）の直後、`return path` の前に:

1. `load_entries(cfg.memory_path)` → `judge_entries(...)` で前日（`day - 1日`）提案分を判定
2. 差分があれば `append_entries` で追記し、コンソールへ
   `🧪 アクション判定: KZN-20260724-001 ✅（実測 35 / 目標 context_switches <= 40）` 形式で出力
3. **前日ノートへの書き戻し**: `store.read(day - 1日)` が存在する場合のみ、`ADVICE_MARKER` 区間内の該当 KZN ID を含むチェックボックス行の末尾に `｜判定: ✅（実測 35）` / `｜判定: ❌（実測 52）` を付与
   - 冪等性: 行内に既存の `｜判定: ...`（行末まで）があれば**置換**する。generate を同日に再実行しても suffix が増殖しないこと
   - ADVICE 区間外・他ノートの同一 ID 行には触れない。書き込みは `vault.atomic_write_text`
   - ノートや区間が無い場合は Memory への記録のみ行い、警告なしでスキップ

### A1-4 出力契約の拡張（`src/kaizenlog/advisor.py` `advice_contract_errors`）

最小アクション検証ループ（`pass_position = action.find("PASS:")` 付近）に追加:

- PASS 値が機械構文**らしい**（`^\S+\s*(<=|>=|<|>|==?)\s*[\d.]+$` に一致する）のに metric が `is_known_metric` で既知でない場合、新エラー
  「最小アクション{n}の PASS: 指標名が使用可能な指標にありません」
- 機械構文で既知の指標なら合格。従来の自由文＋数値も引き続き合格（現行の「PASS/FAIL は数値条件必須」は維持）
- FAIL 側は現行仕様のまま（機械構文でも自由文でもよい）
- 注意: advisor.py → verdict.py の import が循環しないよう、`is_known_metric` は verdict.py（または experiments.py）に置き、advisor からはそれを import する

### A1-5 プロンプト・スキルの更新

`prompts/daily_advisor.md`・`prompts/privacy_safe.md` の出力契約、`skills/daily-kaizen/SKILL.md` の該当箇所に反映:

- PASS: は**可能な限り** `指標 <= 数値` の機械構文で書く。この形式は翌晩に自動判定され ✅/❌ が記録される
- 使用可能な指標一覧を明記: `context_switches` / `total_active_minutes` / `ai_cc_sessions` / `ai_fragmented_sessions` / `ai_tool_errors` / `ai_interruptions` / `ai_avg_turns` / `focus_blocks` / `focus_minutes` / `input_keypresses` / `category_minutes:<カテゴリ名>` / `site_minutes:<ドメイン>`
- 上記指標で測れない行動（例: ドキュメント整備）のみ、従来どおり数値を含む自由文で書く
- アクション行のフォーマット例を機械構文の例に差し替える

---

## §A2 朝の引き継ぎ（今日のアクション転記）

### A2-1 マーカー追加（`src/kaizenlog/vault.py`）

`ACTIONS_MARKER = "kaizenlog:actions"` を追加（ACTIVITY / ADVICE と同形式）。

### A2-2 レンダリング（`src/kaizenlog/memory.py` に追加）

`render_actions_section(entries, target_day, note_content: str | None) -> str | None`

- 対象: `status == "proposed"` かつ提案日が `target_day - 7日` 〜 `target_day - 1日` のエントリ（古い順）
- 見出し: `## 📌 今日のアクション` ＋ 説明1行（例:「前日までの改善提案の未完了アクション。完了したらチェック」）
- 行形式: `- [ ] KZN-...: <action>（M/D提案）`。verdict があれば `（M/D提案・判定 ❌ 実測52）`
- **チェック状態の保持**: `note_content`（転記先ノートの現内容。無ければ None）のどこかに同じ KZN ID のチェック済み行（`- [x]`）があれば `- [x]` で描画する。同日中の再 upsert（generate→advise、または夜の再実行）でユーザーが日中に付けたチェックを失わないための必須要件
- 対象0件なら None（セクションを書かない。既存セクションの削除もしない）

### A2-3 呼び出し（`src/kaizenlog/cli.py`）

`cmd_generate(day)` の末尾（A1-3 の判定後）と、`cmd_advise(day)` の保存成功後（`append_entries(cfg.memory_path, new_entries)` の後。dry_run 時は行わない）に:

- `target = day + 1日` とし、`target >= 今日（cfg.timezone の現在日付）` の場合のみ書き込む（backfill で過去ノートを汚さないため）
- advise 側は ID 採番後の最新エントリ集合（effective_entries ＋ new_entries）を渡す
- `render_actions_section(..., note_content=store.read(target))` → None でなければ `store.write_section(target, ACTIONS_MARKER, section)`（ノートが無ければ frontmatter 付きで新規作成される既存挙動のままでよい）
- コンソール出力: `📌 今日のアクションを転記しました: <path>`

---

## 受け入れ条件（テスト）

`python -m pytest -q` が全件通ること。新規テストは `tests/test_verdict.py` 等として追加（既存ファイルへの追記も可）:

1. `parse_pass_condition`: 全角/半角パイプ、`category_minutes:エンタメ`、`site_minutes:YouTube.com`、未知指標→None、自由文→None、FAIL: 側の数値に反応しないこと
2. `judge_entries`: pass / fail / compute_metric が None でスキップ / 同一 verdict の再判定は差分を返さない / status・done_date を保持
3. memory 互換: verdict フィールド無しの旧 JSONL が読める。`update_statuses_from_note` が verdict を保持したまま done 化する
4. ノート書き戻し: `｜判定:` の追記と、再実行時の置換（冪等）。ADVICE 区間外の同一 ID 行が変更されないこと
5. `advice_contract_errors`: `PASS: context_switches <= 40` 合格 / `PASS: pomodoro_count <= 4` 不合格（新エラー）/ 自由文＋数値は合格 / 数値なしは従来どおり不合格
6. `render_actions_section`: 7日窓・done 除外・0件で None・チェック状態保持・verdict 表示
7. CLI 統合: generate 再実行で判定 suffix が増殖しない / 過去日の generate（backfill 相当）では actions セクションを書かない / advise dry-run では書かない
8. 既存テストのデグレなし

作業完了時: 変更ファイル一覧と pytest 実行結果（要約）を報告すること。

## 禁止事項（毎回共通）

- git commit / push / branch 操作をしない（変更はワーキングツリーに残すだけ）
- ssh / scp / リモートアクセスをしない
- DB スキーマ変更をしない
- テスト・実装から実 LLM・ネットワークを呼び出さない（LLM は既存テストのモック方式に従う）
- バージョン番号の変更・CHANGELOG のリリース節の追加をしない（README への機能説明追記は可）
- マーカー区間外のノート内容・手書きテキストを変更するコードを書かない
