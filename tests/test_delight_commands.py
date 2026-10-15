from kaizenlog.cli import build_parser


def test_new_command_routes():
    parser = build_parser()
    for argv in [['recall', 'meeting'], ['rules', 'list'],
                 ['rules', 'add', 'Work', '--pattern', 'editor'],
                 ['task', 'install'], ['guard', 'watch', '--once'],
                 ['guard', 'install', '--preflight'], ['preflight', '--hook'],
                 ['prompts', 'export', 'PRM-20260906-001']]:
        assert parser.parse_args(argv).command == argv[0]


def test_previews_leave_no_files(tmp_path, capsys):
    from kaizenlog.config import Config
    from kaizenlog.delight_cli import dispatch
    cfg = Config(vault_dir=tmp_path)
    parser = build_parser()
    for argv in [['recall', 'missing'], ['rules', 'list'], ['task', 'install'], ['preflight']]:
        assert dispatch(cfg, parser.parse_args(argv)) == 0
    assert list(tmp_path.iterdir()) == []


def test_rules_preview_then_explicit_write(tmp_path, capsys):
    from kaizenlog.config import Config
    from kaizenlog.delight_cli import dispatch
    path = tmp_path / 'config.toml'
    path.write_text('# user comment\n[general]\n', encoding='utf-8')
    cfg = Config(vault_dir=tmp_path)
    argv = ['--config', str(path), 'rules', 'add', 'Editor', '--pattern', 'custom-editor']
    assert dispatch(cfg, build_parser().parse_args(argv)) == 0
    assert path.read_text() == '# user comment\n[general]\n'
    assert dispatch(cfg, build_parser().parse_args(argv + ['--write'])) == 0
    assert 'custom-editor' in path.read_text()
    assert path.read_text().startswith('# user comment')


def test_missing_config_preflight_hook_silent(tmp_path, capsys):
    from kaizenlog.cli import main
    assert main(['--config', str(tmp_path / 'missing.toml'), 'preflight', '--hook']) == 0
    assert capsys.readouterr().out == ''


def test_guard_watch_requires_once_and_disabled_does_not_write(tmp_path, capsys):
    from kaizenlog.config import Config
    from kaizenlog.delight_cli import dispatch
    cfg = Config(vault_dir=tmp_path)
    assert dispatch(cfg, build_parser().parse_args(['guard', 'watch'])) == 1
    cfg.guard.enabled = False
    assert dispatch(cfg, build_parser().parse_args(['guard', 'watch', '--once'])) == 0
    assert not list(tmp_path.iterdir())


def test_adapter_registration_opt_in(tmp_path):
    from kaizenlog.config import Config
    from kaizenlog.aiwork import available_adapters
    cfg = Config()
    cfg.aiwork.claude_projects_dir = str(tmp_path / 'absent1')
    cfg.aiwork.codex_sessions_dir = str(tmp_path / 'absent2')
    cfg.aiwork.browser_export_dir = str(tmp_path / 'absent3')
    cfg.aiwork.grok_sessions_dir = str(tmp_path)
    assert [a.name for a in available_adapters(cfg)] == ['grok']
    cfg.aiwork.cursor_sessions_dir = str(tmp_path)
    assert [a.name for a in available_adapters(cfg)] == ['grok', 'cursor']


def test_export_requires_target_source_and_project_observations(tmp_path, monkeypatch, capsys):
    import json
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from kaizenlog.config import Config
    from kaizenlog.aiwork import UserPrompt
    from kaizenlog.promptmine import normalize
    from kaizenlog.delight_cli import dispatch
    cfg = Config(vault_dir=tmp_path)
    cfg.memory_path.mkdir(parents=True)
    now = datetime.now(ZoneInfo(cfg.timezone))
    marked = now.date() - timedelta(days=30)
    text = 'Build the dashboard release report with verified acceptance details'
    entry = {'id': 'PRM-20260701-001', 'representative': normalize(text), 'status': 'skilled',
             'marked_on': marked.isoformat(), 'first_seen': (marked-timedelta(days=30)).isoformat(),
             'last_seen': (marked-timedelta(days=1)).isoformat(), 'count_total': 30, 'days_seen': 30}
    ledger = cfg.memory_path / 'prompt_clusters.jsonl'
    ledger.write_text(json.dumps(entry) + '\n', encoding='utf-8')
    initial = ledger.read_bytes()
    prompts = [UserPrompt(datetime.combine(marked+timedelta(days=i), now.time(), tzinfo=now.tzinfo),
                          'target', text, 'codex') for i in range(-30, 0)]
    prompts += [UserPrompt(datetime.combine(marked+timedelta(days=i), now.time(), tzinfo=now.tzinfo),
                           'unrelated', 'Botanical specimen taxonomy explanation', 'claude-code') for i in range(30)]
    monkeypatch.setattr('kaizenlog.aiwork.available_adapters', lambda _: [object()])
    monkeypatch.setattr('kaizenlog.aiwork.collect_ai_telemetry', lambda *_: ([], prompts, 0))
    args = build_parser().parse_args(['prompts', 'export', entry['id'], '--project', str(tmp_path)])
    assert dispatch(cfg, args) == 1
    assert not (tmp_path / '.claude').exists()
    # Daily observations of the target source/project make a real comparison possible.
    for prompt in prompts[30:]:
        prompt.source, prompt.project = 'codex', 'target'
    assert dispatch(cfg, args) == 0
    assert '30回 → 0回' in capsys.readouterr().out
    assert ledger.read_bytes() == initial
    assert not (tmp_path / '.claude').exists()


def test_preflight_hook_outputs_once_and_logs_only_counts(tmp_path, monkeypatch, capsys):
    import io
    import json
    from kaizenlog.config import Config
    from kaizenlog.delight_cli import dispatch
    cfg = Config(vault_dir=tmp_path)
    cfg.privacy.redact_patterns = ['SECRET']
    monkeypatch.setattr('kaizenlog.preflight.build_preflight_context',
                        lambda **kw: kw['redactor']('SECRET current goal'))
    args = build_parser().parse_args(['preflight', '--hook'])
    for _ in range(2):
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'cwd': str(tmp_path)})))
        assert dispatch(cfg, args) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    content = json.loads(lines[0])['hookSpecificOutput']['additionalContext']
    assert 'SECRET' not in content and 'current goal' in content
    for path in cfg.logs_path.rglob('*.json*'):
        raw = path.read_text(encoding='utf-8')
        assert 'SECRET' not in raw and 'current goal' not in raw
    log = (cfg.logs_path / 'runs.jsonl').read_text(encoding='utf-8')
    assert 'injected_chars' in log


def test_preflight_cli_route_reads_daily_note_through_config(tmp_path, capsys):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from kaizenlog.config import Config
    from kaizenlog.delight_cli import dispatch
    from kaizenlog.vault import DailyNoteStore, GOAL_MARKER

    cfg = Config(vault_dir=tmp_path)
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    store = DailyNoteStore(cfg)
    store.dir.mkdir(parents=True)
    store.path_for(today).write_text(
        f'<!-- {GOAL_MARKER}:start -->\n'
        'CLI preflight integration fixture\n'
        f'<!-- {GOAL_MARKER}:end -->\n',
        encoding='utf-8',
    )

    assert dispatch(cfg, build_parser().parse_args(['preflight'])) == 0
    assert '今日の目標: CLI preflight integration fixture' in capsys.readouterr().out


def test_preflight_cli_route_reports_daily_note_conflict(tmp_path, capsys):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from kaizenlog.config import Config
    from kaizenlog.delight_cli import dispatch

    cfg = Config(vault_dir=tmp_path)
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    notes = cfg.daily_notes_path
    notes.mkdir(parents=True)
    (notes / f'{today.isoformat()}.md').write_text('configured', encoding='utf-8')
    (notes / f'{today.strftime("%Y%m%d")}.md').write_text('known', encoding='utf-8')

    assert dispatch(cfg, build_parser().parse_args(['preflight'])) == 1
    captured = capsys.readouterr()
    assert '日次ノートを解決できません' in captured.err
    assert '複数の日次ノート候補' in captured.err


def test_monthly_weekly_cli_integrate_separate_markers_and_preserve_crlf(tmp_path, capsys):
    from kaizenlog.cli import main
    from kaizenlog.config import load_config
    from kaizenlog.weekly_context import weekly_review_path
    from datetime import date
    config = tmp_path / 'config.toml'
    config.write_text('[general]\nvault_dir="' + tmp_path.as_posix() + '"\n[aiwork]\nenabled=false\n', encoding='utf-8')
    cfg = load_config(str(config))
    initial = {p for p in tmp_path.rglob('*')}
    assert main(['--config', str(config), 'monthly', '--month', '2026-09']) == 0
    assert main(['--config', str(config), 'weekly-context', '--week', '2026-W36']) == 0
    assert {p for p in tmp_path.rglob('*')} == initial
    month = cfg.monthly_path / '2026-09.md'
    week = weekly_review_path(cfg.daily_notes_path, date(2026, 8, 31))
    for path in (month, week):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'# User note\r\n\r\nKeep this paragraph.\r\n')
    assert main(['--config', str(config), 'monthly', '--month', '2026-09', '--write']) == 0
    assert main(['--config', str(config), 'weekly-context', '--week', '2026-W36', '--write', '--strict-write']) == 0
    for path, marker in ((month, b'kaizenlog:monthly-album'), (week, b'kaizenlog:weekly-story')):
        payload = path.read_bytes()
        assert b'Keep this paragraph.\r\n' in payload
        assert payload.count(marker+b':start') == 1
