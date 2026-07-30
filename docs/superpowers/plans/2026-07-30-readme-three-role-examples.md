# KaizenLog README Three-Role Examples Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dense technical Activity Log example with three mobile-readable role examples that make KaizenLog's AI-optional daily journal value understandable in seconds.

**Architecture:** Keep the existing README section, command flow, and runtime boundaries, but replace the large Markdown table with vertically stacked examples for a software developer, planning/sales worker, and writer/researcher. Update the existing README contract so it protects the three-layer story, the runtime-accurate 25-minute focus threshold, manual authorship, incomplete-device coverage, and optional LLM use.

**Tech Stack:** GitHub-flavored Markdown, Python 3.11, pytest, existing README audit script.

## Global Constraints

- The approved revised design is `docs/superpowers/specs/2026-07-30-readme-daily-journal-story-design.md` at commit `7f7cfdf`.
- Modify only `README.md` and `tests/test_readme_contract.py`; this plan and the revised spec are planning artifacts.
- Do not modify Python implementation, `docs/USAGE.md`, SVG assets, browser-extension files, M365 copy, configuration, prompts, or user data.
- Do not connect to ActivityWatch, Obsidian, Ollama, OpenAI, M365, browser telemetry, or any real service.
- Keep the examples vertically stacked; do not use a wide comparison table or a full technical Activity Log dump.
- Use exactly three roles: `ソフトウェア開発者`, `企画・営業`, and `ライター・研究者`.
- Each role must have exactly three labeled lines: `自動記録（Activity Log）`, `自分の振り返り（手書き）`, and `明日の目標（自分で設定）`.
- State that all numbers and prose are synthetic, not user data, adoption proof, or measured improvement.
- State that role-category times are principal breakdowns, not a complete accounting of the displayed total.
- State that the reflection and next-day goal are written by the person, not generated automatically.
- Activity Log covers observed PC foreground activity, not working hours, goal achievement, concentration quality, productivity, or a complete record of work and life.
- Smartphones, other devices, and away-from-PC behavior are not measured by default.
- The current focus threshold is exactly `25分以上`; never retain or introduce `15分以上`.
- Daily journal generation and manual reflection do not require an LLM. Only reflecting the person's note into an improvement proposal uses the configured LLM backend.
- Preserve the existing exact operational boundaries: `run` calls `generate` then `advise`; `backend = "none"` makes `advise` fail; daily advice remains 1–3 items; M365 remains `Next / Planned` and unimplemented.
- Use `C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe` with `PYTHONPATH` set to this isolated worktree's `src`.
- Every pytest run must use a newly created, full-path-validated `--basetemp` under the Windows OS temp root.

---

### Task 1: Replace the Technical Sample with Three Role Stories

**Files:**

- Modify: `tests/test_readme_contract.py`
- Modify: `README.md:138-209`

**Interfaces:**

- Consumes: the existing `## 毎日の記録と振り返りに使う` section and its current contract test.
- Produces: three stacked role examples, explicit human/automatic boundaries, and an enforceable contract for the revised story.

- [ ] **Step 1: Record the revision base and confirm a clean tracked tree**

Run:

```powershell
$revisionBase = git rev-parse HEAD
git branch --show-current
git status --short
git diff --name-only
```

Expected: a named feature branch and no tracked changes. Record `$revisionBase` in the task report.

- [ ] **Step 2: Replace the old journal contract with the revised failing contract**

Replace `test_readme_presents_an_ai_optional_daily_journal_and_reflection_loop` in `tests/test_readme_contract.py` with:

```python
def test_readme_presents_three_ai_optional_daily_journal_stories():
    text = README.read_text(encoding="utf-8")
    marker = "## 毎日の記録と振り返りに使う"
    assert marker in text
    section = text.split(marker, 1)[1].split("\n---", 1)[0]

    for required in (
        "AIを使わない日でも",
        "朝に目標を書く → 日中は自動記録 → 夜に振り返る → 必要な場合だけAI提案",
        "数値と文章はすべて架空の例です。実在する利用者のデータ、導入実績、改善効果ではありません。",
        "#### 1. ソフトウェア開発者",
        "6時間12分｜実装 3時間05分｜レビュー 1時間20分｜会議 50分",
        "会議後に開発へ戻るまで時間がかかった",
        "午前中にレビュー対応を終える",
        "#### 2. 企画・営業",
        "5時間48分｜顧客会議 2時間10分｜提案書 1時間45分｜調査 1時間05分",
        "会議が続き、提案書の作成が夕方に偏った",
        "最初の会議までに提案書の骨子を作る",
        "#### 3. ライター・研究者",
        "5時間30分｜執筆 2時間35分｜調査 1時間50分｜推敲 45分",
        "午前の調査が長引いたが、午後は執筆に集中できた",
        "調査を90分で区切り、初稿へ進む",
        "各例のカテゴリ時間は主な内訳であり、合計時間の完全な内訳ではありません。",
        "「自分の振り返り」と「明日の目標」は本人が書くもので、自動生成ではありません。",
        "Activity LogはActivityWatchが観測したPC前景活動の記録です。勤務時間、目標達成、集中力、生産性を判定するものではありません。",
        "スマートフォン、他デバイス、離席中の行動は既定では測定できません。",
        "入力watcherから統計を取得できる場合は、25分以上入力が続いた区間を「集中ブロック」として表示します。",
        "`generate --date YYYY-MM-DD`による日誌生成と、本人が書く振り返りにはLLMが不要です。",
        "振り返りを翌日の改善提案へ反映するときだけ、設定済みのLLMバックエンドを使います。",
        'kaizenlog goal "提案書の初稿を完成させる @執筆・ノート"',
        "## 振り返り",
        "管理マーカー区間だけを更新",
        "その外側の手書き本文を置換しません",
    ):
        assert required in section

    assert section.count("**自動記録（Activity Log）**") == 3
    assert section.count("**自分の振り返り（手書き）**") == 3
    assert section.count("**明日の目標（自分で設定）**") == 3

    for forbidden in (
        "### こんな日誌が残ります",
        "**集中ブロック**: 3回",
        "15分以上",
        "Activity Logは勤務時間です",
        "スマートフォンの行動も記録します",
        "振り返りを自動生成します",
        "AIが明日の目標を決めます",
        "日誌の作成にはLLMが必須です",
    ):
        assert forbidden not in section
```

- [ ] **Step 3: Run the revised contract and confirm RED**

Run:

```powershell
$redTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-three-role-red-" + [guid]::NewGuid())
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$resolvedRedTmp = [System.IO.Path]::GetFullPath($redTmp)
if (-not $resolvedRedTmp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedRedTmp"
}
New-Item -ItemType Directory -Path $resolvedRedTmp | Out-Null
$env:PYTHONPATH = (Join-Path (git rev-parse --show-toplevel) "src")
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" -m pytest -q `
  tests/test_readme_contract.py::test_readme_presents_three_ai_optional_daily_journal_stories `
  --basetemp $resolvedRedTmp
```

Expected: one failure because the README does not yet contain the revised three-role flow. If it passes, stop and inspect the current README because the test is not proving the revision.

- [ ] **Step 4: Replace the current daily-journal section**

Replace the complete section beginning with `## 毎日の記録と振り返りに使う` and ending with the paragraph whose last sentence is `日々のLLM提案は現在の契約どおり1〜3件です。` with:

````markdown
## 毎日の記録と振り返りに使う

KaizenLogは、AIを使わない日でも、ActivityWatchが観測したPC前景アプリを一日の業務日誌としてObsidianへ残せます。

> **一日の流れ:** 朝に目標を書く → 日中は自動記録 → 夜に振り返る → 必要な場合だけAI提案

### 3つの仕事で見る、日誌の使い方

> 数値と文章はすべて**架空の例**です。実在する利用者のデータ、導入実績、改善効果ではありません。

#### 1. ソフトウェア開発者

- **自動記録（Activity Log）** — 6時間12分｜実装 3時間05分｜レビュー 1時間20分｜会議 50分
- **自分の振り返り（手書き）** — 会議後に開発へ戻るまで時間がかかった
- **明日の目標（自分で設定）** — 午前中にレビュー対応を終える

#### 2. 企画・営業

- **自動記録（Activity Log）** — 5時間48分｜顧客会議 2時間10分｜提案書 1時間45分｜調査 1時間05分
- **自分の振り返り（手書き）** — 会議が続き、提案書の作成が夕方に偏った
- **明日の目標（自分で設定）** — 最初の会議までに提案書の骨子を作る

#### 3. ライター・研究者

- **自動記録（Activity Log）** — 5時間30分｜執筆 2時間35分｜調査 1時間50分｜推敲 45分
- **自分の振り返り（手書き）** — 午前の調査が長引いたが、午後は執筆に集中できた
- **明日の目標（自分で設定）** — 調査を90分で区切り、初稿へ進む

各例のカテゴリ時間は主な内訳であり、合計時間の完全な内訳ではありません。カテゴリ名は設定に合わせて変更できます。

「自分の振り返り」と「明日の目標」は本人が書くもので、自動生成ではありません。

### 自動記録で分かること・分からないこと

Activity LogはActivityWatchが観測したPC前景活動の記録です。勤務時間、目標達成、集中力、生産性を判定するものではありません。

スマートフォン、他デバイス、離席中の行動は既定では測定できません。入力watcherから統計を取得できる場合は、25分以上入力が続いた区間を「集中ブロック」として表示します。

### 朝の目標と、夜の自分の言葉を残す

朝は一日の目標を記録できます。

```powershell
kaizenlog goal "提案書の初稿を完成させる @執筆・ノート"
```

夜は同じObsidianの日誌へ、自分の言葉を自由に追記できます。

```markdown
## 振り返り

午前は執筆に集中できた。午後は会議後の再開に時間がかかった。
```

KaizenLogは管理マーカー区間だけを更新するため、その外側の手書き本文を置換しません。`## Reflections`または`## 振り返り`があり、`advise`を実行する場合は、その内容を本人の言葉として優先的な文脈に使います。

`generate --date YYYY-MM-DD`による日誌生成と、本人が書く振り返りにはLLMが不要です。振り返りを翌日の改善提案へ反映するときだけ、設定済みのLLMバックエンドを使います。

### 毎日のコマンド

```powershell
kaizenlog run
kaizenlog morning
kaizenlog today
```

`kaizenlog run`は`generate`と`advise`を順に実行します。LLMを使わずActivity Logだけを作る場合は`kaizenlog generate --date YYYY-MM-DD`を使ってください。`backend = "none"`の状態で`advise`を呼ぶと、LLM生成を行わずエラーとして終了します。

`morning`は未完了アクションを再表示し、必要な場合は追いつき処理も行います。追いつきを行わない表示だけの確認には`kaizenlog morning --skip-catch-up`を使います。`today`で候補を確認し、実行済みなら`kaizenlog done KZN-…001`で完了にできます。日々のLLM提案は現在の契約どおり1〜3件です。
````

- [ ] **Step 5: Run the focused README contract suite**

Run:

```powershell
$greenTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-three-role-green-" + [guid]::NewGuid())
$resolvedGreenTmp = [System.IO.Path]::GetFullPath($greenTmp)
if (-not $resolvedGreenTmp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedGreenTmp"
}
New-Item -ItemType Directory -Path $resolvedGreenTmp | Out-Null
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" -m pytest -q `
  tests/test_readme_contract.py --basetemp $resolvedGreenTmp
```

Expected: all README contract tests pass.

- [ ] **Step 6: Run the full test suite once**

Run:

```powershell
$fullTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-three-role-full-" + [guid]::NewGuid())
$resolvedFullTmp = [System.IO.Path]::GetFullPath($fullTmp)
if (-not $resolvedFullTmp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedFullTmp"
}
New-Item -ItemType Directory -Path $resolvedFullTmp | Out-Null
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" -m pytest -q `
  --basetemp $resolvedFullTmp
```

Expected: exit code 0 and no failures.

- [ ] **Step 7: Run the README audit and inspect the reading order**

Run:

```powershell
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" `
  "C:\Users\awano\.codex\skills\beautify-github-readme\scripts\audit_readme.py" README.md
git diff --check
git diff -- README.md tests/test_readme_contract.py
```

Confirm:

- the flow sentence appears before the examples;
- the three roles are vertical `####` subsections, never columns;
- each role has exactly three short labeled lines;
- the technical Activity Log table and old 15-minute line are gone;
- the accuracy boundary follows immediately after the three examples;
- the goal, handwritten reflection, optional LLM advice, and daily commands remain in that order;
- all existing four-image and M365 boundaries remain unchanged.

- [ ] **Step 8: Commit the revised README**

Run:

```powershell
git add -- README.md tests/test_readme_contract.py
git diff --cached --check
if ($LASTEXITCODE -ne 0) { throw "Cached diff has whitespace errors" }
$staged = @(git diff --cached --name-only)
if (($staged -join "`n") -ne "README.md`ntests/test_readme_contract.py") {
  throw "Unexpected staged files: $($staged -join ', ')"
}
git commit -m "docs: show daily journals across three roles"
```

Expected: one commit containing exactly the README and its contract test.

---

### Task 2: Verify the Revised Three-Role README

**Files:**

- Verify only; do not create persistent repository files.

**Interfaces:**

- Consumes: the revised README implementation commit.
- Produces: fresh evidence for focused tests, full tests, audit, and final changed-file scope.

- [ ] **Step 1: Resolve the revision-plan base**

Run:

```powershell
$revisionBase = git log -1 --format=%H -- docs/superpowers/plans/2026-07-30-readme-three-role-examples.md
if (-not $revisionBase) { throw "Revision plan commit not found" }
git log --oneline --decorate $revisionBase..HEAD
git diff --name-only $revisionBase..HEAD
```

Expected: the implementation commit or its reviewed fix commits, with only `README.md` and `tests/test_readme_contract.py` changed after the revision-plan commit.

- [ ] **Step 2: Run focused README tests**

Run:

```powershell
$focusedTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-three-role-focused-" + [guid]::NewGuid())
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$resolvedFocusedTmp = [System.IO.Path]::GetFullPath($focusedTmp)
if (-not $resolvedFocusedTmp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedFocusedTmp"
}
New-Item -ItemType Directory -Path $resolvedFocusedTmp | Out-Null
$env:PYTHONPATH = (Join-Path (git rev-parse --show-toplevel) "src")
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" -m pytest -q `
  tests/test_readme_contract.py --basetemp $resolvedFocusedTmp
```

Expected: all README contract tests pass.

- [ ] **Step 3: Run the full test suite**

Run:

```powershell
$verifyTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-three-role-verify-" + [guid]::NewGuid())
$resolvedVerifyTmp = [System.IO.Path]::GetFullPath($verifyTmp)
if (-not $resolvedVerifyTmp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedVerifyTmp"
}
New-Item -ItemType Directory -Path $resolvedVerifyTmp | Out-Null
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" -m pytest -q `
  --basetemp $resolvedVerifyTmp
```

Expected: exit code 0 and no failures.

- [ ] **Step 4: Re-run static verification and scope checks**

Run:

```powershell
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" `
  "C:\Users\awano\.codex\skills\beautify-github-readme\scripts\audit_readme.py" README.md
git diff --check $revisionBase..HEAD
git status --short
git diff --name-only $revisionBase..HEAD
git diff --stat $revisionBase..HEAD
```

Expected:

- README audit exits 0 and the existing four local images remain valid;
- no whitespace errors;
- tracked worktree is clean;
- only `README.md` and `tests/test_readme_contract.py` changed after the revision-plan commit;
- no runtime, `docs/USAGE.md`, SVG, extension, config, prompt, user-data, or SDD scratch file changed;
- no push or real service call occurred.

- [ ] **Step 5: Report the reader-facing result and evidence**

Report:

- exact implementation/fix commits;
- changed files;
- RED failure reason;
- focused and full pytest counts;
- README audit result;
- confirmation that the three examples are vertically stacked and the old technical table is gone;
- confirmation that `25分以上`, manual authorship, incomplete-device coverage, and optional LLM use are present;
- remaining limitation that KaizenLog records observed PC foreground activity, not all work or life activity.
