"""Small user-facing routes for read-only discovery and explicit local actions."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, time, date
from pathlib import Path
from zoneinfo import ZoneInfo


def add_parsers(sub):
    recall = sub.add_parser('recall', help='全期間の統計からキーワードを検索')
    recall.add_argument('keyword')
    rules = sub.add_parser('rules', help='分類ルールの確認と追加')
    rules_sub = rules.add_subparsers(dest='rules_command', required=True)
    rules_sub.add_parser('list')
    add = rules_sub.add_parser('add')
    add.add_argument('name')
    add.add_argument('--pattern', required=True)
    add.add_argument('--ai', action='store_true')
    add.add_argument('--write', action='store_true')
    task = sub.add_parser('task', help='定期実行の登録')
    task_sub = task.add_subparsers(dest='task_command', required=True)
    install = task_sub.add_parser('install')
    install.add_argument('--time', default='21:30')
    install.add_argument('--weekly', action='store_true')
    install.add_argument('--guard-watch', action='store_true')
    install.add_argument('--weekly-time', default='18:00')
    install.add_argument('--write', action='store_true')
    preflight = sub.add_parser('preflight', help='セッション開始用の短い文脈を表示')
    preflight.add_argument('--hook', action='store_true')


def _redactor(cfg):
    from .advice_fallback import safe_text
    return lambda value: safe_text(value, cfg)


def rule_suggestions(cfg, today):
    from .rules import unknown_candidates
    from .stats import load_stats
    rows = unknown_candidates(load_stats(cfg.stats_path, 14, today), cfg.rules,
                              as_of=today, redactor=_redactor(cfg),
                              private_title_patterns=cfg.privacy.private_title_patterns)
    if not rows:
        return ''
    return ('### 分類ルールの候補\n\n名前だけではルールに一致しないアプリ・サイト（過去14日、120分以上）。'
            'ウィンドウタイトルを含む分類の失敗を意味しません。\n\n'
            + '\n'.join(f'- {r.kind}: {r.name} — {r.minutes:.0f}分' for r in rows[:10])
            + '\n\n`kaizenlog rules list` で確認し、`rules add` のプレビューで追加できます。')


def _preflight(cfg, args):
    from .preflight import build_preflight_context, deliver_preflight_once
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    context = build_preflight_context(memory_dir=cfg.memory_path,
                                     cfg=cfg,
                                     today=today, redactor=_redactor(cfg))
    if not args.hook:
        print(context or '事前文脈はまだありません。')
        return 0
    try:
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            return 0
        payload = json.loads(raw)
        cwd = payload.get('cwd') if isinstance(payload, dict) else None
        if not isinstance(cwd, str) or not Path(cwd).is_dir():
            return 0
        def deliver(value):
            print(json.dumps(value, ensure_ascii=False), flush=True)
            return True
        delivered = deliver_preflight_once(context=context, repo_path=Path(cwd),
                                          today=today, state_root=cfg.logs_path / 'preflight',
                                          deliver=deliver)
        if delivered:
            from .runlog import log_run
            log_run(cfg.logs_path, 'preflight', True, 0,
                    note=f'injected_chars={len(context)}; estimated_tokens={(len(context)+1)//2}')
    except Exception:
        # SessionStart must never prevent a session from starting.
        return 0
    return 0


def _export(cfg, args):
    from .aiwork import available_adapters, collect_ai_telemetry
    from .promptledger import load_prompt_ledger, find_matching_entry
    from .promptmine import normalize
    from .promptroi import load_roi_for_paths, prompt_roi_scan_start
    from .prompt_export import export_prompt
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    entries = load_prompt_ledger(cfg.memory_path)
    target = next((e for e in entries if e.id == args.id), None)
    if target is None:
        raise ValueError('指定されたPRMが台帳にありません。')
    adapters = available_adapters(cfg)
    if not adapters:
        raise ValueError('依頼ログの観測元がありません。未観測を減少として扱いません。')
    tz = ZoneInfo(cfg.timezone)
    start = datetime.combine(prompt_roi_scan_start(entries, today), time.min, tzinfo=tz)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz)
    sessions, prompts, _ = collect_ai_telemetry(adapters, start, end)
    from dataclasses import replace
    # Ledger representatives are redacted. Match against the same text and
    # timezone rather than comparing masked text to a raw/UTC window.
    matching = [replace(p, timestamp=p.timestamp.astimezone(tz), text=_redactor(cfg)(p.text)) for p in prompts]
    rows = load_roi_for_paths(cfg.memory_path, matching, sessions, as_of=today)
    row = next(r for r in rows if r.entry.id == args.id)
    # Calendar completion alone cannot prove that the source was observed.
    # Require activity evidence on every date of both comparison windows.
    if not target.marked_on:
        raise ValueError('採用日がありません。')
    marked = date.fromisoformat(target.marked_on)
    required = {marked + timedelta(days=i) for i in range(-30, 30)}
    relevant = [p for p in matching if find_matching_entry([target], normalize(p.text))]
    cohorts = {(p.source, p.project) for p in relevant if marked-timedelta(days=30) <= p.timestamp.date() < marked}
    if not cohorts:
        raise ValueError('採用前の対象依頼ログがありません。')
    for cohort in cohorts:
        observed = {p.timestamp.date() for p in matching if (p.source, p.project) == cohort}
        if not required <= observed:
            raise ValueError('対象の観測元・プロジェクトで前後30日の依頼ログが揃っていません。欠損を再発ゼロとして出力しません。')
    example = next((raw.text for raw, matched in zip(prompts, matching)
                    if find_matching_entry([target], normalize(matched.text))), '')
    result = export_prompt(Path(args.project), row, example, name=args.name,
                           write=args.write, force=args.force, redactor=_redactor(cfg),
                           private_title_patterns=cfg.privacy.private_title_patterns)
    print(result.content)
    print(f'{result.status}: {result.path}')
    if result.backup:
        print(f'バックアップ: {result.backup}')
    return 0


def dispatch(cfg, args):
    """Return None for routes owned by the original CLI."""
    try:
        if args.command == 'recall':
            from .history_recall import search_history
            rows = search_history(cfg.stats_path, args.keyword, redactor=_redactor(cfg),
                                  private_title_patterns=cfg.privacy.private_title_patterns)
            for row in rows:
                print(f'{row.day} [{row.source}] {row.text}')
            if not rows:
                print('該当する記録はありません。')
            return 0
        if args.command == 'rules':
            if args.rules_command == 'list':
                print(_redactor(cfg)(json.dumps(cfg.rules, ensure_ascii=False, indent=2)))
                print(rule_suggestions(cfg, datetime.now(ZoneInfo(cfg.timezone)).date()))
            else:
                from .config import find_config_file
                from .rules import append_rule
                path = find_config_file(args.config)
                if path is None:
                    raise ValueError('設定ファイルがありません。')
                print(append_rule(Path(path), name=args.name, pattern=args.pattern,
                                  ai=args.ai, write=args.write))
                print('設定を更新しました。' if args.write else 'プレビューのみ。反映は --write。')
            return 0
        if args.command == 'task':
            from .setup import register_daily_task, register_weekly_task
            from .config import find_config_file
            import re
            for value in (args.time, args.weekly_time):
                if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
                    raise ValueError('時刻は HH:MM 形式で指定してください。')
            if not args.write:
                print(f'登録予定: 日次 {args.time}' + (f' / 日曜 {args.weekly_time}' if args.weekly else '')
                      + (' / ライブ検知5分毎' if args.guard_watch else ''))
                print('反映するには task install --write を指定してください。')
                return 0
            path = find_config_file(args.config)
            ok = register_daily_task(time=args.time, config_path=path)
            if ok and args.weekly:
                ok = register_weekly_task(cfg.vault_dir, weekly_time=args.weekly_time, config_path=path)
            if ok and args.guard_watch:
                from .setup import _find_register_task_script
                import shutil
                import subprocess
                script, exe = _find_register_task_script(), shutil.which('kaizenlog')
                if script is None or exe is None:
                    return 1
                command = ['powershell', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                           '-GuardWatch', '-KaizenlogExe', exe]
                if path:
                    command += ['-ConfigPath', str(Path(path).resolve())]
                try:
                    ok = subprocess.run(command, capture_output=True, text=True, timeout=120).returncode == 0
                except subprocess.TimeoutExpired:
                    return 1
            return 0 if ok else 1
        if args.command == 'preflight':
            from .note_paths import DailyNoteResolutionError
            try:
                return _preflight(cfg, args)
            except DailyNoteResolutionError as exc:
                if args.hook:
                    return 0
                resolution = exc.resolution
                print(
                    f"❌ 日次ノートを解決できません: {resolution.reason or resolution.status}",
                    file=sys.stderr,
                )
                for candidate in resolution.candidates:
                    state = "存在" if candidate.exists else "未作成"
                    print(
                        f"   - {candidate.path} ({candidate.format_name}, {state})",
                        file=sys.stderr,
                    )
                print(
                    "   設定の daily_note_format または [daily_notes.overrides] を確認してください。",
                    file=sys.stderr,
                )
                return 1
        if args.command == 'guard' and args.guard_command == 'watch':
            from .guard_watch import watch_once
            if not args.once:
                raise ValueError('guard watch には --once を指定し、定期実行から呼び出してください。')
            if not cfg.guard.enabled or not cfg.aiwork.enabled:
                print('guard または AI観測が無効です。')
                return 0
            events = watch_once(codex_sessions_dir=Path(cfg.aiwork.codex_sessions_dir).expanduser(),
                                browser_export_dir=Path(cfg.aiwork.browser_export_dir).expanduser(),
                                state_root=cfg.logs_path / 'guard-watch', memory_dir=cfg.memory_path,
                                retry_threshold=cfg.guard.retry_threshold,
                                notify_enabled=cfg.guard.notify,
                                cooldown_seconds=cfg.guard.cooldown_seconds, redactor=_redactor(cfg))
            print(f'ライブ通知: {len(events)}件（通知設定: {cfg.guard.notify}）')
            return 0
        if args.command == 'prompts' and args.prompts_command == 'export':
            return _export(cfg, args)
    except (ValueError, OSError) as exc:
        if args.command == 'preflight' and args.hook:
            return 0
        print(f'処理できません: {exc}', file=sys.stderr)
        return 1
    return None
