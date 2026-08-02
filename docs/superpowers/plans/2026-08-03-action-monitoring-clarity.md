# Action Monitoring Clarity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 日誌と `kaizenlog today` で、今日実行する改善、観測だけする改善、日次目標の状態を混同せず5秒以内に判断できるようにする。

**Architecture:** `memory.py` に実行候補分離と判定後trajectoryの純粋な構造化helperを置き、Markdown rendererとCLIが同じ状態判定を共有する。日次目標は既存GOAL区間を読み取るだけでACTIONS内へ状態を再掲し、実Vault更新は完成した純粋renderer出力をACTIONS marker内だけへ適用する。

**Tech Stack:** Python 3.11、pytest、既存KaizenLog Memory/Vault/Goal API、Markdown、JSON知識グラフ

## Global Constraints

- ActivityWatch、screenpipe、LLM、`generate`、`advise` は起動しない。
- KZN提案本文、PASS機械契約、判定履歴、GOAL区間の書き込み所有権を変更しない。
- confirmed PASSは今日のcheckboxへ戻さず、観測カードにはcheckboxを付けない。
- 今日のcheckboxは既定1件、明示指定でも全体最大3件とする。
- 最新観測がFAILの場合だけ警告し、演算子に依存しない `達成 N/M日・未達 X日` 表記を使う。
- stats欠損、分母不足、実行ログ欠損は推測せず `未判定` または `不明` と表示する。
- `kaizenlog today --all`、legacy自由文、checkbox同期、provisional/confirmed境界、privacy、marker外bytesを維持する。
- `.grok/` と `scripts/self_improve_graph.py` は変更、stage、commitしない。
- critique-reviseは実装評価で最大1回とし、全体上限2回を超えない。

---

## File Map

- `src/kaizenlog/memory.py`: 候補分離、trajectory構造、3つの主ブロックと状況導線のMarkdownを所有する。
- `src/kaizenlog/cli.py`: `today` の既定候補へ共通分離契約を適用する。
- `tests/test_round50_action_monitoring_clarity.py`: 今回の新規読者契約とCLI整合性を所有する。
- `tests/test_round40_review_residuals.py`: 古い「過去に❌が1件あれば回帰」契約を最新状態契約へ更新する。
- `tests/test_round45_reader_ux.py`: 「今日の実験」重複リードを新しい `今日やること` 契約へ更新する。
- `tests/test_action_ux_display_cap.py`: checkbox上限と完了導線を新カード構造で維持する。
- `C:\develop\obsidian\2026\01 Daily Notes\2026-08-03.md`: 完成rendererでACTIONS marker内だけを更新する。
- `.kaizenlog/improvement_graph.json`: CodeChange、Evidence、TestResultと根拠edgeを追記する。
- `PLAN.md`: RED/GREEN、実日誌hash、全検証、push結果を追記する。

## Spec Coverage

| 仕様要件 | 実装Task | 検証 |
| --- | --- | --- |
| confirmed PASSを今日のcheckboxから分離 | Task 1–2 | focused candidate/Markdown tests |
| 最新値、達成日数、演算子中立、最新FAILだけ警告 | Task 1–2 | structured trajectory tests |
| 分母不足でも効果目標とUnknownを表示 | Task 2 | denominator test |
| metric集計範囲と因果限界を表示 | Task 2 | scope assertions and real-note check |
| 日次目標の未設定・未入力・自己申告を表示 | Task 2 | parametrized goal test |
| 既定1件・最大3件、legacy、provisional、checkbox同期 | Task 2 | existing regression suite |
| `today` と日誌の候補契約を一致、`--all`保持 | Task 3 | CLI focused/regression tests |
| 実日誌marker外bytes保持 | Task 4 | before/after SHA-256 |
| Graph tripleと全pytestに接地 | Task 4 | graph validator and full suite |
| commit、push、remote/CI確認 | Task 4 | git SHA and GitHub Actions evidence |

---

### Task 1: 実行候補と観測状態を構造化する

**Files:**
- Create: `tests/test_round50_action_monitoring_clarity.py`
- Modify: `src/kaizenlog/memory.py:438-492`
- Modify: `src/kaizenlog/memory.py:1474-1530`

**Interfaces:**
- Consumes: `MemoryEntry`, `order_still_open_for_display`, `metric_from_stats`, `target_met`, `parse_pass_condition`
- Produces: `split_action_candidates(entries: Sequence[MemoryEntry], checked_ids: set[str]) -> tuple[list[MemoryEntry], list[MemoryEntry]]`
- Produces: frozen `MetricObservation(day: date, value: float, met: bool)`
- Produces: frozen `PostVerdictTrajectory(metric: str, op: str, target: float, observations: tuple[MetricObservation, ...])`
- Produces: `_post_verdict_trajectory(entry, target_day, stats_by_day) -> PostVerdictTrajectory | None`

- [ ] **Step 1: honest test rulesを読む**

Run:

```powershell
Get-Content -Raw C:\Users\awano\.codex\plugins\cache\openai-curated-remote\superpowers\6.2.0\skills\test-driven-development\writing-good-tests.md
```

Expected: testが失敗するproduction changeを先に言語化し、mockではなく実際のhelper結果をassertする規則を確認できる。

- [ ] **Step 2: focused test fileの実データ相当fixtureを書く**

```python
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kaizenlog.cli import cmd_today
from kaizenlog.config import Config
from kaizenlog.memory import (
    MemoryEntry,
    _post_verdict_trajectory,
    append_entries,
    render_actions_section,
    split_action_candidates,
)
from kaizenlog.vault import GOAL_MARKER, upsert_section


def _active_entry() -> MemoryEntry:
    return MemoryEntry(
        id="KZN-20260802-001",
        date="2026-08-02",
        action=(
            "午前と午後のアラームが鳴ったとき→30分タイマーをかけ、"
            "その時点で使っているカテゴリのアプリ以外を最小化する"
            "｜PASS: context_switches_per_hour <= 65"
            "（1時間あたりのカテゴリ変更回数）"
            "｜FAIL: context_switches_per_hour > 65"
        ),
    )


def _achieved_entry() -> MemoryEntry:
    return MemoryEntry(
        id="KZN-20260727-002",
        date="2026-07-27",
        action=(
            "codexセッション起動前→期待成果物を書き、終了後にgit logで確認する"
            "｜PASS: ai_avg_turns >= 2.5"
            "（Claude Codeセッションの平均往復数）"
            "｜FAIL: ai_avg_turns < 2.5"
        ),
        verdict="pass",
        verdict_value=3.4,
        verdict_date="2026-07-28",
        verdict_stage="confirmed",
    )


def _active_and_achieved_entries() -> tuple[MemoryEntry, MemoryEntry]:
    return _active_entry(), _achieved_entry()


def _realistic_history(*, latest_ai_turns: float = 3.2) -> list[dict]:
    days = ["2026-07-29", "2026-07-30", "2026-07-31", "2026-08-01", "2026-08-02"]
    values = [6.1, 12.3, 1.5, 13.3, latest_ai_turns]
    history = [
        {
            "day": day,
            "total_minutes": 120.0,
            "context_switches": 20,
            "ai": {"avg_turns": value, "sessions": 22},
        }
        for day, value in zip(days, values)
    ]
    history.append(
        {
            "day": "2026-08-03",
            "total_minutes": 22.7,
            "context_switches": 25,
            "ai": {"avg_turns": 1.0, "sessions": 1},
        }
    )
    return history


def _history_with_latest(value: float) -> list[dict]:
    return _realistic_history(latest_ai_turns=value)


def _config_with_entries(tmp_path: Path, entries: list[MemoryEntry]) -> Config:
    vault = tmp_path / "vault"
    (vault / "01 Daily Notes").mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / ".kaizenlog" / "logs").mkdir(parents=True)
    cfg = Config(
        vault_dir=vault,
        timezone="Asia/Tokyo",
        daily_notes_dir="01 Daily Notes",
        memory_dir="Kaizen/Memory",
        stats_dir=".kaizenlog/stats",
        logs_dir=".kaizenlog/logs",
        actions_position="top",
    )
    append_entries(cfg.memory_path, entries)
    return cfg
```

- [ ] **Step 3: 候補分離の failing testを書く**

```python
def test_confirmed_pass_is_monitoring_not_action_candidate():
    active = MemoryEntry(
        id="KZN-20260802-001",
        date="2026-08-02",
        action="alarm→minimize｜PASS: context_switches_per_hour <= 65｜FAIL: context_switches_per_hour > 65",
    )
    achieved = MemoryEntry(
        id="KZN-20260727-002",
        date="2026-07-27",
        action="note→verify｜PASS: ai_avg_turns >= 2.5｜FAIL: ai_avg_turns < 2.5",
        verdict="pass",
        verdict_date="2026-07-28",
        verdict_stage="confirmed",
    )

    actionable, monitoring = split_action_candidates([active, achieved], set())

    assert [e.id for e in actionable] == [active.id]
    assert [e.id for e in monitoring] == [achieved.id]

    checked_actionable, checked_monitoring = split_action_candidates(
        [active, achieved], {achieved.id}
    )
    assert [e.id for e in checked_actionable] == [active.id]
    assert checked_monitoring == []
```

Production change that makes it pass: `split_action_candidates` がconfirmed PASSかつ未チェックだけをmonitoringへ分ける。

- [ ] **Step 4: 候補分離testを実行してREDを確認する**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py::test_confirmed_pass_is_monitoring_not_action_candidate -q
```

Expected: `ImportError` または未定義functionでFAILする。fixture構築エラーなら修正し、意図した未実装FAILまで再実行する。

- [ ] **Step 5: 最小の候補分離helperを書く**

```python
def split_action_candidates(
    entries: Sequence[MemoryEntry],
    checked_ids: set[str],
) -> tuple[list[MemoryEntry], list[MemoryEntry]]:
    confirmed_pass = [
        e
        for e in entries
        if e.verdict == "pass"
        and e.verdict_stage == "confirmed"
    ]
    monitoring = [e for e in confirmed_pass if e.id not in checked_ids]
    actionable = [e for e in entries if e not in confirmed_pass]
    return order_still_open_for_display(actionable), monitoring
```

- [ ] **Step 6: 候補分離testをGREENにする**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py::test_confirmed_pass_is_monitoring_not_action_candidate -q
```

Expected: `1 passed`。

- [ ] **Step 7: trajectoryの failing testを書く**

```python
def test_post_verdict_trajectory_keeps_operator_and_latest_state():
    entry = MemoryEntry(
        id="KZN-20260727-002",
        date="2026-07-27",
        action="note→verify｜PASS: ai_avg_turns >= 2.5｜FAIL: ai_avg_turns < 2.5",
        verdict="pass",
        verdict_date="2026-07-28",
        verdict_stage="confirmed",
    )
    values = [6.1, 12.3, 1.5, 13.3, 3.2]
    stats = {
        f"2026-{month_day}": {"day": f"2026-{month_day}", "ai": {"avg_turns": value, "sessions": 22}}
        for month_day, value in zip(
            ["07-29", "07-30", "07-31", "08-01", "08-02"], values
        )
    }

    trajectory = _post_verdict_trajectory(entry, date(2026, 8, 3), stats)

    assert trajectory is not None
    assert (trajectory.metric, trajectory.op, trajectory.target) == ("ai_avg_turns", ">=", 2.5)
    assert [point.met for point in trajectory.observations] == [True, True, False, True, True]
    assert trajectory.observations[-1].value == 3.2
```

Production change that makes it pass: 文字列を組み立てる前の観測点をfrozen dataclassで返す。

- [ ] **Step 8: trajectory testのREDを確認する**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py::test_post_verdict_trajectory_keeps_operator_and_latest_state -q
```

Expected: `_post_verdict_trajectory` またはdataclass未定義でFAILする。

- [ ] **Step 9: dataclassと純粋trajectory helperを実装する**

```python
@dataclass(frozen=True)
class MetricObservation:
    day: date
    value: float
    met: bool


@dataclass(frozen=True)
class PostVerdictTrajectory:
    metric: str
    op: str
    target: float
    observations: tuple[MetricObservation, ...]


def _post_verdict_trajectory(
    entry: MemoryEntry,
    target_day: date,
    stats_by_day: dict[str, Mapping[str, Any]],
) -> PostVerdictTrajectory | None:
    if entry.verdict not in ("pass", "fail"):
        return None
    if entry.verdict_stage != "confirmed" or not entry.verdict_date:
        return None
    from .experiments import metric_from_stats, target_met
    from .verdict import parse_pass_condition

    parsed = parse_pass_condition(entry.action)
    if parsed is None:
        return None
    metric, op, target = parsed
    try:
        start = date.fromisoformat(entry.verdict_date) + timedelta(days=1)
    except ValueError:
        return None
    end = target_day - timedelta(days=1)
    observations: list[MetricObservation] = []
    current = start
    while current <= end:
        day_stats = stats_by_day.get(current.isoformat())
        if day_stats is not None:
            value = metric_from_stats(metric, dict(day_stats))
            if value is not None:
                observations.append(
                    MetricObservation(
                        day=current,
                        value=float(value),
                        met=target_met(float(value), op, float(target)),
                    )
                )
        current += timedelta(days=1)
    if not observations:
        return None
    return PostVerdictTrajectory(
        metric=metric,
        op=op,
        target=float(target),
        observations=tuple(observations[-5:]),
    )
```

既存 `_post_verdict_trajectory_lines` は次の薄い互換rendererへ変える。provisional、verdict_date欠損、機械PASSなし、測定点なしは `None` / `[]` のままにする。

```python
def _post_verdict_trajectory_lines(
    entry: MemoryEntry,
    target_day: date,
    stats_by_day: dict[str, Mapping[str, Any]],
) -> list[str]:
    trajectory = _post_verdict_trajectory(entry, target_day, stats_by_day)
    if trajectory is None:
        return []
    observations = trajectory.observations
    chain = " → ".join(
        f"{point.day.month}/{point.day.day} {point.value:g} "
        f"{'✅' if point.met else '❌'}"
        for point in observations
    )
    met_count = sum(point.met for point in observations)
    return [
        f"  └ 判定後の実測: {chain}",
        f"     (測定できた{len(observations)}日のうち"
        f"{met_count}日達成・{len(observations)-met_count}日未達。"
        "実行の有無は問わない指標の挙動です)",
    ]
```

- [ ] **Step 10: Task 1 focused testsをGREENにする**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py -q
```

Expected: Task 1の2件がPASSする。

- [ ] **Step 11: Task 1をcommitする**

```powershell
git add -- src/kaizenlog/memory.py tests/test_round50_action_monitoring_clarity.py
git diff --cached --check
git commit -m "refactor: structure action monitoring state"
```

Expected: この2ファイルだけを含むcommitが作成される。

---

### Task 2: ACTIONSを3つの主ブロックと状況導線へ変える

**Files:**
- Modify: `src/kaizenlog/memory.py:1796-1958`
- Modify: `tests/test_round50_action_monitoring_clarity.py`
- Modify: `tests/test_round40_review_residuals.py:286-345`
- Modify: `tests/test_round45_reader_ux.py:28-67`
- Modify: `tests/test_action_ux_display_cap.py:145-184`

**Interfaces:**
- Consumes: `split_action_candidates`, `_post_verdict_trajectory`, `format_effect_metric_clause`, `_denominator_shortfall_note`, `goal.read_goal`
- Produces: `_split_action_trigger(body: str) -> tuple[str | None, str]`
- Produces: `_metric_scope_note(metric: str, latest_stats: Mapping[str, Any] | None) -> str | None`
- Produces: `_goal_monitoring_lines(note_content: str | None) -> list[str]`
- Produces: `_action_card_lines(entry, mark, target_day, stats_by_day, *, thin_coverage=False) -> list[str]`
- Produces: `_monitoring_card_lines(entry, target_day, stats_by_day) -> list[str]`
- Produces: `_status_and_all_lines(stats, buckets, actionable, shown, monitoring) -> list[str]`
- Updates: `render_actions_section(...) -> str | None` without changing its public signature

- [ ] **Step 1: 3ブロックと非checkbox monitoringの failing testを書く**

```python
def test_actions_render_action_monitor_goal_as_separate_blocks():
    active, achieved = _active_and_achieved_entries()
    history = _realistic_history()

    out = render_actions_section(
        [active, achieved],
        date(2026, 8, 3),
        note_content="# note without goal marker\n",
        stats_history=history,
    )

    assert out is not None
    assert "## 📌 今日やること（1件）" in out
    assert "- [ ] KZN-20260802-001" in out
    assert "- [ ] KZN-20260727-002" not in out
    assert "## 📈 効果モニタリング（今日やることではない）" in out
    assert "- KZN-20260727-002" in out
    assert "最新: 8/2 3.2 ✅" in out
    assert "直近5日: 4/5達成・未達1日（目標 >= 2.5）" in out
    assert "指標が戻っています" not in out
    assert "閾値超過" not in out
    assert "## 🎯 日次目標" in out
    assert '未設定: `kaizenlog goal "今日達成したい成果"`' in out
```

Production changes that make it pass: renderer見出し、候補/観測分離、structured trajectory、GOAL読み取りサマリ。

- [ ] **Step 2: 3ブロックtestのREDを確認する**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py::test_actions_render_action_monitor_goal_as_separate_blocks -q
```

Expected: 旧見出しまたは旧checkboxのassertでFAILする。

- [ ] **Step 3: action cardの最小rendererを書く**

```python
def _split_action_trigger(body: str) -> tuple[str | None, str]:
    if " → " not in body:
        return None, body
    trigger, action = body.split(" → ", 1)
    return trigger.strip() or None, action.strip()
```

`render_actions_section` は `shown` ごとに次を出す。

```python
def _action_card_lines(
    entry: MemoryEntry,
    mark: str,
    target_day: date,
    stats_by_day: dict[str, Mapping[str, Any]],
    *,
    thin_coverage: bool = False,
) -> list[str]:
    from .verdict import format_action_verdict_tag, parse_pass_condition

    lines = [f"- [{mark}] {entry.id}"]
    trigger, action = _split_action_trigger(humanize_action_body(entry.action))
    if trigger:
        lines.append(f"  - いつ: {trigger}")
    lines.append(f"  - やる: {action}")
    lines.append(
        f"  - 完了条件: 今日の予定分を実施して `kaizenlog done {entry.id}`"
    )
    if effect := format_effect_metric_clause(entry.action):
        lines.append(f"  - 効果目標: {effect}")
    shortfall = _denominator_shortfall_note(entry, stats_by_day)
    if shortfall:
        lines.append(f"  - 測定: 集計待ち（{shortfall}）")
    else:
        lines.append(
            f"  - 測定: "
            f"{format_action_verdict_tag(entry, thin_coverage=thin_coverage)}"
        )
    parsed = parse_pass_condition(entry.action)
    if parsed is not None:
        metric, _op, _target = parsed
        latest_stats = stats_by_day.get(target_day.isoformat())
        if scope := _metric_scope_note(metric, latest_stats):
            lines.append(f"  - 因果の範囲: {scope}")
    return lines
```

`_denominator_shortfall_note` の分母不足本文は必要量を含む次の形へ変える。既存testが要求する `判定不成立`、`稼働45`、`AIセッション0` は残す。

```python
if metric.endswith("_per_hour") and float(mins) < 60:
    return (
        f"判定不成立・稼働{float(mins):g}/必要60分"
        "・分母不足"
    )
if metric.endswith("_per_session") and float(sessions) <= 0:
    return (
        f"判定不成立・AIセッション{float(sessions):g}/必要1件"
        "・分母不足"
    )
```

旧 `今日の実験:` とブロック末尾の単一ID完了導線は削除し、各カードへ完了条件を持たせる。

- [ ] **Step 4: monitoring cardをstructured trajectoryから描画する**

```python
def _monitoring_card_lines(
    entry: MemoryEntry,
    target_day: date,
    stats_by_day: dict[str, Mapping[str, Any]],
) -> list[str]:
    trajectory = _post_verdict_trajectory(entry, target_day, stats_by_day)
    if trajectory is None:
        return []
    latest = trajectory.observations[-1]
    met_count = sum(point.met for point in trajectory.observations)
    total = len(trajectory.observations)
    lines = [f"- {entry.id}"]
    lines.append(
        f"  - 最新: {latest.day.month}/{latest.day.day} {latest.value:g} "
        f"{'✅' if latest.met else '❌'}"
    )
    lines.append(
        f"  - 直近{total}日: {met_count}/{total}達成・未達{total-met_count}日"
        f"（目標 {trajectory.op} {trajectory.target:g}）"
    )
    latest_stats = stats_by_day.get(latest.day.isoformat())
    if scope := _metric_scope_note(trajectory.metric, latest_stats):
        lines.append(f"  - 集計範囲: {scope}")
    if not latest.met:
        lines.append("  - ⚠ 最新観測が目標未達です")
    return lines
```

観測カードは最新ID順で最大2件。trajectoryがない項目と3件目以降は個別表示せず、`ほか N件（kaizenlog today --all）` へ集約する。

- [ ] **Step 5: metric scopeと日次目標の読み取り専用行を実装する**

```python
def _metric_scope_note(
    metric: str,
    latest_stats: Mapping[str, Any] | None,
) -> str | None:
    if metric == "ai_avg_turns":
        ai = latest_stats.get("ai") if latest_stats else None
        sessions = ai.get("sessions") if isinstance(ai, Mapping) else None
        count = f"{int(sessions)}セッション。" if isinstance(sessions, (int, float)) else ""
        return f"全AI {count}特定AIツール単独の効果は判定できません"
    if metric == "context_switches_per_hour":
        return "日全体の観測値。特定の実施区間だけの効果は判定できません"
    return None
```

```python
def _goal_monitoring_lines(note_content: str | None) -> list[str]:
    from .goal import read_goal

    goal = read_goal(note_content)
    lines = ["## 🎯 日次目標", ""]
    if goal is None:
        return lines + ['- 未設定: `kaizenlog goal "今日達成したい成果"`']
    label = re.sub(
        r"^(?:🎯\s*)?今日の目標\s*[:：]\s*", "", goal.raw_line
    ).strip()
    achieved = (
        f"{goal.achieved}%（自己申告）"
        if goal.achieved is not None
        else "未入力"
    )
    return lines + [f"- 目標: {label}", f"  - 達成度: {achieved}"]
```

- [ ] **Step 6: 3ブロックtestをGREENにする**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py::test_actions_render_action_monitor_goal_as_separate_blocks -q
```

Expected: `1 passed`。

- [ ] **Step 7: 分母不足・最新FAIL・goal 3状態の failing testsを書く**

```python
def test_action_keeps_effect_target_when_denominator_is_short():
    out = render_actions_section(
        [_active_entry()],
        date(2026, 8, 3),
        stats_history=[{"day": "2026-08-03", "total_minutes": 22.7, "context_switches": 25}],
    )
    assert "効果目標:" in out
    assert "65 以下" in out
    assert "測定: 集計待ち" in out
    assert "稼働22.7分" in out
    assert "分母不足" in out


def test_monitor_warns_only_when_latest_observation_fails():
    out = render_actions_section(
        [_achieved_entry()],
        date(2026, 8, 3),
        stats_history=_history_with_latest(1.5),
    )
    assert "⚠ 最新観測が目標未達です" in out


@pytest.mark.parametrize(
    ("goal_section", "expected"),
    [
        (None, '未設定: `kaizenlog goal "今日達成したい成果"`'),
        ("🎯 今日の目標: 実装を終える", "達成度: 未入力"),
        ("🎯 今日の目標: 実装を終える\n達成度: 80%（自己申告）", "達成度: 80%（自己申告）"),
    ],
)
def test_goal_monitoring_states(goal_section, expected):
    note = "# day\n"
    if goal_section is not None:
        note = upsert_section(note, GOAL_MARKER, goal_section)
    out = render_actions_section([_active_entry()], date(2026, 8, 3), note)
    assert expected in out
```

Production changes that make them pass: 効果目標と測定理由の独立、latest.met警告、GOAL区間の3状態。

- [ ] **Step 8: edge testsのREDを確認する**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py -q
```

Expected: 新しいedge testsが意図した表示欠損でFAILする。

- [ ] **Step 9: Step 3〜5のguardを接続してedge testsをGREENにする**

`render_actions_section` の末尾は次の順序で組み立てる。

```python
for entry in shown:
    mark = "x" if entry.id in checked_ids else " "
    lines.extend(
        _action_card_lines(
            entry,
            mark,
            target_day,
            stats_by_day,
            thin_coverage=_thin_coverage_for(entry),
        )
    )
for entry in monitoring[:2]:
    lines.extend(_monitoring_card_lines(entry, target_day, stats_by_day))
lines.extend(_goal_monitoring_lines(note_content))
lines.extend(_status_and_all_lines(stats, buckets, actionable, shown, monitoring))
```

既存の週次summary、残件数、`today --all` は次のhelperへ移し、最後に表示する。

```python
def _status_and_all_lines(
    stats: ActionStats,
    buckets: OpenActionBuckets,
    actionable: Sequence[MemoryEntry],
    shown: Sequence[MemoryEntry],
    monitoring: Sequence[MemoryEntry],
) -> list[str]:
    lines = ["## 🗂 状況・全件", ""]
    if stats.proposed > 0 or stats.skipped > 0:
        if stats.done == 0 and stats.proposed > 0:
            summary = (
                f"今週の提案は{stats.proposed}件"
                "（未チェックの実験が残っています）。"
            )
        elif (
            stats.done_rate is not None
            and stats.done_rate < _DOSING_DONE_RATE
            and stats.proposed >= _DOSING_MIN_PROPOSED
        ):
            summary = (
                f"今週は{stats.proposed}件提案し、"
                f"チェック完了は{stats.done}件。"
            )
        else:
            rate = _pct_label(stats.done_rate) if stats.done_rate is not None else "—"
            summary = (
                f"直近{stats.window_days}日は{stats.proposed}件提案し、"
                f"チェック完了は{stats.done}件（完了率 {rate}）。"
            )
        if stats.skipped:
            summary += f"スキップは{stats.skipped}件。"
        if monitoring:
            summary += f"指標達成済みは{len(monitoring)}件。"
        lines.append(summary)

    rest_recent = max(0, len(actionable) - len(shown))
    lines.append(
        f"ほか直近7日の未完了 {rest_recent}件"
        f" / 8〜30日前 {len(buckets.stale)}件"
        f" / 31日以上 {len(buckets.older)}件"
    )
    monitoring_extra = max(0, len(monitoring) - 2)
    if monitoring_extra:
        lines.append(f"ほか効果モニタリング {monitoring_extra}件")
    lines.append("全件表示: `kaizenlog today --all`")
    return lines
```

既存のdosing計算そのものは `compute_action_stats` / `resolve_display_cap` に残し、候補数の意味は変更しない。

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py -q
```

Expected: focused fileが全件PASSする。

- [ ] **Step 10: 旧contract testsを新しい意味契約へ更新する**

`test_round40_review_residuals.py` は、過去点にFAILがあっても最新PASSなら警告なし、最新FAILなら警告ありをassertする。`test_round45_reader_ux.py` は `今日の実験` の代わりに `## 📌 今日やること（1件）` がscoreboardより前にあることをassertする。`test_action_ux_display_cap.py` はトップレベルcheckboxが1件で、そのカード内にID専用完了条件があることをassertする。

```python
assert "⚠ 最新観測が目標未達です" not in out_with_historical_fail
assert "直近5日:" in out_with_historical_fail
assert "⚠ 最新観測が目標未達です" in out_with_latest_fail

assert md.index("## 📌 今日やること（1件）") < md.index("今週の提案")
assert "今日の実験:" not in md

checkbox_rows = [line for line in out.splitlines() if line.startswith("- [ ] KZN-")]
assert len(checkbox_rows) == 1
assert "完了条件: 今日の予定分を実施して `kaizenlog done KZN-" in out
```

- [ ] **Step 11: renderer関連回帰を実行する**

Run:

```powershell
$taskTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-round50-render-" + [guid]::NewGuid())
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py tests/test_round40_review_residuals.py tests/test_round45_reader_ux.py tests/test_action_ux_display_cap.py tests/test_round36_verdict_stage.py tests/test_round44_rehumanize.py --basetemp $taskTemp -q
```

Expected: 全件PASSし、provisional、legacy、checkbox上限の回帰がない。

- [ ] **Step 12: Task 2をcommitする**

```powershell
git add -- src/kaizenlog/memory.py tests/test_round50_action_monitoring_clarity.py tests/test_round40_review_residuals.py tests/test_round45_reader_ux.py tests/test_action_ux_display_cap.py
git diff --cached --check
git commit -m "feat: separate actions from effect monitoring"
```

Expected: rendererと対応testだけのcommitが作成される。

---

### Task 3: `kaizenlog today` を同じ候補契約へ揃える

**Files:**
- Modify: `src/kaizenlog/cli.py:1929-2021`
- Modify: `tests/test_round50_action_monitoring_clarity.py`

**Interfaces:**
- Consumes: `split_action_candidates(entries, checked_ids)` and `resolve_display_cap(stats)`
- Produces: 既定 `cmd_today` はactionableだけを候補表示し、confirmed PASS件数を非候補として通知する
- Preserves: `cmd_today(..., show_all=True)` は全bucketを表示する

- [ ] **Step 1: CLI整合性の failing testを書く**

```python
def test_today_default_excludes_confirmed_pass_but_all_keeps_it(tmp_path, capsys):
    cfg = _config_with_entries(tmp_path, [_active_entry(), _achieved_entry()])
    day = date(2026, 8, 3)

    assert cmd_today(cfg, day, no_sync=True) == 0
    default_out = capsys.readouterr().out
    assert "今日の候補 1件" in default_out
    assert "KZN-20260802-001" in default_out
    assert "KZN-20260727-002" not in default_out
    assert "効果モニタリング 1件" in default_out

    assert cmd_today(cfg, day, no_sync=True, show_all=True) == 0
    all_out = capsys.readouterr().out
    assert "KZN-20260727-002" in all_out
```

Production change that makes it pass: default branchが共通分離helperを使い、`show_all` branchは現状の全件表示を維持する。

- [ ] **Step 2: CLI testのREDを確認する**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py::test_today_default_excludes_confirmed_pass_but_all_keeps_it -q
```

Expected: confirmed PASSが既定候補へ混ざる、またはmonitoring件数がないためFAILする。

- [ ] **Step 3: default候補だけ共通helperへ置き換える**

```python
from .memory import resolve_display_cap, split_action_candidates

actionable, monitoring = split_action_candidates(list(buckets.recent), set())
display_n = min(
    resolve_display_cap(stats),
    TODAY_CANDIDATE_CAP,
    len(actionable),
)
candidates = actionable[:display_n]
rest_recent = max(0, len(actionable) - len(candidates))
if monitoring:
    print(f"効果モニタリング {len(monitoring)}件（今日の候補ではありません）")
```

既定候補0件でもmonitoring件数を表示し、stale/olderの保留件数と混ぜない。`show_all` branchは変更しない。

- [ ] **Step 4: CLI testをGREENにする**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py::test_today_default_excludes_confirmed_pass_but_all_keeps_it -q
```

Expected: `1 passed`。

- [ ] **Step 5: today関連回帰を実行する**

Run:

```powershell
$taskTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-round50-today-" + [guid]::NewGuid())
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py tests/test_ux_round13.py tests/test_action_ux_display_cap.py --basetemp $taskTemp -q
```

Expected: focused、既定cap、`--all`、Memory非変更contractが全件PASSする。

- [ ] **Step 6: Task 3をcommitする**

```powershell
git add -- src/kaizenlog/cli.py tests/test_round50_action_monitoring_clarity.py
git diff --cached --check
git commit -m "fix: align today with actionable candidates"
```

Expected: CLIと対応testだけのcommitが作成される。

---

### Task 4: 実日誌反映、Graph評価、全検証、push

**Files:**
- Modify: `C:\develop\obsidian\2026\01 Daily Notes\2026-08-03.md` (ACTIONS marker内のみ)
- Modify: `.kaizenlog/improvement_graph.json`
- Modify: `PLAN.md`

**Interfaces:**
- Consumes: `load_entries`, `load_stats`, `render_actions_section`, `extract_section`, `ACTIONS_MARKER`
- Produces: 実データで生成された3つの主ブロックと状況導線を持つACTIONS区間
- Produces: CodeChange、Evidence、TestResult nodes and `implements` / `supports` / `evaluated-as` edges

- [ ] **Step 1: 実日誌の更新前fingerprintを保存する**

Run a read-only script that prints:

```python
from hashlib import sha256
from pathlib import Path

path = Path(r"C:\develop\obsidian\2026\01 Daily Notes\2026-08-03.md")
raw = path.read_bytes()
start = raw.index(b"<!-- kaizenlog:actions:start -->")
end_tag = b"<!-- kaizenlog:actions:end -->"
end = raw.index(end_tag, start) + len(end_tag)
print("bytes", sha256(raw).hexdigest())
print("marker_external", sha256(raw[:start] + raw[end:]).hexdigest())
```

Expected: pathが存在し、baseline hashをPLANへ記録できる。現監査値 `d0ecbc8590a83208175d600a4802886ecc93a3f36da50e67c6b32a90520caf3a` と一致しない場合は、ユーザー編集として再監査し、古いsectionを上書きしない。

- [ ] **Step 2: 既存JSONL/statsから純粋renderer出力を作る**

`.venv` のPythonでconfig、Memory、stats、実noteを読み、`render_actions_section` の文字列を標準出力へ出す。`generate`、`advise`、`morning`、`done` は呼ばない。

```powershell
@'
from datetime import date
from pathlib import Path

from kaizenlog.config import load_config
from kaizenlog.memory import ACTIONS_HANDOFF_DAYS, load_entries, render_actions_section
from kaizenlog.stats import load_stats

cfg = load_config("kaizenlog.toml")
day = date(2026, 8, 3)
note_path = Path(r"C:\develop\obsidian\2026\01 Daily Notes\2026-08-03.md")
note = note_path.read_text(encoding="utf-8")
entries = load_entries(cfg.memory_path)
history = load_stats(
    cfg.stats_path,
    days=ACTIONS_HANDOFF_DAYS + 14,
    end_day=day,
)
section = render_actions_section(
    entries,
    day,
    note,
    stats_history=history,
)
assert section is not None
print(section)
'@ | .venv\Scripts\python.exe -
```

Expected output assertions:

```text
## 📌 今日やること（1件）
- [ ] KZN-20260802-001
## 📈 効果モニタリング（今日やることではない）
- KZN-20260727-002
最新: 8/2 3.2 ✅
直近5日: 4/5達成・未達1日（目標 >= 2.5）
## 🎯 日次目標
未設定
```

- [ ] **Step 3: apply_patchでACTIONS marker内だけを置換する**

生成結果をそのまま使い、開始・終了markerとその外側をpatch対象に含めない。PowerShellやPythonの独自upsertでファイル全体を書き直さない。

- [ ] **Step 4: marker外bytesと実sectionを検証する**

Step 1と同じscriptで `marker_external` SHA-256を再計算し、baselineと完全一致させる。`extract_section(content, ACTIONS_MARKER)` で次をassertする。

```python
assert section.count("- [ ] KZN-") == 1
assert "- [ ] KZN-20260802-001" in section
assert "- [ ] KZN-20260727-002" not in section
assert "- KZN-20260727-002" in section
assert "最新: 8/2 3.2 ✅" in section
assert "未設定" in section
```

- [ ] **Step 5: focused/full verificationをfreshに実行する**

```powershell
$focusedTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-round50-focused-" + [guid]::NewGuid())
.venv\Scripts\python.exe -m pytest tests/test_round50_action_monitoring_clarity.py tests/test_round40_review_residuals.py tests/test_round45_reader_ux.py tests/test_action_ux_display_cap.py tests/test_ux_round13.py tests/test_round36_verdict_stage.py tests/test_round44_rehumanize.py --basetemp $focusedTemp -q
```

```powershell
$fullTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-round50-full-" + [guid]::NewGuid())
.venv\Scripts\python.exe -m pytest --basetemp $fullTemp -q
```

```powershell
.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

Expected: focused/full pytest exit 0、compileall exit 0、diff check出力なし。

- [ ] **Step 6: Graphへ実装と評価証拠を追記する**

最低限次を追加する。

```text
C-ACTION-MONITOR-IMPLEMENTATION-001 -implements-> D-ACTION-MONITOR-SPLIT-001
T-ACTION-MONITOR-FOCUSED-001 -supports-> C-ACTION-MONITOR-IMPLEMENTATION-001
T-ACTION-MONITOR-FULL-001 -supports-> C-ACTION-MONITOR-IMPLEMENTATION-001
E-ACTION-MONITOR-REAL-NOTE-001 -supports-> G-ACTION-SEPARATION-001
E-ACTION-MONITOR-MARKER-PRESERVED-001 -supports-> C-ACTION-MONITOR-IMPLEMENTATION-001
```

各node/edgeへsource、step、JST timestampを付け、重複ID、dangling、provenance欠落をPythonで検証する。TestResultには実行command、passed count、exit codeをclaimまたはprovenanceへ保存する。

- [ ] **Step 7: implementation evidenceをcommitする**

```powershell
git add -- .kaizenlog/improvement_graph.json PLAN.md
git diff --cached --check
git commit -m "docs: record action monitoring evidence"
```

実Vaultはrepository外なのでcommit対象外。`.grok/` と `scripts/self_improve_graph.py` はstageしない。

- [ ] **Step 8: commit済みrevisionを再検証する**

全pytest、compileall、Graph validator、実日誌marker外hashをHEADで再実行する。未commit差分が `.grok/` と `scripts/self_improve_graph.py` だけであることを `git status -sb` で確認する。

- [ ] **Step 9: 明示対象だけをpushしremote SHAを確認する**

```powershell
gh --version
gh auth status
git fetch --prune origin
git status -sb
git push origin main
git rev-parse HEAD
git rev-parse origin/main
```

Expected: `HEAD` と `origin/main` が一致する。push前にoriginが進んでいた場合は停止し、fetch後の差分を読み取りで監査してから統合方針を決める。

- [ ] **Step 10: GitHub Actionsを確認し最終結果をGraphへ残す**

```powershell
gh run list --branch main --limit 5
```

対象SHAのrunが開始されたら完了までbounded waitし、結論をTestResultとしてGraphへ追記する。失敗時は今回差分によるものか既存baselineかを分け、同じ失敗へ2回遭遇したらError Recovery Protocolに従って停止・原因候補を調査する。
