"""Input measurement quality is distinct from a valid numeric zero."""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kaizenlog.config import Config
from kaizenlog.collector import (
    classify_input_bucket_health,
    collect_input,
    collect_input_observation,
)
from kaizenlog.focus import compute_input_stats
from kaizenlog.reliability import FailureReason, QualityState
from kaizenlog.report import DailySummary
from kaizenlog.stats import build_stats


class FakeInputClient:
    def __init__(self) -> None:
        self.bucket_ids: list[str] = []
        self.raw: list[dict] = []

    def find_buckets(self, bucket_type: str) -> list[str]:
        assert bucket_type == "os.hid.input"
        return self.bucket_ids

    def events(self, bucket_id: str, start: datetime, end: datetime) -> list[dict]:
        assert bucket_id in self.bucket_ids
        return self.raw


@pytest.fixture
def fake_client() -> FakeInputClient:
    return FakeInputClient()


@pytest.fixture
def day_bounds() -> tuple[datetime, datetime]:
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def test_missing_input_bucket_returns_missing_and_compatibility_none(
    fake_client: FakeInputClient, day_bounds: tuple[datetime, datetime]
) -> None:
    """A watcher that is not installed must remain unmeasurable, not zero."""
    observation = collect_input_observation(fake_client, *day_bounds)

    assert observation.state is QualityState.MISSING
    assert observation.reason is FailureReason.INPUT_BUCKET_MISSING
    assert collect_input(fake_client, *day_bounds) is None


def test_empty_input_bucket_is_unavailable_not_zero(
    fake_client: FakeInputClient, day_bounds: tuple[datetime, datetime]
) -> None:
    """A selected input bucket with no events has no numeric measurement."""
    fake_client.bucket_ids = ["input-host"]

    observation = collect_input_observation(fake_client, *day_bounds)

    assert observation.state is QualityState.UNAVAILABLE
    assert observation.reason is FailureReason.INPUT_EVENTS_ABSENT
    assert collect_input(fake_client, *day_bounds) is None


def test_zero_heartbeat_is_observed_zero(
    fake_client: FakeInputClient, day_bounds: tuple[datetime, datetime]
) -> None:
    """Removing the event itself, not its all-zero payload, is the unavailable branch."""
    fake_client.bucket_ids = ["input-host"]
    fake_client.raw = [{
        "timestamp": day_bounds[0].isoformat(),
        "duration": 5,
        "data": {"presses": 0, "clicks": 0, "deltaX": 0, "deltaY": 0},
    }]

    observation = collect_input_observation(fake_client, *day_bounds)
    stats = compute_input_stats(
        observation.events, day_start=day_bounds[0], day_end=day_bounds[1]
    )

    assert observation.state is QualityState.OBSERVED
    assert observation.reason is FailureReason.NONE
    assert collect_input(fake_client, *day_bounds) == fake_client.raw
    assert stats.keypresses == 0


def test_input_bucket_older_than_26_hours_is_stale(now: datetime) -> None:
    """Freshness must depend on the bucket timestamp, not type existence."""
    buckets = {
        "input-host": {
            "type": "os.hid.input",
            "last_updated": (now - timedelta(hours=27)).isoformat(),
        }
    }

    state, reason, bucket_id = classify_input_bucket_health(buckets, now=now)

    assert state is QualityState.STALE
    assert reason is FailureReason.INPUT_SOURCE_STALE
    assert bucket_id == "input-host"


def _summary(day: date) -> DailySummary:
    return DailySummary(
        day=day,
        total_minutes=0.0,
        by_category={},
        by_app={},
        blocks=[],
        ai_tool_minutes={},
        ai_sessions=0,
        context_switches=0,
    )


def test_stats_persist_input_quality_without_creating_missing_numeric_input() -> None:
    """Unavailable input metadata must never become a fabricated zero record."""
    stats = build_stats(
        date(2026, 9, 1),
        _summary(date(2026, 9, 1)),
        [],
        input_quality={
            "state": QualityState.UNAVAILABLE,
            "reason": FailureReason.INPUT_EVENTS_ABSENT,
            "bucket_id": "input-host",
            "last_event_at": None,
            "events": [{"data": {"presses": 99}}],
        },
    )

    assert "input" not in stats
    assert stats["source_quality"]["input"] == {
        "state": "unavailable",
        "reason": "input_events_absent",
        "bucket_id": "input-host",
        "last_event_at": None,
    }
    assert "events" not in stats["source_quality"]["input"]

    canonical = build_stats(
        date(2026, 9, 1),
        _summary(date(2026, 9, 1)),
        [],
        input_quality={
            "state": "missing",
            "reason": "input_bucket_missing",
        },
    )
    assert canonical["source_quality"]["input"] == {
        "state": "missing",
        "reason": "input_bucket_missing",
        "bucket_id": "",
        "last_event_at": None,
    }


def test_doctor_warns_with_stable_reason_for_stale_input_bucket(
    monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    """A stale watcher must not receive the installed-and-healthy OK message."""
    from kaizenlog import doctor

    buckets = {
        "input-host": {
            "type": "os.hid.input",
            "last_updated": (now - timedelta(hours=27)).isoformat(),
        }
    }
    monkeypatch.setattr(doctor, "datetime", SimpleNamespace(now=lambda tz=None: now))
    monkeypatch.setattr(
        doctor.requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: buckets,
        ),
    )

    check = doctor.Check()
    doctor._check_activitywatch(check, SimpleNamespace(aw_base_url="http://aw"))

    assert any("⚠️" in line and "input_source_stale" in line for line in check.lines)
    assert not any("✅ 入力watcher検出" in line for line in check.lines)


def test_generate_omits_unavailable_input_but_records_quality(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generate must pass only observed input through to numeric stats consumers."""
    from kaizenlog import cli

    vault = tmp_path / "vault"
    (vault / "01 Daily Notes").mkdir(parents=True)
    (vault / "Kaizen" / "Memory").mkdir(parents=True)
    (vault / ".kaizenlog" / "stats").mkdir(parents=True)
    (vault / "03 Areas" / "Kaizen Experiments").mkdir(parents=True)
    day = date(2026, 9, 1)
    cfg = Config(
        vault_dir=vault,
        timezone="UTC",
        daily_notes_dir="01 Daily Notes",
        experiments_dir="03 Areas/Kaizen Experiments",
        stats_dir=".kaizenlog/stats",
        memory_dir="Kaizen/Memory",
        logs_dir=".kaizenlog/logs",
    )
    cfg.aiwork.enabled = False
    observation = SimpleNamespace(
        state=QualityState.UNAVAILABLE,
        events=[],
        bucket_id="input-host",
        reason=FailureReason.INPUT_EVENTS_ABSENT,
        last_event_at=None,
    )
    monkeypatch.setattr(cli, "collect_day", lambda *args: ([], True))
    monkeypatch.setattr(cli, "collect_input_observation", lambda *args: observation)
    monkeypatch.setattr(cli.Classifier, "classify_all", lambda self, events: [])
    monkeypatch.setattr(cli, "summarize", lambda *args, **kwargs: _summary(day))
    monkeypatch.setattr(cli, "render_markdown", lambda *args, **kwargs: "log")
    monkeypatch.setattr(cli, "ActivityWatchClient", lambda url: SimpleNamespace())

    cli.cmd_generate(cfg, day)

    stats = json.loads((cfg.stats_path / f"{day.isoformat()}.json").read_text("utf-8"))
    assert "input" not in stats
    assert stats["source_quality"]["input"]["state"] == "unavailable"
    assert stats["source_quality"]["input"]["reason"] == "input_events_absent"
