from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kaizenlog.config import Config
from kaizenlog.file_lock import file_lock, lock_path_for
import kaizenlog.human_input as human_input
from kaizenlog.human_input import (
    HumanOperation,
    apply_human_operation,
    load_human_day,
    stable_target_key,
)


DAY = date(2026, 10, 15)


def _cfg(tmp_path: Path) -> Config:
    return Config(vault_dir=tmp_path, memory_dir="Memory")


def _op(
    operation_id: str = "op-1",
    *,
    kind: str = "outcome",
    payload: dict[str, object] | None = None,
    target_key: str | None = None,
    cancel_operation_id: str | None = None,
    target_day: date = DAY,
) -> HumanOperation:
    if payload is None:
        payload = {"text": "成果を記録"} if kind == "outcome" else {
            "text": "次に試す",
            "source": "free_text",
            "source_id": None,
        }
    return HumanOperation(
        operation_id=operation_id,
        target_day=target_day,
        kind=kind,
        payload=payload,
        target_key=target_key,
        cancel_operation_id=cancel_operation_id,
    )


def _write_stats(cfg: Config, *, app: str = "Code.exe", title: str = "main.py") -> str:
    path = cfg.stats_path / f"{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True)
    start = "2026-10-15T00:00:00+00:00"
    end = "2026-10-15T01:00:00+00:00"
    path.write_text(
        json.dumps(
            {
                "day": DAY.isoformat(),
                "blocks": [{
                    "start": start,
                    "end": end,
                    "app": app,
                    "title": title,
                    "category": "開発",
                    "minutes": 60,
                }],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return stable_target_key(
        datetime.fromisoformat(start), datetime.fromisoformat(end), app, title
    )


def test_load_missing_is_read_only(tmp_path: Path):
    cfg = _cfg(tmp_path)
    path = tmp_path / ".kaizenlog" / "human" / f"{DAY.isoformat()}.json"
    loaded = load_human_day(cfg, DAY)
    assert loaded.status == "missing"
    assert loaded.revision == 0
    assert loaded.history == ()
    assert not path.exists()
    assert not path.parent.exists()


def test_apply_increments_revision(tmp_path: Path):
    result = apply_human_operation(_cfg(tmp_path), DAY, _op(), expected_revision=0)
    assert result.status == "saved"
    assert result.revision == 1
    assert load_human_day(_cfg(tmp_path), DAY).revision == 1


def test_duplicate_operation_is_idempotent(tmp_path: Path):
    cfg = _cfg(tmp_path)
    op = _op()
    apply_human_operation(cfg, DAY, op, expected_revision=0)
    duplicate = apply_human_operation(cfg, DAY, op, expected_revision=0)
    loaded = load_human_day(cfg, DAY)
    assert duplicate.status == "idempotent"
    assert loaded.revision == 1
    assert len(loaded.history) == 1


def test_same_id_different_payload_conflicts(tmp_path: Path):
    cfg = _cfg(tmp_path)
    apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    result = apply_human_operation(
        cfg, DAY, _op(payload={"text": "別の成果"}), expected_revision=0
    )
    assert result.status == "conflict"
    assert load_human_day(cfg, DAY).revision == 1


def test_stale_revision_does_not_overwrite(tmp_path: Path):
    cfg = _cfg(tmp_path)
    apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    result = apply_human_operation(cfg, DAY, _op("op-2"), expected_revision=0)
    assert result.status == "stale"
    assert load_human_day(cfg, DAY).revision == 1


def test_cancel_is_history_only(tmp_path: Path):
    cfg = _cfg(tmp_path)
    original = _op()
    apply_human_operation(cfg, DAY, original, expected_revision=0)
    cancel = _op(
        "cancel-1", kind="cancel", payload={}, cancel_operation_id=original.operation_id
    )
    result = apply_human_operation(cfg, DAY, cancel, expected_revision=1)
    loaded = load_human_day(cfg, DAY)
    assert result.status == "saved"
    assert len(loaded.history) == 2
    assert loaded.history[0].active is False
    assert loaded.history[1].active is True


def test_correction_key_mismatch_is_stale(tmp_path: Path):
    cfg = _cfg(tmp_path)
    key = _write_stats(cfg)
    op = _op(
        kind="classification_correction",
        payload={"category": "集中"},
        target_key="0" * 64,
    )
    result = apply_human_operation(cfg, DAY, op, expected_revision=0)
    assert result.status == "stale"
    assert load_human_day(cfg, DAY).status == "missing"
    assert key != op.target_key


def test_next_action_source_kzn_requires_unique_memory_id(tmp_path: Path):
    cfg = _cfg(tmp_path)
    op = _op(
        kind="next_action",
        payload={"text": "既存作業", "source": "kzn", "source_id": "KZN-20261015-001"},
    )
    result = apply_human_operation(cfg, DAY, op, expected_revision=0)
    assert result.status == "invalid"
    assert not (tmp_path / ".kaizenlog" / "human").exists()


def test_free_text_next_action_has_no_source_id(tmp_path: Path):
    cfg = _cfg(tmp_path)
    op = _op(kind="next_action")
    result = apply_human_operation(cfg, DAY, op, expected_revision=0)
    assert result.status == "saved"
    assert load_human_day(cfg, DAY).history[0].operation.payload["source_id"] is None


@pytest.mark.parametrize("text", ["a\nb", "a\rb", "a\x00b"])
def test_rejects_multiline_or_nul_text(tmp_path: Path, text: str):
    result = apply_human_operation(
        _cfg(tmp_path), DAY, _op(payload={"text": text}), expected_revision=0
    )
    assert result.status == "invalid"
    assert not (tmp_path / ".kaizenlog" / "human").exists()


def test_cancel_cannot_target_cancel(tmp_path: Path):
    cfg = _cfg(tmp_path)
    original = _op()
    apply_human_operation(cfg, DAY, original, expected_revision=0)
    cancel = _op(
        "cancel-1", kind="cancel", payload={}, cancel_operation_id=original.operation_id
    )
    apply_human_operation(cfg, DAY, cancel, expected_revision=1)
    second = _op(
        "cancel-2", kind="cancel", payload={}, cancel_operation_id=cancel.operation_id
    )
    result = apply_human_operation(cfg, DAY, second, expected_revision=2)
    assert result.status == "conflict"
    assert load_human_day(cfg, DAY).revision == 2


def test_fixed_timezone_equivalence_and_changes_are_stale(tmp_path: Path):
    cfg = _cfg(tmp_path)
    start_utc = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(hours=1)
    key = _write_stats(cfg)
    assert key == stable_target_key(
        start_utc.astimezone(timezone(timedelta(hours=9))),
        end_utc.astimezone(timezone(timedelta(hours=9))),
        "Code.exe",
        "main.py",
    )
    op = _op(
        kind="classification_correction",
        payload={"project": "P"},
        target_key=key,
    )
    assert apply_human_operation(cfg, DAY, op, expected_revision=0).status == "saved"

    bad = _op(
        "op-2",
        kind="classification_correction",
        payload={"category": "集中"},
        target_key=stable_target_key(start_utc, end_utc, "Other.exe", "main.py"),
    )
    assert apply_human_operation(cfg, DAY, bad, expected_revision=1).status == "stale"


def test_naive_target_key_is_rejected():
    with pytest.raises(ValueError):
        stable_target_key(
            datetime(2026, 10, 15),
            datetime(2026, 10, 15, 1),
            "Code.exe",
            "main.py",
        )


@pytest.mark.parametrize(
    "end",
    [
        datetime(2026, 10, 15, tzinfo=timezone.utc),
        datetime(2026, 10, 14, 23, tzinfo=timezone.utc),
    ],
)
def test_stable_target_key_rejects_non_positive_aware_interval(end: datetime):
    with pytest.raises(ValueError):
        stable_target_key(
            datetime(2026, 10, 15, tzinfo=timezone.utc),
            end,
            "Code.exe",
            "main.py",
        )


@pytest.mark.parametrize(
    "end",
    [
        "2026-10-15T00:00:00+00:00",
        "2026-10-14T23:00:00+00:00",
    ],
)
def test_correction_for_contradictory_saved_block_is_stale_without_human_write(
    tmp_path: Path, end: str
):
    cfg = _cfg(tmp_path)
    key = _write_stats(cfg)
    stats_path = cfg.stats_path / f"{DAY.isoformat()}.json"
    source = json.loads(stats_path.read_text(encoding="utf-8"))
    source["blocks"][0]["end"] = end
    stats_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

    operation = _op(
        kind="classification_correction",
        payload={"category": "集中"},
        target_key=key,
    )
    result = apply_human_operation(cfg, DAY, operation, expected_revision=0)

    assert result.status == "stale"
    assert load_human_day(cfg, DAY).status == "missing"
    assert not (tmp_path / ".kaizenlog" / "human" / f"{DAY.isoformat()}.json").exists()


def test_schema_version_true_is_invalid(tmp_path: Path):
    cfg = _cfg(tmp_path)
    path = tmp_path / ".kaizenlog" / "human" / f"{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "schema_version": True,
            "day": DAY.isoformat(),
            "updated_at_utc": "2026-10-15T00:00:00.000000Z",
            "revision": 0,
            "history": [],
            "projection": {"state": "none", "projected_revision": None, "digest": None},
        }),
        encoding="utf-8",
    )
    assert load_human_day(cfg, DAY).status == "invalid"


@pytest.mark.parametrize(
    "projection",
    [
        {"state": "none", "projected_revision": 1, "digest": "a" * 64},
        {"state": "applied", "projected_revision": 0, "digest": "a" * 64},
        {"state": "applied", "projected_revision": 1, "digest": None},
        {"state": "pending", "projected_revision": 2, "digest": "a" * 64},
        {"state": "pending", "projected_revision": 1, "digest": None},
    ],
)
def test_projection_metadata_inconsistency_is_invalid(tmp_path: Path, projection: dict):
    cfg = _cfg(tmp_path)
    path = tmp_path / ".kaizenlog" / "human" / f"{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "day": DAY.isoformat(),
            "updated_at_utc": "2026-10-15T00:00:00.000000Z",
            "revision": 1,
            "history": [{
                "operation": {
                    "operation_id": "op-1",
                    "target_day": DAY.isoformat(),
                    "kind": "outcome",
                    "payload": {"text": "成果"},
                    "target_key": None,
                    "cancel_operation_id": None,
                },
                "recorded_at_utc": "2026-10-15T00:00:00.000000Z",
            }],
            "projection": projection,
        }),
        encoding="utf-8",
    )
    assert load_human_day(cfg, DAY).status == "invalid"


def test_append_retains_projection_baseline_and_sets_pending(tmp_path: Path):
    cfg = _cfg(tmp_path)
    apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    path = tmp_path / ".kaizenlog" / "human" / f"{DAY.isoformat()}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["projection"] = {
        "state": "applied",
        "projected_revision": 1,
        "digest": "a" * 64,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = apply_human_operation(cfg, DAY, _op("op-2"), expected_revision=1)
    loaded = load_human_day(cfg, DAY)
    assert result.status == "saved"
    assert loaded.revision == 2
    assert loaded.projection_state == "pending"
    assert loaded.projected_revision == 1
    assert loaded.projection_digest == "a" * 64


def test_unsafe_human_path_is_typed_invalid_without_artifact(tmp_path: Path):
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    human_root = vault / ".kaizenlog" / "human"
    human_root.parent.mkdir(parents=True)
    try:
        human_root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink unavailable")
    cfg = _cfg(vault)
    loaded = load_human_day(cfg, DAY)
    result = apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    assert loaded.status == "invalid"
    assert result.status == "invalid"
    assert not (outside / f"{DAY.isoformat()}.json").exists()


def test_injected_outside_resolution_is_typed_invalid_without_artifact(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = _cfg(vault)
    planned = vault / ".kaizenlog" / "human" / f"{DAY.isoformat()}.json"
    outside = tmp_path / "forced-outside" / f"{DAY.isoformat()}.json"

    def forced_containment_failure(lock_cfg, lock_day):
        planned_path = human_input._planned_human_path(lock_cfg, lock_day)
        vault_root = Path(lock_cfg.vault_dir).resolve()
        forced_resolved_path = outside
        try:
            forced_resolved_path.relative_to(vault_root)
        except ValueError as exc:
            raise ValueError("human JSONの保存先がVaultの外にあります") from exc
        return planned_path

    monkeypatch.setattr(human_input, "_human_path", forced_containment_failure)
    before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))
    loaded = load_human_day(cfg, DAY)
    result = apply_human_operation(cfg, DAY, _op(), expected_revision=0)
    after = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))
    assert loaded.status == "invalid"
    assert result.status == "invalid"
    assert before == after
    assert not outside.exists()


def test_delayed_source_lock_rechecks_human_idempotency_before_source(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    key = _write_stats(cfg)
    operation = _op(
        kind="classification_correction",
        payload={"category": "集中"},
        target_key=key,
    )
    real_source_lock = human_input._source_lock
    source_waiting = threading.Event()
    allow_source_lock = threading.Event()
    first = {"value": True}

    @contextmanager
    def delayed_real_source_lock(lock_cfg, lock_operation, lock_day):
        if first["value"]:
            first["value"] = False
            source_waiting.set()
            assert allow_source_lock.wait(timeout=5)
        with real_source_lock(lock_cfg, lock_operation, lock_day):
            yield

    monkeypatch.setattr(human_input, "_source_lock", delayed_real_source_lock)
    delayed_result: dict[str, object] = {}

    def delayed_writer():
        delayed_result["result"] = apply_human_operation(
            cfg, DAY, operation, expected_revision=0
        )

    delayed_thread = threading.Thread(target=delayed_writer)
    delayed_thread.start()
    assert source_waiting.wait(timeout=5)
    competing = apply_human_operation(cfg, DAY, operation, expected_revision=0)
    assert competing.status == "saved"
    stats_path = cfg.stats_path / f"{DAY.isoformat()}.json"
    with file_lock(lock_path_for(stats_path)):
        stats_path.write_text(
            stats_path.read_text(encoding="utf-8").replace("Code.exe", "Other.exe"),
            encoding="utf-8",
        )
    allow_source_lock.set()
    delayed_thread.join(timeout=5)
    assert not delayed_thread.is_alive()
    assert delayed_result["result"].status == "idempotent"
    loaded = load_human_day(cfg, DAY)
    assert loaded.revision == 1
    assert len(loaded.history) == 1


def test_stats_store_lock_rechecks_source_before_human_write(tmp_path: Path):
    cfg = _cfg(tmp_path)
    key = _write_stats(cfg)
    stats_path = cfg.stats_path / f"{DAY.isoformat()}.json"
    ready = __import__("threading").Event()
    release = __import__("threading").Event()

    def mutate_under_lock():
        with file_lock(lock_path_for(stats_path)):
            ready.set()
            release.wait(timeout=5)
            stats_path.write_text(
                stats_path.read_text(encoding="utf-8").replace("Code.exe", "Other.exe"),
                encoding="utf-8",
            )

    thread = __import__("threading").Thread(target=mutate_under_lock)
    thread.start()
    assert ready.wait(timeout=5)
    operation = _op(
        kind="classification_correction",
        payload={"category": "集中"},
        target_key=key,
    )
    release.set()
    result = apply_human_operation(cfg, DAY, operation, expected_revision=0)
    thread.join(timeout=5)
    assert result.status == "stale"
    assert load_human_day(cfg, DAY).status == "missing"
