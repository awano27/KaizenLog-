from datetime import date

import pytest

from kaizenlog.config import load_config


def test_rule_append_is_preview_first_preserves_comments_and_defaults(tmp_path):
    from kaizenlog.rules import append_rule
    path = tmp_path / 'config.toml'
    original = b'# user comment\r\n[general]\r\ntimezone = "Asia/Tokyo"\r\n'
    path.write_bytes(original)
    preview = append_rule(path, name='Design', pattern='new-app', write=False)
    assert 'new-app' in preview and path.read_bytes() == original
    append_rule(path, name='Design', pattern='new-app', write=True)
    assert path.read_bytes().startswith(original)
    assert load_config(str(path)).rules[0]['name'] == 'Design'
    assert len(load_config(str(path)).rules) > 1
    once = path.read_bytes()
    append_rule(path, name='Design', pattern='new-app', write=True)
    assert path.read_bytes() == once


def test_rule_invalid_regex_does_not_write(tmp_path):
    from kaizenlog.rules import append_rule
    path = tmp_path / 'config.toml'
    path.write_text('[general]\n', encoding='utf-8')
    with pytest.raises(ValueError):
        append_rule(path, name='Design', pattern='[', write=True)
    assert path.read_text(encoding='utf-8') == '[general]\n'


def test_unknown_candidates_exclude_known_private_bad_and_duplicate_days():
    from kaizenlog.rules import unknown_candidates
    stats = [
        {'day': '2026-09-05', 'by_app': {'Mystery': 90, 'Chrome': 90, 'secret': 90, 'bad': -1}, 'by_site': {'novel.example': 30}},
        {'day': '2026-09-05', 'by_app': {'Mystery': 90}, 'by_site': {}},
        {'day': '2026-09-06', 'by_app': {'Mystery': 60, 'secret': 90}, 'by_site': {}},
        {'day': '2026-10-06', 'by_app': {'Future': 9999}},
    ]
    candidates = unknown_candidates(stats, [{'name': 'Web','patterns':['chrome']}],
                                    as_of=date(2026,9,6), min_minutes=120, private_title_patterns=['secret'])
    assert [(c.kind, c.name, c.minutes) for c in candidates] == [('app', 'Mystery', 150)]
