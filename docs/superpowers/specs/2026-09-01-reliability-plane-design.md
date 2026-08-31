# KaizenLog Reliability Plane Design

## Problem statement

KaizenLog's value loop can stop for weeks while the existing health surface still reports that the required tools and buckets exist. The immediate incident combined three distinct failures:

- Claude Code returned a successful JSON envelope whose payload was actually `Not logged in`; the text entered the advice-contract repair path instead of being classified as a non-retryable authentication failure.
- The ActivityWatch input bucket still existed but contained no events after its watcher stopped; an empty event list was persisted as a measured zero and could drive false PASS/FAIL decisions.
- JSONL records lacked a stable run identifier, actual backend, reason code, source freshness, and an append-safe canonical store, so scheduler, provider, repair, fallback, and notification failures could not be correlated.

## Goals

1. Classify provider capability failures before model text reaches content validation.
2. Fall back once, with bounded attempts, when the configured provider is unavailable or repeatedly violates the advice contract.
3. Preserve the distinction between an observed numeric zero and missing, stale, unavailable, or unknown input evidence.
4. Record correlated, privacy-safe operational events in a machine-local SQLite ledger while keeping the existing JSONL reader compatible.
5. Make `status` and `doctor` show configured backend, actual backend, failure reason, source freshness, and the age of the last successful value-producing run.

## Non-goals

- This change does not log in to Claude Code, modify Windows Scheduled Tasks, enable Task Scheduler Operational logging, repair the live Obsidian vault, or rewrite historical stats.
- This change does not store prompts, model responses, window titles, notification bodies, credentials, or ActivityWatch event payloads in the operational ledger.
- This change does not replace the existing advice JSON contract or alter action-card semantics.

## Global constraints

- Python remains `>=3.11`; runtime dependencies remain limited to the standard library plus the existing `requests` dependency.
- Existing public call sites that expect `generate_text(...) -> str`, `collect_input(...) -> list[dict] | None`, and `load_runs(...) -> list[dict]` remain valid.
- Provider execution is bounded: a non-retryable capability failure receives zero same-backend retries; a daily-contract response receives at most one same-backend repair; a contract failure may start at most one local-fallback pipeline.
- A Claude JSON envelope containing an authentication sentinel such as `Not logged in`, `/login`, `unauthorized`, OAuth failure, HTTP 401, or HTTP 403 is a capability failure even when `returncode == 0` and `is_error == false`.
- Empty input events are never persisted as numeric zero. Numeric zero is observed only when at least one input watcher event exists for the target day and its measured counters are zero.
- Measurement quality states are exactly `observed`, `missing`, `stale`, `unavailable`, and `unknown`.
- Operational reason codes are stable lowercase snake case and contain no user content.
- SQLite uses WAL mode, `busy_timeout=5000`, parameterized statements, and a schema version recorded in `PRAGMA user_version`.
- The canonical database path for a file-backed user configuration is `%LOCALAPPDATA%\kaizenlog\ops.sqlite3` on Windows and `$XDG_STATE_HOME/kaizenlog/ops.sqlite3` (or `~/.local/state/kaizenlog/ops.sqlite3`) elsewhere.
- Tests must direct the ledger to a temporary path; importing modules or constructing an in-memory `Config()` must not write to the user's profile.
- JSONL remains a sanitized compatibility export. New rows carry `schema_version=2` and `run_id`; old rows without those keys remain readable.
- If SQLite is unavailable, the command still writes JSONL and reports ledger degradation without losing the primary command outcome.
- All external capability probes are read-only, time-bounded, and redact command output to stable reason codes.

## Architecture

### Reliability vocabulary

`kaizenlog.reliability` owns shared enums and trace records:

- `QualityState`: the five measurement states.
- `FailureReason`: stable provider, measurement, ledger, scheduler, and notification reason codes.
- `GenerationAttempt`: backend, attempt number, outcome, and reason code.
- `GenerationTrace`: configured backend, actual backend, fallback flag, and ordered attempts.

No domain module stores free-form payloads in these records.

### Provider recovery

The backend adapter remains responsible for transport/envelope classification. `generate_text` keeps returning `str`, but accepts an optional trace object and records every attempt. `generate_advice` owns content-level recovery:

1. Run the configured pipeline.
2. Repair the configured backend's content once if contract validation fails.
3. If it still fails and local fallback is enabled and was not already used, run one fresh `openai-compatible` pipeline.
4. Return `AdviceResult` with actual backend and trace metadata, or raise `AdviceContractError` carrying the same metadata.

`advise_health` stores both `configured_backend` and `actual_backend`; legacy `backend` remains populated with the actual backend when known, otherwise the configured backend.

### Measurement quality

`collect_input_observation` returns an `InputObservation` rather than collapsing all empty cases:

- no bucket: `missing`, reason `input_bucket_missing`;
- bucket exists but target-day query returns no events: `unavailable`, reason `input_events_absent`;
- events exist: `observed`, including a genuine all-zero heartbeat day.

The existing `collect_input` wrapper returns events only for `observed` and `None` otherwise. `cmd_generate` persists numeric input metrics only for `observed`. A sibling `source_quality.input` object stores state, reason code, bucket identifier, and last event time; it never stores raw events.

`doctor` inspects bucket `last_updated`. A bucket older than 26 hours is `stale`; an unparseable timestamp is `unknown`; recent metadata is `observed`. The 26-hour threshold tolerates the nightly schedule while detecting a stopped daily watcher.

### Operational ledger

`kaizenlog.ops_ledger` owns SQLite initialization and append/read operations. The first schema contains:

- `runs`: one row per command or phase, keyed by `run_id`, with optional `parent_run_id`;
- timestamps, command/phase, success/partial flags, duration;
- configured/actual backend, outcome, reason code;
- measurement-quality JSON containing only state/reason/timestamp metadata;
- notification failure and a redacted error summary capped at 500 characters.

`runlog.log_run` and `runlog.log_advise_health` continue to write JSONL and optionally dual-write the same sanitized entry to SQLite. SQLite failure is returned as a stable degradation signal and never replaces the command's own failure.

### Status and doctor

`status` prefers SQLite v2 rows when the configured ledger exists and otherwise uses JSONL. It displays:

- last run and last success per command;
- configured backend → actual backend;
- reason code and failure streak;
- input source state and freshness;
- age of the last successful `advise_health` outcome.

`doctor` performs read-only provider and input capability probes. For Claude Code, `claude auth status --json` is invoked with a short timeout and mapped to `provider_auth_required`, `provider_probe_timeout`, or healthy without printing tokens or raw output.

## Rollout and rollback

The ledger is additive and dual-written. Existing JSONL files and stats remain valid, so rollback is deleting or disabling the SQLite path and returning status reads to JSONL. No database migration rewrites user content. Live Claude login, scheduled-task repair, and any correction of historical input-derived verdicts require a separate explicit operational gate after code verification.

## Acceptance criteria

- Synthetic Claude login text with exit code 0 is rejected before advice parsing and falls back without same-backend sleep/retry.
- Two invalid primary responses can trigger one local pipeline; no execution path exceeds the documented bound.
- Health records identify the backend that actually produced the accepted or rejected content.
- A missing or empty target-day input source produces no numeric input metrics and cannot satisfy numeric PASS/FAIL checks.
- A real all-zero heartbeat event remains an observed zero.
- Concurrent ledger appends produce distinct correlated rows without truncating existing data.
- Legacy JSONL fixtures still load and render.
- Full test suite passes with no writes outside the isolated worktree and OS temporary directories.
