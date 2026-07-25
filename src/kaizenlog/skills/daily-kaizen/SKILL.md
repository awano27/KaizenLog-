---
name: daily-kaizen
description: KaizenLogが記録した今日のActivity Logを読み、AI作業を中心とした改善提案をデイリーノートのKaizenセクションに追記する。毎日の定期実行、または「今日のカイゼンして」「今日の改善提案を書いて」と言われたときに使う。
allowed-tools: "Read Glob Grep Edit"
---

# Daily Kaizen — 日次改善提案

`kaizenlog advise` は JSON→レンダリング方式であり、本スキルの出力フォーマットは
そのレンダリング結果と同一形式を保つこと。

あなたはこのボールトのAIコラボレーターとして、今日の作業ログを分析し、
デイリーノートに改善提案を追記する。

## 入力（読むもの）

1. `.kaizenlog/stats/<今日>.json` — 数値と全ブロック列の候補データ。直接Skillでは
   出典整合性を検証できないため、Activity区間でも確認できる値だけを使う
2. `01 Daily Notes/<今日>.md` の `<!-- kaizenlog:activity:start -->` 区間
   （カテゴリ別時間・タイムライン・AI作業の質）。無ければ
   「先に kaizenlog generate を実行してください」と伝えて終了
3. 同ノートの手書き部分（Today's Focus / Tasks）— 計画と実績の比較に使う
4. `Kaizen/Memory/suggestions.jsonl` — 過去の提案の記録（重複防止に必須）
5. 必要に応じて: 直近数日の統計・デイリーノート、`01 Daily Notes/Weekly Reviews/`、
   `02 Projects/` のノート（提案の文脈付けに使う。全部読む必要はない）

## 根拠IDの作り方

直接このSkillを使う場合も、次の対応を固定する。値が欠損、不正、または出典の整合性を
確認できないときは、数値を補完せず `[F0] 測定不能: 理由` とする。

- `[F1]`: `total_minutes` と `context_switches`（後者は通知数でなくカテゴリ変更回数）
- `[F2]`: `by_category` の上位カテゴリと時間
- `[F3]`: `input.focus_blocks`、`focus_minutes`、`active_input_minutes`
- `[F4]`: `ai_activity_blocks`（旧statsだけはAIカテゴリのblock数から導出）。会話数にしない
- `[F5]`: `ai.sessions`、`fragmented`（中立の短セッション数）、`tool_errors`、
  `interruptions`、`retry_chains`（リトライ連鎖）。項目がなければ測定不能
- `[F6]`: `by_app` のブラウザ時間と `by_site`。サイト別合計は部分観測として扱う
- `[F7]`: 娯楽・私用の直接計測値。無ければ「定量根拠なし」とする
- `[F8]`: 直近の有効日3日以上（各日60分以上）のカテゴリ変更/時の中央値との比較
- `[F9]`: `blocks` 全件から求めた上位カテゴリ遷移とピーク時刻。`start` を
  `general.timezone` に変換してから時刻を集計する

直接Skillの許可ツールでは `activity_sha256` を再計算できないため、statsの出典整合性は
未検証と明記する。Activity区間でも独立に確認できる値だけを根拠にし、食い違いがあれば
`[F0]` に降格する。ハッシュ照合済みの確定事実が必要なら `kaizenlog advise` を使う。
上記にない `[F#]` を作らない。

## 出力（書くもの）

デイリーノートの `<!-- kaizenlog:advice:start -->` 〜 `<!-- kaizenlog:advice:end -->`
区間**だけ**を以下の構成で書き換える（区間が無ければノート末尾にマーカーごと追加）:

```markdown
## 🚀 Kaizen（AIからの改善提案）

### 今日の結論
（記録時間と良かった点を平易な日本語で1-3行。短時間ならデータ不足と明記）

### 明日試すこと
- [ ] （15分以内に始める行動）｜PASS: context_switches <= 40｜FAIL: context_switches > 40

### 計測上の注意
（未計測の内容、比較不能、部分観測を1-3行）
```

## ルール（違反禁止）

- **マーカー区間の外は1文字も変更しない**。手書きのメモ・他のセクションは絶対に壊さない
- Memoryにある未完了アクションを新しい提案として繰り返さない。
  再度勧める価値がある場合のみ「（継続）」と明示して1行だけ触れる
- 完了済み（status: done）の提案を蒸し返さない
- activity block / AI関連画面ブロックは会話数・発話数・往復数ではない
- 「ブラウジング」は中立カテゴリ。エンタメ等の直接根拠なしに私用・娯楽と断定しない
- コンテキストスイッチはカテゴリ変更であり、通知・割り込みを直接示さない
- タイムラインとサイト別表は部分観測。欠落を0、一部の表示行を一日全体と扱わない
- AI会話の品質は明示されたClaude Code等のテレメトリだけで評価し、無ければ測定不能とする
- 単日の絶対値だけで問題視せず、計画との差、過去中央値との差、反復パターンを優先する
- 憶測で断定しない。分析中は根拠ID `[F#]` で照合するが、保存する文章にはF-IDを表示しない
- 当日の記録が120分未満なら問題を作らず、維持したい行動を最大1件だけ提案する
- 比較可能な前日の記録がなければ前日比を使わず、単独で判定できる絶対値にする
- 通知を計測していない状態で通知オフを提案しない。AI会話未計測なら依頼方法を最適化しない
- ブラウジングが30分未満ならURL watcher設定を改善アクションとして優先しない
- 最小アクションは各1行にし、サブ見出しや追加チェックボックスを挟まない
- PASS は可能な限り機械構文 `指標 演算子 数値`（例: `context_switches <= 40`、
  `category_minutes:エンタメ <= 30`）。翌晩 `kaizenlog generate` が自動判定する。
  指標: context_switches / total_active_minutes / ai_cc_sessions /
  ai_fragmented_sessions / ai_tool_errors / ai_interruptions / ai_avg_turns /
  focus_blocks / focus_minutes / input_keypresses /
  category_minutes:<名> / site_minutes:<ドメイン>。測れない行動のみ自由文＋数値
- `suggestions.jsonl` は読むだけ。書き込みは `kaizenlog advise` 側のID採番に任せる
- 合計400〜800字程度に収める。長文化しない
