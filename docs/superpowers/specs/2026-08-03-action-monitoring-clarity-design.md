# 今日のアクション明確化と目標モニタリング設計

**日付:** 2026-08-03

**状態:** ユーザー承認済み。書面レビュー待ち

**対象:** 日誌の `kaizenlog:actions` 管理区間、`kaizenlog today` の既定表示、実日誌 `2026-08-03.md`

## 目的

日誌を開いて5秒以内に、次の3点を区別できるようにする。

1. 今日、本人が実行するアクション
2. 実行対象ではなく、効果だけを観測している過去の改善
3. 当日目標の設定・自己申告状況

効果指標が測定できない場合や、行動との因果を直接測っていない場合は、その境界を隠さず `未判定` または `不明` と表示する。

## 確認済みの現状

- `KZN-20260802-001` は今日の実行候補だが、2026-08-03の有効稼働22.7分が指標の最低分母60分に届かず、現在値は未判定である。
- 同KZNは日全体の `context_switches_per_hour` を測る。午前・午後のアラーム実施区間だけの効果や、実際にアラームへ反応したかは記録していない。
- `KZN-20260727-002` の最新観測は2026-08-02の3.2で、`>= 2.5` を満たす。直近5観測日は4/5達成であり、未達1日は1.5の「閾値未満」である。
- `ai_avg_turns` は全AIセッションの集計であり、表示文にあるCodexまたはClaude Code単独の効果ではない。
- 2026-08-03の日誌には `kaizenlog:goal` 区間がなく、statsの `goal_text` と `goal_achieved` も `null` である。当日目標は未設定で、達成度もモニタリングできていない。
- 現行rendererは、confirmed PASSを再び未チェックのトップレベルcheckboxとして表示できる。また、最新がPASSでも過去5点に1件の未達があるだけで「指標が戻っています」と警告する。

## 採用する設計

「実行」と「観測」を同じcheckbox一覧へ混ぜず、1つの管理区間を次の3ブロックへ分ける。

### 1. 今日やること

- 見出しは `## 📌 今日やること（N件）` とする。
- 既定は1件、明示的な表示上限を使う場合も全体で最大3件とする。
- confirmed PASSかつ未チェックの提案は候補から除外する。
- 各候補だけにcheckboxを付け、トリガーと行動を `いつ` / `やる` に分ける。`→` がない自由文は本文を `やる` として表示する。
- 各候補へ、そのID専用の完了コマンドを隣接表示する。
- PASS構文から得た効果目標は、分母不足でも消さない。測定不能の理由は独立した `測定` 行にする。

想定出力:

```markdown
## 📌 今日やること（1件）

- [ ] KZN-20260802-001
  - いつ: 午前と午後のアラームが鳴ったとき
  - やる: 30分タイマーをかけ、その時点で使っているカテゴリのアプリ以外を最小化する
  - 完了条件: 今日の予定分を実施して `kaizenlog done KZN-20260802-001`
  - 効果目標: 1時間あたりのカテゴリ変更回数を65以下にする
  - 測定: 集計待ち（稼働22.7 / 必要60分）
  - 因果の範囲: 日全体の観測値。アラーム実施区間だけの効果は判定できません
```

### 2. 効果モニタリング

- 見出しに `今日やることではない` と明記する。
- confirmed PASSで未チェックの項目はcheckboxを持たない観測カードにする。
- 最新側最大5点から、最新観測日・最新値・最新PASS/FAIL・達成日数・未達日数を表示する。
- `閾値超過` のように演算子の向きを誤解させる文言は使わず、`達成 N/M日・未達 X日（目標 >= 2.5）` とする。
- 警告は最新観測がFAILの場合だけ表示する。過去に未達があっても最新がPASSなら中立表示にする。
- 個別ツールを測れない集計指標には、実際の集計範囲と因果限界を表示する。
- 観測カードは最新順で最大2件とし、残件は件数と `kaizenlog today --all` 導線へ圧縮する。

想定出力:

```markdown
## 📈 効果モニタリング（今日やることではない）

- KZN-20260727-002
  - 最新: 8/2 3.2 ✅
  - 直近5日: 4/5達成・未達1日（目標 >= 2.5）
  - 集計範囲: 全AI 22セッション。Codex単独の効果は判定できません
```

### 3. 日次目標

- `kaizenlog:goal` 区間を唯一の入力元とし、actions rendererは読み取り専用の状態サマリだけを表示する。
- 目標区間がない、またはプレースホルダだけの場合は `未設定` と設定コマンドを表示する。
- 目標があり達成度がない場合は `達成度: 未入力`、自己申告がある場合はその値を表示する。
- ActivityWatchの活動量から目標達成を推定しない。
- `kaizenlog goal` 以外の経路で目標本文を書き換えない。

想定出力:

```markdown
## 🎯 日次目標

- 未設定: `kaizenlog goal "今日達成したい成果"`
```

## コンポーネント境界

### `src/kaizenlog/memory.py`

- 判定後の観測点を構造化して返す純粋helperを追加する。rendererはこの構造から最新状態と達成件数を作り、文字列中の `❌` 検索で状態判定しない。
- 実行候補とconfirmed PASSの観測対象を1回だけ分離するhelperを設け、MarkdownとCLIで同じ選択契約を使う。
- action bodyの `→` 分割、効果目標、分母不足、指標スコープを、それぞれ独立した表示行へ変換する。
- `note_content` から日次目標を読み、書き込みを伴わない状態サマリを生成する。

### `src/kaizenlog/cli.py`

- `kaizenlog today` の既定候補にも共通の候補分離を適用し、confirmed PASSを「今日の候補」として推薦しない。
- `kaizenlog today --all` は全件確認導線として維持する。
- `generate`、`advise`、`done`、`morning` の証拠ゲートや書き込み所有権は変更しない。

### 実日誌

- 実装後の純粋renderer出力を使い、`C:\develop\obsidian\2026\01 Daily Notes\2026-08-03.md` の `kaizenlog:actions` マーカー内だけを1回更新する。
- 更新前後でマーカー外bytesのSHA-256が一致することを確認する。
- ActivityWatch、LLM、`generate`、`advise` は実行しない。

## データフロー

1. Memory JSONLから最新のKZN状態を読み、日付窓で未完了項目を分ける。
2. confirmed PASS以外を実行候補、confirmed PASSかつ未チェックを観測対象へ分離する。
3. stats履歴から測定可能な観測点だけを取り、PASS演算子で各点を判定する。
4. `kaizenlog:goal` 区間から当日目標と自己申告達成度を読む。
5. 3ブロックを `kaizenlog:actions` のMarkdownへレンダリングする。
6. Vault更新時は既存のmarker upsert契約を使い、管理区間外を保持する。

## エラー・Unknownの扱い

- stats欠損、最低分母未達、対象セッション0件は、数値を推測せず `集計待ち` または `測定できません` とする。
- 最新観測がない場合は「最新値なし」とし、過去の判定値を現在値のように表示しない。
- 実行ログを持たない指標は「実行の有無は未記録」と明記し、因果効果を断定しない。
- 自由文のlegacy actionは、機械PASS構文がなくても本文とcheckboxを失わない。
- 不正な日付、未知metric、provisional verdictは既存のfail-closed境界を維持する。

## テスト設計

新規のfocused契約テストを `tests/test_round50_action_monitoring_clarity.py` にまとめ、TDDで次を固定する。

1. confirmed PASSは今日のcheckboxではなく非checkboxの観測カードになる。
2. 今日のcheckboxは既定1件、明示時も全体最大3件である。
3. `>=` 指標の未達を `閾値超過` と表示せず、最新PASSなら回復警告を出さない。
4. 最新FAILの場合だけ警告する。
5. 分母不足でも効果目標を残し、必要分母と現在分母を表示する。
6. `ai_avg_turns` と `context_switches_per_hour` の集計範囲を断定しすぎない。
7. 目標未設定、達成度未入力、達成度入力済みの3状態を表示する。
8. `kaizenlog today` もconfirmed PASSを既定候補から除外し、`--all` は維持する。
9. legacy action、checkbox同期、provisional/confirmed境界、marker外bytesを回帰させない。

focused RED→GREEN後、関連テスト、全pytest、`python -m compileall -q src`、Graph JSON検証、`git diff --check` をfreshに実行する。pytestの一時領域はOS temp配下の `--basetemp` を使う。

## Graph Engineering評価

実装を合格とするには、コード差分だけでなく、永続グラフに次のトリプルが存在し、対応するTestResultがPASSであることを要求する。

- `D-ACTION-MONITOR-SPLIT-001 -improves-> G-ACTION-SEPARATION-001`
- `D-ACTION-MONITOR-SPLIT-001 -improves-> G-GOAL-MONITORING-001`
- `D-ACTION-MONITOR-SPLIT-001 -improves-> G-DAILY-GOAL-MONITORING-001`
- 実装CodeChange `-implements-> D-ACTION-MONITOR-SPLIT-001`
- focused/full-suite TestResult `-supports->` 実装CodeChange

critique-reviseは既に選定段階で1回使用した。実装評価で基準未達の場合のreviseは最大1回とし、全体上限2回を超えない。

## 非目標

- アラーム実施イベントや30分区間の新規計測
- ActivityWatch、screenpipe、LLMの起動
- KZN提案本文、PASS契約、判定履歴の書き換え
- 日次目標の自動生成や活動量からの達成推定
- `.grok/`、`scripts/self_improve_graph.py` の変更・stage
- actions marker外の手書き本文の変更

## 受け入れ条件

- 2026-08-03の日誌で、実行対象が `KZN-20260802-001` の1件だと5秒以内に分かる。
- `KZN-20260727-002` はcheckboxを持たず、最新PASS・4/5達成・全AI集計という事実が読める。
- 日次目標が未設定であり、設定コマンドが必要だと分かる。
- 未判定・因果不明をPASS/FAILへ補完しない。
- 既存の証拠ゲート、最大3件、checkbox同期、privacy、手書き保護、confirmed-only、legacy fallback、`today --all` が維持される。
- 実日誌のactions marker外SHA-256が更新前後で一致する。
- 全テスト、compileall、Graph検証、diff checkが成功する。
