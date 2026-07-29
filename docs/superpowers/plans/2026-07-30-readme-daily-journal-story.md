# KaizenLog README Daily Journal Story Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the README show, with a concrete synthetic example, how KaizenLog works as both an AI-optional daily work journal and a personal reflection loop.

**Architecture:** Keep this as a documentation-only visual refresh. Extend one README section with a code-accurate synthetic Activity Log, three non-AI improvement uses, and the existing goal/reflection/optional-advice flow; enforce the reader-facing claims through `tests/test_readme_contract.py`.

**Tech Stack:** GitHub-flavored Markdown, Python 3.11, pytest, existing README audit script.

## Global Constraints

- The approved design is `docs/superpowers/specs/2026-07-30-readme-daily-journal-story-design.md`.
- Modify only `README.md` and `tests/test_readme_contract.py`; this plan and the approved spec are already committed planning artifacts.
- Do not modify Python implementation, `docs/USAGE.md`, any SVG, browser-extension files, M365 copy, configuration, prompts, or user data.
- Preserve all unrelated Round 31 work in the main checkout. Execute implementation in an isolated worktree created from the plan commit.
- Do not connect to ActivityWatch, Obsidian, Ollama, OpenAI, M365, browser telemetry, or any other real service.
- Label the journal sample as synthetic. Do not present it as user data, adoption proof, or measured improvement.
- Describe ActivityWatch observations as PC foreground activity, not working hours, achievement, concentration quality, or a complete record of the day.
- State that focus blocks appear only when input statistics are available.
- Preserve the existing exact operational boundaries: `run` calls `generate` then `advise`; `backend = "none"` makes `advise` fail; daily advice remains 1–3 items; M365 remains `Next / Planned` and unimplemented.
- Use `C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe` with `PYTHONPATH` set to the isolated worktree's `src` directory.
- Every pytest run that can create a base temp directory must use a newly created, full-path-validated directory under the Windows OS temp root.

---

### Task 1: Add the Daily Journal and Reflection Story

**Files:**

- Modify: `tests/test_readme_contract.py`
- Modify: `README.md:138-149`

**Interfaces:**

- Consumes: the current README headings, four existing HTML image references, and current CLI/runtime claims.
- Produces: a reader-facing `## 毎日の記録と振り返りに使う` section and a regression contract for its value, sample, boundaries, and command flow.

- [ ] **Step 1: Record the task base and verify the isolated scope**

Run:

```powershell
$taskBase = git rev-parse HEAD
git branch --show-current
git status --short
git diff --name-only
```

Expected: a named feature branch, no tracked changes, and no implementation diff. Store `$taskBase` in the task report for the final scope check.

- [ ] **Step 2: Add the failing README contract**

Append this test to `tests/test_readme_contract.py`:

```python
def test_readme_presents_an_ai_optional_daily_journal_and_reflection_loop():
    text = README.read_text(encoding="utf-8")
    marker = "## 毎日の記録と振り返りに使う"
    assert marker in text
    section = text.split(marker, 1)[1].split("\n---", 1)[0]

    for required in (
        "AIを使わない日でも",
        "架空の日誌例",
        "**合計アクティブ時間**: 6h42m",
        "コンテキストスイッチ: 18回",
        "**集中ブロック**: 3回",
        "執筆・ノート",
        "会議",
        "調査",
        "事務作業",
        "時間配分を見直す",
        "集中と中断を振り返る",
        "一日の流れを思い出す",
        'kaizenlog goal "提案書の初稿を完成させる @執筆・ノート"',
        "## 振り返り",
        "管理マーカー区間だけを更新",
        "その外側の手書き本文を置換しません",
        "`generate --date YYYY-MM-DD`だけならLLMは不要です。",
        "設定済みのLLMバックエンド",
        "生産性を自動判定するものではありません",
    ):
        assert required in section

    for forbidden in (
        "## 毎日の記録として使う",
        "Activity Logは勤務時間です",
        "スマートフォンの行動も記録します",
        "振り返りを自動生成します",
        "日誌の作成にはLLMが必須です",
    ):
        assert forbidden not in text
```

- [ ] **Step 3: Run the new contract and confirm RED**

Create and validate a temporary pytest directory:

```powershell
$redTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-readme-red-" + [guid]::NewGuid())
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$resolvedRedTmp = [System.IO.Path]::GetFullPath($redTmp)
if (-not $resolvedRedTmp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedRedTmp"
}
New-Item -ItemType Directory -Path $resolvedRedTmp | Out-Null
$env:PYTHONPATH = (Join-Path (git rev-parse --show-toplevel) "src")
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" -m pytest -q `
  tests/test_readme_contract.py::test_readme_presents_an_ai_optional_daily_journal_and_reflection_loop `
  --basetemp $resolvedRedTmp
```

Expected: one failure because `## 毎日の記録と振り返りに使う` is not yet in the README. If it passes, the test is not proving the planned change; stop and inspect the current README.

- [ ] **Step 4: Replace the current daily-use section**

In `README.md`, replace the complete section from `## 毎日の記録として使う` through the paragraph ending with `日々のLLM提案は現在の契約どおり1〜3件です。` with this exact content:

````markdown
## 毎日の記録と振り返りに使う

KaizenLogは、AIを使わない日でも、ActivityWatchが観測したPC前景アプリを一日の業務日誌としてObsidianへ残せます。記憶だけに頼らず、実際の時間配分と作業の流れを見てから振り返れます。

### こんな日誌が残ります

> 以下は出力形式を示す**架空の日誌例**です。実在する利用者のデータや改善効果ではありません。

```markdown
## 📊 Activity Log

**合計アクティブ時間**: 6h42m / コンテキストスイッチ: 18回

**集中ブロック**: 3回 / 合計 2h05m（15分以上入力が続いた区間） / キー入力 8,420回

### カテゴリ別

| カテゴリ | 時間 | 割合 |
| --- | ---: | ---: |
| 執筆・ノート | 2h18m | 34% |
| 調査 | 1h36m | 24% |
| 会議 | 1h24m | 21% |
| 事務作業 | 1h24m | 21% |

### タイムライン

| 時刻 | 時間 | カテゴリ | アプリ | 内容 |
| --- | ---: | --- | --- | --- |
| 09:10-10:22 | 1h12m | 執筆・ノート | Obsidian | 提案書の初稿 |
| 13:00-13:48 | 48m | 会議 | Teams | 定例ミーティング |
| 15:05-15:52 | 47m | 調査 | Chrome | 競合サービス調査 |
```

集中ブロックは、入力watcherから統計を取得できる場合だけ表示されます。Activity Logは観測できたPC作業の記録であり、勤務時間、目標達成、集中力、生産性を自動判定するものではありません。

### AI以外にも使える3つの振り返り

- **時間配分を見直す** — 執筆、会議、調査、事務作業など、予定と実際の配分の違いを確認できます。
- **集中と中断を振り返る** — 集中ブロック、コンテキストスイッチ、タイムラインから、作業が細切れになった時間帯を探せます。
- **一日の流れを思い出す** — 時刻、カテゴリ、アプリを手がかりに「何をしていたか」を振り返れます。

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

`generate --date YYYY-MM-DD`だけならLLMは不要です。手書きの振り返りにもLLMは必要ありません。振り返りを翌日の改善提案へ反映するときだけ、設定済みのLLMバックエンドを使います。

### 毎日のコマンド

```powershell
kaizenlog run
kaizenlog morning
kaizenlog today
```

`kaizenlog run`は`generate`と`advise`を順に実行します。LLMを使わずActivity Logだけを作る場合は`kaizenlog generate --date YYYY-MM-DD`を使ってください。`backend = "none"`の状態で`advise`を呼ぶと、LLM生成を行わずエラーとして終了します。

`morning`は未完了アクションを再表示し、必要な場合は追いつき処理も行います。追いつきを行わない表示だけの確認には`kaizenlog morning --skip-catch-up`を使います。`today`で候補を確認し、実行済みなら`kaizenlog done KZN-…001`で完了にできます。日々のLLM提案は現在の契約どおり1〜3件です。
````

- [ ] **Step 5: Run the README contracts and confirm GREEN**

Create a fresh validated temp directory:

```powershell
$greenTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-readme-green-" + [guid]::NewGuid())
$resolvedGreenTmp = [System.IO.Path]::GetFullPath($greenTmp)
if (-not $resolvedGreenTmp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedGreenTmp"
}
New-Item -ItemType Directory -Path $resolvedGreenTmp | Out-Null
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" -m pytest -q `
  tests/test_readme_contract.py --basetemp $resolvedGreenTmp
```

Expected: all README contract tests pass, including the new daily-journal contract and the existing four-image, runtime-boundary, and M365 contracts.

- [ ] **Step 6: Run the README audit**

Run:

```powershell
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" `
  "C:\Users\awano\.codex\skills\beautify-github-readme\scripts\audit_readme.py" README.md
```

Expected: exit code 0, every local image target exists, and the existing four SVGs remain valid.

- [ ] **Step 7: Inspect the copy and scope**

Run:

```powershell
git diff --check
git diff -- README.md tests/test_readme_contract.py
git status --short
```

Check each of these explicitly:

- the sample is labeled synthetic;
- all sample categories and numbers are internally consistent;
- focus blocks are conditional;
- Activity Log is not called working hours or a productivity verdict;
- goal, manual reflection, and optional LLM advice are visibly distinct;
- the existing `run`, backend-none, 1–3 advice, four-image, and M365 boundaries remain unchanged;
- no file other than `README.md` and `tests/test_readme_contract.py` is changed by this task.

- [ ] **Step 8: Commit the tested documentation change**

Run:

```powershell
git add -- README.md tests/test_readme_contract.py
git diff --cached --check
if ($LASTEXITCODE -ne 0) { throw "Cached diff has whitespace errors" }
git diff --cached --name-only
git commit -m "docs: present KaizenLog as a daily reflection journal"
```

Expected: the staged file list contains exactly `README.md` and `tests/test_readme_contract.py`.

---

### Task 2: Perform Final Documentation Verification

**Files:**

- Verify only; do not create persistent repository files.

**Interfaces:**

- Consumes: the Task 1 documentation commit.
- Produces: fresh evidence that the README contracts, full test suite, audit, and changed-file scope remain clean.

- [ ] **Step 1: Run the focused README tests again**

Create a new validated OS temp directory:

```powershell
$implementationBase = git rev-parse HEAD^
$focusedTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-readme-focused-" + [guid]::NewGuid())
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

- [ ] **Step 2: Run the full test suite**

Create another validated OS temp directory:

```powershell
$fullTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-readme-full-" + [guid]::NewGuid())
$resolvedFullTmp = [System.IO.Path]::GetFullPath($fullTmp)
if (-not $resolvedFullTmp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedFullTmp"
}
New-Item -ItemType Directory -Path $resolvedFullTmp | Out-Null
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" -m pytest -q `
  --basetemp $resolvedFullTmp
```

Expected: exit code 0 and no failures.

- [ ] **Step 3: Re-run static README verification**

Run:

```powershell
& "C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe" `
  "C:\Users\awano\.codex\skills\beautify-github-readme\scripts\audit_readme.py" README.md
git diff --check $implementationBase..HEAD
```

Expected: README audit exit code 0 and no whitespace errors.

- [ ] **Step 4: Inspect the final changed-file scope**

Run:

```powershell
git status --short
git log --oneline --decorate $implementationBase..HEAD
git diff --name-only $implementationBase..HEAD
git diff --stat $implementationBase..HEAD
```

Expected:

- one implementation commit after the task base;
- changed files are exactly `README.md` and `tests/test_readme_contract.py`;
- no Python implementation, `docs/USAGE.md`, SVG, extension, config, prompt, user-data, or `.superpowers/` file is present;
- no push or external service call has occurred.

- [ ] **Step 5: Report exact evidence**

Report:

- implementation commit hash;
- changed files;
- RED failure reason and GREEN README test count;
- full pytest count;
- README audit result;
- final `git status --short`;
- the remaining limitation that the journal covers observed PC activity and not a complete record of all work or life activity.
