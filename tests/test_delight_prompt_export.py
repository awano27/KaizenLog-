from pathlib import Path

import pytest

from kaizenlog.promptledger import PromptLedgerEntry
from kaizenlog.promptroi import PromptROIRow


def row():
    entry = PromptLedgerEntry('PRM-20260701-001', 'summarize report', 5, 4,
                              '2026-06-01', '2026-09-01', 'skilled', 'summary', '2026-07-15')
    return PromptROIRow(entry, 1, 100, 5, 1, 80)


def test_export_is_preview_first_and_never_marks_adopted(tmp_path):
    from kaizenlog.prompt_export import export_prompt
    before = list(tmp_path.rglob('*'))
    result = export_prompt(tmp_path, row(), 'customer-secret report in full',
                           redactor=lambda s: s.replace('customer-secret', '[REDACTED]'))
    assert result.status == 'preview'
    assert 'customer-secret' not in result.content
    assert '下書き' in result.content and '5回 → 1回' in result.content
    assert 'report in full' in result.content
    assert list(tmp_path.rglob('*')) == before
    result = export_prompt(tmp_path, row(), 'report in full', write=True)
    assert result.path.is_file()
    assert not (tmp_path / 'Kaizen' / 'Memory').exists()
    original = result.path.read_bytes()
    again = export_prompt(tmp_path, row(), 'report in full', write=True)
    assert again.status == 'unchanged' and again.path.read_bytes() == original


def test_export_preserves_user_edited_draft_without_force(tmp_path):
    from kaizenlog.prompt_export import export_prompt
    initial = export_prompt(tmp_path, row(), 'first prompt', write=True)
    initial.path.write_text('USER CONTENT', encoding='utf-8')
    with pytest.raises(ValueError):
        export_prompt(tmp_path, row(), 'first prompt', write=True)
    assert initial.path.read_text(encoding='utf-8') == 'USER CONTENT'
    forced = export_prompt(tmp_path, row(), 'first prompt', write=True, force=True)
    assert forced.backup.read_text(encoding='utf-8') == 'USER CONTENT'


@pytest.mark.parametrize('before,after,pending', [(None,None,False),(0,0,False),(1,2,False),(5,1,True)])
def test_export_requires_completed_positive_observed_difference(tmp_path, before, after, pending):
    from kaizenlog.prompt_export import export_prompt
    candidate = row()
    candidate.skilled_before, candidate.skilled_after, candidate.skilled_pending = before, after, pending
    with pytest.raises(ValueError):
        export_prompt(tmp_path, candidate, 'example', write=True)
    assert list(tmp_path.rglob('*')) == []


@pytest.mark.parametrize('name', ['../escape', 'CON', 'a/b', 'bad:name', 'a.'])
def test_export_rejects_unsafe_names_before_writes(tmp_path, name):
    from kaizenlog.prompt_export import export_prompt
    with pytest.raises(ValueError):
        export_prompt(tmp_path, row(), 'example', name=name, write=True)
    assert list(tmp_path.rglob('*')) == []
