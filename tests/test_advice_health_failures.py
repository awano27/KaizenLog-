import pytest

from kaizenlog import cli
from kaizenlog.advisor import AdviceContractError
from kaizenlog.runlog import load_runs
from kaizenlog.memory import load_entries
from kaizenlog.vault import ADVICE_MARKER, DailyNoteStore
from tests.test_advice_concurrency import _setup
from tests.test_advise_integration import DAY, VALID_GENERATED


@pytest.mark.parametrize("stage", ["degraded_save", "advice_save", "handoff"])
def test_storage_failure_records_one_failed_health_and_preserves_error(monkeypatch, tmp_path, stage):
    cfg, store = _setup(tmp_path)
    error = OSError("injected storage failure")

    def fail(*args, **kwargs):
        raise error

    def generate(*args, **kwargs):
        if stage == "degraded_save":
            raise AdviceContractError("contract", violations=["通知はダメ"], reason_codes=["contract_invalid"])
        return VALID_GENERATED

    monkeypatch.setattr(cli, "generate_advice", generate)
    if stage == "degraded_save":
        original = DailyNoteStore.write_section

        def write(self, day, marker, text):
            if marker == ADVICE_MARKER:
                raise error
            return original(self, day, marker, text)

        monkeypatch.setattr(DailyNoteStore, "write_section", write)
    else:
        monkeypatch.setattr(cli, "_save_advice_with_entries" if stage == "advice_save" else "_write_actions_handoff", fail)
    with pytest.raises(OSError) as caught:
        cli.cmd_advise(cfg, DAY)
    assert caught.value is error
    health = [row for row in load_runs(cfg.logs_path) if row.get("command") == "advise_health"]
    assert len(health) == 1
    assert health[0]["outcome"] == "failed"
    assert stage + "_failed" in health[0]["reason_codes"]
    if stage == "degraded_save":
        assert "notification" in health[0]["violations"]
    if stage == "handoff":
        assert load_entries(cfg.memory_path)
        assert "KZN-20260721" in store.path_for(DAY).read_text(encoding="utf-8")
