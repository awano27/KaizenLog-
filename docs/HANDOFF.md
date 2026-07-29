# KaizenLog 開発引き継ぎ(2026-07-29時点)

新しいセッションはまずこのファイルを読むこと。改善ループの現在地・体制・残件を1枚にまとめている。

## プロジェクト概要

Windows のPC作業を ActivityWatch + AI CLIセッションログから自動計測し、Obsidianデイリーノートに日誌とLLM改善提案(Kaizen)を書き込むツール。設計原則: **数字はコード(決定論)、解釈はLLM / 無い指標は無いと言う / マーカー区間外の手書きは不可侵 / redactしてから外に出す**。

## 開発体制(Codexレビューループ)

1. Claude が指示書 `docs/codex-prompts/MMDD_Codex_<件名>指示プロンプト_第N弾.md` を作成
2. ユーザーが Codex CLI に実装させる(**git commit禁止**が毎回の禁止事項。ただし違反7回の前科あり — AGENTS.md への恒常ルール化が未対応の推奨事項)
3. Claude が read-onlyサブエージェントで **§ID判定表**(✅/⚠️/❌ + file:line根拠)レビュー。**Codexの報告を鵜呑みにせず実挙動をpythonで実行確認**する
4. 小さい残件(数行〜1関数)は Claude が直接修正、大きければ次弾の指示書化
5. コミットはユーザー指示時のみ。**判定は必ずHEAD+ワーキングツリーで行う**(Codexは並行作業することがある)
6. **CodexとClaudeレビューを同時に走らせない**(並行実行で壊れた状態を検証する事故が実際に発生)

## 完了済み: 第10〜26弾(全てコミット・プッシュ済み、HEAD: df0fd0d)

| 弾 | 内容 |
|---|---|
| 10 | 計測修理: Codexセッションマージ・深夜跨ぎトークン差分・api_calls一本化 |
| 11 | 運用可視化: today/done CLI・ヘルスレジャー・実行時間警告 |
| 12-13 | 学習ループ(done層別・トリガー必須・F10較正)・stats v2・weekly-context --write |
| 14 | ai_output_tokens入口ガード・CLI統合テスト |
| 15 | 学習較正: 未実行PASS層別・F10計測可否ゲート・連続FAIL 30日窓・streak走査境界 |
| 16 | 日誌価値: 非挑戦的PASS目標の入口ガード・結論の決定論リッチ化・指標ラベル実態一致 |
| 17 | **PASS/FAIL機械構文の契約強制**(自由文PASSの遮断)・violations種別追加 |
| 18 | AI作業の質UX: セッション「内容」列・依頼長別観察・成果列(変更/✓/⚠)・週次深掘り配線 |
| 19 | 17-18残件: 分類順・retry_touch配線・title抽出共通化 |
| 20-21 | **ブラウザAIテレメトリ**: MV3拡張(3ドメイン限定・ローカルのみ)+BrowserAIAdapter・安定キー/日次バッファ堅牢化 |
| 22 | doctor拡充(タスク登録・成果物チェック)・.gitignore・指示書を docs/codex-prompts/ へ |
| 23 | 引き算: 旧Markdown契約-194行削除・_assert_render_shape安全網・死コード削除 |
| 24 | **プロンプト資産台帳**: PRM-ID・類似度合流・mark skilled/dismissed・再提案防止 |
| 25 | **自己計測除外**: [kaizenlog-internal]センチネル+テンプレ前方一致でadvisor自身のLLM呼び出しを全指標から除外 |
| 26 | **目標トレース**: goalマーカー区間・`kaizenlog goal`・F14/F15/F16事実・結論/today/週次連動(達成断定は禁止、機械PASS経由のみ) |
| 27 | **「計測から調教」5機能**(実装: Grok): `handoff`(CLAUDE.md申し送り注入)・`prompts --roi`・ループ税メーター(+gpt-4o-mini料金順序バグ修正)・`coach`(承認制CLAUDE.md差分)・`abtest`(個人METR+SVGカード) |
| 28 | 第27弾レビュー残件修正(実装: Grok): マーカー外1バイト完全保持・ループ税fail-closed会計・stats保存と週次実値化・coach evidence契約/失敗時ゼロ書き込み・abtest同曜日正規化fail-closed |
| 29 | **発掘監査と風化検知**(実装: Grok): `excavate`(過去ログのシングルパス空転税監査・stats/日誌不変・SVGカード)・改善風化センチネル(skilled PRM再発/実験退行/KZN PASS後悪化→decay_ledger+weekly/status/F17)・通知の_notify統一とstatus無言失敗解消 |

テスト基準線: **pytest 644 passed**(`./.venv/Scripts/python.exe -m pytest -q`)。
**注意: 第29弾はワーキングツリーのみ(未コミット)**。第27-28弾はコミット済み(05a408c)。レビュー済み(全§✅)、コミットはユーザー指示待ち。実装者にGrokが加わった(禁止事項はCodexと同一、commit禁止を遵守)。
既知の潜在バグ(第29弾レビューで発見・別タスク起票済み): `retry_chain_excerpts`/`session_title_from_text` が40字切詰め後にredactするため、秘匿パターンが境界をまたぐと一部漏れる余地(既存コード由来・顕在化例なし)。

## 運用状態

- **スケジュールタスク登録済み**: KaizenLog Daily(21:30)/ KaizenLog Morning(08:30)。2026-07-29夜が初の完全無人実行予定
- 設定はリポジトリ直下 `kaizenlog.toml`(タスクは作業フォルダ焼き込みで解決)。CLIは `./.venv/Scripts/python.exe -m kaizenlog.cli --config kaizenlog.toml <cmd>`
- LLMバックエンド: claude-code-cli(フォールバック: Ollama)。advise所要 ~260秒
- ブラウザ拡張: 実装済み・**ユーザーのChrome/Brave読み込みは未確認**(手順: browser-extension/README.md)
- ボールト: `C:/develop/obsidian/2026`、日誌は `01 Daily Notes/YYYY-MM-DD.md`

## 既知の限界・残課題(優先度順)

1. Codexの `git commit` 禁止違反が7回 → **AGENTS.md に恒常ルール化**(未対応・推奨)
2. 第17弾以前の自由文PASSエントリ(KZN-20260728-001「ChatGPT履歴に…」)が残存 — 機械判定不能のまま(実害小・自然消滅待ち)
3. scan_user_prompts は内部セッションの2発話目以降を除外しない(claude -p は1発話なので実害ほぼゼロ)
4. ChatGPTデスクトップアプリは計測対象外(ブラウザ利用に寄せる運用)
5. Codex側ツールエラー判定は文字列ヒューリスティック(過大計上の可能性 — 構造化フィールド優先化が将来候補)
6. test_round*.py の弾番号命名が機能横断的(機能軸への再編は「大」規模・保留)
7. 将来候補: VSCode Copilot Chat アダプタ(ユーザーが使い始めたら)・screenpipe連携・実験自動昇格

## レビューで頻出した欠陥パターン(次のレビューでも見るべき箇所)

- テストが通るのに実挙動が違う(片側文面のみ検証・データ未接続のデッドコード)→ **実挙動確認を必ず行う**
- ガード強化するとLLMが検証の緩い経路に流れる(自由文PASS事件)→ 入口の一本化で対処
- 新機能のID/ラベルが既存と衝突(F11事件)・セレクタ定数をキーに使う(data-testid事件)
- 検証系サブエージェントの副作用ファイル(リポジトリ直下 `Kaizen/` 等)は削除する
