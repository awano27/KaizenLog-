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
| 30 | **コーチ効果検証台帳**(実装: Grok): coach_ledger.jsonl(CCH-ID・watching/superseded/pass/fail/rolled_back)・適用後7日窓の同曜日正規化機械判定(過半数方式・fail-closed)・FAIL時ロールバック提案(承認ゲート・区間一致検証・ゼロ書き込み)・weekly「コーチ勝率」/status/F18・摩擦3指標(ai_retry_chains/ai_tool_errors/loop_tax_episodes)をKZN機械PASS構文でも使用可能に |
| 31 | **空転ブレーカー**(実装: Grok): `kaizenlog guard` — UserPromptSubmit/Stopフックでセッション中のリトライ連鎖・ツールエラー連続を検知し、トースト+additionalContextで即時警告。増分tail・状態キャッシュ・デバウンス・全エラーexit 0。`guard install --write`(バックアップ・他フック不可侵)・live_episodes・日誌⚡行・doctor項目 |
| 32 | 第31弾レビュー残件修正(実装: Grok): debounce設定値の一段目ゲート反映(effective_debounce保存方式)・空hooksブロック保護・⚡カウントのTZ変換・streak成功リセット(エラー優先規約)・doctor副作用除去・片側検証テスト6項目の実質化 |
| 33 | **申し送りROIメーター**(実装: Grok): handoff_ledger.jsonl(自然キー安定ID HND-*・first_injected)・コンテキスト家賃(概算tok×リポジトリ帰属セッション、不一致は「不明」)・注入前後30日の効果測定(fail-closed)・`handoff roi --suppress/--unsuppress/--promote`(承認=コマンド、global_target注入+以後除外)・weekly小節 |
| 34 | **日誌可読性と計測正直化**(実装: Grok、実日誌2026-07-30レビュー起点): システム注入XML(`<task-notification>`等)のユーザー発話除外(scan/user_turns/guard/codexアダプタ全系統)・ループ税のエピソード間セッションデデュープ(浪費≦総出力の不変条件)・codex過大計上注記・`$-.--`→「金額不明」・アクション文言(「未実行のままPASS到達」説明付き・達成済み分離+超過件数表示)・内容列basename/フォールバック・短小セッション畳み・結論の時分表記統一 |

| 35 | **日誌情報設計**(描画層一括): 前日比テーブル・reader_notes 空許容・履歴中央値ベースライン・ループ税/コスト表示正直化 |
| 36 | **判定の2段階確定** `verdict_stage`(provisional/confirmed)・測定日ノート再同期・confirmed-only 学習消費 |
| 37 | **提案の質と学習ループ再起動**: 稼働正規化PASS指標・2トラック学習・因果仮説 mechanism/falsifier・PRM日次1行・測定日固定(§Z1) |
| 38 | **提案寿命管理と成果可視化**: TERMINAL_STATUSES(unmeasurable/graduated/retired)・digest・outcome_git |
| 39 | **指標の行動性回復と欠測検知**: レート優先契約・生カウント入口/判定ゲート・計測欠測F19・📌1件表示 |
| 40 | **第38弾レビュー残件**: digest redactor ガード撤去・git root 正規化・known_categories 伝搬・卒業日境界・変異テスト強化 |
| 41 | **日誌可読性の抜本改善**(実日誌2026-08-02レビュー起点): アクション2行平文化(描画のみ・台帳契約不変・演算子和訳)・サマリ行平文化・タイムライン×AIセッション時間突合(ツール適合・（ログなし）フォールバック)・未計測分数の表記正規化・outcome_git subjects取得+stats永続化・日報ドラフトのプロジェクト事実化(業務=digests集約/成果=コミットsubjects/明日=未チェックKZN) |

| 42 | **screenpipe 画面内容連携**: `screenpipe_source.py`(read-only 3エンドポイント・Bearer認証・OCR優先/accessibility フォールバック・要約は純関数・自己参照除外・サーキットブレーカ)・未突合AIブロックの「（ログなし）」を「（画面テキスト: …）」で補完・🖥小節/日報/advisor参考節・`doctor`3状態・`screenpipe-probe` |

| 43 | **第41弾レビュー残件**: Kaizen節アクションの平文化(`humanize_advice_markdown_actions`・ID付与後/書込前・台帳は機械構文のまま・冪等)・status文言の平文化・日報の40字切詰めを39字+「…」へ |

| 44 | **過去ノートの遡及平文化**(実装: Grok): `kaizenlog rehumanize`(既定dry-run・`--write`/`--days`/`--date`・冪等・タイムスタンプ付きバックアップ・変換/書込の失敗は当該ファイルのみスキップして続行)・ADVICE/ACTIONS 両区間対応(判定タグ保持・変換不能行は無変換)・digest/aiwork の切詰めを「結果が上限字数」規約へ統一 |

テスト基準線: **pytest 937 passed**（2026-08-02 第42弾適用後・実行結果を正とする。`./.venv/Scripts/python.exe -m pytest -q`）。
**HEAD**: `93f3d03`(第38〜44弾までコミット)。第42弾はワーキングツリー適用済み・未コミット。

## screenpipe 運用メモ(第42弾・2026-08-02 実機検証)

- 既定 **OFF**。有効化は `kaizenlog.toml` の `[screenpipe] enabled = true` と、**環境変数 `SCREENPIPE_API_KEY` の設定**が両方必要(キー値は toml に書かない)。トークンは `screenpipe auth token` で取得。**スケジュールタスク(21:30/08:30)から使うにはタスク側にも環境変数が要る**(未設定なら disabled 扱いで既存出力のまま)。
- 認証は localhost でも必須。`Authorization: Bearer` のみ有効(`X-API-Key`・クエリ方式は 403)。`/health` だけ無認証で通る。
- 本文が読めるのは **OCR**。accessibility は UI 部品(平均19〜25字)が主で、Electron アプリの会話本文は取れない。
- 補完対象は「AI作業カテゴリ かつ セッションログと突合できなかったブロック」のみ。claude/codex のようにログがあるアプリでは screenpipe は照会されない(設計どおり)。
- **既知の限界(実測)**: ChatGPT デスクトップの OCR にはサイドバーの過去会話タイトル一覧が混ざるため、「その時間帯の作業内容」とは限らない。日本語 OCR は分かち書き誤認識・字形誤認(`エ`→`工`)あり。Claude Code の画面に映る KaizenLog 自身の議論は `SELF_REFERENCE_PATTERNS` で全ては落とせない(キーワードに当たらない文が残る)。いずれも参考層扱いのため指標は汚染しない。
- リソース実測: screenpipe 本体 1.05GB/最大23%CPU + MCP 関連 1.03GB。
**未実施の運用作業**: 過去ノートの遡及平文化は第44弾で実装済みだが、**実ボールトへの `rehumanize --write` はまだ実行していない**(dry-run で 7/26〜8/2 の8件が変更対象と確認済み・ユーザー実行待ち)。実行前に dry-run で差分を確認すること。バックアップは `<vault>/.kaizenlog/backup/rehumanize/<timestamp>/` に残る。
設計メモ(第44弾): rehumanize の書込は `write_section` ではなく `upsert_section`+`atomic_write_text` の直呼び(ADVICE/ACTIONS の2区間を1回の atomic 書込にまとめるため)。区間APIを通すので区間外不可侵は保たれる(区間外バイト完全一致をテストと実挙動の両方で確認済み)。
既知の残存: 平文化は `requires_daily_contract` 判定の外にあるため weekly/自作プロンプト経路のノートにも適用される(ノート側にPASS構文の消費者はゼロと第43弾§P0で確認済みのため無害)。タイムラインのツール適合は source→tool_class 写像方式(仕様の project 判定と現行 source 語彙では等価・不一致時は（ログなし）側に倒れる fail-safe)。不正な `--date` は全コマンド共通で `ValueError` の生トレースになる(既存仕様・rehumanize も踏襲)。
既知の限界(第33弾): retry系レッスンの効果指標は AISession に retry_chains 属性が無いため `is_fragmented`(細切れセッション)をプロキシに使用(handoffledger.py:278-294 にコメント明示)。UserPrompt.project でのプロンプトフィルタ+detect_retry_chains による真のリポジトリ別リトライ計測が次弾候補。
既知のグレーゾーン: guard の `_has_successful_tool_result` は content 文字列ヒューリスティック併用のため「"no errors found" のような成功文」を成功と判定せず streak がリセットされない(発火過剰側の偏り・実害小。既知課題「Codexツールエラー判定の文字列ヒューリスティック」と同系)。
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
8. 第25弾 §S3 のトークン数値1回表示は第35弾 §B2で廃止（コスト行が出力トークンと未登録トークンを併記するため）
9. 当日中の判定は確定しない（夜間判定は暫定、翌日以降の generate 内 backfill で確定する）

## レビューで頻出した欠陥パターン(次のレビューでも見るべき箇所)

- テストが通るのに実挙動が違う(片側文面のみ検証・データ未接続のデッドコード)→ **実挙動確認を必ず行う**
- ガード強化するとLLMが検証の緩い経路に流れる(自由文PASS事件)→ 入口の一本化で対処
- 新機能のID/ラベルが既存と衝突(F11事件)・セレクタ定数をキーに使う(data-testid事件)
- 検証系サブエージェントの副作用ファイル(リポジトリ直下 `Kaizen/` 等)は削除する
- **既定configで機能全体が死ぬガード**(digest の `redactor is None` 早期 return)— 機能追加時は実configの既定値(`redact_patterns=[]` 等)で発火することを確認する
- **レビュー中に別エージェントの実装がツリーへ書き込まれた**(プロトコル6違反の実例・第38弾レビュー時)— レビュー開始前に実装エージェントの停止を確認する
