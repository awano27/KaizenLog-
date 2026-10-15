from kaizenlog.config import Config
from datetime import date
from types import SimpleNamespace
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from kaizenlog import cli
from kaizenlog.vault import DailyNoteStore, DIGEST_MARKER, extract_section


def test_degraded_never_exports_machine_evidence_or_creates_actions():
    evidence = SimpleNamespace(markdown='[F21] SECRET machine input',
        reader_summary='本日は90分の記録があります。', reader_notes=('比較対象は未計測です。',),
        insight_candidates=(('INS-1', '作業をまとめて行う'),))
    text = cli._degraded_advice_section(evidence)
    assert 'SECRET' not in text and '[F21]' not in text
    assert '90分' in text and '機械選出' in text
    assert '- [ ]' not in text and 'KZN-' not in text
    assert 'kaizenlog status' in text


@pytest.mark.parametrize('has_report', [False, True])
def test_morning_link_requires_nonempty_report_section(tmp_path, has_report):
    store = DailyNoteStore(Config(vault_dir=tmp_path, daily_notes_dir=""))
    yesterday = date(2026, 9, 5)
    store.write_section(yesterday, DIGEST_MARKER, '## ⏱ 30秒サマリ\n作業記録')
    if has_report:
        store.write_section(yesterday, cli.NIPPOU_MARKER, '## 📝 日報ドラフト\n実績')
    cli._write_morning_yesterday_brief(store, date(2026, 9, 6))
    text = store.read(date(2026, 9, 6))
    assert ('#📝 日報ドラフト' in text) is has_report


def test_default_help_is_small_and_extended_help_preserves_commands(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(['--help'])
    assert result.value.code == 0
    text = capsys.readouterr().out
    assert 'today' in text and '--help --all' in text
    assert 'excavate' not in text
    with pytest.raises(SystemExit) as result:
        cli.main(['--help', '--all'])
    assert result.value.code == 0
    assert 'excavate' in capsys.readouterr().out


def test_bare_command_routes_to_today_using_explicit_config(tmp_path, capsys):
    config = tmp_path / 'config.toml'
    config.write_text('[general]\nvault_dir = "' + tmp_path.as_posix() + '"\n', encoding='utf-8')
    assert cli.main(['--config', str(config)]) == 0
    assert '目標' in capsys.readouterr().out


def test_configured_private_title_is_hidden_without_changing_defaults():
    from kaizenlog.privacy_filter import is_private_block
    assert is_private_block('開発', 'sensitive page', private_title_patterns=['sensitive'])
    assert not is_private_block('開発', 'public page', private_title_patterns=['sensitive'])
    assert is_private_block('ブラウジング', 'YouTube', private_title_patterns=[])


def test_input_zeros_collapse_only_for_consecutive_observed_days():
    from kaizenlog.report import DailySummary, render_markdown
    from kaizenlog.focus import InputStats
    day = date(2026, 9, 6)
    summary = DailySummary(day, 90, {'開発': 90}, {}, [], {}, 0, 0)
    history = [{'day': (day - timedelta(days=n)).isoformat(),
                'source_quality': {'input': {'state': 'observed'}},
                'input': {'focus_blocks': 0, 'keypresses': 0, 'clicks': 0}}
               for n in range(1, 7)]
    text = render_markdown(summary, ZoneInfo('Asia/Tokyo'), input_stats=InputStats(), stats_history=history)
    assert '**集中ブロック**' not in text
    assert '0回が7日以上継続' in text
    history[0]['source_quality']['input']['state'] = 'unavailable'
    text = render_markdown(summary, ZoneInfo('Asia/Tokyo'), input_stats=InputStats(), stats_history=history)
    assert '**集中ブロック**' in text
    text = render_markdown(summary, ZoneInfo('Asia/Tokyo'), input_stats=InputStats(keypresses=1), stats_history=history)
    assert '**集中ブロック**' in text


def test_degraded_diagnostic_archive_is_redacted(tmp_path):
    from kaizenlog import advice_fallback
    from kaizenlog.config import Config
    cfg = Config(vault_dir=tmp_path)
    cfg.privacy.redact_patterns = ['customer-secret']
    evidence = SimpleNamespace(markdown='[F1] customer-secret\n[F2] YouTube private title')
    path = advice_fallback.archive_evidence(cfg, date(2026, 9, 6), evidence)
    body = path.read_text(encoding='utf-8')
    assert 'customer-secret' not in body and 'private title' not in body
    assert '[F1]' in body


@pytest.mark.parametrize('kinds,label', [(['json'], '出力形式'), (['watcher'], '観測データ'), (['pass_fail'], '提案条件')])
def test_doctor_classifies_safe_violation_tags(kinds, label):
    from kaizenlog import advice_fallback
    text = advice_fallback.repair_guidance([{'command': 'advise_health', 'outcome': 'degraded', 'violations': kinds}])
    assert label in text and 'kaizenlog' in text


def test_private_title_configuration_reaches_journal_and_report(tmp_path):
    from datetime import datetime
    from kaizenlog.report import DailySummary, Block, render_markdown
    from kaizenlog.nippou import generate_nippou_deterministic
    tz = ZoneInfo('Asia/Tokyo')
    start = datetime(2026, 9, 6, 9, tzinfo=tz)
    end = start + timedelta(minutes=30)
    title = 'unpublished ' + 'a' * 80 + ' sensitive'
    block = Block(start, end, '開発', 'editor', [title], False, None)
    summary = DailySummary(start.date(), 30, {'開発': 30}, {'editor': 30}, [block], {}, 0, 0)
    text = render_markdown(summary, tz, private_title_patterns=['sensitive'])
    assert 'unpublished' not in text and '30m' in text
    stats = {'blocks': [{'title': title, 'app': 'editor', 'category': '開発', 'minutes': 30, 'start': start.isoformat()}]}
    text = generate_nippou_deterministic(stats, tz, private_title_patterns=['sensitive'])
    assert 'unpublished' not in text
