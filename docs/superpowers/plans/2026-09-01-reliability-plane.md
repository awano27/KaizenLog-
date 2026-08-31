# KaizenLog Reliability Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent silent multi-day KaizenLog failure by classifying provider capability faults, preserving measurement quality, and recording correlated operational evidence.

**Architecture:** Add a small shared reliability vocabulary, then adapt provider and input collection boundaries so invalid capability states cannot masquerade as model content or measured zero. Add an append-safe SQLite ledger behind the existing JSONL interface and teach `status`/`doctor` to expose actual backend, reason codes, and freshness while retaining legacy readers.

**Tech Stack:** Python 3.11+, dataclasses/enums, stdlib `sqlite3`, `contextvars`, existing `requests`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-reliability-plane-design.md`

## Global Constraints

- Python remains `>=3.11`; runtime dependencies remain limited to the standard library plus the existing `requests` dependency.
- Existing public call sites that expect `generate_text(...) -> str`, `collect_input(...) -> list[dict] | None`, and `load_runs(...) -> list[dict]` remain valid.
- Provider execution is bounded: a non-retryable capability failure receives zero same-backend retries; a daily-contract response receives at most one same-backend repair; a contract failure may start at most one local-fallback pipeline.
- Measurement quality states are exactly `observed`, `missing`, `stale`, `unavailable`, and `unknown`.
- Empty input events are never persisted as numeric zero; observed zero requires at least one target-day watcher event.
- Operational reason codes are stable lowercase snake case and contain no user content.
- SQLite uses WAL mode, `busy_timeout=5000`, parameterized statements, and `PRAGMA user_version`.
- Tests direct SQLite to temporary paths; module import and `Config()` construction never write to the user's profile.
- JSONL v1 rows remain readable; new JSONL rows are sanitized v2 compatibility exports.
- No task logs in to a provider, modifies Scheduled Tasks, writes the live vault, or rewrites historical stats.

---

### Task 1: Reliability vocabulary and provider capability classification

**Files:**
- Create: `src/kaizenlog/reliability.py`
- Modify: `src/kaizenlog/advisor.py:220-540`
- Create: `tests/test_reliability_provider.py`

**Interfaces:**
- Consumes: existing `BackendUnavailable`, `AdvisorError`, and `LLMConfig`.
- Produces: `QualityState`, `FailureReason`, `GenerationAttempt`, `GenerationTrace`; `generate_text(..., trace: GenerationTrace | None = None) -> str`.

- [ ] **Step 1: Write failing tests for success-envelope authentication and attempt tracing**

```python
def test_claude_exit_zero_login_payload_is_non_retryable(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["claude"], 0,
        stdout=json.dumps({
            "is_error": False,
            "subtype": "success",
            "result": "Not logged in · Please run /login",
            "model": "<synthetic>",
        }),
        stderr="",
    )
    monkeypatch.setattr(advisor.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(advisor.subprocess, "run", lambda *a, **k: completed)
    with pytest.raises(BackendUnavailable, match="未認証"):
        advisor._call_claude_code_cli(LLMConfig(), "system", "user")


def test_generate_text_trace_records_actual_fallback_backend(monkeypatch):
    monkeypatch.setattr(advisor, "_call_claude_code_cli",
                        lambda *_: (_ for _ in ()).throw(BackendUnavailable("login")))
    monkeypatch.setattr(advisor, "_call_openai_compatible", lambda *_: "local text")
    trace = GenerationTrace(configured_backend="claude-code-cli")
    cfg = LLMConfig(backend="claude-code-cli", fallback_to_local=True, retries=2)
    sleeps = []
    assert advisor.generate_text(cfg, "s", "u", sleep=sleeps.append, trace=trace) == "local text"
    assert sleeps == []
    assert trace.actual_backend == "openai-compatible"
    assert trace.fallback_used is True
    assert [a.reason for a in trace.attempts] == [
        FailureReason.PROVIDER_AUTH_REQUIRED,
        FailureReason.NONE,
    ]
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `$env:PYTHONDONTWRITEBYTECODE='1'; & 'C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/test_reliability_provider.py`

Expected: collection/import failure because `kaizenlog.reliability` and the `trace` parameter do not exist.

- [ ] **Step 3: Add the reliability types**

```python
class QualityState(str, Enum):
    OBSERVED = "observed"
    MISSING = "missing"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class FailureReason(str, Enum):
    NONE = "none"
    PROVIDER_AUTH_REQUIRED = "provider_auth_required"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_PROBE_TIMEOUT = "provider_probe_timeout"
    PROVIDER_PROBE_UNKNOWN = "provider_probe_unknown"
    CONTRACT_INVALID = "contract_invalid"
    INPUT_BUCKET_MISSING = "input_bucket_missing"
    INPUT_EVENTS_ABSENT = "input_events_absent"
    INPUT_SOURCE_STALE = "input_source_stale"
    LEDGER_WRITE_FAILED = "ledger_write_failed"
    NOTIFICATION_FAILED = "notification_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GenerationAttempt:
    backend: str
    attempt: int
    outcome: str
    reason: FailureReason = FailureReason.NONE


@dataclass
class GenerationTrace:
    configured_backend: str
    actual_backend: str | None = None
    fallback_used: bool = False
    attempts: list[GenerationAttempt] = field(default_factory=list)
```

- [ ] **Step 4: Classify authentication sentinels before returning Claude text**

Add `_claude_auth_failure(text: str, data: dict | None) -> bool` using the exact case-insensitive needles `not logged in`, `/login`, `unauthor`, `oauth`, `401`, and `403`. Call it for non-zero stdout/stderr and for a parsed dict before accepting `result`, regardless of `is_error`.

- [ ] **Step 5: Trace every backend attempt without changing the string return type**

Add a keyword-only `trace` parameter to `generate_text`. Append one `GenerationAttempt` for every call; map `BackendUnavailable` messages containing login/auth/OAuth/401/403 to `PROVIDER_AUTH_REQUIRED`, timeouts to `PROVIDER_TIMEOUT`, other unavailable errors to `PROVIDER_UNAVAILABLE`, and other `AdvisorError` values to `PROVIDER_ERROR`. On success set `actual_backend`; set `fallback_used` when actual differs from configured.

- [ ] **Step 6: Run focused and existing provider tests**

Run: `$env:PYTHONDONTWRITEBYTECODE='1'; & 'C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/test_reliability_provider.py tests/test_bughunt_round2.py tests/test_ops.py tests/test_memory_privacy_skills.py`

Expected: all tests pass with no warnings.

- [ ] **Step 7: Commit**

```powershell
git add src/kaizenlog/reliability.py src/kaizenlog/advisor.py tests/test_reliability_provider.py
git commit -m "fix: classify provider capability failures"
```

---

### Task 2: Bounded contract fallback and actual-backend health

**Files:**
- Modify: `src/kaizenlog/advisor.py:220-260,1009-1190`
- Modify: `src/kaizenlog/cli.py:1920-2050`
- Modify: `src/kaizenlog/runlog.py:140-185`
- Create: `tests/test_reliability_advice.py`
- Modify: `tests/test_round11.py`

**Interfaces:**
- Consumes: `GenerationTrace` and `FailureReason` from Task 1.
- Produces: `AdviceResult.actual_backend`, `AdviceResult.fallback_used`, `AdviceResult.reason_codes`; equivalent metadata on `AdviceContractError`; v2 advice-health backend fields.

- [ ] **Step 1: Write failing tests for the one-pipeline fallback bound**

```python
def test_daily_contract_falls_back_once_after_primary_repair_fails(monkeypatch):
    from tests.test_advice_evidence import CURRENT, VALID_ADVICE_JSON

    calls = []
    primary_bad = "not json"
    evidence = build_advice_evidence(CURRENT)

    def fake_generate(cfg, system, user, **kwargs):
        calls.append(cfg.backend)
        return VALID_ADVICE_JSON if cfg.backend == "openai-compatible" else primary_bad

    monkeypatch.setattr(advisor, "generate_text", fake_generate)
    cfg = LLMConfig(backend="claude-code-cli", fallback_to_local=True,
                    system_prompt="daily_advisor")
    result = advisor.generate_advice(cfg, "today", [], evidence=evidence)
    assert calls == ["claude-code-cli", "claude-code-cli", "openai-compatible"]
    assert result.actual_backend == "openai-compatible"
    assert result.fallback_used is True


def test_advice_health_records_configured_and_actual_backend(tmp_path):
    log_advise_health(
        tmp_path, day="2026-09-01", backend="openai-compatible",
        configured_backend="claude-code-cli", actual_backend="openai-compatible",
        outcome="repaired", duration_seconds=1.2,
        reason_codes=["contract_invalid"],
    )
    row = load_runs(tmp_path)[-1]
    assert row["schema_version"] == 2
    assert row["configured_backend"] == "claude-code-cli"
    assert row["actual_backend"] == "openai-compatible"
    assert row["reason_codes"] == ["contract_invalid"]
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `$env:PYTHONDONTWRITEBYTECODE='1'; & 'C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/test_reliability_advice.py tests/test_round11.py`

Expected: failures for missing result/error metadata and health fields.

- [ ] **Step 3: Carry trace metadata through advice success and failure**

Extend `AdviceResult` with defaults:

```python
actual_backend: str | None = None
fallback_used: bool = False
reason_codes: list[str] = field(default_factory=list)
```

Extend `AdviceContractError.__init__` with the same metadata. Preserve existing constructor call sites by making all new arguments keyword-only with defaults.

- [ ] **Step 4: Add one bounded local pipeline after primary contract failure**

Have `generate_advice` create one `GenerationTrace`. Pass a closure into `_run_daily_pipeline` that calls `generate_text(..., trace=trace)`. When the first pipeline remains invalid and `fallback_to_local` is true and `trace.actual_backend != "openai-compatible"`, run `_run_daily_pipeline` once with `dataclasses.replace(cfg, backend="openai-compatible", fallback_to_local=False, retries=0)`. Do not add a third pipeline. Set reason code `contract_invalid` when this branch is used.

- [ ] **Step 5: Persist actual backend without breaking the legacy field**

Extend `log_advise_health` with optional `configured_backend`, `actual_backend`, and `reason_codes`. Write `schema_version=2`; keep `backend` equal to `actual_backend or configured_backend or backend`. In `_safe_log_advise_health`, pass result/error metadata on success and failure.
Every v2 health row also receives a UUID4 hexadecimal `run_id`; Task 4 later uses the same field as the SQLite primary key and adds parent correlation.

- [ ] **Step 6: Run advice and health regressions**

Run: `$env:PYTHONDONTWRITEBYTECODE='1'; & 'C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/test_reliability_advice.py tests/test_round11.py tests/test_advice_evidence.py tests/test_advice_format.py tests/test_round35_journal_information_design.py`

Expected: all tests pass; the new bound test observes exactly three calls.

- [ ] **Step 7: Commit**

```powershell
git add src/kaizenlog/advisor.py src/kaizenlog/cli.py src/kaizenlog/runlog.py tests/test_reliability_advice.py tests/test_round11.py
git commit -m "feat: bound advice fallback and record actual backend"
```

---

### Task 3: Input measurement quality and freshness-aware doctor

**Files:**
- Modify: `src/kaizenlog/collector.py:1-115,319-326`
- Modify: `src/kaizenlog/cli.py:250-275`
- Modify: `src/kaizenlog/stats.py:45-310`
- Modify: `src/kaizenlog/doctor.py:85-125`
- Create: `tests/test_input_quality.py`
- Modify: `tests/test_web_input.py`

**Interfaces:**
- Consumes: `QualityState` and input reason codes from Task 1.
- Produces: `InputObservation`, `collect_input_observation(...)`, `classify_input_bucket_health(...)`, and optional `source_quality.input` stats metadata.

- [ ] **Step 1: Write failing tests that distinguish empty, observed zero, and stale**

```python
def test_empty_input_bucket_is_unavailable_not_zero(fake_client, day_bounds):
    fake_client.bucket_ids = ["input-host"]
    fake_client.raw = []
    obs = collect_input_observation(fake_client, *day_bounds)
    assert obs.state is QualityState.UNAVAILABLE
    assert obs.reason is FailureReason.INPUT_EVENTS_ABSENT
    assert collect_input(fake_client, *day_bounds) is None


def test_zero_heartbeat_is_observed_zero(fake_client, day_bounds):
    fake_client.bucket_ids = ["input-host"]
    fake_client.raw = [{
        "timestamp": day_bounds[0].isoformat(), "duration": 5,
        "data": {"presses": 0, "clicks": 0, "deltaX": 0, "deltaY": 0},
    }]
    obs = collect_input_observation(fake_client, *day_bounds)
    assert obs.state is QualityState.OBSERVED
    stats = compute_input_stats(obs.events, day_start=day_bounds[0], day_end=day_bounds[1])
    assert stats.keypresses == 0


def test_input_bucket_older_than_26_hours_is_stale(now):
    buckets = {"input-host": {"type": "os.hid.input", "last_updated": (now - timedelta(hours=27)).isoformat()}}
    state, reason, _ = classify_input_bucket_health(buckets, now=now)
    assert state is QualityState.STALE
    assert reason is FailureReason.INPUT_SOURCE_STALE
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `$env:PYTHONDONTWRITEBYTECODE='1'; & 'C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/test_input_quality.py`

Expected: import failures for `InputObservation` and the two new functions.

- [ ] **Step 3: Implement the typed observation boundary**

```python
@dataclass(frozen=True)
class InputObservation:
    state: QualityState
    events: list[dict]
    bucket_id: str | None
    reason: FailureReason
    last_event_at: str | None = None
```

`collect_input_observation` returns `missing` when no bucket exists, `unavailable` when the selected bucket has no target-day events, and `observed` otherwise. Preserve `collect_input` as a compatibility wrapper returning `obs.events` only for `observed`.

- [ ] **Step 4: Persist quality separately and numeric values only when observed**

Add `input_quality: Mapping[str, Any] | None = None` to `build_stats` and `write_stats`. Store only:

```python
stats.setdefault("source_quality", {})["input"] = {
    "state": str(input_quality["state"]),
    "reason": str(input_quality["reason"]),
    "bucket_id": str(input_quality.get("bucket_id") or ""),
    "last_event_at": input_quality.get("last_event_at"),
}
```

In `cmd_generate`, compute `InputStats` only for `QualityState.OBSERVED`; pass quality metadata to `write_stats`. Do not create `stats["input"]` for missing/unavailable/stale/unknown states.

- [ ] **Step 5: Make doctor validate freshness, not mere bucket existence**

Implement `classify_input_bucket_health(buckets, now, stale_after=timedelta(hours=26))`. Parse `last_updated` with `Z` support. In `_check_activitywatch`, print OK only for `observed`, warn for `stale`, `unknown`, or `missing`, and include the stable reason code in parentheses.

- [ ] **Step 6: Run measurement and consumer regressions**

Run: `$env:PYTHONDONTWRITEBYTECODE='1'; & 'C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/test_input_quality.py tests/test_web_input.py tests/test_round10.py tests/test_round22_doctor_hygiene.py tests/test_advice_evidence.py tests/test_experiments.py tests/test_verdict.py`

Expected: all selected tests pass; missing numeric paths still resolve to `None` in existing consumers.

- [ ] **Step 7: Commit**

```powershell
git add src/kaizenlog/collector.py src/kaizenlog/cli.py src/kaizenlog/stats.py src/kaizenlog/doctor.py tests/test_input_quality.py tests/test_web_input.py
git commit -m "fix: preserve input measurement quality"
```

---

### Task 4: Append-safe operational ledger and correlated status

**Files:**
- Create: `src/kaizenlog/ops_ledger.py`
- Modify: `src/kaizenlog/config.py:220-280,516-610`
- Modify: `src/kaizenlog/runlog.py:1-188,252-325`
- Modify: `src/kaizenlog/cli.py:200-230,2026-2050,4319-4365,4565-4635`
- Modify: `src/kaizenlog/doctor.py:170-220,540-590`
- Create: `tests/test_ops_ledger.py`
- Modify: `tests/test_ops.py`
- Modify: `tests/test_round11.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: v2 JSONL entries and reason/backend/quality fields from Tasks 1-3.
- Produces: `default_ops_db_path()`, `OpsLedger.append(entry)`, `OpsLedger.load_runs()`, `new_run_id()`, `bind_run(run_id)`, `current_run_id()`, `load_operational_runs(cfg)`.

- [ ] **Step 1: Write failing schema, concurrency, and legacy-fallback tests**

```python
def test_ops_ledger_initializes_v1_schema(tmp_path):
    ledger = OpsLedger(tmp_path / "ops.sqlite3")
    ledger.append({"schema_version": 2, "run_id": "run-1", "ts": "2026-09-01T00:00:00+00:00",
                   "command": "run", "ok": True, "duration_seconds": 1.0})
    assert ledger.load_runs()[0]["run_id"] == "run-1"
    with sqlite3.connect(tmp_path / "ops.sqlite3") as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_concurrent_appends_do_not_drop_rows(tmp_path):
    path = tmp_path / "ops.sqlite3"
    def write(i):
        OpsLedger(path).append({"schema_version": 2, "run_id": f"run-{i}",
                                "ts": f"2026-09-01T00:00:{i:02d}+00:00",
                                "command": "run", "ok": True, "duration_seconds": 0.1})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))
    assert len(OpsLedger(path).load_runs()) == 40


def test_load_operational_runs_falls_back_to_legacy_jsonl(tmp_path):
    log_run(tmp_path, "run", ok=True, duration_seconds=1.0)
    cfg = Config(vault_dir=tmp_path, logs_dir=".", ops_db_path=tmp_path / "missing.sqlite3")
    assert load_operational_runs(cfg)[-1]["command"] == "run"
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `$env:PYTHONDONTWRITEBYTECODE='1'; & 'C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/test_ops_ledger.py`

Expected: import failures because the ledger module and config field do not exist.

- [ ] **Step 3: Implement the SQLite ledger with one normalized JSON payload column**

Create table `runs` with explicit indexed columns plus sanitized JSON:

```sql
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    ts TEXT NOT NULL,
    command TEXT NOT NULL,
    ok INTEGER NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL,
    configured_backend TEXT,
    actual_backend TEXT,
    outcome TEXT,
    reason_code TEXT,
    notify_failed INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_command_ts ON runs(command, ts);
CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id);
```

On every connection execute `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, and `PRAGMA user_version=1`. Use `INSERT OR REPLACE` with parameters. `load_runs` decodes `payload_json` in timestamp order.

- [ ] **Step 4: Add configuration without profile writes in tests**

Add `ops_db_path: Path | None = None` to `Config`. `load_config` sets it to `default_ops_db_path()` only when a configuration file was actually found, unless `[general].ops_db_path` explicitly supplies a path. Add an `operational_db_path` property returning the configured path.

- [ ] **Step 5: Dual-write sanitized v2 entries and correlate phases**

Add `run_id`, `parent_run_id`, backend/reason/quality fields, and optional `ops_db_path` to `log_run` and `log_advise_health`. Generate UUID4 hex IDs when absent; reuse `current_run_id()` as `parent_run_id` for advice-health rows. JSONL is written first. Catch SQLite exceptions and return `FailureReason.LEDGER_WRITE_FAILED` while preserving JSONL and command outcome.

In the top-level `generate`/`advise`/`run` execution block, bind one run ID before work and pass it to the terminal `log_run`. Configure all `_safe_log_*` helpers with `cfg.operational_db_path`.

- [ ] **Step 6: Prefer the ledger for status and add value-age fields**

Implement `load_operational_runs(cfg)` as SQLite-if-present/nonempty, otherwise JSONL. Extend `render_status` so v2 rows show `configured_backend → actual_backend`, `reason_code`, parent correlation, and `source_quality.input.state/last_event_at`. Show the elapsed days since the last `advise_health` outcome `ok` or `repaired`; use `不明` when no timestamp is parseable.

- [ ] **Step 7: Add read-only Claude auth probe to doctor**

For `claude-code-cli`, after executable discovery invoke `[path, "auth", "status", "--json"]` with `capture_output=True`, UTF-8, and `timeout=10`. Parse only `loggedIn`; do not print raw output. Map false/exit 1 to `provider_auth_required`, timeout to `provider_probe_timeout`, invalid JSON to `provider_probe_unknown`. Keep the existing local-fallback probe.

- [ ] **Step 8: Document the operational boundary and recovery commands**

Add a README troubleshooting subsection stating that `status` uses the machine-local ledger, the vault JSONL is a compatibility export, `doctor` checks Claude authentication and input freshness, and live repair commands remain manual (`claude` then `/login`; restart `aw-watcher-input`). Do not claim Scheduled Tasks are automatically repaired.

- [ ] **Step 9: Run focused and full regression suites**

Focused run: `$env:PYTHONDONTWRITEBYTECODE='1'; & 'C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/test_ops_ledger.py tests/test_ops.py tests/test_round11.py tests/test_round22_doctor_hygiene.py tests/test_readme_contract.py`

Full run: `$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('kaizenlog-reliability-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $testRoot | Out-Null; $env:PYTHONDONTWRITEBYTECODE='1'; & 'C:\develop\KaizenLog\KaizenLog-\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider --basetemp $testRoot`

Expected: focused and full suites pass with no warnings and no profile/vault writes.

- [ ] **Step 10: Commit**

```powershell
git add src/kaizenlog/ops_ledger.py src/kaizenlog/config.py src/kaizenlog/runlog.py src/kaizenlog/cli.py src/kaizenlog/doctor.py tests/test_ops_ledger.py tests/test_ops.py tests/test_round11.py README.md
git commit -m "feat: add correlated operational ledger"
```
