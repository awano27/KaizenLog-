# KaizenLog 改善ループ利用可能化・README再設計

日付: 2026-07-30

状態: ユーザー承認済み

対象:

- Loop Tax、Prompt ROI、`handoff`、`coach`、`abtest`の利用確認
- `abtest`ヘルプ不具合の修正
- `README.md`とREADME用純SVGの再設計
- M365 Copilot向けChrome拡張構想の正確な記載

## 1. 現状

改善ループ5機能は、コミット`05a408c`として`main`と`origin/main`へ取り込み済みである。したがってREADMEでは`Local Preview`とせず、現行機能として扱う。

CLIの利用確認では、次の結果を得た。

- `handoff --help`: 成功
- `coach --help`: 成功
- `prompts --help`: 成功し、`--roi`を表示
- `abtest --help` / `abtest status --help`: 成功
- `abtest new --help` / `abtest finish --help`: `argparse`のヘルプ展開時にクラッシュ

後者は、ヘルプ文字列内のリテラル`%`が`argparse`のフォーマット展開に渡ることが原因である。本体の実験計算や保存処理ではなく、ヘルプ表示契約だけを修正する。

## 2. 目的

初見の読者が、KaizenLogを単なるPC日誌ではなく、次の閉ループを作るツールとして理解できるREADMEにする。

> AIとの仕事を、実測で調教する。

閉ループは次の3段階とする。

1. Measure: やり直しのムダと、効果の高い依頼方法を測る
2. Teach: 実測した教訓を次のAIセッションへ渡す
3. Verify: 改善ルールが本当に効いたか前後比較する

専門名やコマンド名を先に並べず、「利用者に何が起きるか」を説明した後に対応コマンドを示す。

## 3. 対象読者

主対象:

- Windows上でAIと日常的に仕事をする人
- GitHub Copilot、Claude Code、ChatGPT、Claude.ai、Geminiを使う人
- ActivityWatchとObsidianを使い、作業記録を改善につなげたい人
- AIの提案を感覚だけで評価せず、再試行や前後差で確かめたい人

副対象:

- ローカル保存、外部送信、書き込み範囲を事前に確認したい人
- 将来のM365 Copilot対応に関心がある人
- 開発やコントリビュートを検討する人

## 4. 採用するREADME方向

採用案は「Closed Loop」である。

ヒーローでは、次の短いコピーと3段階を表示する。

```text
AIとの仕事を、実測で調教する。

MEASURE                 TEACH                 VERIFY
Loop Tax / Prompt ROI → handoff / coach → predict / felt / measured
```

補足コピー:

> リトライのムダを測り、AGENTS.mdへ教訓を戻し、A/Bで本当に効いたか確かめる。

`AGENTS.md`は代表例であり、現行`handoff`は設定された任意のMarkdownパスを対象にできることを本文で補足する。`coach`の生成契約はCLAUDE.md追記案であるため、両者を同一機能として断定しない。

## 5. 情報設計

READMEは次の順番で構成する。

1. Hero
   - 中心メッセージ
   - Measure → Teach → Verify
   - Windows / ActivityWatch / Obsidian / AI work
2. 何が変わるか
   - やり直しのムダを測る
   - 効果の高い依頼方法を見つける
   - 学んだルールを次のAIへ渡す
   - 改善効果を実測する
3. 実際に残る証拠
   - Loop Tax
   - Prompt ROI
   - agent-context差分
   - A/B結果カード
4. 基本ワークフロー
   - ActivityWatch → KaizenLog → Obsidian
   - LLMバックエンドは任意
5. 最短セットアップ
   - `kaizenlog setup`
   - `kaizenlog doctor`
   - `kaizenlog generate --date YYYY-MM-DD`
6. 改善ループの詳細
   - Measure
   - Teach
   - Verify
7. ブラウザAI
   - 現行: ChatGPT / Claude.ai / Gemini
   - Next: M365 Copilot改善アシスト
8. 安全・制限
9. 開発・ライセンス

## 6. 現行5機能の説明

READMEでは、次の平易な説明を先に出す。

| 利用者にできること | 機能・コマンド |
| --- | --- |
| AIとのやり直しで失った時間と推定費用を知る | Loop Tax |
| 繰り返し使う依頼方法の効果を比較する | `kaizenlog prompts --roi` |
| 実測した失敗傾向や教訓を次のAIへ渡す | `kaizenlog handoff` |
| 30日分の記録から改善ルール案を作る | `kaizenlog coach` |
| 変更前後の予測・体感・実測を比較する | `kaizenlog abtest` |

制約も近接表示する。

- Loop Taxは測定できた範囲だけを集計し、不明値を捏造しない。
- Prompt ROIの確定値には比較期間が必要である。
- `handoff`は管理マーカー区間だけを更新する。
- `coach`は提案を保存・表示するだけで、自動適用しない。適用にはユーザーの`--apply`操作が必要である。
- `abtest`は必要なベースラインが不足する場合、結果を成立させない。

## 7. M365 Copilotの扱い

M365 Copilot対応は`Next / Planned`とし、現行機能から視覚的にも文章上も分離する。

将来構想:

- `m365.cloud.microsoft.com/chat`だけを任意のホスト権限として許可する
- M365 Copilot Chatの依頼・回答・往復回数・文字数をローカル計測する
- 本文保存は明示的なオプトインとし、企業利用ではメタデータのみを推奨する
- 改善プロンプトやカスタム指示を拡張パネルで提案し、コピー操作で手動反映する

現時点で実装済みと書いてはいけないこと:

- M365 Copilot会話の取得
- M365テナントやMicrosoft Graphとの連携
- M365 Copilotへの自動送信
- カスタム指示やAgent設定の自動変更

## 8. CLI修正設計

`argparse`はヘルプ文字列を`%`形式で展開する。`abtest new`と`abtest finish`のヘルプに表示するリテラル`%`だけを、既存のPython標準ライブラリ契約に従ってエスケープする。

実装順:

1. `cli.main(["abtest", "new", "--help"])`と`finish`が`SystemExit(0)`になる回帰テストを追加する。
2. テストが現行コードで失敗することを確認する。
3. ヘルプ文字列だけを最小修正する。
4. 回帰テストと関連CLIヘルプを再実行する。

実験作成、効果量計算、SVGカード生成、ファイル保存の挙動は変更しない。

## 9. ビジュアルシステム

既存のDaily Ledger配色を維持する。

- 背景: `#0B1211`
- 主文字: `#F4F0E8`
- Measure / 進行: `#75CFA3`
- Teach / 注意: `#F0B667`
- 補足: `#81958C`

方向:

- 純SVG
- 等幅ラベルと台帳グリッド
- 派手なグラデーション、外部フォント、GIF、大量バッジは使わない
- 色だけに依存せず、MEASURE / TEACH / VERIFYの文字と矢印で関係を示す
- モバイル縮小時は説明文より中心コピーと3段階を優先する

更新対象候補:

- `assets/readme/hero.svg`: Closed Loop全体
- `assets/readme/section-loop.svg`: Measure → Teach → Verify
- `assets/readme/workflow.svg`: ActivityWatch / AI logs → KaizenLog → Obsidian / agent context
- `assets/readme/section-start.svg`: setup → doctor → generate

既存資産を再利用できる場合は、不必要な新規アセットを増やさない。

## 10. 安全境界

- ActivityWatch、Obsidian、実LLM、M365 Copilotを実検証で呼び出さない。
- READMEのコマンド例は副作用のない`--help`または明示日付の例を基本とする。
- `generate`の既定catch-up副作用を初回導線で隠さない。
- LLMへ送る可能性がある情報と、ローカル保存だけの情報を区別する。
- ブラウザ拡張は現在3ドメイン限定であり、M365権限を実装済みとしない。
- ユーザー所有のファイルや一時的な`.superpowers/`成果物をコミットしない。

## 11. 変更対象と非対象

変更対象:

- `src/kaizenlog/cli.py`
- CLI回帰テスト
- `README.md`
- 必要な`assets/readme/*.svg`
- 本設計に対応する実装計画

非対象:

- M365 Copilot向けChrome拡張の実装
- 5機能のデータモデルや計算方式の再設計
- ActivityWatch、Obsidian、実LLMを使うE2E
- PyPI公開、GitHub Release、デプロイ
- ユーザーから明示されていないpush

## 12. 受け入れ条件

- `abtest new --help`と`abtest finish --help`が終了コード0で、`+N%`の説明を表示する。
- 5機能がREADMEで正式な現行機能として説明される。
- 専門名より先に「何ができるか」が分かる。
- `handoff`と`coach`の適用契約を混同しない。
- M365 Copilot改善アシストが`Next / Planned`かつ未実装だと同じ画面範囲で分かる。
- READMEの主要コマンドが現行CLIヘルプと一致する。
- 純SVGがXMLとして正しく、デスクトップ幅とモバイル幅で主要情報を読める。
- README内の相対リンクと画像参照がすべて解決する。
- 対象回帰テストと全pytestが成功する。
- `git diff --check`が成功し、対象外ファイルが変更・コミットされない。

## 13. 検証方法

1. 回帰テストを現行コードで失敗させ、修正後に成功させる。
2. `handoff`、`coach`、`prompts`、`abtest`各階層の`--help`を実行する。
3. `.venv`のPythonとOS一時`--basetemp`で全pytestを実行する。
4. README内の相対リンク・画像参照を検査する。
5. SVGをXMLとして解析する。
6. `beautify-github-readme`の監査手順を実行する。
7. READMEをデスクトップ相当とモバイル相当で表示確認する。
8. `git diff --check`と`git status --short`で最終差分を確認する。
