# GitHub Copilot個人利用のローカル観測可能性調査

調査基準日: 2026年7月18日（JST）  
主な確認対象: GitHub公式ドキュメント、VS Code/GitHub公式リポジトリ、実在OSSリポジトリ

表記:

- 「事実」: 一次情報または公開実装で確認
- 「評価・推論」: 一次情報から導いたKaizenLog向け判断
- 「不明」: 公開された安定仕様を確認できない

## エグゼクティブサマリー

結論は、次のように対象を分ける必要があります。

- Copilot CLIを主対象にするなら、KaizenLogの方向転換は現実的です。`events.jsonl` はユーザー発話、応答、ツール実行、エラー、中断、モデル、コード変更、セッション集計トークンを持ち、Claude Code JSONLにかなり近い観測ができます。
- VS CodeのCopilot Chat／agent modeは、既存履歴を内部JSONLから部分復元できますが、保存形式はVS Codeの内部実装です。今後の観測には、2026年7月時点で公式提供されているOpenTelemetry出力が最良です。
- 通常のインラインコード補完、いわゆるghost textの提示・受入・拒否は、個人が外部ツールから遡及解析できる安定したローカルデータ源を確認できませんでした。組織向けMetrics APIには受入率がありますが、個人契約用APIではありません。
- したがって「Copilot全サーフェスをClaude Code JSONLと同じように観測」は不可能です。一方、「Copilot CLIとVS Code agent/chatの協働品質を観測」なら、十分に製品化可能です。

推奨MVPは、`Copilot CLI events.jsonl + Copilot/VS Code OpenTelemetry` の2系統です。

---

## 1. GitHub Copilot CLI

### 1.1 保存場所と形式

2026年7月16日公開の[Copilot CLI v1.0.71](https://github.com/github/copilot-cli/releases/tag/v1.0.71)を基準に確認しました。

事実として、Copilot CLIはセッションごとにローカル履歴を自動保存します。[公式セッションデータ説明](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/chronicle)は、各セッションが再開可能な完全記録として `~/.copilot/session-state/` に保存されると説明しています。

Windowsの既定パスは次のとおりです。

```text
%USERPROFILE%\.copilot\session-state\<session-id>\events.jsonl
%USERPROFILE%\.copilot\session-state\<session-id>\workspace.yaml
%USERPROFILE%\.copilot\session-state\<session-id>\plan.md
%USERPROFILE%\.copilot\session-state\<session-id>\checkpoints\
%USERPROFILE%\.copilot\session-store.db
%USERPROFILE%\.copilot\logs\process-<timestamp>-<pid>.log
```

通常の絶対パス例は `C:\Users\<user>\.copilot\...` です。`COPILOT_HOME` でルートを変更できます。[CLI configuration directory](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference)

| データ | 形式 | 用途 | KaizenLog適性 |
|---|---|---|---|
| `session-state/*/events.jsonl` | JSON Lines | セッション本文・イベント | 最良 |
| `session-store.db` | SQLite | `/chronicle`検索、索引、チェックポイント | 補助のみ |
| `process-*.log` | テキストログ | デバッグ、障害診断 | エラー補完のみ |
| `%LOCALAPPDATA%\copilot` | キャッシュ | 実行キャッシュ | 履歴解析には不向き |

`session-store.db` は完全履歴ではなく、セッションファイルのサブセットであることが公式に明記されています。主データ源には `events.jsonl` を使うべきです。

なお、CLIセッションは既定でGitHubアカウントへ同期されます。`settings.json` で `"remoteExport": false` にするとローカルだけにできます。発話、コード、ツール出力、秘密情報が含まれ得るため、KaizenLogでは明示的なプライバシー説明が必要です。[公式セッションデータ説明](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/chronicle)

### 1.2 `events.jsonl`から取れる粒度

共通イベント形式は概ね次の形です。

```json
{
  "id": "uuid",
  "timestamp": "ISO-8601",
  "parentId": "optional",
  "type": "tool.execution_complete",
  "data": {}
}
```

フィールドは[Streaming session events](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/streaming-events)で文書化され、公式SDKには[生成済みTypeScript型](https://github.com/github/copilot-sdk/blob/main/nodejs/src/generated/session-events.ts)もあります。

| 観測項目 | 判定 | 内容・制約 |
|---|---|---|
| ユーザー発話 | 可能 | `user.message.content`、添付、agent mode、interaction ID |
| Copilot応答 | 可能 | `assistant.message.content`、ツール要求、出力トークン |
| 人間との往復数 | 可能 | `user.message`単位で集計 |
| LLM呼び出し回数 | 可能 | `assistant.turn_start/end`。公式はhidden callがないと説明 |
| ツール・コマンド | 可能 | 名前、引数、MCPサーバー、開始・終了、成功可否 |
| ツール出力・エラー | おおむね可能 | result、structured content、error code/message。ただし長い出力は省略され得る |
| セッションエラー | 可能 | 分類、メッセージ、stack、HTTP status、request ID |
| 中断 | 可能 | 永続イベント `abort.reason` |
| モデル | 可能 | 選択・解決モデル、モデル変更、終了時のモデル別集計 |
| 1呼び出しごとの出力トークン | 可能 | `assistant.message.outputTokens` |
| 1呼び出しごとの入力・キャッシュトークン | 不完全 | 詳細な `assistant.usage` はephemeralで履歴に残らない |
| セッション集計トークン | 可能 | 正常終了時の `session.shutdown.modelMetrics` |
| AI Credits相当の利用量 | 部分的に可能 | `totalNanoAiu`等。内部・実験的フィールドを含む |
| Premium Requests | 部分的に可能 | `usage_checkpoint`、`shutdown.totalPremiumRequests`。optional |
| コード変更量 | 可能 | 追加・削除行、変更ファイル |
| 権限の承認・拒否 | 履歴だけでは不完全 | `permission.requested/completed` はephemeral |
| 強制終了・プロセスクラッシュ | 不完全 | `session.shutdown`書込み前に終了すると末尾欠落から推定するしかない |

LLM呼び出し数の数え方は[公式Agent loop資料](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/agent-loop)でも `assistant.turn_start` の集計として説明されています。

### 1.3 文書化と安定性

事実として、現行形式はフィールドレベルまでかなり文書化されています。

一方、[公式changelog](https://github.com/github/copilot-cli/blob/main/changelog.md)には、2025年10月のセッションログ形式全面刷新や、その後のトークン・モデル・利用量フィールド追加が記録されています。

評価として、`events.jsonl` は「公開された有用な形式」ですが、「第三者パーサー向けの長期安定API」とまでは明記されていません。

KaizenLog側では以下が必要です。

- `session.start`のイベントバージョンとCLIバージョンを保存する
- 未知イベント・未知フィールドを無視する
- optional/internalフィールドを必須にしない
- CLIバージョン別fixtureを保持する
- 書込み途中の最終行を次回読込みへ持ち越す

### 1.4 OpenTelemetryとの併用

[Copilot CLIのOpenTelemetry仕様](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#opentelemetry-monitoring)では、以下を取得できます。

- ユーザー要求単位の `invoke_agent`
- LLM呼び出しごとの `chat`
- ツール実行ごとの `execute_tool`
- requested/resolved model
- input/output/cache/reasoning tokens
- 実行時間、エラー、AI利用量
- abort、shutdown、コード変更量

ファイル出力は `COPILOT_OTEL_FILE_EXPORTER_PATH` でJSONLにできます。プロンプト、応答、ツール引数・結果は既定では含まれず、content captureを明示的に有効化した場合だけ含まれます。

評価・推論として、Copilot CLI向けKaizenLogは次の組合せが最適です。

1. `events.jsonl`: 発話・応答・履歴・中断などの意味的記録
2. OTel JSONL: 呼び出しごとの正確なトークン・時間・ツールtrace
3. Hooks: `userPromptSubmitted`、`preToolUse`、`postToolUse`、`errorOccurred`等のリアルタイム補助。[公式Hooks資料](https://docs.github.com/en/copilot/concepts/agents/hooks)

---

## 2. VS CodeのCopilot Chat／agent mode

### 2.1 ローカル保存場所

現在のVS Code実装では、チャット本文はVS Code共通のChatSessionStoreに保存されます。

Windowsの通常構成では次のパスです。

```text
%APPDATA%\Code\User\workspaceStorage\<workspace-id>\
  chatSessions\<session-id>.jsonl
```

空ウィンドウでは次の領域です。

```text
%APPDATA%\Code\User\globalStorage\
  emptyWindowChatSessions\<session-id>.jsonl
```

根拠はVS Code本体の[chatSessionStore.ts](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/chat/common/model/chatSessionStore.ts)です。実際のWindowsパスも[VS Code公式リポジトリのissue](https://github.com/microsoft/vscode/issues/278948)で確認できます。

Insiders、Portable Mode、Remote Development、`--user-data-dir`利用時はルートが変わります。

重要な点は、現行の `.jsonl` が「1行1チャットイベント」ではないことです。

- 先頭行: セッション全体の初期状態
- 以後: property set、array push/splice、deleteといった差分操作
- 一定件数後に再コンパクション

[objectMutationLog.ts](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/chat/common/model/objectMutationLog.ts)の実装上、各行を順番にリプレイして最終状態を復元する必要があります。VS Code 1.109未満はセッション全体を持つ `.json` でした。

`state.vscdb`などのSQLiteは主に索引です。本文の第一候補ではありません。また、公式の[Session insights](https://code.visualstudio.com/docs/agents/sessions/session-insights)が説明する検索用SQLiteも、発話を1,000文字、応答を5,000文字までに制限した検索用サブセットです。

### 2.2 外部ツールによる復元可否

VS Codeの[chatModel.ts](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/chat/common/model/chatModel.ts)では、メッセージ、応答、モデル状態、トークン、Copilot Credits、所要時間などのシリアライズ項目を確認できます。

| 項目 | 判定 | 制約 |
|---|---|---|
| ユーザー発話・応答 | 可能 | mutation logのリプレイが必要 |
| 人間との往復数 | 可能 | request単位で復元可能 |
| ツール呼び出し | 部分的に可能 | serialized tool、terminal command等。全引数・stdoutが常に残る保証はない |
| エラー | 部分的に可能 | result、error details、Failed状態など |
| 中断 | 可能 | `Cancelled`状態を保持 |
| 要求モデル | 可能 | `modelId` |
| 実際に解決されたモデル | 不完全 | 常に永続化される保証はない |
| トークン | 部分的に可能 | `promptTokens`、`completionTokens`等はoptional |
| チャット評価 | 部分的に可能 | thumbs up/downの`vote` |
| agent編集の受入・拒否 | 不完全 | 編集状態の一部は残るが、安定した受入台帳ではない |
| 通常のインライン補完受入率 | 不可 | 安定したローカル履歴/APIを確認できない |

VS Codeはチャットを手動でJSONエクスポートでき、すべてのプロンプトと応答を含みます。「Copy All」にはthinkingやtool callsも含まれます。[公式Chat sessions資料](https://code.visualstudio.com/docs/chat/chat-sessions)

ただし手動操作なので、継続的なKaizenLog収集には向きません。

### 2.3 OpenTelemetryが最良の新規データ源

2026年7月時点の[VS Code公式OpenTelemetry資料](https://code.visualstudio.com/docs/agents/guides/monitoring-agents)は、Copilot Chat／agent modeの外部観測に非常に有力です。

Windows向け設定例:

```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "file",
  "github.copilot.chat.otel.outfile": "C:\\Users\\<user>\\KaizenLog\\copilot-otel.jsonl",
  "github.copilot.chat.otel.captureContent": false
}
```

取得可能な主要情報:

- agent invocation全体のtrace
- 1 LLM API呼び出しごとのmodel、input/output/cache/reasoning tokens
- LLM往復数
- ツール名、call ID、所要時間、成功・失敗
- エラー分類
- repository、branch、commit
- agent編集のaccept/reject
- hunk単位のaccept/reject
- 受入コードのsurvival score
- copy、insert、apply、follow-upなどの操作
- thumbs up/down

`github.copilot.chat.otel.dbSpanExporter.enabled=true` ならローカルSQLiteにも保存でき、`Chat: Export Agent Traces DB`でエクスポートできます。

制約:

- OTelは既定で無効であり、過去へ遡れない
- `captureContent=false`が既定
- trueにすると発話、応答、system prompt、コード、ツール引数・結果まで含まれ、秘密情報の保存リスクがある
- `copilot_chat.edit.acceptance.count`はinline chat、chat editing、agentのhunk編集を対象とし、通常のghost-text補完受入率とは同一ではない

[Copilot Language Server公開リポジトリ](https://github.com/github/copilot-language-server-release)には、補完表示・受入・部分受入の通知が定義されています。したがってCopilot内部で受入テレメトリを扱っていることは事実ですが、第三者が後から読む永続ローカル台帳としては公開されていません。

### 2.4 Copilot拡張ログ

公式手順は次のとおりです。

- Outputチャンネルの `GitHub Copilot`／`GitHub Copilot Chat`
- `Developer: Open Extension Logs Folder`
- `Developer: Set Log Level`でTrace
- `Developer: Chat Diagnostics`

通常は次のルート配下です。

```text
%APPDATA%\Code\logs\<起動時刻>\window<N>\exthost\...
```

ログからは、接続、認証、ネットワーク、拡張機能例外、内部処理失敗などを拾えます。ただし、公式にも接続問題の診断用として説明されており、会話・トークン・受入率の台帳ではありません。[GitHub公式ログ資料](https://docs.github.com/en/copilot/how-tos/troubleshoot-copilot/view-logs)

評価として、KaizenLogでは「セッション解析の主入力」ではなく「エラー原因の補助入力」に限定すべきです。

---

## 3. その他のサーフェスと個人向けAPI

### 3.1 JetBrains／Visual Studio／Xcode

| サーフェス | 事実として確認できるログ | KaizenLog適性 |
|---|---|---|
| JetBrains | IDE標準の`idea.log`。Windowsでは通常 `%LOCALAPPDATA%\JetBrains\<Product><Version>\log\idea.log`。`#com.github.copilot:trace`で詳細化 | 診断用。安定した発話・トークン・受入スキーマではない |
| JetBrains Agent Debug Panel | Copilot CLIエージェントイベントを時系列表示。Public Preview。履歴にはfile loggingが必要 | 有望だが保存パス・スキーマが未文書化 |
| Visual Studio | `View > Output > GitHub Copilot` | 診断用。セッション履歴としては未文書化 |
| Xcode | `~/Library/Logs/GitHubCopilot/github-copilot-for-xcode.log` | テキスト診断ログ。品質分析には弱い |

一次情報は[GitHubのIDE別ログ資料](https://docs.github.com/en/copilot/how-tos/troubleshoot-copilot/view-logs)です。

評価として、これらのIDEは当面「公式な詳細ログがある」とは扱わず、障害診断だけをサポートするのが安全です。

### 3.2 個人向け利用量

2026年6月1日から通常のCopilotプランはPremium Requests中心ではなく、トークンとモデルに基づくGitHub AI Credits課金へ移行しました。[GitHub公式変更案内](https://github.blog/changelog/2026-06-01-updates-to-github-copilot-billing-and-plans/)

Premium Requestsが残るのは、既存年間Pro／Pro+契約でレガシー課金を維持した利用者です。[レガシー課金の公式説明](https://docs.github.com/en/copilot/reference/copilot-billing/request-based-billing-legacy/what-changed-with-billing)

個人ユーザーはGitHub.comの「Billing and licensing → AI usage」で次を確認できます。

- 使用済みAI Credits
- 追加利用量
- 利用モデル
- モデル別Creditsと費用

VS Code、JetBrains、Visual Studio、XcodeのCopilotアイコンからも利用枠とリセット日を確認できます。[個人利用量の公式資料](https://docs.github.com/en/copilot/how-tos/manage-and-track-spending/monitor-ai-usage)

個人向けREST APIもあります。

```text
GET /users/{username}/settings/billing/ai_credit/usage
GET /users/{username}/settings/billing/premium_request/usage
```

[Billing Usage REST API](https://docs.github.com/en/rest/billing/usage)

ただし取得できるのは製品、SKU、モデル、数量、費用等です。発話、ツール、エラー、中断、受入・拒否、セッション品質は含みません。また、組織から割り当てられたCopilot利用は個人Billing APIの対象外です。

### 3.3 組織向けMetrics API

結論として、個人契約しか持たないユーザーはCopilot Usage Metrics APIを自分自身に対して利用できません。

現行APIはEnterprise／Organization単位で、組織所有者または`Organization Copilot metrics: read`権限を持つ利用者向けです。[Copilot Usage Metrics REST API](https://docs.github.com/en/rest/copilot/copilot-usage-metrics)

「user-level report」は個人用APIではなく、組織レポート内のユーザー別明細です。

組織向けレポートには次の情報があります。

- インライン補完のshown／accepted
- コード補完acceptance rate
- LoC
- チャット利用量
- agent利用
- Copilot CLIのprompt／request／token集計

[Copilot usage metricsフィールド一覧](https://docs.github.com/en/copilot/reference/copilot-usage-metrics/copilot-usage-metrics)

したがって、組織権限を持つユーザー向けには任意コネクタとして有用ですが、個人向けKaizenLogの中核にはできません。

---

## 4. 既存OSSの先行事例

2026年7月18日にGitHubで以下のRepository Searchを実施しました。

```text
"github copilot" "usage tracker"
"github copilot" local analytics
copilot usage vscode extension
ccusage copilot
```

存在確認できた主なOSSは次のとおりです。

| OSS | 対象・データ源 | 評価 |
|---|---|---|
| [ccusage](https://github.com/ccusage/ccusage) | `ccusage copilot daily`。Copilot CLIのOTel JSONL | 現時点で最も明確な「Copilot版ccusage」 |
| [tokenuse](https://github.com/russmckendrick/tokenuse) | Copilot CLI／VS Codeを含む複数エージェントの利用・コスト分析 | KaizenLogに近い先行例 |
| [openusage](https://github.com/janekbaraniewski/openusage) | 複数AI製品のquota・利用量ダッシュボード | 品質より利用量寄り |
| [copilot-lens](https://github.com/abdur-rakib/copilot-lens) | `events.jsonl`を読むCopilot CLIローカルダッシュボード | CLI履歴解析の実例 |
| [CopilotPulse](https://github.com/cipheraxat/CopilotPulse) | VS Codeローカル履歴／SQLite解析 | VS Code内部形式依存の実験例 |

特にccusageは[Copilotデータソース文書](https://ccusage.com/guide/copilot/)で、`~/.copilot/otel/*.jsonl` または `COPILOT_OTEL_FILE_EXPORTER_PATH` を読み、token、cache、reasoning、model、costを日次・月次・セッション別に集計すると説明しています。

注意点として、ccusageのCopilot対応は事前にOTel出力を有効化する方式です。OTelを有効化していなかった過去セッションはccusageでは復元できません。一方、KaizenLogは `events.jsonl` を直接解析すれば、過去の意味的履歴も一定範囲で扱えます。

評価・推論:

- 「Copilot版ccusageに相当するOSSが存在するか」への答えは、明確に「存在する」です。
- ただし、発話、軌道修正、失敗、受入、ツール挙動まで横断的に「AI協働の質」を評価する成熟したデファクト製品は確認できませんでした。
- 既存OSSはquota／cost、CLI session、VS Code内部履歴に分散しています。ここにはKaizenLogの差別化余地があります。

---

## 5. 配布チャネル

以下の工数は公式値ではなく、既存Pythonコアを再利用する小規模チームを前提とした推定です。

| チャネル | 推定初期コスト | 到達性 | データ収集適性 |
|---|---:|---|---|
| PyPI／pipx＋GitHub Releases | 低: 数日～2人週 | CLI利用者全般 | 高。現在のKaizenLogを再利用可能 |
| Copilot CLI plugin／hooks | 中低: 1～3人週 | Copilot CLI利用者 | 高。設定導入とリアルタイムイベント収集に適する |
| `github/awesome-copilot`掲載 | 低: 数日 | Copilot関心層 | 認知獲得のみ |
| VS Code拡張 | 中高: 4～8人週 | 最大 | OTel設定、ローカル解析、ダッシュボード表示に最適 |
| ローカルMCPサーバー | 中: 3～8人週 | 複数AIクライアント | 収集済み結果をCopilotから照会する用途 |
| GitHub App／Marketplace | 高: 6～12人週以上＋運用 | 組織・GitHub.com | ローカルIDEログを読めず、個人観測の一次チャネルには不向き |
| 旧Copilot Extensions | 選択不可 | — | 廃止済み |

VS Code拡張は[Marketplaceへ通常配布](https://code.visualstudio.com/api/working-with-extensions/publishing-extension)できます。ただしChat Participant APIは独自participantへの会話を拡張するAPIであり、全Copilot会話を観測するためのグローバルtapとしては文書化されていません。

Copilot CLI向けには[Plugins](https://docs.github.com/en/copilot/concepts/agents/about-plugins)と[github/awesome-copilot](https://github.com/github/awesome-copilot)が現実的です。

GitHub AppベースのCopilot Extensionsは、2025年9月に新規作成が停止され、2025年11月10日に全面廃止されました。後継としてMCPが案内されています。クライアント側のVS Code拡張は引き続き有効です。[公式Sunset Notice](https://github.blog/changelog/2025-09-24-deprecate-github-copilot-extensions-github-apps/)

推奨配布順は次のとおりです。

1. PyPI／pipxでローカルCLIを提供
2. Copilot CLI用セットアップコマンドまたはpluginでOTel／hooksを設定
3. VS Code拡張でOTelの安全な有効化、分析結果表示、欠損診断を提供
4. MCPで「昨日の失敗傾向は？」などをCopilotから照会可能にする
5. 組織導入・課金が必要になった時点でGitHub Appを検討

---

## 6. Claude Code JSONLとの比較と最終結論

### 6.1 観測可否マトリクス

凡例:

- ◎: 同等またはほぼ同等
- ○: 可能
- △: 部分的・不安定・集計のみ
- ×: 外部ツールでは実質不可
- —: 対象外

| 項目 | CLI `events.jsonl` | VS Code既存履歴 | VS Code OTel | 個人Billing API | 組織Metrics API |
|---|---:|---:|---:|---:|---:|
| ユーザー発話・応答 | ◎ | ○ | ○ ※content opt-in | × | × |
| 人間との往復数 | ◎ | ○ | ◎ | × | △ |
| LLM内部往復数 | ◎ | △ | ◎ | × | △ |
| ツール名・実行結果 | ◎ | △ | ◎ | × | 集計のみ |
| コマンド・ツールエラー | ◎ | △ | ◎ | × | × |
| 中断・キャンセル | ○ | ○ | ○ | × | × |
| requested/resolved model | ○ | △ | ◎ | モデル別課金のみ | 集計 |
| 呼び出しごとのトークン | △ | △ | ◎ | × | × |
| セッション集計トークン | ○ | △ | ◎ | △ | ○・日次集計 |
| AI Credits／Premium利用 | △ | △ | ○ | ◎ | ○ |
| agent/chat編集の受入・拒否 | △ | △ | ◎ | × | ○ |
| 通常のインライン補完受入率 | — | × | ×・未文書化 | × | ◎ ※組織権限必須 |
| 設定前の過去履歴 | ◎ | △ | × | 利用量のみ | 保持期間内集計 |

### 6.2 最大の制約

最大の制約は、Copilotに個人向けの統一されたローカル観測インターフェースがないことです。

具体的には次の3点です。

1. CLI、VS Code Chat、インライン補完、他IDEで保存方式が異なる
2. 最も構造化されたOTelは事前opt-inで、content captureも別途同意が必要
3. 通常のインライン補完acceptance rateは、個人向けローカルAPIとして公開されていない

したがって「Copilot全体の利用の質」を単一の完全な指標で表現すると、欠損を隠すことになります。

### 6.3 最良のデータソース推奨順位

個人向けKaizenLogとしての推奨順位は次のとおりです。

1. Copilot CLI `session-state/*/events.jsonl`
2. Copilot CLI OpenTelemetry JSONL
3. VS Code Copilot Chat OpenTelemetry JSONL／SQLite
4. VS Code `chatSessions/*.jsonl`のバージョン別リプレイ
5. 個人Billing AI Credits APIによる請求照合
6. Copilot／IDE診断ログによるエラー補完
7. 組織権限がある場合のみMetrics APIを追加

実際には、1と2、または3と4を組み合わせるのが最良です。

### 6.4 KaizenLogへの最終提案

評価・推論として、主対象の切替は次の定義なら推奨できます。

> 「GitHub Copilot全体」ではなく、「Copilot CLIおよびVS Code agent/chatにおけるAI協働品質」を観測する。

実装順は次が安全です。

1. `copilot_cli_events` adapter
2. `copilot_otel`共通adapter
3. `vscode_chat_sessions` mutation-log adapter
4. `github_billing`照合adapter
5. 任意の`organization_metrics` adapter

各指標には、値だけでなく次の完全性情報を付けるべきです。

```text
source
source_version
collection_mode: passive | otel | estimated
content_capture_enabled
session_closed_cleanly
metric_completeness: complete | partial | unavailable
```

これにより、「エラー0件」が本当にエラーなしなのか、単にログに含まれなかったのかを区別できます。

最終判断は以下です。

- Copilot CLIへの主対象切替: 可能。Claude Code JSONLに近い品質で観測可能
- VS Code agent/chatへの展開: 可能。ただしOTelを標準経路にする
- VS Code既存履歴のゼロ設定解析: 可能だが劣化・保守コストあり
- 通常のコード補完受入率を含む個人全体分析: 現時点では不可能
- JetBrains／Visual Studio／Xcodeの詳細品質分析: 公式診断ログだけでは不可能
- 製品としての最良方針: CLI＋VS Code agent/chatに対象を明示的に限定し、データ完全性を表示する

