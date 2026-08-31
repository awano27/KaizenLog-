"""Reliability vocabulary shared by KaizenLog execution paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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
