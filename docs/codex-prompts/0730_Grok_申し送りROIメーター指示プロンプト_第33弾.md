# 0730 Grok 指示プロンプト 第33弾 — 申し送りROIメーター(CLAUDE.mdの1行ごとに家賃を払わせる)

あなたはこのリポジトリ(`C:\develop\KaizenLog\KaizenLog-`)の実装エージェントです。Phase 1 → 3 の順に実装し、完了ごとに `./.venv/Scripts/python.exe -m pytest -q` で全PASS(基準線: 678 passed)を確認すること。

## 絶対的禁止事項(毎回同じ・違反厳禁)

1. **`git commit` / `git push` 禁止** 2. ssh/scp/リモート禁止 3. DBスキーマ変更禁止 4. 実LLM呼び出し禁止(全て決定論) 5. マーカー外不可侵 6. 新規外部依存禁止 7. **fail-closed**(セッション帰属不明・測定日不足は「不明/計測中」表示、数字の捏造禁止) 8. redact必須 9. CLAUDE.md変更(抑制・昇格)は**明示的CLIコマンド=承認**のみ。自動では絶対に変えない

## 目的

`kaizenlog handoff` が注入する各レッスン行に「**コンテキスト家賃**(概算トークン×対象リポジトリの30日セッション数)」と「**実測効果**(注入前後30日の対象指標)」を対照させ、家賃>効果の行の抑制提案と、複数リポジトリで効いた行のグローバル昇格候補を出す。**CLAUDE.md肥大への進化圧**。

## 設計上の要点(実装前に必ず理解)

- **安定ID**: handoff は冪等再生成(handoff.py:100 build_agent_context_section)なので行番号や連番は使えない。レッスンIDは**生成元の自然キーから決定論導出**する: `HND-prm-<PRM-id>` / `HND-kzn-<KZN-id>` / `HND-retry-trend` / `HND-tool-errors`(集約ブロックは1ブロック=1レッスン)
- **セッション帰属**: `AISession.project` は cwd の basename(aiwork.py:212-217)。target との対応は `Path(target).parent.name` の大文字小文字無視一致。**一致ゼロなら家賃は「セッション数不明」**(fail-closed、数字を出さない)
- **家賃の単位**: 概算トークン(文字数/4、「概算」と明記)× セッション数。**金額換算はしない**(入力トークン単価データを持たないため。fail-closed)
- coach 側(kaizenlog:coachマーカー)の効果検証は第30弾の coach_ledger が担当済み。本弾は **agent-context マーカーのみ**対象

## Phase構成

| Phase | §ID | 内容 | 工数 |
|---|---|---|---|
| 1 | §A1-A3 | handoff_ledger(安定ID・first_injected)+家賃+効果測定 | M |
| 2 | §B1 | `handoff roi` CLI(表・抑制・昇格) | S |
| 3 | §C1 | weekly小節+テスト | S |

---

## Phase 1 — §A

### §A1 handoff_ledger(新規 `src/kaizenlog/handoffledger.py`)

- `<memory_path>/handoff_ledger.jsonl`、last-wins読み(promptledger方式)+append-only書き
- エントリ: `lesson_id`・`target`(絶対パス文字列)・`first_injected`(日付)・`kind`(prm|kzn|retry|toolerr)・`ref_id`・`status`(`active|suppressed|promoted`)
- **handoff実行時の記録**: `apply_handoff`(handoff.py:133)経路で、生成セクションに含まれる各レッスンの (lesson_id, target) が台帳に無ければ first_injected=当日 で追記。既存はそのまま(first_injectedを動かさない)
- **抑制の反映**: `build_agent_context_section` に台帳を渡し、status=suppressed の (lesson_id, target) に該当するアイテムを**生成から除外**(ブロック丸ごとのレッスンはブロックを出さない)。抑制後も冪等性維持(2回実行でbyte同一)

### §A2 家賃計算

- 各レッスンの現在行テキストから概算トークン(len(chars)//4)
- 対象リポジトリの30日セッション数: `collect_ai_telemetry` 30日分の `AISession.project` を上記帰属規約で照合してカウント
- 家賃 = tokens × sessions(表示: 「~N tok × M sess = R tok·sess」)。sessions不明時は「不明」

### §A3 効果測定(全て決定論・fail-closed)

first_injected を境界に前30日 vs 後30日で比較(後窓が30日未満なら「計測中(N/30日)」):
- `prm`: 該当PRMクラスタの再発回数(promptroi の帰属ロジック流用)
- `kzn`: 該当KZNの機械PASS条件(verdict.parse_pass_condition)の違反日数/測定可能日数(測定可能3日未満は「不明」)
- `retry` / `toolerr`: 対象リポジトリに帰属するセッションの retry_chains / tool_errors 合計(stats v2はリポジトリ別を持たないため、**セッション単位の実測から集計**。帰属不明なら「不明」)
- 判定: 後窓 < 前窓 なら「効いている(前→後)」、それ以外は「効果なし(前→後)」

## Phase 2 — §B1 CLI

- `kaizenlog handoff roi`: target別の表 — レッスン行(redact済み・60字切詰めは**redact後に**切詰め)/家賃/効果/判定/status。効果なし かつ first_injected から30日以上経過の行に「→ 抑制候補」マーク
- `kaizenlog handoff roi --suppress <lesson_id> [--target <path>]`: 台帳に suppressed を追記し、**その場で handoff を再実行して**対象セクションから除去(このコマンド実行=承認)。`--unsuppress` で復帰
- 昇格: 同一 lesson_id が**2つ以上の target で「効いている」**場合に「→ 昇格候補」マーク。`--promote <lesson_id>` は config `[handoff] global_target`(新規キー、未設定ならエラー)の agent-context マーカー区間へ該当レッスンを注入し status=promoted、以後の各 target 生成からは除外(重複コンテキスト防止)
- target 未指定の照合・存在しない lesson_id は明確なエラーで exit 1

## Phase 3 — §C1 weekly+テスト

- `render_weekly_context` に「申し送りROI」小節(風化・コーチ勝率と同型のlazy-import try): 最高家賃の1行+抑制候補件数+promoted件数。台帳空なら省略
- `tests/test_round33_handoff_roi.py`:
  - 安定ID: 再生成2回で同一lesson_id・first_injected不変
  - 抑制→生成から除外→冪等・`--unsuppress` で復帰・マーカー外不可侵(byte検証)
  - 帰属: project一致でカウント・不一致ゼロで家賃「不明」
  - 効果境界: 後窓29日=計測中/30日=判定・kzn測定可能2日=不明・前後同数=効果なし
  - 昇格: 1 targetのみ効いている=候補にならない/2 targetで候補・promote後の各target生成から除外・global_target未設定エラー
  - redact→切詰めの順序・weekly小節出現/省略
- `docs/USAGE.md` に handoff roi 節

## 仕上げ

1. `./.venv/Scripts/python.exe -m pytest -q` 全PASS(基準線678+新規)
2. 完了報告は §ID 判定表(✅/⚠️/❌ + file:line + 追加テスト名)。未完は正直に。テスト未通過で ✅ 禁止

| §ID | 判定 | 根拠 (file:line) | 追加テスト |
|---|---|---|---|
| §A1 | | | |
| §A2 | | | |
| §A3 | | | |
| §B1 | | | |
| §C1 | | | |
