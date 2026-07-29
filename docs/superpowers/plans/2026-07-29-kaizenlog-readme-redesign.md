# KaizenLog README Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KaizenLogの価値を「一日のPC作業を、明日の改善1つに変える」と最初の5秒で伝える日本語READMEと、統一されたDaily LedgerスタイルのSVG 4点を制作する。

**Architecture:** `README.md`を利用者の意思決定順に再構成し、ヒーロー、実出力、初回成功、日次ループ、安全境界、任意機能、開発情報の順で読ませる。視覚要素は外部依存のない純SVGへ閉じ、本文だけでも同じ意味が伝わる二重化を行う。

**Tech Stack:** GitHub Flavored Markdown、SVG 1.1相当の静的XML、PowerShell、Python 3.11+、`beautify-github-readme`監査スクリプト

## Global Constraints

- 主対象は日本語話者のWindows／ActivityWatch／Obsidian利用者。
- ヒーローの中心メッセージは「一日のPC作業を、明日の改善1つに変える。」。
- `6h42m`は必ず表示例と明記し、実績値やベンチマークとして扱わない。
- 初回成功導線は`kaizenlog setup → kaizenlog doctor → kaizenlog generate`。
- LLMは任意。フォールバック、自動タスク登録、マスキングの条件を省略して断定しない。
- 配色は`#0B1211`、`#F4F0E8`、`#75CFA3`、`#F0B667`、`#81958C`。
- SVGは外部画像、外部フォント、JavaScript、GIFに依存しない。
- `docs/HANDOFF.md`を変更、stage、commitしない。
- CLI、アプリ本体、パッケージ公開、GitHub Release、DB、実データを変更しない。

---

### Task 1: README本文を利用者の意思決定順に再構成する

**Files:**

- Modify: `README.md`

**Interfaces:**

- Consumes: `docs/superpowers/specs/2026-07-29-kaizenlog-readme-redesign-design.md`
- Produces: SVG 4点を相対参照し、現行CLI契約を説明するGitHub README

- [ ] **Step 1: 変更前READMEが新しい受け入れ条件を満たさないことを確認する**

Run:

```powershell
$readme = Get-Content -LiteralPath README.md -Raw
$required = @(
  '一日のPC作業を、明日の改善1つに変える。',
  '表示例',
  'kaizenlog setup',
  'kaizenlog doctor',
  'kaizenlog generate'
)
$required | ForEach-Object {
  [pscustomobject]@{ Text = $_; Present = $readme.Contains($_) }
}
```

Expected: 中心メッセージまたは`表示例`の少なくとも1項目が`Present=False`。

- [ ] **Step 2: 冒頭と実出力を置き換える**

`README.md`冒頭を次の責務で構成する。

```markdown
# KaizenLog

![KaizenLog — 一日のPC作業を、明日の改善1つに変える](assets/readme/hero.svg)

**一日のPC作業を、明日の改善1つに変える。**

KaizenLogは、ActivityWatchの記録を整理してObsidianのデイリーノートへ残し、
必要な場合だけLLMを使って「明日試すこと」を1つ提案するWindows向けCLIです。

> ヒーロー内の時間と提案は表示例です。利用者の実績値や効果保証ではありません。

## まず、何が残るのか
```

直後に現行READMEの実出力例を、次の4要素が一画面で読める長さへ圧縮して置く。

```markdown
## 📊 Activity Log
- 開発: 2時間02分
- コミュニケーション: 38分

## 🚀 Kaizen
### 今日の結論
午前は開発時間をまとめて確保できました。

### 明日試すこと
- [ ] 9:00に25分の集中枠を入れる

### 計測上の注意
ActivityWatchで取得できた範囲をもとにしています。
```

- [ ] **Step 3: 初回成功導線を追加する**

`## 3コマンドで最初の日誌を作る`を追加し、ソースからの`pipx`インストール後に次を掲載する。

```powershell
kaizenlog setup
kaizenlog doctor
kaizenlog generate
```

説明には次を含める。

- `setup`: ActivityWatchとObsidianの場所を設定する
- `doctor`: 接続、設定、書き込み先を診断する
- `generate`: まずLLMなしでも活動ログを生成して初回成功を確認する
- 日次タスク登録は、対話中に許可した場合または`--register-task`を指定した場合のみ行われる

- [ ] **Step 4: 日次ループと安全境界を追加する**

次の見出しをこの順で追加する。

```markdown
## 毎日どう使うか
## LLMとデータの扱い
## 必要に応じて広げる
## 制限とカスタマイズ
## 開発
## ライセンス
```

`## LLMとデータの扱い`には次の事実を短く記載する。

- ActivityWatchとObsidianの処理はローカル
- LLMは任意で、`none`では提案を生成しない
- OpenAI互換バックエンドからローカルへ切り替わるのは`fallback_to_local=true`の場合だけ
- マスキングはLLMに送るプロンプトへ適用し、ローカルの元日誌を書き換える機能ではない
- KaizenLogが管理マーカー外の手書き本文を置換しない

- [ ] **Step 5: 任意機能と開発情報を整理する**

既存情報を削除せず、次の3項目を`## 必要に応じて広げる`配下へ短く移す。

- Claude Codeスキル
- watcher
- ブラウザAIテレメトリ

測定限界、分類カスタム、開発手順、ロードマップ、ライセンスは後半へ残す。長い説明は箇条書きへ圧縮し、同じ内容の重複をなくす。

- [ ] **Step 6: README本文の受け入れ条件を確認する**

Run:

```powershell
$readme = Get-Content -LiteralPath README.md -Raw
$required = @(
  '一日のPC作業を、明日の改善1つに変える。',
  '表示例',
  '## まず、何が残るのか',
  '## 3コマンドで最初の日誌を作る',
  'kaizenlog setup',
  'kaizenlog doctor',
  'kaizenlog generate',
  'fallback_to_local=true',
  '管理マーカー'
)
$missing = $required | Where-Object { -not $readme.Contains($_) }
if ($missing) { throw "README missing: $($missing -join ', ')" }
```

Expected: 終了コード0、出力なし。

- [ ] **Step 7: README本文をコミットする**

```powershell
git add -- README.md
git diff --cached --check
git commit -m "docs: reshape README around daily improvement"
```

Expected: `README.md`だけを含むコミットが作成される。

---

### Task 2: Daily LedgerスタイルのSVG 4点を再制作する

**Files:**

- Modify: `assets/readme/hero.svg`
- Modify: `assets/readme/section-start.svg`
- Modify: `assets/readme/section-loop.svg`
- Modify: `assets/readme/workflow.svg`

**Interfaces:**

- Consumes: Task 1の相対画像参照とGlobal Constraintsの色・コピー
- Produces: README本文と意味が一致する外部依存なしのSVG 4点

- [ ] **Step 1: 既存SVGが新しい識別語を満たさないことを確認する**

Run:

```powershell
$checks = @{
  'assets/readme/hero.svg' = '1 ACTION'
  'assets/readme/section-start.svg' = 'GENERATE'
  'assets/readme/section-loop.svg' = 'MORNING'
  'assets/readme/workflow.svg' = 'OPTIONAL LLM'
}
$checks.GetEnumerator() | ForEach-Object {
  $content = Get-Content -LiteralPath $_.Key -Raw
  [pscustomobject]@{ File = $_.Key; Marker = $_.Value; Present = $content.Contains($_.Value) }
}
```

Expected: 少なくとも1ファイルが`Present=False`。

- [ ] **Step 2: `hero.svg`を制作する**

構成:

- `viewBox="0 0 1200 520"`
- `<title>`は`KaizenLog — 一日のPC作業を、明日の改善1つに変える`
- `<desc>`で時間と提案が表示例であることを説明
- 上部に`KAIZENLOG · WINDOWS · ACTIVITYWATCH · OBSIDIAN`
- 主見出しに`一日のPC作業を、明日の改善1つに変える。`
- 下部カードに`6h42m → 1 ACTION`
- 行動例に`9:00に25分の集中枠を入れる`
- `表示例`ラベルを明示

すべての図形とテキストをSVG内部で定義し、背景グリッドもSVGパターンで描画する。

- [ ] **Step 3: `section-start.svg`を制作する**

構成:

- `viewBox="0 0 1200 220"`
- `<title>`は`KaizenLogの初回セットアップ`
- `01 SETUP → 02 DOCTOR → 03 GENERATE`
- 最終ノードだけ`#F0B667`で強調し、「最初の日誌」を併記

- [ ] **Step 4: `section-loop.svg`を制作する**

構成:

- `viewBox="0 0 1200 240"`
- `<title>`は`KaizenLogの毎日の改善ループ`
- `NIGHT / 計測を日誌へ`
- `MORNING / 昨日を確認`
- `TODAY / 改善を1つ試す`
- 3ノードを循環矢印で結び、TODAYのチェック項目を`#F0B667`で強調

- [ ] **Step 5: `workflow.svg`を制作する**

構成:

- `viewBox="0 0 1200 300"`
- `<title>`は`ActivityWatchからObsidianまでのデータフロー`
- 主経路は`ACTIVITYWATCH → KAIZENLOG → OBSIDIAN`
- `OPTIONAL LLM`はKaizenLogから分岐して戻る任意経路
- マスキングは`LLM-BOUND PROMPT REDACTION`と表記し、元日誌の書き換えと誤解させない

- [ ] **Step 6: SVGの構造・文言・外部依存を検証する**

Run:

```powershell
@'
from pathlib import Path
import re
import xml.etree.ElementTree as ET

checks = {
    "hero.svg": ["1 ACTION", "表示例"],
    "section-start.svg": ["SETUP", "DOCTOR", "GENERATE"],
    "section-loop.svg": ["NIGHT", "MORNING", "TODAY"],
    "workflow.svg": ["ACTIVITYWATCH", "KAIZENLOG", "OBSIDIAN", "OPTIONAL LLM"],
}

root = Path("assets/readme")
for name, markers in checks.items():
    path = root / name
    ET.parse(path)
    text = path.read_text(encoding="utf-8")
    assert "<title" in text and "<desc" in text, f"{name}: title/desc missing"
    for marker in markers:
        assert marker in text, f"{name}: {marker} missing"
    assert not re.search(r'(?:href|xlink:href)="https?://', text), f"{name}: external asset"
print("SVG validation passed")
'@ | python -
```

Expected: `SVG validation passed`、終了コード0。

- [ ] **Step 7: SVGアセットをコミットする**

```powershell
git add -- assets/readme/hero.svg assets/readme/section-start.svg assets/readme/section-loop.svg assets/readme/workflow.svg
git diff --cached --check
git commit -m "docs: redraw README visuals as daily ledger"
```

Expected: SVG 4点だけを含むコミットが作成される。

---

### Task 3: READMEを監査してCLI契約と表示を検証する

**Files:**

- Modify if required: `README.md`
- Modify if required: `assets/readme/hero.svg`
- Modify if required: `assets/readme/section-start.svg`
- Modify if required: `assets/readme/section-loop.svg`
- Modify if required: `assets/readme/workflow.svg`

**Interfaces:**

- Consumes: Task 1のREADMEとTask 2のSVG 4点
- Produces: 静的監査、CLI照合、デスクトップ／モバイル表示を通過した最終成果物

- [ ] **Step 1: README監査スクリプトを実行する**

Run:

```powershell
python 'C:\Users\awano\.codex\skills\beautify-github-readme\scripts\audit_readme.py' README.md
```

Expected: 致命的エラーなし。警告が出た場合は、外部依存追加ではなくREADME構成または代替テキストで解消する。

- [ ] **Step 2: READMEの相対リンクと画像を検証する**

Run:

```powershell
@'
from pathlib import Path
import re

readme = Path("README.md")
text = readme.read_text(encoding="utf-8")
targets = re.findall(r'!?\[[^\]]*\]\(([^)]+)\)', text)
missing = []
for target in targets:
    target = target.strip().split("#", 1)[0]
    if not target or "://" in target or target.startswith("#"):
        continue
    if not (readme.parent / target).exists():
        missing.append(target)
assert not missing, f"Missing local targets: {missing}"
print("Local README targets passed")
'@ | python -
```

Expected: `Local README targets passed`、終了コード0。

- [ ] **Step 3: 記載コマンドを現行CLIと照合する**

Run:

```powershell
python -m kaizenlog --help
python -m kaizenlog setup --help
python -m kaizenlog doctor --help
python -m kaizenlog generate --help
```

Expected: READMEの`setup`、`doctor`、`generate`、`--register-task`がヘルプに存在し、記載と矛盾しない。

- [ ] **Step 4: SVGを900pxと360px相当で表示確認する**

ブラウザでREADMEプレビューを開き、次を確認する。

- 900px: ヒーローの見出し、`6h42m → 1 ACTION`、表示例ラベルが判読可能
- 360px: 横スクロールなし、ヒーロー直後の本文と3コマンドが縦に読める
- 4 SVG: 文字切れ、重なり、外部読み込みエラーなし
- GitHub相当のライト背景とダーク背景の両方でSVG境界が見える

- [ ] **Step 5: 最終差分を検証する**

Run:

```powershell
git diff --check HEAD~2..HEAD
git status --short
git log -3 --oneline
```

Expected:

- `git diff --check`が終了コード0
- 未追跡`docs/HANDOFF.md`がそのまま残る
- 対象外ファイルの新規変更がない
- READMEとSVGのコミットが確認できる

- [ ] **Step 6: 検証修正がある場合だけ追加コミットする**

```powershell
git add -- README.md assets/readme/hero.svg assets/readme/section-start.svg assets/readme/section-loop.svg assets/readme/workflow.svg
git diff --cached --check
git commit -m "docs: polish README after visual review"
```

Expected: 修正がなければこのステップは実行しない。修正がある場合は対象5ファイル以外を含めない。
