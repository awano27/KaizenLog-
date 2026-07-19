"""kaizenlog コマンドラインインターフェース。

  kaizenlog generate [--date YYYY-MM-DD]   ログ収集→デイリーノート書き込み→実験の自動計測
  kaizenlog advise   [--date YYYY-MM-DD]   デイリーノートを読んでLLMの改善提案を追記する
  kaizenlog run      [--date YYYY-MM-DD]   generate + advise
  kaizenlog experiment new/list            カイゼン実験の起票・一覧
  kaizenlog patterns [--days N]            繰り返しパターンの検出レポート（自動化候補）
  kaizenlog report [--date] [--no-llm] [--write]  提出用の日報ドラフトを生成
  kaizenlog prompts [--days N]             Claude Codeへの繰り返し依頼を発掘（プロンプト資産化）
  kaizenlog backfill [--days N]            欠損日の日誌・統計をまとめて補完する
  kaizenlog status                         実行履歴（最終成功・直近の失敗）を表示
  kaizenlog doctor                         セットアップと環境の健全性を診断
  kaizenlog init-config                    設定ファイルの雛形を出力する
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime, time, timedelta
from time import monotonic
from pathlib import Path
from zoneinfo import ZoneInfo

from .advisor import (
    AdvisorError,
    build_prompt,
    generate_advice,
    resolve_system_prompt,
)
from .aiwork import render_aiwork_markdown, scan_sessions, scan_user_prompts
from .doctor import run_doctor
from .memory import (
    append_entries,
    assign_action_ids,
    load_entries,
    summarize_for_prompt,
    update_statuses_from_note,
)
from .nippou import generate_nippou_deterministic, generate_nippou_llm
from .notify import notify
from .privacy import PrivacyError, make_redactor
from .promptmine import render_prompt_report
from .runlog import load_runs, log_run, render_status
from .skill_manager import (
    bundled_skill_content,
    bundled_skill_names,
    check_skill,
    diff_skill,
    install_skill,
    skill_description,
)
from .classifier import Classifier
from .collector import ActivityWatchClient, ActivityWatchError, collect_day, collect_input
from .focus import compute_input_stats
from .config import Config, load_config
from .experiments import (
    METRIC_DESCRIPTIONS,
    ExperimentError,
    compute_metric,
    create_experiment,
    load_experiments,
    record_measurement,
    render_experiments_context,
)
from .patterns import render_patterns_markdown
from .report import render_markdown, summarize
from .stats import load_stats, missing_days, write_stats
from .vault import (
    ACTIVITY_MARKER,
    ADVICE_MARKER,
    DailyNoteStore,
    extract_heading_section,
    extract_section,
)


def _parse_date(s: str | None, tz: ZoneInfo) -> date:
    if s:
        return date.fromisoformat(s)
    return datetime.now(tz).date()


def cmd_generate(cfg: Config, day: date) -> Path:
    tz = ZoneInfo(cfg.timezone)
    day_start = datetime.combine(day, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    client = ActivityWatchClient(cfg.aw_base_url)
    events = collect_day(client, day_start, day_end)
    classified = Classifier(cfg.rules).classify_all(events)
    summary = summarize(day, classified, gap_minutes=cfg.session_gap_minutes)

    # aw-watcher-input 導入時のみ集中ブロックを算出（未導入ならNone）
    input_raw = collect_input(client, day_start, day_end)
    input_stats = compute_input_stats(input_raw) if input_raw is not None else None

    section = render_markdown(summary, tz, min_block_minutes=cfg.min_block_minutes,
                              input_stats=input_stats)

    cc_sessions = []
    if cfg.aiwork.enabled:
        projects_dir = Path(cfg.aiwork.claude_projects_dir).expanduser()
        cc_sessions = scan_sessions(projects_dir, day_start, day_end)
        aiwork_md = render_aiwork_markdown(cc_sessions, tz)
        if aiwork_md:
            section = section.rstrip() + "\n\n" + aiwork_md

    store = DailyNoteStore(cfg.daily_notes_path)
    path = store.write_section(day, ACTIVITY_MARKER, section)
    print(f"✅ Activity Log を書き込みました: {path}")
    print(f"   合計 {summary.total_minutes:.0f}分 / {len(summary.blocks)}ブロック"
          f" / AIセッション {summary.ai_sessions}回"
          f" / Claude Codeセッション {len(cc_sessions)}回")

    # パターン検出用の機械可読な統計を蓄積する
    write_stats(cfg.stats_path, day, summary, cc_sessions, input_stats)

    # 実行中のカイゼン実験に対象日の実測値を追記する
    for exp in load_experiments(cfg.experiments_path):
        if exp.status not in ("running",):
            continue
        value = compute_metric(exp.metric, summary, cc_sessions, input_stats)
        if value is None:
            print(f"⚠️  実験「{exp.title}」の指標 {exp.metric} は不明のためスキップしました")
            continue
        met = record_measurement(exp, day, value)
        mark = "✅" if met else "❌"
        print(f"🧪 実験「{exp.title}」: {value:g}（目標 {exp.target_op} {exp.target_value:g} {mark}）")
    return path


def cmd_backfill(cfg: Config, days: int, end_day: date) -> int:
    """統計の無い過去日をまとめて生成する。生成できた日数を返す。"""
    targets = missing_days(cfg.stats_path, end_day, days)
    if not targets:
        print(f"✅ 過去{days}日に欠損はありません。")
        return 0
    print(f"🔄 欠損 {len(targets)}日分を補完します: "
          + ", ".join(d.isoformat() for d in targets))
    done = 0
    for d in targets:
        try:
            cmd_generate(cfg, d)
            done += 1
        except ActivityWatchError as e:
            print(f"⚠️  {d} の補完に失敗: {e}", file=sys.stderr)
            break  # ActivityWatch自体に繋がらないなら以降も無駄
    return done


def cmd_advise(cfg: Config, day: date, dry_run: bool = False) -> Path | None:
    store = DailyNoteStore(cfg.daily_notes_path)
    content = store.read(day)
    if content is None:
        raise SystemExit(
            f"デイリーノートがありません: {store.path_for(day)}\n"
            "先に `kaizenlog generate` を実行してください。"
        )
    activity_md = extract_section(content, ACTIVITY_MARKER)
    if activity_md is None:
        raise SystemExit("Activity Log セクションがありません。先に `kaizenlog generate` を実行してください。")

    intent = _extract_intent(content)

    # 直近数日のカテゴリ別サマリーだけを傾向情報として渡す（トークン節約）
    recent: list[str] = []
    for i in range(1, cfg.llm.lookback_days + 1):
        past_day = day - timedelta(days=i)
        past = store.read(past_day)
        if not past:
            continue
        past_activity = extract_section(past, ACTIVITY_MARKER)
        if not past_activity:
            continue
        head = _category_table_only(past_activity)
        if head:
            recent.append(f"\n## {past_day.isoformat()}\n{head}\n")

    experiments_ctx = render_experiments_context(load_experiments(cfg.experiments_path))

    # Kaizen Memory: 直近ノートのチェックボックスからdoneを検出し、要約をLLMに渡す
    entries = load_entries(cfg.memory_path)
    status_updates = []
    for i in range(0, 4):  # 今日を含む直近4日分のノートを走査
        past = store.read(day - timedelta(days=i))
        if past:
            status_updates.extend(update_statuses_from_note(past, entries, day))
    if status_updates:
        append_entries(cfg.memory_path, status_updates)
        entries = load_entries(cfg.memory_path)
        print(f"📗 完了アクションを記録しました: "
              + ", ".join(e.id for e in status_updates))
    memory_ctx = summarize_for_prompt(entries, day)

    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)

    if dry_run:
        # LLMに送る内容を送信せずに表示する（送信内容の監査用。マスク適用後を表示）
        prompt = build_prompt(activity_md, recent, intent, experiments_ctx or None,
                              memory_ctx or None)
        if redactor:
            prompt = redactor(prompt)
        print("===== system prompt =====")
        print(resolve_system_prompt(cfg.llm))
        print("===== user prompt =====")
        print(prompt)
        print("=====")
        print("（--dry-run のためLLMには送信していません。ノートも変更していません）")
        return None

    advice_md = generate_advice(
        cfg.llm, activity_md, recent,
        intent=intent,
        experiments=experiments_ctx or None,
        memory=memory_ctx or None,
        redactor=redactor,
    )
    # 「明日の最小アクション」に安定ID（KZN-YYYYMMDD-NNN）を付与して記録する
    advice_md, new_entries = assign_action_ids(advice_md, day, entries)
    path = store.write_section(day, ADVICE_MARKER, advice_md)
    append_entries(cfg.memory_path, new_entries)
    print(f"✅ 改善提案を書き込みました: {path}")
    if new_entries:
        print("🆔 アクションID: " + ", ".join(e.id for e in new_entries))
    return path


def _category_table_only(activity_md: str) -> str:
    """Activity Logからカテゴリ別テーブル部分だけを抜き出す。"""
    lines = activity_md.splitlines()
    out: list[str] = []
    in_table = False
    for line in lines:
        if line.startswith("### カテゴリ別"):
            in_table = True
            continue
        if in_table:
            if line.startswith("### "):
                break
            if line.strip():
                out.append(line)
    return "\n".join(out)


CONFIG_TEMPLATE = '''\
# KaizenLog 設定ファイル
# 置き場所: このファイルを %APPDATA%/kaizenlog/config.toml に置くか、
#           環境変数 KAIZENLOG_CONFIG でパスを指定してください。

[general]
timezone = "Asia/Tokyo"
vault_dir = 'C:/develop/obsidian/2026'   # Obsidianボールトのルート
daily_notes_dir = "01 Daily Notes"
experiments_dir = "03 Areas/Kaizen Experiments"   # カイゼン実験ノートの置き場所
stats_dir = ".kaizenlog/stats"   # パターン検出用の日次統計JSON（ドットフォルダ=Obsidian非表示）
logs_dir = ".kaizenlog/logs"     # 実行ログ（kaizenlog status で確認）
memory_dir = "Kaizen/Memory"     # 提案の記録（Kaizen Memory、重複提案の防止に使用）
auto_backfill_days = 3      # 毎晩の実行時に直近N日の欠損を自動補完（0で無効）
log_retention_days = 90     # 実行ログの保持日数
min_block_minutes = 3.0     # タイムラインに載せる最小ブロック長（分）
session_gap_minutes = 5.0   # この分数以上空いたら別セッション扱い

[notifications]
on_failure = true   # 夜間実行が失敗したときWindows通知を出す

[privacy]
# LLMへ送信する前にマスクする正規表現（ボールト内の日誌は原文のまま保持される）
# 例: redact_patterns = ["(株)〇〇商事", "案件[A-Z]-\\d+", "\\S+@\\S+\\.co\\.jp"]
redact_patterns = []
replacement = "[REDACTED]"

[activitywatch]
base_url = "http://localhost:5600"

[aiwork]
# Claude Codeのセッションログ（JSONL）から「AI作業の質」を集計する
# 往復数・細切れセッション・ツールエラー・中断を検出し、改善提案の材料にする
enabled = true
claude_projects_dir = "~/.claude/projects"

[llm]
# "claude-code-cli"   : Claude Code CLI（要: https://claude.com/claude-code & ログイン済み）
# "copilot-cli"       : GitHub Copilot CLI（要: npm install -g @github/copilot & ログイン済み）
# "openai-compatible" : GitHub Models / Ollama などOpenAI互換API
# "none"              : 改善提案をスキップ（ログ生成のみ）
backend = "copilot-cli"
# システムプロンプト: 同梱テンプレート名（daily_advisor / privacy_safe /
# weekly_review / ai_work_deep_review）または自作プロンプトのファイルパス
system_prompt = "daily_advisor"
lookback_days = 7   # 傾向分析のために渡す過去日数
retries = 2                # 一時エラー時の再試行回数
retry_wait_seconds = 20    # 再試行までの待ち秒数

[llm.claude_code_cli]
command = "claude"
extra_args = []   # 例: ["--model", "haiku"]

[llm.copilot_cli]
command = "copilot"
extra_args = []   # 例: ["--model", "claude-sonnet-4"]

[llm.openai_compatible]
# --- Ollama（完全ローカル・GPU不要、8Bモデルは16GB RAM推奨）---
base_url = "http://localhost:11434/v1"
model = "qwen3:8b"
# --- GitHub Models（無料API）を使う場合は下記に差し替え ---
# base_url = "https://models.github.ai/inference"
# model = "openai/gpt-4o"
# api_key_env に指定した環境変数へ models:read 権限のPATを設定
api_key_env = "KAIZENLOG_API_KEY"
timeout_seconds = 600

# カテゴリ分類ルールの追加例（デフォルトルールより優先されます）
# [[categories.rules]]
# name = "AI作業"
# ai = true
# patterns = ["自社のAIツール名"]
'''


def _extract_intent(content: str) -> str | None:
    """手書きの計画欄（Today's Focus / Tasks）を取り出す。"""
    parts = []
    for heading in ("Today's Focus", "Tasks"):
        section = extract_heading_section(content, heading)
        if section:
            parts.append(f"## {heading}\n{section}")
    return "\n\n".join(parts) or None


NIPPOU_MARKER = "kaizenlog:nippou"


def cmd_report(cfg: Config, day: date, use_llm: bool, write: bool) -> None:
    tz = ZoneInfo(cfg.timezone)
    store = DailyNoteStore(cfg.daily_notes_path)
    content = store.read(day) or ""
    intent = _extract_intent(content)

    if use_llm:
        activity_md = extract_section(content, ACTIVITY_MARKER)
        if activity_md is None:
            raise SystemExit("Activity Log がありません。先に `kaizenlog generate` を実行してください。")
        redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
        section = generate_nippou_llm(cfg.llm, activity_md, intent, redactor=redactor)
    else:
        stats_list = load_stats(cfg.stats_path, days=1, end_day=day)
        if not stats_list:
            raise SystemExit(
                f"{day} の統計がありません。先に `kaizenlog generate` を実行してください。"
            )
        section = generate_nippou_deterministic(
            stats_list[0], tz, intent, min_block_minutes=cfg.min_block_minutes
        )

    print(section)
    if write:
        path = store.write_section(day, NIPPOU_MARKER, section)
        print(f"\n✅ 日報ドラフトをデイリーノートに書き込みました: {path}")


def cmd_prompts(cfg: Config, days: int, end_day: date, min_count: int) -> None:
    tz = ZoneInfo(cfg.timezone)
    end = datetime.combine(end_day, time.min, tzinfo=tz) + timedelta(days=1)
    start = end - timedelta(days=days)
    projects_dir = Path(cfg.aiwork.claude_projects_dir).expanduser()
    prompts = scan_user_prompts(projects_dir, start, end)
    print(render_prompt_report(prompts, days=days, min_count=min_count))


def cmd_experiment_new(cfg: Config, args: argparse.Namespace) -> None:
    tz = ZoneInfo(cfg.timezone)
    today = datetime.now(tz).date()
    deadline = today + timedelta(days=args.days)
    try:
        path = create_experiment(
            cfg.experiments_path,
            title=args.title,
            metric=args.metric,
            target=args.target,
            today=today,
            deadline=deadline,
            hypothesis=args.hypothesis,
        )
    except ExperimentError as e:
        raise SystemExit(f"❌ {e}")
    print(f"🧪 実験を起票しました: {path}")
    print(f"   指標: {args.metric} / 目標: {args.target} / 期限: {deadline.isoformat()}")
    print("   毎晩の kaizenlog generate が実測値を自動追記します。")


def cmd_experiment_list(cfg: Config) -> None:
    experiments = load_experiments(cfg.experiments_path)
    if not experiments:
        print(f"実験はまだありません（{cfg.experiments_path}）。")
        print("起票例: kaizenlog experiment new"
              " --title \"エンタメ30分以内\" --metric \"category_minutes:エンタメ\" --target \"<= 30\"")
        print("\n使える指標:")
        for name, desc in METRIC_DESCRIPTIONS.items():
            print(f"  {name:<28} {desc}")
        return
    for e in experiments:
        latest = ""
        if e.measurements:
            d = max(e.measurements)
            latest = f" / 直近 {d.strftime('%m/%d')}={e.measurements[d]:g}"
        print(f"[{e.status:>8}] {e.title} — {e.metric} {e.target_op} {e.target_value:g}"
              f"（期限 {e.deadline or '未設定'}{latest}）")


def cmd_skill(cfg: Config, args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser() if getattr(args, "vault", None) else Path(cfg.vault_dir).expanduser()

    if args.skill_command == "show":
        print("同梱されているClaude Codeスキル:")
        for name in bundled_skill_names():
            desc = skill_description(bundled_skill_content(name))
            print(f"  {name:<18} {desc}")
        print("\nインストール: kaizenlog skill install [--vault PATH] [--force]")
        return 0

    if args.skill_command == "doctor":
        if not vault.is_dir():
            print(f"❌ ボールトが存在しません: {vault}")
            return 1
        print(f"ボールト: {vault}")
        worst = 0
        for name in bundled_skill_names():
            status = check_skill(vault, name)
            if status.state == "up-to-date":
                print(f"✅ {name}: 最新です")
            elif status.state == "not-installed":
                print(f"⚠️  {name}: 未インストール（kaizenlog skill install で導入）")
                worst = max(worst, 0)
            else:
                print(f"⚠️  {name}: 同梱版と差分あり（更新 or ローカル改変）。"
                      "差分を確認して --force で更新できます")
        return worst

    # install
    if not vault.is_dir():
        print(f"❌ ボールトが存在しません: {vault}", file=sys.stderr)
        return 1
    rc = 0
    for name in bundled_skill_names():
        result, dest = install_skill(vault, name, force=args.force)
        if result == "installed":
            print(f"✅ {name}: インストールしました → {dest}")
        elif result == "unchanged":
            print(f"✅ {name}: 既に最新です")
        elif result == "overwritten":
            print(f"♻️  {name}: 上書きしました（元ファイルは {dest.with_suffix('.md.bak').name} に退避）")
        else:  # skipped
            print(f"⚠️  {name}: 既存ファイルと差分があるため上書きしませんでした。")
            d = diff_skill(vault, name)
            if d:
                print(d)
            print(f"   上書きする場合: kaizenlog skill install --force（{dest.name} は.bakに退避されます）")
            rc = 1
    return rc


def cmd_init_config() -> None:
    out = Path("kaizenlog.toml")
    if out.exists():
        raise SystemExit(f"{out} は既に存在します。上書きしたい場合は削除してから実行してください。")
    out.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    print(f"✅ 設定ファイルの雛形を作成しました: {out.resolve()}")


def _harden_console_encoding() -> None:
    """絵文字入りメッセージがcp932コンソール/パイプで UnicodeEncodeError にならないようにする。

    日本語Windowsではリダイレクト時のstdoutがcp932になり、✅/⚠️等の出力で
    クラッシュする（タスクスケジューラの夜間実行が典型）。表示できない文字は
    置換して、出力エラーで本処理を落とさない。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _harden_console_encoding()
    parser = argparse.ArgumentParser(prog="kaizenlog", description=__doc__)
    parser.add_argument("--config", help="設定ファイル（TOML）のパス")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "advise", "run"):
        p = sub.add_parser(name)
        p.add_argument("--date", help="対象日 YYYY-MM-DD（省略時は今日）")
        if name in ("advise", "run"):
            p.add_argument("--dry-run", action="store_true",
                           help="LLMに送る内容を表示するだけで送信・書き込みしない")
    bf = sub.add_parser("backfill", help="欠損日の日誌・統計をまとめて補完")
    bf.add_argument("--days", type=int, default=7, help="遡る日数（デフォルト7）")
    bf.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は今日）")
    sub.add_parser("status", help="実行履歴の確認")
    sub.add_parser("doctor", help="セットアップ診断")
    sk = sub.add_parser("skill", help="Claude Codeスキルの管理")
    sk_sub = sk.add_subparsers(dest="skill_command", required=True)
    sk_install = sk_sub.add_parser("install")
    sk_install.add_argument("--vault", help="インストール先ボールト（省略時はconfigのvault_dir）")
    sk_install.add_argument("--force", action="store_true",
                            help="既存スキルを.bakに退避した上で上書きする")
    sk_show = sk_sub.add_parser("show")
    sk_doctor = sk_sub.add_parser("doctor")
    sk_doctor.add_argument("--vault", help="確認先ボールト（省略時はconfigのvault_dir）")
    exp = sub.add_parser("experiment", help="カイゼン実験の起票・一覧")
    exp_sub = exp.add_subparsers(dest="exp_command", required=True)
    exp_new = exp_sub.add_parser("new")
    exp_new.add_argument("--title", required=True, help="実験タイトル")
    exp_new.add_argument("--metric", required=True,
                         help="追跡指標（kaizenlog experiment list で一覧表示）")
    exp_new.add_argument("--target", required=True, help='目標（例: "<= 15", ">= 120"）')
    exp_new.add_argument("--days", type=int, default=14, help="実験期間（日数、デフォルト14）")
    exp_new.add_argument("--hypothesis", default="", help="仮説（なぜ効くと考えるか）")
    exp_sub.add_parser("list")
    pat = sub.add_parser("patterns", help="繰り返しパターンの検出（自動化候補）")
    pat.add_argument("--days", type=int, default=14, help="遡る日数（デフォルト14）")
    pat.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は今日）")
    rep = sub.add_parser("report", help="提出用の日報ドラフトを生成")
    rep.add_argument("--date", help="対象日 YYYY-MM-DD（省略時は今日）")
    rep.add_argument("--no-llm", action="store_true", help="LLMを使わず事実ベースの箇条書きで生成")
    rep.add_argument("--write", action="store_true", help="デイリーノートにも書き込む")
    pr = sub.add_parser("prompts", help="Claude Codeへの繰り返し依頼の発掘")
    pr.add_argument("--days", type=int, default=14, help="遡る日数（デフォルト14）")
    pr.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は今日）")
    pr.add_argument("--min-count", type=int, default=3, help="レポートする最低反復回数（デフォルト3）")
    sub.add_parser("init-config")

    args = parser.parse_args(argv)

    # 古いWindowsコンソール（cp932）での絵文字・日本語の文字化けを防ぐ
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

    if args.command == "init-config":
        cmd_init_config()
        return 0

    cfg = load_config(args.config)

    if args.command == "status":
        print(render_status(load_runs(cfg.logs_path)))
        return 0

    if args.command == "skill":
        return cmd_skill(cfg, args)

    if args.command == "doctor":
        report, has_error = run_doctor(cfg, args.config)
        print(report)
        return 1 if has_error else 0

    if args.command == "backfill":
        tz = ZoneInfo(cfg.timezone)
        end_day = _parse_date(args.date, tz)
        cmd_backfill(cfg, args.days, end_day)
        return 0

    if args.command == "patterns":
        tz = ZoneInfo(cfg.timezone)
        end_day = _parse_date(args.date, tz)
        stats = load_stats(cfg.stats_path, args.days, end_day)
        print(render_patterns_markdown(stats))
        return 0

    if args.command == "report":
        tz = ZoneInfo(cfg.timezone)
        day = _parse_date(args.date, tz)
        try:
            cmd_report(cfg, day, use_llm=not args.no_llm, write=args.write)
        except AdvisorError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        return 0

    if args.command == "prompts":
        tz = ZoneInfo(cfg.timezone)
        end_day = _parse_date(args.date, tz)
        cmd_prompts(cfg, args.days, end_day, args.min_count)
        return 0

    if args.command == "experiment":
        if args.exp_command == "new":
            cmd_experiment_new(cfg, args)
        else:
            cmd_experiment_list(cfg)
        return 0

    tz = ZoneInfo(cfg.timezone)
    day = _parse_date(args.date, tz)
    dry_run = bool(getattr(args, "dry_run", False))

    start_time = monotonic()
    try:
        if args.command in ("generate", "run"):
            # 日付指定のない通常実行では、直近の欠損日を先に自動補完する
            if not args.date and cfg.auto_backfill_days > 0:
                if missing_days(cfg.stats_path, day, cfg.auto_backfill_days):
                    cmd_backfill(cfg, cfg.auto_backfill_days, day)
            if not dry_run:
                cmd_generate(cfg, day)
        if args.command in ("advise", "run"):
            cmd_advise(cfg, day, dry_run=dry_run)
    except (ActivityWatchError, AdvisorError, PrivacyError) as e:
        print(f"❌ {e}", file=sys.stderr)
        if not dry_run:
            log_run(cfg.logs_path, args.command, ok=False,
                    duration_seconds=monotonic() - start_time,
                    error=str(e), retention_days=cfg.log_retention_days)
            if cfg.notify_on_failure:
                notify("KaizenLog 失敗", f"{args.command}: {e}")
        return 1
    except Exception as e:
        # 想定外の例外でも「静かな故障」にしない：無人の夜間実行では
        # 実行ログへの記録と失敗通知が唯一の発覚経路になる
        traceback.print_exc()
        if not dry_run:
            log_run(cfg.logs_path, args.command, ok=False,
                    duration_seconds=monotonic() - start_time,
                    error=f"想定外のエラー: {e.__class__.__name__}: {e}",
                    retention_days=cfg.log_retention_days)
            if cfg.notify_on_failure:
                notify("KaizenLog 失敗", f"{args.command}: {e.__class__.__name__}: {e}")
        return 1
    if not dry_run:
        log_run(cfg.logs_path, args.command, ok=True,
                duration_seconds=monotonic() - start_time,
                retention_days=cfg.log_retention_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
