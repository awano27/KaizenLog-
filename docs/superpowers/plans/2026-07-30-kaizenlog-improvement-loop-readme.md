# KaizenLog Improvement Loop README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all five shipped improvement-loop features reliably discoverable and usable, fix the broken nested `abtest` help, and rebuild the README around the measurable Measure → Teach → Verify loop without presenting M365 Copilot support as implemented.

**Architecture:** Preserve the existing feature implementations and change only the two malformed `argparse` help strings. Treat the README and four pure-SVG assets as one documentation surface: assets communicate the loop at a glance, while Markdown provides accurate commands, evidence, safety boundaries, and an explicitly separated M365 `Next / Planned` concept.

**Tech Stack:** Python 3.11, `argparse`, pytest, GitHub-flavored Markdown, pure SVG 1.1-compatible XML, PowerShell on Windows.

## Global Constraints

- The five features in commit `05a408c` are current features, not `Local Preview`.
- M365 Copilot Chrome capture and improvement overlay are `Next / Planned` and currently unimplemented.
- Do not change the five features' data models, calculations, persistence, or approval contracts.
- Do not call ActivityWatch, Obsidian, a real LLM backend, Microsoft 365, or any live user-data path.
- Keep browser telemetry local-only and describe only the current three supported domains as available.
- Use only pure SVG: no `foreignObject`, JavaScript, remote fonts, remote images, or GIF.
- Preserve the Daily Ledger palette: `#0B1211`, `#F4F0E8`, `#75CFA3`, `#F0B667`, `#81958C`.
- Do not stage or commit `.superpowers/` brainstorming output.
- Do not publish, deploy, release, or push without separate user authorization.

## File Map

- Modify `src/kaizenlog/cli.py`: escape two literal percent signs in nested `abtest` help.
- Modify `tests/test_round28_review_fixes.py`: add the regression test for both nested help screens.
- Modify `assets/readme/hero.svg`: show the product promise and full closed loop.
- Modify `assets/readme/section-loop.svg`: explain Measure → Teach → Verify.
- Modify `assets/readme/workflow.svg`: show evidence inputs, deterministic KaizenLog processing, Obsidian output, and controlled agent-context output.
- Modify `assets/readme/section-start.svg`: show the side-effect-conscious first-success path.
- Create `tests/test_readme_contract.py`: preserve the approved README story, local links, and SVG validity.
- Modify `README.md`: replace the old “PC work → one action” story with the measurable AI-improvement story.

---

### Task 1: Fix Nested `abtest` Help with a Regression Test

**Files:**

- Modify: `tests/test_round28_review_fixes.py` at the end of the file
- Modify: `src/kaizenlog/cli.py:2094-2108`

**Interfaces:**

- Consumes: `kaizenlog.cli.main(argv: list[str] | None) -> int`
- Produces: both nested help screens exit through `SystemExit(0)` and render a literal `+N%`
- Does not change: `cmd_abtest`, experiment files, effect calculation, card generation

- [ ] **Step 1: Add the failing regression test**

Append this exact test:

```python
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["abtest", "new", "--help"], "予測効果 +N または +N%"),
        (["abtest", "finish", "--help"], "体感効果 +N または +N%"),
    ],
)
def test_r13_abtest_nested_help_renders_literal_percent(
    argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
):
    from kaizenlog import cli as cli_mod

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(argv)

    assert exc.value.code == 0
    assert expected in capsys.readouterr().out
```

- [ ] **Step 2: Run the test and confirm the current failure**

Run:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pytest -q tests/test_round28_review_fixes.py::test_r13_abtest_nested_help_renders_literal_percent
```

Expected: both parameter cases fail before reaching `SystemExit(0)` with an `argparse` percent-formatting `ValueError`.

- [ ] **Step 3: Make the minimal production change**

Change only the two help strings:

```python
ab_new.add_argument(
    "--predict",
    required=True,
    help="予測効果 +N または +N%%（例: +30）",
)
```

```python
ab_fin.add_argument(
    "--felt",
    required=True,
    help="体感効果 +N または +N%%",
)
```

`argparse` converts `%%` to the user-facing literal `%`; the parser's accepted values remain unchanged.

- [ ] **Step 4: Run the regression test and related command help**

Run:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pytest -q tests/test_round28_review_fixes.py::test_r13_abtest_nested_help_renders_literal_percent
.\.venv\Scripts\python.exe -m kaizenlog.cli abtest new --help
.\.venv\Scripts\python.exe -m kaizenlog.cli abtest finish --help
.\.venv\Scripts\python.exe -m kaizenlog.cli abtest status --help
```

Expected: pytest reports `2 passed`; all three commands exit 0; the first two display `+N%` without a traceback.

- [ ] **Step 5: Review and commit the isolated fix**

Run:

```powershell
git diff --check -- src/kaizenlog/cli.py tests/test_round28_review_fixes.py
git diff -- src/kaizenlog/cli.py tests/test_round28_review_fixes.py
git add -- src/kaizenlog/cli.py tests/test_round28_review_fixes.py
git commit -m "fix: render abtest help percentages"
```

The diff must contain only the test and the two escaped help strings.

---

### Task 2: Redraw the Four Pure-SVG README Assets

**Files:**

- Modify: `assets/readme/hero.svg`
- Modify: `assets/readme/section-loop.svg`
- Modify: `assets/readme/workflow.svg`
- Modify: `assets/readme/section-start.svg`

**Interfaces:**

- Consumes: the approved Closed Loop story and Daily Ledger palette
- Produces: four self-contained SVG files embedded by `README.md`
- Does not consume: generated raster images, external URLs, JavaScript, browser data

- [ ] **Step 1: Rebuild `hero.svg` around the closed loop**

Keep a `1200`-unit viewBox and use this exact content hierarchy:

```xml
<title id="title">KaizenLog — AIとの仕事を、実測で調教する</title>
<desc id="desc">AIとのやり直しを測り、実測した教訓を次のAIへ渡し、変更前後の効果を確かめる閉ループ。</desc>
```

The visible text must be:

```text
KAIZENLOG / AI WORK LEDGER
AIとの仕事を、実測で調教する。
リトライのムダを測り、教訓を戻し、本当に効いたか確かめる。

01 MEASURE             02 TEACH              03 VERIFY
Loop Tax               handoff               predict
Prompt ROI             coach                 felt / measured

Windows · ActivityWatch · Obsidian · AI work
```

Use three equal cards connected by arrows. Use `#75CFA3` for Measure and arrows, `#F0B667` for Teach, and both colors plus plain text for Verify. Main Japanese copy must be at least `42` SVG units, card headings at least `22`, and required card labels at least `20`.

- [ ] **Step 2: Rebuild `section-loop.svg` as the detailed loop**

Use `viewBox="0 0 1200 760"` with three vertically stacked, full-width stage cards so the required labels remain readable at a 360px GitHub render. Use these exact labels:

```text
MEASURE / ムダを測る
Loop Tax · Prompt ROI

TEACH / 学びを渡す
handoff · coach

VERIFY / 効いたか確かめる
predict · felt · measured
```

Connect Verify back to Measure with a visible return arrow labeled `NEXT RUN`. Include:

```xml
<title id="title">KaizenLogのAI改善ループ</title>
<desc id="desc">やり直しのムダを測り、学びを次のAIへ渡し、予測・体感・実測で効果を確かめる循環。</desc>
```

- [ ] **Step 3: Rebuild `workflow.svg` with accurate boundaries**

Use four areas in a two-column, two-row grid so their required labels remain readable at a 360px GitHub render:

```text
EVIDENCE
ActivityWatch
AI session logs
Browser AI JSONL

KAIZENLOG
deterministic metrics
validated proposals

DAILY RECORD
Obsidian Markdown
stats JSON

AGENT CONTEXT
handoff: marker update
coach: approval required
```

Render `LLM / optional` as a dotted branch into `validated proposals`, not as the owner of deterministic metrics or file writes. Include:

```xml
<title id="title">KaizenLogの計測・記録・引き継ぎフロー</title>
<desc id="desc">ActivityWatchとAI作業ログをKaizenLogが決定的に集計し、Obsidianへ保存する。任意のLLM提案を検証し、承認された教訓だけをエージェント向け文書へ反映する。</desc>
```

- [ ] **Step 4: Update `section-start.svg` to the explicit-date first success**

Use these three steps:

```text
01 SETUP
接続先を選ぶ
kaizenlog setup

02 DOCTOR
設定を診断する
kaizenlog doctor

03 FIRST PROOF
指定日の日誌を作る
kaizenlog generate --date YYYY-MM-DD
```

Include:

```xml
<title id="title">KaizenLogの副作用を抑えた初回導線</title>
<desc id="desc">setupで接続先を選び、doctorで診断し、明示した日付のgenerateで最初の日誌を作る。</desc>
```

- [ ] **Step 5: Validate XML and prohibited constructs**

Run:

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from xml.etree import ElementTree as ET; files=sorted(Path('assets/readme').glob('*.svg')); [ET.parse(p) for p in files]; print(f'{len(files)} SVGs parsed')"
rg -n 'foreignObject|<script|@font-face|<image|href="https?://' assets/readme
```

Expected: `4 SVGs parsed`; `rg` has no matches.

- [ ] **Step 6: Inspect every asset at GitHub and mobile widths**

Serve the repository without writing generated files:

```powershell
$server = Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "-m","http.server","8765","--bind","127.0.0.1" -WorkingDirectory (Get-Location) -WindowStyle Hidden -PassThru
```

Open each `http://127.0.0.1:8765/assets/readme/<name>.svg` in the in-app browser. Inspect at approximately `900px` and `360px` viewport widths. Required outcomes:

- hero promise and MEASURE / TEACH / VERIFY remain readable;
- no text clips a card or the viewBox;
- arrows have visible heads and do not cross labels;
- color is not the only way to identify a stage.

Stop only the recorded process:

```powershell
Stop-Process -Id $server.Id
```

- [ ] **Step 7: Review and commit the asset set**

Run:

```powershell
git diff --check -- assets/readme
git diff --stat -- assets/readme
git add -- assets/readme/hero.svg assets/readme/section-loop.svg assets/readme/workflow.svg assets/readme/section-start.svg
git commit -m "docs: redraw the measurable AI improvement loop"
```

Do not stage `.superpowers/`.

---

### Task 3: Write a README Contract Test and Rebuild the README

**Files:**

- Create: `tests/test_readme_contract.py`
- Modify: `README.md`

**Interfaces:**

- Consumes: current CLI behavior, the four SVG assets from Task 2, `docs/USAGE.md`, `browser-extension/README.md`
- Produces: a GitHub landing page whose claims are mechanically checked
- External references: Microsoft 365 official custom-instruction and Copilot Chat documentation

- [ ] **Step 1: Add the failing README story and link contract**

Create `tests/test_readme_contract.py` with:

```python
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
LOCAL_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def test_readme_tells_the_current_improvement_loop_story():
    text = README.read_text(encoding="utf-8")
    for required in (
        "AIとの仕事を、実測で調教する。",
        "やり直しのムダを測る",
        "効果の高い依頼方法を見つける",
        "学んだルールを次のAIへ渡す",
        "改善効果を実測する",
        "kaizenlog prompts --roi",
        "kaizenlog handoff",
        "kaizenlog coach",
        "kaizenlog abtest",
        "M365 Copilot",
        "Next / Planned",
        "現在未実装",
    ):
        assert required in text
    assert "Local Preview" not in text


def test_readme_local_targets_exist():
    text = README.read_text(encoding="utf-8")
    missing: list[str] = []
    for raw in LOCAL_LINK_RE.findall(text):
        target = raw.strip().split(maxsplit=1)[0]
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path_text = unquote(target.split("#", 1)[0])
        if path_text and not (ROOT / path_text).resolve().exists():
            missing.append(target)
    assert missing == []


def test_readme_svg_assets_are_well_formed_and_self_contained():
    for name in ("hero.svg", "section-start.svg", "section-loop.svg", "workflow.svg"):
        path = ROOT / "assets" / "readme" / name
        ET.parse(path)
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "<foreignObject",
            "<script",
            "@font-face",
            "<image",
            'href="http://',
            'href="https://',
        ):
            assert forbidden not in text
```

- [ ] **Step 2: Run the contract and verify the story test fails**

Run:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pytest -q tests/test_readme_contract.py
```

Expected: the current README fails `test_readme_tells_the_current_improvement_loop_story` because it does not contain the approved headline and five-feature story. Link and SVG tests may already pass.

- [ ] **Step 3: Replace the opening and information order**

Use this exact opening:

```markdown
# KaizenLog

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="KaizenLog — AIとの仕事を、実測で調教する">
</p>

**AIとの仕事を、実測で調教する。**

KaizenLogは、PC作業とAIセッションの記録から、やり直しのムダ、再利用できる依頼方法、改善ルールの効果を測るWindows向けCLIです。ActivityWatchの事実をObsidianへ残し、必要な場合だけLLMを使います。

`Windows` · `Python 3.11+` · `ActivityWatch` · `Obsidian` · `MIT`

[3コマンドで始める](#3コマンドで始める) · [改善ループ](#measure--teach--verify) · [データの扱い](#llmとデータの扱い) · [詳しい使い方](docs/USAGE.md)
```

The complete top-level reading order must be:

```text
まず、何が変わるのか
実際に残る証拠
3コマンドで始める
Measure → Teach → Verify
基本ワークフロー
毎日の記録として使う
ブラウザAIとM365 Copilot
LLMとデータの扱い
制限とカスタマイズ
開発
ロードマップ
ライセンス
```

- [ ] **Step 4: Add the plain-language feature map and evidence**

Use this table:

```markdown
| できること | 現行機能 |
| --- | --- |
| やり直しのムダを測る | **Loop Tax** — 最終試行を除くリトライ連鎖の時間・取得可能なトークン・推定費用 |
| 効果の高い依頼方法を見つける | **Prompt ROI** — `kaizenlog prompts --roi` |
| 学んだルールを次のAIへ渡す | **handoff / coach** — `kaizenlog handoff`、`kaizenlog coach` |
| 改善効果を実測する | **A/B test** — `kaizenlog abtest` |
```

Show short, explicitly labeled output examples for Loop Tax, Prompt ROI, agent-context marker output, and the A/B result card. Do not invent benchmark or customer data. Use neutral sample values and label the entire block `表示例`.

State near the examples:

- unknown token or cost values remain unknown;
- Prompt ROI requires a completed comparison window for confirmed `skilled` effect;
- `handoff` regenerates only its marker range;
- `coach` creates a proposal and requires explicit `--apply`;
- insufficient A/B baseline produces an invalid/not-established result instead of a fabricated effect.

- [ ] **Step 5: Use the explicit-date first-success path**

Use:

```powershell
kaizenlog setup
kaizenlog doctor
kaizenlog generate --date YYYY-MM-DD
```

Explain that the user replaces `YYYY-MM-DD` with the day to generate. Keep the existing Git clone and `pipx install .` path, version `1.5.0rc1`, Windows 10/11, Python 3.11+, Obsidian, and ActivityWatch prerequisites.

Do not present bare `generate` as the safest first run because the default path can perform catch-up work.

- [ ] **Step 6: Document Measure, Teach, and Verify accurately**

The section must contain these commands:

```powershell
# MEASURE
kaizenlog status
kaizenlog prompts --roi

# TEACH
kaizenlog handoff --dry-run
kaizenlog coach --dry-run

# VERIFY
kaizenlog abtest new --predict +30 --days 28
kaizenlog abtest status
kaizenlog abtest finish --felt +20
```

Explain:

- `handoff --dry-run` previews deterministic, measured lessons; configured targets live under `[handoff] targets`.
- `coach --dry-run` shows the redacted 30-day context and does not call the LLM.
- plain `coach` creates the proposal file and diff; only `coach --apply <proposal-file>` writes the managed coach section.
- `abtest` compares prediction, feeling, and measured effect, and creates an SVG card when finished.

- [ ] **Step 7: Correct the daily and backend claims**

Replace the inaccurate statement that `run` silently skips advice when `backend = "none"` with:

```markdown
`kaizenlog run`は`generate`と`advise`を順に実行します。LLMを使わずActivity Logだけを作る場合は`kaizenlog generate --date YYYY-MM-DD`を使ってください。`backend = "none"`の状態で`advise`を呼ぶと、LLM生成を行わずエラーとして終了します。
```

Describe daily advice as `1〜3件` where relevant, matching the current contract. Describe `openai-compatible` as the configured endpoint, which can be local or remote; do not call every fallback local.

- [ ] **Step 8: Separate current browser support from M365 `Next / Planned`**

Keep current support:

```markdown
### Available — ブラウザAIテレメトリ

現在のChrome拡張はChatGPT、Claude.ai、Geminiの3ドメインだけを対象にし、会話イベントをローカルJSONLへ保存します。
```

Add this exact boundary immediately after it:

```markdown
### Next / Planned — M365 Copilot改善アシスト

> **現在未実装です。**

M365 Copilot Chatだけに任意のサイト権限を与え、依頼・回答・往復回数をローカル計測し、改善プロンプトやカスタム指示をコピー可能な形で提案する構想です。会話の自動取得、Microsoft Graph／テナント連携、自動送信、カスタム指示の自動変更にはまだ対応していません。
```

Link the planning statement to these official pages:

- `https://support.microsoft.com/en-us/microsoft-365-copilot/customize-how-microsoft-365-copilot-responds-to-you`
- `https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/declarative-agent-instructions`

Do not add `m365.cloud.microsoft.com` to the extension manifest in this task.

- [ ] **Step 9: Run the README contract and README audit**

Run:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pytest -q tests/test_readme_contract.py
.\.venv\Scripts\python.exe C:\Users\awano\.codex\skills\beautify-github-readme\scripts\audit_readme.py README.md
```

Expected: `3 passed`; audit reports `OK: image references and SVG basics passed`.

- [ ] **Step 10: Review the full documentation diff and commit**

Run:

```powershell
git diff --check -- README.md tests/test_readme_contract.py
git diff -- README.md tests/test_readme_contract.py
git add -- README.md tests/test_readme_contract.py
git commit -m "docs: explain the measurable AI improvement loop"
```

Confirm the staged diff does not contain `.superpowers/`, an M365 implementation claim, or a `Local Preview` label.

---

### Task 4: Perform Cross-Surface Verification

**Files:**

- Verify only; no persistent output files

**Interfaces:**

- Consumes: the three implementation commits from Tasks 1–3
- Produces: fresh evidence for CLI behavior, tests, README integrity, SVG validity, and scope

- [ ] **Step 1: Run all relevant help screens without truncating errors**

Run:

```powershell
$env:PYTHONPATH = "src"
$commands = @(
  @("handoff", "--help"),
  @("coach", "--help"),
  @("prompts", "--help"),
  @("abtest", "--help"),
  @("abtest", "new", "--help"),
  @("abtest", "finish", "--help"),
  @("abtest", "status", "--help")
)
foreach ($argv in $commands) {
  & .\.venv\Scripts\python.exe -m kaizenlog.cli @argv
  if ($LASTEXITCODE -ne 0) { throw "CLI help failed: kaizenlog $($argv -join ' ')" }
}
```

Expected: every command exits 0 with no traceback.

- [ ] **Step 2: Run focused tests**

Create an OS temporary test directory and verify it is inside the Windows temp root:

```powershell
$testTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-readme-" + [guid]::NewGuid())
$resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$resolvedTestTmp = [System.IO.Path]::GetFullPath($testTmp)
if (-not $resolvedTestTmp.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedTestTmp"
}
New-Item -ItemType Directory -Path $resolvedTestTmp | Out-Null
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pytest -q tests/test_round28_review_fixes.py tests/test_readme_contract.py --basetemp $resolvedTestTmp
```

Expected: all focused tests pass.

- [ ] **Step 3: Run the full test suite**

Use a fresh verified OS temporary directory:

```powershell
$fullTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-full-" + [guid]::NewGuid())
$resolvedFullTmp = [System.IO.Path]::GetFullPath($fullTmp)
if (-not $resolvedFullTmp.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Unsafe pytest temp path: $resolvedFullTmp"
}
New-Item -ItemType Directory -Path $resolvedFullTmp | Out-Null
.\.venv\Scripts\python.exe -m pytest -q --basetemp $resolvedFullTmp
```

Expected: exit code 0 and zero failures.

- [ ] **Step 4: Re-run README and SVG static verification**

Run:

```powershell
.\.venv\Scripts\python.exe C:\Users\awano\.codex\skills\beautify-github-readme\scripts\audit_readme.py README.md
.\.venv\Scripts\python.exe -c "from pathlib import Path; from xml.etree import ElementTree as ET; files=sorted(Path('assets/readme').glob('*.svg')); [ET.parse(p) for p in files]; print(f'{len(files)} SVGs parsed')"
git diff --check HEAD~3..HEAD
```

Expected: README audit OK, four parsed SVGs, and no whitespace errors.

- [ ] **Step 5: Inspect final Git scope**

Run:

```powershell
git status --short
git log -4 --oneline --decorate
git diff --stat 9509bb7..HEAD
```

Expected:

- persistent changes are represented by the three task commits;
- `.superpowers/` remains untracked and is not in any commit;
- no ActivityWatch, Obsidian, generated note, config, or user-data file appears;
- no push has occurred.

- [ ] **Step 6: Report evidence without changing external state**

Report:

- exact commits created;
- files changed;
- nested help results;
- focused and full pytest counts;
- README audit and SVG parse result;
- any remaining limitation, especially that M365 Copilot support is planned, not implemented;
- `git status --short` and whether `.superpowers/` was intentionally left untracked.
