import pytest

from kaizenlog.advice_format import validate_advice
from tests.test_advice_format import _evidence, _valid_data


@pytest.mark.parametrize("text", [
    "kaizenlog stat で切替を確認する",
    "`kaizenlog stats` を確認する",
    "kaizenlog.exe statで確認する",
    "KAIZENLOG STAT を確認する",
])
def test_rejects_nonexistent_statistics_command_in_action(text):
    data = _valid_data()
    data["actions"][0]["action"] = text
    errors = validate_advice(data, _evidence())
    assert any("存在しない" in error and "日誌" in error for error in errors)


@pytest.mark.parametrize("section,field", [("proposals", "proposal"), ("ai_review", "text"), ("actions", "trigger")])
def test_rejects_nonexistent_statistics_command_in_other_reader_fields(section, field):
    data = _valid_data()
    data[section][0][field] = "kaizenlog statで確認する"
    assert any("存在しない" in error for error in validate_advice(data, _evidence()))


def test_rejects_nonexistent_statistics_command_in_plan_review():
    data = _valid_data(plan_review="計画を確認する\nkaizenlog statで確認する")
    assert any("存在しない" in error for error in validate_advice(data, _evidence()))


@pytest.mark.parametrize("text", ["kaizenlog status で実行履歴を確認する", "翌日の日誌で集中ブロック回数を確認する"])
def test_keeps_supported_status_and_note_instructions(text):
    data = _valid_data()
    data["actions"][0]["action"] = text
    assert validate_advice(data, _evidence()) == []
