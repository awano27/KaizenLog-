"""kaizenlog コマンドラインインターフェース。

  kaizenlog generate [--date YYYY-MM-DD]   ログ収集→デイリーノート書き込み→実験の自動計測
  kaizenlog advise   [--date YYYY-MM-DD]   デイリーノートを読んでLLMの改善提案を追記する
  kaizenlog run      [--date YYYY-MM-DD]   generate + advise
  kaizenlog experiment new/list            カイゼン実験の起票・一覧
  kaizenlog patterns [--days N]            繰り返しパターンの検出レポート（自動化候補）
  kaizenlog report [--date] [--no-llm] [--write]  提出用の日報ドラフトを生成
  kaizenlog prompts [--days N] [--unhandled] [--roi]  繰り返し依頼の発掘＋ROI
  kaizenlog prompts mark <id> skilled|dismissed [--skill NAME]
  kaizenlog handoff [--target PATH ...] [--dry-run]  実測教訓を CLAUDE.md 等へ注入
  kaizenlog handoff roi [--suppress|--unsuppress|--promote ID]  申し送りROI
  kaizenlog coach [--dry-run] [--apply FILE]  コパイロット調教パック（承認制）
  kaizenlog abtest new|finish|status       パーソナル METR 実験
  kaizenlog excavate [--days N] [--write] [--card]  過去ログ発掘監査
  kaizenlog guard --hook|install|status    空転ブレーカー（Claude Code フック）
  kaizenlog backfill [--days N]            欠損日の日誌・統計をまとめて補完する
  kaizenlog status                         実行履歴（最終成功・直近の失敗）を表示
  kaizenlog doctor                         セットアップと環境の健全性を診断
  kaizenlog morning [--date]               朝の追いつき・アクション再描画・通知
  kaizenlog today  [--date]                今日の未完了アクションを表示
  kaizenlog goal   [\"目標 @カテゴリ\"]       今日の作業目標を設定/表示
  kaizenlog done   <id>                    アクションをターミナルから消化
  kaizenlog setup                          対話式セットアップウィザード
  kaizenlog init-config                    設定ファイルの雛形を出力する
"""

from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from time import monotonic
from pathlib import Path
from zoneinfo import ZoneInfo

from .advisor import (
    AdviceContractError,
    apply_internal_sentinel,
    AdviceResult,
    AdvisorError,
    generate_advice,
    prepare_advice_request,
    render_reader_advice,
    requires_daily_contract,
)
from .advice_evidence import build_advice_evidence
from .aiwork import (
    available_adapters,
    collect_ai_telemetry,
    compute_loop_tax,
    detect_retry_chains,
    format_loop_tax_line,
    render_aiwork_markdown,
)
from .doctor import run_doctor
from .memory import (
    ACTIONS_HANDOFF_DAYS,
    TODAY_CANDIDATE_CAP,
    append_entries,
    assign_action_ids,
    compute_action_stats,
    compute_streaks,
    format_today_action_line,
    load_entries,
    mark_entry_done,
    mark_entry_skipped,
    partition_open_actions,
    render_action_stats_line,
    render_actions_section,
    resolve_action_id,
    summarize_for_prompt,
    update_statuses_from_note,
)
from .nippou import generate_nippou_deterministic, generate_nippou_llm
from .notify import notify
from .privacy import PrivacyError, make_redactor
from .promptledger import (
    append_prompt_ledger,
    find_matching_entry,
    format_ledger_line,
    load_prompt_ledger,
    mark_prompt_entry,
    resolve_prm_id,
    upsert_clusters,
)
# redact→normalize は upsert と同じキーで台帳照合するため再利用
from .promptmine import cluster_prompts, render_prompt_report
from .runlog import (
    advise_health_warning_line,
    load_runs,
    log_advise_health,
    log_run,
    render_status,
)
from .skill_manager import (
    bundled_skill_content,
    bundled_skill_names,
    check_skill,
    diff_skill,
    install_skill,
    skill_description,
)
from .classifier import Classifier, known_category_names
from .collector import ActivityWatchClient, ActivityWatchError, collect_day, collect_input
from .focus import compute_input_stats
from .intervention import detect_time_sinks, render_leechblock_options, render_plan, suggest_rules
from .config import Config, ConfigError, find_config_file, load_config
from .experiments import (
    METRIC_DESCRIPTIONS,
    ExperimentError,
    baseline_median_from_stats,
    compute_metric,
    create_experiment,
    detect_regressions,
    format_effect_size,
    load_experiments,
    metric_from_stats,
    record_measurement,
    render_experiments_context,
    should_measure_experiment,
    target_met,
    weekday_baseline,
)
from .patterns import render_patterns_markdown
from .report import render_change_table, render_markdown, summarize
from .stats import activity_fingerprint, build_stats, load_stats, missing_days, write_stats
from .vault import (
    ACTIONS_MARKER,
    ACTIVITY_MARKER,
    ADVICE_MARKER,
    DailyNoteStore,
    atomic_write_text,
    extract_heading_section,
    extract_section,
)
from .verdict import (
    apply_verdicts_to_actions_note,
    apply_verdicts_to_advice_note,
    backfill_verdicts,
    judge_entries,
    parse_pass_condition,
)


def _parse_date(s: str | None, tz: ZoneInfo) -> date:
    if s:
        return date.fromisoformat(s)
    return datetime.now(tz).date()


def _safe_log_notify_failed(cfg: Config, context: str) -> None:
    """通知失敗を runlog に残す。二次例外で元の失敗処理を壊さない。"""
    try:
        log_run(
            cfg.logs_path,
            "notify",
            ok=False,
            duration_seconds=0.0,
            error=f"notify_failed: {context}"[:500],
            retention_days=cfg.log_retention_days,
            notify_failed=True,
        )
    except Exception:
        pass


def _notify(cfg: Config, title: str, message: str, **kwargs) -> bool | None:
    """notify の結果を返す。False のときだけ notify_failed を記録（None=非Windowsは記録しない）。"""
    result = notify(title, message, **kwargs)
    # False のみ失敗。None は送出未試行（スキップ）なので runlog に残さない
    if result is False:
        print(f"⚠️  Windows 通知の送出に失敗しました: {title}", file=sys.stderr)
        _safe_log_notify_failed(cfg, f"{title}: {message[:80]}")
    return result


def cmd_generate(
    cfg: Config,
    day: date,
    *,
    skip_verdict_ids: set[str] | None = None,
) -> Path:
    """日次 generate。

    skip_verdict_ids: 同一プロセスで retro-advise したばかりの KZN を判定から除外する。
    判定は「実行機会があった提案のみ」— 数秒前に生まれた提案に即 PASS/FAIL を付けない。
    """
    tz = ZoneInfo(cfg.timezone)
    day_start = datetime.combine(day, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    known_cats = known_category_names(cfg.rules)

    client = ActivityWatchClient(cfg.aw_base_url)
    events, afk_ok = collect_day(client, day_start, day_end)
    classified = Classifier(cfg.rules).classify_all(events)
    summary = summarize(day, classified, gap_minutes=cfg.session_gap_minutes)

    # aw-watcher-input 導入時のみ集中ブロックを算出（未導入ならNone）
    input_raw = collect_input(client, day_start, day_end)
    input_stats = (compute_input_stats(input_raw, day_start=day_start, day_end=day_end)
                   if input_raw is not None else None)

    section = render_markdown(summary, tz, min_block_minutes=cfg.min_block_minutes,
                              input_stats=input_stats)

    ai_sessions = []
    day_prompts = []
    day_retry_chains = []
    retry_chain_count: int | None = None
    pricing = cfg.aiwork.pricing or None
    from .privacy import make_redactor

    # 依頼抜粋は日誌・stats に載るため privacy redact を適用（画面タイトル原文方針の例外）
    title_redactor = make_redactor(
        cfg.privacy.redact_patterns, cfg.privacy.replacement
    )
    internal_ai_n = 0
    if cfg.aiwork.enabled:
        adapters = available_adapters(cfg)
        ai_sessions, day_prompts, internal_ai_n = collect_ai_telemetry(
            adapters, day_start, day_end
        )
        day_retry_chains = detect_retry_chains(day_prompts)
        retry_chain_count = len(day_retry_chains)
        loop_tax = compute_loop_tax(
            day_retry_chains, ai_sessions, pricing=pricing
        )
        try:
            from .guard import count_live_breaker_fires

            breaker_n = count_live_breaker_fires(
                cfg.memory_path, day, tz=tz
            )
        except Exception:
            breaker_n = 0
        aiwork_md = render_aiwork_markdown(
            ai_sessions,
            tz,
            retry_chain_count=retry_chain_count,
            pricing=pricing,
            session_titles=bool(getattr(cfg.aiwork, "session_titles", True)),
            redactor=title_redactor,
            retry_chains=day_retry_chains,
            internal_ai_sessions=internal_ai_n,
            usd_jpy=getattr(cfg.aiwork, "usd_jpy", None),
            loop_tax_summary=loop_tax,
            breaker_fires=breaker_n,
            screen_tool_minutes=summary.ai_tool_minutes,
        )
        if aiwork_md:
            section = section.rstrip() + "\n\n" + aiwork_md
        # ループ税閾値通知（閾値超過のみ。同額は発火しない）— _notify 経由で runlog に残す
        alert = getattr(cfg.aiwork, "loop_tax_alert_usd", None)
        if (
            alert is not None
            and loop_tax.est_cost_usd is not None
            and loop_tax.est_cost_usd > float(alert)
        ):
            _notify(
                cfg,
                "KaizenLog ループ税",
                format_loop_tax_line(
                    loop_tax, usd_jpy=getattr(cfg.aiwork, "usd_jpy", None)
                ),
                icon="Warning",
            )
    else:
        loop_tax = None

    today_stats = build_stats(
        day,
        summary,
        ai_sessions,
        input_stats,
        activity_md=None,
        retry_chains=day_retry_chains if cfg.aiwork.enabled else None,
        pricing=pricing,
        title_redactor=title_redactor if cfg.aiwork.enabled else None,
        internal_ai_sessions=internal_ai_n,
        loop_tax_summary=loop_tax,
    )
    previous_day = (day - timedelta(days=1)).isoformat()
    previous_stats = next(
        (
            item
            for item in load_stats(cfg.stats_path, days=2, end_day=day)
            if item.get("day") == previous_day
        ),
        None,
    )
    change_table = render_change_table(today_stats, previous_stats)
    if change_table:
        section = section.rstrip() + "\n\n" + change_table

    store = DailyNoteStore(cfg.daily_notes_path)
    path = store.write_section(day, ACTIVITY_MARKER, section)
    print(f"✅ Activity Log を書き込みました: {path}")
    print(f"   合計 {summary.total_minutes:.0f}分 / {len(summary.blocks)}ブロック"
          f" / AI関連画面ブロック {summary.ai_activity_blocks}回"
          f" / AIセッション {len(ai_sessions)}回")

    # 空転ブレーカー状態掃除（7日超過）
    try:
        from .guard import cleanup_old_states

        cleaned = cleanup_old_states(max_age_days=7)
        if cleaned:
            print(f"🧹 guard 状態ファイルを {cleaned} 件削除（7日超過）")
    except Exception:
        pass

    # 目標は goal コマンド専用区間。generate は読むだけ（書き換えない）
    from .goal import goal_stats_fields, read_goal

    note_after = store.read(day)
    day_goal = read_goal(note_after, known_cats)
    goal_text_stat, goal_cat_stat = goal_stats_fields(day_goal, title_redactor)

    # パターン検出用の機械可読な統計を蓄積する
    write_stats(
        cfg.stats_path,
        day,
        summary,
        ai_sessions,
        input_stats,
        activity_md=section,
        retry_chains=day_retry_chains if cfg.aiwork.enabled else None,
        afk_watcher_available=afk_ok,
        pricing=pricing,
        title_redactor=title_redactor if cfg.aiwork.enabled else None,
        goal_text=goal_text_stat,
        goal_category=goal_cat_stat,
        internal_ai_sessions=internal_ai_n,
        loop_tax_summary=loop_tax,
    )

    # 実験の実測追記: running 全件 + adopted（deadline から30日以内のみ）
    from .promptledger import representative_for_cluster_id
    from .promptmine import count_cluster_matches

    experiments = load_experiments(cfg.experiments_path)
    for exp in experiments:
        if not should_measure_experiment(exp, day):
            continue
        # prompt_cluster: compute_metric を汚さずループ側で件数算出
        if exp.metric.startswith("prompt_cluster:"):
            rep = None
            if exp.cluster_id:
                rep = representative_for_cluster_id(cfg.memory_path, exp.cluster_id)
                if not rep:
                    print(
                        f"⚠️  実験「{exp.title}」の cluster_id={exp.cluster_id} が"
                        "台帳に無いためスキップ"
                    )
                    continue
            elif exp.cluster_rep:
                rep = exp.cluster_rep
            else:
                print(
                    f"⚠️  実験「{exp.title}」の prompt_cluster に "
                    "cluster_id / cluster_rep が無いためスキップ"
                )
                continue
            value = float(count_cluster_matches(day_prompts, rep))
        else:
            value = compute_metric(
                exp.metric, summary, ai_sessions, input_stats,
                retry_chains=retry_chain_count,
                known_categories=known_cats,
            )
        if value is None:
            print(f"⚠️  実験「{exp.title}」の指標 {exp.metric} は不明のためスキップしました")
            continue
        # 同曜日基準: 実験開始前28日分（start 欠落時は計測日前日まで）
        if exp.start is not None:
            pre_end = exp.start - timedelta(days=1)
        else:
            pre_end = day - timedelta(days=1)
        pre_stats = load_stats(cfg.stats_path, days=28, end_day=pre_end)
        wb_map: dict = {}
        for d_m in list(exp.measurements) + [day]:
            wb_map[d_m] = weekday_baseline(exp.metric, d_m, pre_stats)
        met = record_measurement(exp, day, value, weekday_baselines=wb_map)
        mark = "✅" if met else "❌"
        wb_today = wb_map.get(day)
        wb_note = f" / 同曜日基準 {wb_today:g}" if wb_today is not None else ""
        print(
            f"🧪 実験「{exp.title}」: {value:g}（目標 {exp.target_op} {exp.target_value:g} {mark}{wb_note}）"
        )

    # adopted の退行検知（再読込して最新 measurements を反映）
    experiments = load_experiments(cfg.experiments_path)
    for exp in detect_regressions(experiments, window=7, as_of=day):
        recent = [
            (d, v) for d, v in exp.measurements.items()
            if day - timedelta(days=6) <= d <= day
        ]
        misses = sum(
            1 for _, v in recent
            if not target_met(v, exp.target_op, exp.target_value)
        )
        print(
            f"⚠️  退行検知: 「{exp.title}」が採用後に目標未達"
            f"（直近7日で{misses}/{len(recent)}日未達）。再実験を検討してください"
        )

    # 改善風化センチネル（stats 書き込み後・夜間 run に乗る）
    try:
        from .decay import run_decay_detection

        decay_fresh = run_decay_detection(
            cfg,
            as_of=day,
            prompts=day_prompts if cfg.aiwork.enabled else [],
            redactor=title_redactor,
        )
        for ev in decay_fresh:
            print(f"⚠️  風化: [{ev.kind}] {ev.ref_id} — {ev.detail}")
        if decay_fresh:
            _notify(
                cfg,
                "KaizenLog 風化",
                f"風化した改善: {len(decay_fresh)}件",
                icon="Warning",
            )
    except Exception as e:
        print(f"⚠️  風化検知をスキップ: {e}", file=sys.stderr)

    # コーチ効果検証台帳の夜間判定
    try:
        from .coachledger import (
            generate_rollback_proposal,
            judge_coach_entries,
        )

        coach_results = judge_coach_entries(
            cfg.memory_path,
            cfg.stats_path,
            as_of=day,
            redactor=title_redactor,
        )
        fail_notified = False
        for ce in coach_results:
            print(
                f"🎓 コーチ判定: {ce.id} → {ce.status}"
                f"（{ce.verdict_detail or ''}）"
            )
            if ce.status == "fail":
                rb = generate_rollback_proposal(
                    cfg.memory_path, ce, as_of=day
                )
                if rb is not None:
                    print(f"   ロールバック提案: {rb}")
                if not fail_notified:
                    _notify(
                        cfg,
                        "KaizenLog コーチFAIL",
                        f"{ce.id}: {ce.verdict_detail or 'FAIL'}",
                        icon="Warning",
                    )
                    fail_notified = True
    except Exception as e:
        print(f"⚠️  コーチ判定をスキップ: {e}", file=sys.stderr)

    # A1: 前日提案の PASS 機械判定 → Memory と前日ノートへ書き戻し
    memory_entries = load_entries(cfg.memory_path)
    proposal_day = day - timedelta(days=1)
    judged = judge_entries(
        memory_entries, proposal_day, summary, ai_sessions, input_stats, day,
        retry_chains=retry_chain_count,
        known_categories=known_cats,
        today=datetime.now(ZoneInfo(cfg.timezone)).date(),
    )
    if skip_verdict_ids:
        judged = [e for e in judged if e.id not in skip_verdict_ids]
    if judged:
        append_entries(cfg.memory_path, judged)
        for entry in judged:
            parsed = parse_pass_condition(entry.action)
            if not parsed:
                continue
            metric, op, target_value = parsed
            provisional = entry.verdict_stage == "provisional"
            mark = "⏳" if provisional else ("✅" if entry.verdict == "pass" else "❌")
            label = "途中値" if provisional else "実測"
            print(
                f"🧪 アクション判定: {entry.id} {mark}"
                f"（{label} {entry.verdict_value:g} / 目標 {metric} {op} {target_value:g}）"
            )
        prev_note = store.read(proposal_day)
        if prev_note is not None:
            updated = apply_verdicts_to_advice_note(prev_note, judged)
            if updated is not None:
                atomic_write_text(store.path_for(proposal_day), updated)

    # A1b: 遅延 PASS バックフィル（提案翌日に stats が無かった行を後追い）
    # 測定日 = done_date+1（行動効果）/ 無ければ提案日+1（提案妥当性）
    by_id = {e.id: e for e in memory_entries}
    by_id.update({e.id: e for e in judged})
    bf = backfill_verdicts(
        list(by_id.values()),
        cfg.stats_path,
        day,
        known_categories=known_cats,
    )
    print(bf.log_line())
    # 無言スキップ可視化: コンソールに加え runlog にも1行（二次失敗は握り潰す）
    try:
        log_run(
            cfg.logs_path,
            "verdict_backfill",
            ok=True,
            duration_seconds=0.0,
            note=bf.log_line(),
            retention_days=cfg.log_retention_days,
        )
    except Exception:
        pass
    if bf.judged:
        append_entries(cfg.memory_path, bf.judged)
        for entry in bf.judged:
            by_id[entry.id] = entry
            provisional = entry.verdict_stage == "provisional"
            mark = "⏳" if provisional else ("✅" if entry.verdict == "pass" else "❌")
            label = "途中値" if provisional else "実測"
            print(
                f"🧪 バックフィル判定: {entry.id} {mark}"
                f"（{label} {entry.verdict_value:g} / 判定日 {entry.verdict_date}）"
            )
            # 提案日ノートへ注記（冪等）
            try:
                prop_day = date.fromisoformat(entry.date)
            except ValueError:
                continue
            prev_note = store.read(prop_day)
            if prev_note is not None:
                updated = apply_verdicts_to_advice_note(prev_note, [entry])
                if updated is not None:
                    atomic_write_text(store.path_for(prop_day), updated)

    # C3: 判定結果が更新された測定日自身の ACTIONS を再同期する。
    # backfill の as_of（実行日）ではなく各 entry.verdict_date をキーにする。
    _resync_measurement_day_actions(
        cfg,
        store,
        [*judged, *bf.judged],
        today=datetime.now(ZoneInfo(cfg.timezone)).date(),
    )

    # A2: 翌日ノートへ未完了アクションを転記（backfill で過去日を汚さない）
    _write_actions_handoff(cfg, store, day, list(by_id.values()))
    return path


def _resync_measurement_day_actions(
    cfg: Config,
    store: DailyNoteStore,
    updates: list,
    *,
    today: date,
) -> None:
    """判定が更新された測定日ノートの既存 ACTIONS だけを再同期する。

    過去ノートへ新しい区間を作らず、直近の handoff 窓内にある既存区間の
    対象 IDだけを更新する。複数経路の同一IDは後勝ちにして、JSONLの表示
    契約（同一ID後勝ち）と揃える。
    """
    latest_by_id = {entry.id: entry for entry in updates if entry.id}
    if not latest_by_id:
        return
    start_tag = f"<!-- {ACTIONS_MARKER}:start -->"
    end_tag = f"<!-- {ACTIONS_MARKER}:end -->"
    window_start = today - timedelta(days=ACTIONS_HANDOFF_DAYS)
    by_measurement_day: dict[date, list] = {}
    for entry in latest_by_id.values():
        if entry.verdict not in ("pass", "fail") or not entry.verdict_date:
            continue
        try:
            measurement_day = date.fromisoformat(entry.verdict_date)
        except ValueError:
            continue
        if measurement_day < window_start or measurement_day > today:
            continue
        by_measurement_day.setdefault(measurement_day, []).append(entry)

    for measurement_day, day_updates in sorted(by_measurement_day.items()):
        note = store.read(measurement_day)
        if note is None:
            continue
        start_idx = note.find(start_tag)
        end_idx = note.find(end_tag, start_idx + len(start_tag)) if start_idx >= 0 else -1
        if start_idx < 0 or end_idx < 0:
            continue
        updated = apply_verdicts_to_actions_note(note, day_updates)
        if updated is None:
            continue
        path = store.path_for(measurement_day)
        atomic_write_text(path, updated)
        print(f"📌 判定日 ACTIONS を再同期しました: {path}")


def _write_actions_handoff(
    cfg: Config, store: DailyNoteStore, day: date, entries: list,
) -> None:
    """target=day+1 が今日以降のときだけ ACTIONS セクションを書く。"""
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    target = day + timedelta(days=1)
    if target < today:
        return
    section = render_actions_section(entries, target, store.read(target))
    if not section:
        return
    path = store.write_section(
        target, ACTIONS_MARKER, section, position=cfg.actions_position
    )
    print(f"📌 今日のアクションを転記しました: {path}")


# catch-up 失敗ログに提案本文・活動タイトル・プロンプトを残さない
def _safe_catchup_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}"


@dataclass
class CatchUpResult:
    """追いつき各ステップの結果（利用者向け部分成功表示と run の判定除外用）。"""

    generate: str = "not-needed"  # not-needed | succeeded | failed
    advise: str = "not-needed"  # not-needed | skipped | succeeded | failed
    new_ids: set[str] = field(default_factory=set)
    # (step, safe_summary) — 本文・プロンプト・活動タイトルは入れない
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return bool(self.failures)


def catch_up_yesterday(cfg: Config, today: date) -> CatchUpResult:
    """前夜に走らなかった日の追いつき（昨日のみ）。

    1) 昨日の stats が無ければ generate（一昨日分の判定も generate 内で走る）
    2) ACTIVITY あり ADVICE なしなら advise（retro-advise: 今日向けアクションになる）
    失敗は警告＋runlog に留め、呼び出し元を止めない。

    戻り値: CatchUpResult（new_ids は retro-advise で新規作成した KZN ID。
    同一プロセスの直後 generate で判定除外に使う）。
    """
    yesterday = today - timedelta(days=1)
    result = CatchUpResult()
    # generate: stats 欠損時のみ
    try:
        if missing_days(cfg.stats_path, today, 1) == [yesterday]:
            print(f"🔄 追いつき: {yesterday.isoformat()} の generate を実行します")
            t0 = monotonic()
            try:
                cmd_generate(cfg, yesterday)
                result.generate = "succeeded"
                log_run(
                    cfg.logs_path, "generate", ok=True,
                    duration_seconds=monotonic() - t0,
                    retention_days=cfg.log_retention_days,
                )
            except Exception as e:
                result.generate = "failed"
                safe = _safe_catchup_error(e)
                result.failures.append(("generate", safe))
                print(f"⚠️  追いつき generate 失敗: {safe}", file=sys.stderr)
                log_run(
                    cfg.logs_path, "generate", ok=False,
                    duration_seconds=monotonic() - t0,
                    error=f"catch-up generate: {safe}",
                    retention_days=cfg.log_retention_days,
                )
    except Exception as e:
        result.generate = "failed"
        safe = _safe_catchup_error(e)
        result.failures.append(("generate", safe))
        print(f"⚠️  追いつき generate 判定に失敗: {safe}", file=sys.stderr)

    # retro-advise: ACTIVITY あり・ADVICE なしのときだけ（今日向けアクションが価値あり）
    if cfg.llm.backend == "none":
        result.advise = "skipped"
        return result
    try:
        store = DailyNoteStore(cfg.daily_notes_path)
        note = store.read(yesterday)
        if not note:
            return result
        if extract_section(note, ACTIVITY_MARKER) is None:
            return result
        if extract_section(note, ADVICE_MARKER) is not None:
            return result
        print(f"🔄 追いつき: {yesterday.isoformat()} の advise を実行します")
        before_ids = {e.id for e in load_entries(cfg.memory_path)}
        t0 = monotonic()
        try:
            # AdvisorError 含め例外は握り、追加リトライせず当日処理へ（主 advise 優先）
            cmd_advise(cfg, yesterday, dry_run=False)
            result.advise = "succeeded"
            log_run(
                cfg.logs_path, "advise", ok=True,
                duration_seconds=monotonic() - t0,
                retention_days=cfg.log_retention_days,
            )
            yiso = yesterday.isoformat()
            for e in load_entries(cfg.memory_path):
                if e.id not in before_ids and e.date == yiso:
                    result.new_ids.add(e.id)
        except Exception as e:
            result.advise = "failed"
            safe = _safe_catchup_error(e)
            result.failures.append(("advise", safe))
            print(f"⚠️  追いつき advise 失敗: {safe}", file=sys.stderr)
            log_run(
                cfg.logs_path, "advise", ok=False,
                duration_seconds=monotonic() - t0,
                error=f"catch-up advise: {safe}",
                retention_days=cfg.log_retention_days,
            )
    except Exception as e:
        result.advise = "failed"
        safe = _safe_catchup_error(e)
        result.failures.append(("advise", safe))
        print(f"⚠️  追いつき advise 判定に失敗: {safe}", file=sys.stderr)
    return result


def build_morning_notification(
    entries: list,
    today: date,
    *,
    health_line: str | None = None,
    catch_up_failures: list[tuple[str, str]] | None = None,
) -> str | None:
    """朝トースト本文。アクション本文は載せない（ロック画面に固有名詞を出さない）。"""
    buckets = partition_open_actions(entries, today, recent_include_today=True)
    candidate_n = min(TODAY_CANDIDATE_CAP, len(buckets.recent))
    hold_n = max(0, buckets.total - candidate_n)
    yday = (today - timedelta(days=1)).isoformat()
    # 昨日の判定は実行済みを主指標に（未実行 PASS は別掲）
    done_pass = sum(
        1
        for e in entries
        if e.status == "done"
        and e.verdict_date == yday
        and e.verdict == "pass"
        and e.verdict_stage == "confirmed"
    )
    done_fail = sum(
        1
        for e in entries
        if e.status == "done"
        and e.verdict_date == yday
        and e.verdict == "fail"
        and e.verdict_stage == "confirmed"
    )
    undone_pass = sum(
        1
        for e in entries
        if e.status == "proposed"
        and e.verdict_date == yday
        and e.verdict == "pass"
        and e.verdict_stage == "confirmed"
    )
    parts: list[str] = []
    streaks = compute_streaks(entries, today)
    if candidate_n or hold_n or done_pass or done_fail or undone_pass or streaks.current:
        judge = f"昨日の判定 実行済み✅{done_pass} ❌{done_fail}"
        if undone_pass:
            judge += f"（未実行のままPASS到達 {undone_pass}件）"
        streak_s = f" / 🔥{streaks.current}日" if streaks.current >= 1 else ""
        parts.append(
            f"今日の候補 {candidate_n}件 / 保留 {hold_n}件 / {judge}{streak_s}"
        )
    if catch_up_failures:
        failed_steps = sorted({step for step, _ in catch_up_failures})
        for step in failed_steps:
            parts.append(f"⚠ 昨日の追いつきは未完了（{step}）")
    # 昨夜 degraded/failed のときだけ（提案本文・違反内容は載せない）
    if health_line:
        parts.append(health_line)
    if not parts:
        return None
    return " / ".join(parts) if len(parts) == 1 else "\n".join(parts)


def cmd_morning(cfg: Config, day: date, *, skip_catch_up: bool = False) -> int:
    """朝の到達性: 追いつき → 📌 再描画 → 通知。"""
    t0 = monotonic()
    try:
        if skip_catch_up:
            catch_up = CatchUpResult(generate="not-needed", advise="skipped")
        else:
            catch_up = catch_up_yesterday(cfg, day)
        store = DailyNoteStore(cfg.daily_notes_path)
        entries = load_entries(cfg.memory_path)
        section = render_actions_section(entries, day, store.read(day))
        if section:
            path = store.write_section(
                day, ACTIONS_MARKER, section, position=cfg.actions_position
            )
            print(f"📌 今日のアクションを更新しました: {path}")
        else:
            print("📌 今日の未完了アクションはありません")

        if catch_up.has_failures:
            for step, _safe in catch_up.failures:
                print(f"⚠ 昨日の追いつきは未完了（{step}）")

        health = advise_health_warning_line(load_runs(cfg.logs_path))
        msg = build_morning_notification(
            entries,
            day,
            health_line=health,
            catch_up_failures=catch_up.failures or None,
        )
        if msg:
            print(msg)
            # 本文は件数のみ（固有名詞をロック画面に出さない）
            _notify(cfg, "KaizenLog 朝の確認", msg, icon="Information")
        note = None
        if catch_up.has_failures:
            steps = sorted({s for s, _ in catch_up.failures})
            note = "catch-up incomplete: " + ",".join(steps)
        log_run(
            cfg.logs_path, "morning", ok=True,
            duration_seconds=monotonic() - t0,
            retention_days=cfg.log_retention_days,
            partial=catch_up.has_failures,
            note=note,
        )
        return 0
    except Exception as e:
        traceback.print_exc()
        log_run(
            cfg.logs_path, "morning", ok=False,
            duration_seconds=monotonic() - t0,
            error=str(e),
            retention_days=cfg.log_retention_days,
        )
        if cfg.notify_on_failure:
            _notify(cfg, "KaizenLog 失敗", f"morning: {e}")
        return 1


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


def cmd_block(cfg: Config, end_day: date, days: int, min_minutes: float,
              write: bool, out: str | None) -> int:
    """時間泥棒を検出し、LeechBlock NGのブロックルールと効果測定実験を生成する。"""
    stats_list = load_stats(cfg.stats_path, days, end_day)
    if not stats_list:
        print("❌ 日次統計がまだありません。まず `kaizenlog generate` を数日分実行してください"
              "（過去分は `kaizenlog backfill`）。", file=sys.stderr)
        return 1

    sinks = detect_time_sinks(stats_list, cfg.rules, min_avg_minutes=min_minutes)
    rules = suggest_rules(sinks)
    print(render_plan(sinks, rules))
    if not rules:
        return 0
    if not write:
        print("\n👉 適用するには: kaizenlog block --write")
        return 0

    # 1) LeechBlock インポートファイル（適用は人間がブラウザでインポートする）
    out_path = Path(out) if out else cfg.stats_path.parent / "leechblock-options.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_leechblock_options(rules), encoding="utf-8")
    print(f"\n✅ ブロックルールを書き出しました: {out_path}")

    # 2) 効果測定のカイゼン実験を起票（毎晩の generate が自動計測）
    for rule in rules:
        title = f"介入 {rule.set_name.removeprefix('KZN: ')}"
        baseline = _resolve_pre_start_baseline(cfg, rule.metric, end_day)
        try:
            path = create_experiment(
                cfg.experiments_path, title, rule.metric, rule.target,
                today=end_day, deadline=end_day + timedelta(days=14),
                hypothesis=(
                    f"{rule.evidence}。LeechBlockの制限で目標まで下げられるはず。"
                    "PCブロックはスマホ等への移行（風船効果）を測定できない。"
                ),
                baseline=baseline,
            )
            msg = f"🧪 実験を起票: {path.name}（{rule.metric} {rule.target}）"
            if baseline is not None:
                msg += f" / baseline {baseline:g}"
            print(msg)
        except ExperimentError as e:
            print(f"⚠️  実験の起票をスキップ: {e}")

    print("\n👉 次の手順（人間の承認ゲート）:")
    print("   1. ブラウザの LeechBlock NG → Options → Import Options で上記ファイルを選択")
    print("   2. 適用後は毎晩の kaizenlog run が効果を自動計測します")
    print("   3. 2週間後の週次レビューが採用/棄却を判定します")
    return 0


def _is_valid_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def cmd_advise(cfg: Config, day: date, dry_run: bool = False) -> Path | None:
    store = DailyNoteStore(cfg.daily_notes_path)
    content = store.read(day)
    # SystemExitはBaseException派生でmain()のexcept Exceptionを素通りし、
    # 実行ログ・失敗通知なしに死ぬ（夜間実行では発覚経路が消える）。AdvisorErrorで投げる
    if content is None:
        raise AdvisorError(
            f"デイリーノートがありません: {store.path_for(day)}\n"
            "先に `kaizenlog generate` を実行してください。"
        )
    activity_md = extract_section(content, ACTIVITY_MARKER)
    if activity_md is None:
        raise AdvisorError("Activity Log セクションがありません。先に `kaizenlog generate` を実行してください。")

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

    # Kaizen Memory: ノートのチェックボックスから done を検出する。
    # 提案は (1) 提案日ノートの ADVICE、(2) 転記先ノートの 📌 の両方に現れうる。
    # 転記ウィンドウ（直近 ACTIONS_HANDOFF_DAYS 日）を常に走査し、
    # さらに未完了エントリの提案日（窓外の遅れチェック用）も足す。
    entries = load_entries(cfg.memory_path)
    scan_days = {day} | {
        day - timedelta(days=i) for i in range(1, ACTIONS_HANDOFF_DAYS + 1)
    } | {
        date.fromisoformat(e.date) for e in entries
        if e.status == "proposed" and _is_valid_date(e.date)
    }
    status_updates = []
    for scan_day in sorted(scan_days, reverse=True):
        past = store.read(scan_day)
        if past:
            status_updates.extend(update_statuses_from_note(past, entries, day))
    # 同じIDが複数ノートに現れても更新は1件に畳む。チェック済みはユーザーが
    # 既に確定した事実なので、新しいLLM提案の成否とは独立して永続化する。
    status_updates = list({entry.id: entry for entry in status_updates}.values())
    effective_by_id = {entry.id: entry for entry in entries}
    effective_by_id.update({entry.id: entry for entry in status_updates})
    effective_entries = sorted(effective_by_id.values(), key=lambda entry: entry.id)
    if status_updates and not dry_run:
        append_entries(cfg.memory_path, status_updates)
        print("📗 完了アクションを記録しました: "
              + ", ".join(entry.id for entry in status_updates))
    memory_ctx = summarize_for_prompt(effective_entries, day)
    action_stats = compute_action_stats(effective_entries, day)
    reflections = _extract_reflections(content)

    # 人間向けMarkdownだけでは測定の意味が曖昧になるため、当日の統計JSONから
    # 確定事実と測定限界を作り、ログ本文より優先するコンテキストとして渡す。
    stats_history = load_stats(
        cfg.stats_path, days=max(1, cfg.llm.lookback_days + 1), end_day=day
    )
    current_stats = next(
        (item for item in reversed(stats_history) if item.get("day") == day.isoformat()),
        None,
    )
    prior_stats = [item for item in stats_history if item.get("day") != day.isoformat()]
    source_status = "missing"
    if current_stats is not None:
        stored_fingerprint = current_stats.get("activity_sha256")
        if isinstance(stored_fingerprint, str):
            if stored_fingerprint == activity_fingerprint(activity_md):
                source_status = "verified"
            else:
                # 同日再生成の途中失敗などで日誌と統計が別runなら、古い統計を優先しない。
                current_stats = None
                source_status = "mismatch"
        else:
            source_status = "unverified"
    try:
        from .decay import load_decay_events

        decay_for_f17 = load_decay_events(
            cfg.memory_path, window_days=7, as_of=day
        )
    except Exception:
        decay_for_f17 = []
    try:
        from .coachledger import load_coach_ledger

        coach_for_f18 = load_coach_ledger(cfg.memory_path)
    except Exception:
        coach_for_f18 = []
    evidence_ctx = build_advice_evidence(
        current_stats,
        prior_stats,
        timezone=ZoneInfo(cfg.timezone),
        source_status=source_status,
        known_categories=known_category_names(cfg.rules),
        action_stats=action_stats,
        decay_events=decay_for_f17,
        coach_entries=coach_for_f18,
    )

    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)

    if dry_run:
        # LLMに送る内容を送信せずに表示する（送信内容の監査用。マスク適用後を表示）
        system_prompt, prompt, _ = prepare_advice_request(
            cfg.llm,
            activity_md,
            recent,
            intent,
            experiments_ctx or None,
            memory_ctx or None,
            redactor,
            evidence_ctx,
            reflections=reflections,
        )
        # dry-run / 本実行同一: generate_text と同じセンチネル規則を表示にも適用
        system_prompt = apply_internal_sentinel(system_prompt, cfg.llm.backend)
        print("===== system prompt =====")
        print(system_prompt)
        print("===== user prompt =====")
        print(prompt)
        print("=====")
        print("（--dry-run のためLLMには送信していません。ノートも変更していません）")
        return None

    t_advise = monotonic()
    outcome = "ok"
    violations: list[str] = []
    try:
        result = generate_advice(
            cfg.llm, activity_md, recent,
            intent=intent,
            experiments=experiments_ctx or None,
            memory=memory_ctx or None,
            reflections=reflections,
            evidence=evidence_ctx,
            redactor=redactor,
        )
        if isinstance(result, AdviceResult):
            advice_md = result.markdown
            outcome = result.outcome
            violations = list(result.violations)
        else:
            # 後方互換（モックが str を返すテスト）
            advice_md = str(result)
        # 日次契約プロンプトだけ reader 向け再構成。weekly / 自作プロンプトは素通し
        # （render_reader_advice は「明日の最小アクション」前提で全文を組み直すため）
        if requires_daily_contract(cfg.llm):
            advice_md = render_reader_advice(advice_md, evidence_ctx)
    except AdviceContractError as e:
        # 契約違反でも確定事実サマリーだけは残す（静かな失敗を防ぐ）。例外は再送出。
        store.write_section(day, ADVICE_MARKER, _degraded_advice_section(evidence_ctx))
        print("⚠️  出力契約を満たせなかったため縮退セクションを保存しました")
        _safe_log_advise_health(
            cfg,
            day=day,
            outcome="degraded",
            duration_seconds=monotonic() - t_advise,
            violations=getattr(e, "violations", None) or [str(e)],
        )
        raise
    except Exception:
        _safe_log_advise_health(
            cfg,
            day=day,
            outcome="failed",
            duration_seconds=monotonic() - t_advise,
            violations=["exception"],
        )
        raise
    # 安定ID（KZN-YYYYMMDD-NNN）を付与して記録する
    advice_md, new_entries = assign_action_ids(advice_md, day, effective_entries)
    path = store.write_section(day, ADVICE_MARKER, advice_md)
    append_entries(cfg.memory_path, new_entries)
    print(f"✅ 改善提案を書き込みました: {path}")
    proposed_entries = [entry for entry in new_entries if entry.status == "proposed"]
    if proposed_entries:
        print("🆔 アクションID: " + ", ".join(e.id for e in proposed_entries))
    # A2: ID 採番後の最新集合で翌日へ転記（dry_run ではここまで来ない）
    merged = {e.id: e for e in effective_entries}
    merged.update({e.id: e for e in new_entries})
    _write_actions_handoff(cfg, store, day, list(merged.values()))
    _safe_log_advise_health(
        cfg,
        day=day,
        outcome=outcome,
        duration_seconds=monotonic() - t_advise,
        violations=violations,
    )
    return path


def _safe_log_advise_health(
    cfg: Config,
    *,
    day: date,
    outcome: str,
    duration_seconds: float,
    violations: list[str] | None = None,
) -> None:
    """ヘルス記録の失敗で本処理を落とさない。"""
    try:
        log_advise_health(
            cfg.logs_path,
            day=day,
            backend=getattr(cfg.llm, "backend", "") or "",
            outcome=outcome,
            duration_seconds=duration_seconds,
            violations=violations,
            retention_days=cfg.log_retention_days,
        )
    except Exception:
        pass


def _sync_checkbox_statuses(cfg: Config, day: date) -> tuple[list, int]:
    """ノートの [x] を Memory に反映してから表示する（表示が古いと信頼を失う）。

    cmd_advise と同じ走査幅（当日 + 転記窓 + 未完了提案日）。
    戻り値: (統合後 entries, 更新件数)
    """
    store = DailyNoteStore(cfg.daily_notes_path)
    entries = load_entries(cfg.memory_path)
    scan_days = {day} | {
        day - timedelta(days=i) for i in range(1, ACTIONS_HANDOFF_DAYS + 1)
    } | {
        date.fromisoformat(e.date)
        for e in entries
        if e.status == "proposed" and _is_valid_date(e.date)
    }
    status_updates: list = []
    for scan_day in sorted(scan_days, reverse=True):
        past = store.read(scan_day)
        if past:
            status_updates.extend(update_statuses_from_note(past, entries, day))
    status_updates = list({entry.id: entry for entry in status_updates}.values())
    if status_updates:
        append_entries(cfg.memory_path, status_updates)
    by_id = {e.id: e for e in entries}
    by_id.update({e.id: e for e in status_updates})
    return sorted(by_id.values(), key=lambda e: e.id), len(status_updates)


def cmd_goal(cfg: Config, day: date, goal_arg: str | None) -> int:
    """今日の作業目標を設定/表示する（goal マーカー区間の唯一の書き手）。"""
    from .goal import format_goal_section, read_goal, write_goal

    known = known_category_names(cfg.rules)
    store = DailyNoteStore(cfg.daily_notes_path)
    if not goal_arg or not str(goal_arg).strip():
        content = store.read(day)
        g = read_goal(content, known)
        if g is None:
            print("🎯 目標: 未設定（`kaizenlog goal \"...\"` で設定）")
            return 0
        cat = f" @{g.category}" if g.category else ""
        print(f"🎯 今日の目標: {g.text}{cat}")
        return 0
    try:
        path, g = write_goal(
            cfg.daily_notes_path,
            day,
            goal_arg,
            known_categories=known,
        )
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    cat = f" @{g.category}" if g.category else ""
    print(f"✅ 目標を書き込みました: {path}")
    print(f"🎯 今日の目標: {g.text}{cat}")
    return 0


def cmd_today(
    cfg: Config,
    day: date,
    *,
    no_sync: bool = False,
    show_all: bool = False,
) -> int:
    """ターミナルから今日の未完了アクションを見る。"""
    # 目標行は未設定でも必ず1行（毎朝の想起）
    from .goal import read_goal

    known = known_category_names(cfg.rules)
    store = DailyNoteStore(cfg.daily_notes_path)
    g = read_goal(store.read(day), known)
    if g is None:
        print("🎯 目標: 未設定（`kaizenlog goal \"...\"` で設定）")
    else:
        cat = f" @{g.category}" if g.category else ""
        print(f"🎯 今日の目標: {g.text}{cat}")

    if no_sync:
        entries = load_entries(cfg.memory_path)
        print("ℹ 同期せずに表示しています")
    else:
        entries, synced = _sync_checkbox_statuses(cfg, day)
        if synced:
            print(f"↻ ノートのチェック状態をMemoryへ同期しました: {synced}件")
    runs = load_runs(cfg.logs_path)
    health = advise_health_warning_line(runs)
    if health:
        print(health)
    stats = compute_action_stats(entries, day)
    print(render_action_stats_line(stats, streaks=compute_streaks(entries, day)))
    buckets = partition_open_actions(entries, day, recent_include_today=True)
    if buckets.total == 0:
        print("未完了のアクションはありません")
        return 0
    print()
    if show_all:
        if buckets.recent:
            print(f"## 直近7日（{len(buckets.recent)}件）")
            for e in buckets.recent:
                print(format_today_action_line(e))
            print()
        if buckets.stale:
            print(f"## 8〜30日前（{len(buckets.stale)}件）")
            for e in buckets.stale:
                print(format_today_action_line(e))
            print()
        if buckets.older:
            print(f"## 31日以上（{len(buckets.older)}件）")
            for e in buckets.older:
                print(format_today_action_line(e))
        return 0

    # 既定: 今日の候補（recent 先頭 max 3）+ 残件数
    candidates = buckets.recent[:TODAY_CANDIDATE_CAP]
    rest_recent = max(0, len(buckets.recent) - len(candidates))
    if candidates:
        print(f"今日の候補 {len(candidates)}件")
        for e in candidates:
            print(format_today_action_line(e))
        print()
        print(
            f"ほか直近7日の未完了 {rest_recent}件"
            f" / 8〜30日前 {len(buckets.stale)}件"
            f" / 31日以上 {len(buckets.older)}件"
        )
        print("全件表示: `kaizenlog today --all`")
    else:
        hold = len(buckets.stale) + len(buckets.older)
        print(f"今日の候補なし。保留 {hold}件")
        print(
            f"ほか直近7日の未完了 0件"
            f" / 8〜30日前 {len(buckets.stale)}件"
            f" / 31日以上 {len(buckets.older)}件"
        )
        print("全件表示: `kaizenlog today --all`")
    return 0


def cmd_skip(cfg: Config, action_id: str, reason: str | None = None) -> int:
    """アクションをスキップ（拒否）として記録する。消化率分母から外す。"""
    entries = load_entries(cfg.memory_path)
    resolved = resolve_action_id(action_id, entries)
    if resolved is None:
        print(f"❌ 該当するアクションがありません: {action_id}", file=sys.stderr)
        return 1
    if isinstance(resolved, list):
        print("❌ ID が曖昧です。候補:", file=sys.stderr)
        for e in resolved:
            print(f"  {e.id}  {e.action[:60]}", file=sys.stderr)
        return 1
    entry = resolved
    if entry.status == "skipped":
        print(f"ℹ️  既にスキップ済みです: {entry.id}")
        return 0
    if entry.status == "done":
        print(f"ℹ️  既に消化済みです（スキップしません）: {entry.id}")
        return 0
    skipped = mark_entry_skipped(entry, reason=reason)
    append_entries(cfg.memory_path, [skipped])
    r = f" 理由: {reason}" if reason else ""
    print(f"⏭  スキップしました: {entry.id}{r}")
    return 0


def cmd_done(cfg: Config, action_id: str, day: date) -> int:
    """ターミナルからアクションを消化する。"""
    entries = load_entries(cfg.memory_path)
    resolved = resolve_action_id(action_id, entries)
    if resolved is None:
        print(f"❌ 該当するアクションがありません: {action_id}", file=sys.stderr)
        return 1
    if isinstance(resolved, list):
        print("❌ ID が曖昧です。候補:", file=sys.stderr)
        for e in resolved:
            print(f"  {e.id}  {e.action[:60]}", file=sys.stderr)
        return 1
    entry = resolved
    if entry.status == "done":
        # 再 done で done_date を上書きしない（M2 測定基準日 = 最初の done_date+1 が正）
        done_label = entry.done_date or "?"
        print(f"ℹ️  既に消化済みです（done_date: {done_label}）: {entry.id}")
        return 0
    done = mark_entry_done(entry, day)
    append_entries(cfg.memory_path, [done])
    # 当日ノートの 📌 を再描画（無ければ Memory のみ）
    store = DailyNoteStore(cfg.daily_notes_path)
    note = store.read(day)
    if note is None:
        print(f"⚠️  当日ノートが無いためノート同期をスキップしました: {store.path_for(day)}")
    else:
        merged = {e.id: e for e in load_entries(cfg.memory_path)}
        section = render_actions_section(list(merged.values()), day, note)
        if section:
            store.write_section(
                day, ACTIONS_MARKER, section, position=cfg.actions_position
            )
    stats = compute_action_stats(load_entries(cfg.memory_path), day)
    rate = (
        f"{round(stats.done_rate * 100)}%"
        if stats.done_rate is not None
        else "-"
    )
    print(f"✅ 消化しました: {done.id}")
    print(f"   {done.action}")
    print(f"   消化率 {rate} に上がりました（直近{stats.window_days}日 {stats.done}/{stats.proposed}）")
    return 0


def _degraded_advice_section(evidence_ctx) -> str:
    """契約違反時に ADVICE 区間へ書く縮退 Markdown（KZN/チェックボックスなし）。"""
    lines = [
        "## 🚀 Kaizen（AIからの改善提案）",
        "",
        "⚠️ 本日は提案の生成が出力契約を満たさず、保存できませんでした"
        "（詳細は `kaizenlog status`）。",
        "以下は当日の確定事実サマリーです。",
    ]
    md = getattr(evidence_ctx, "markdown", None) if evidence_ctx is not None else None
    if md:
        lines.extend(["", md])
    return "\n".join(lines)


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


def _extract_intent(content: str) -> str | None:
    """手書きの計画欄（Today's Focus / Tasks）を取り出す。"""
    parts = []
    for heading in ("Today's Focus", "Tasks"):
        section = extract_heading_section(content, heading)
        if section:
            parts.append(f"## {heading}\n{section}")
    return "\n\n".join(parts) or None


def _extract_reflections(content: str) -> str | None:
    """手書きの振り返り（Reflections / 振り返り）を取り出す。"""
    for heading in ("Reflections", "振り返り"):
        section = extract_heading_section(content, heading)
        if section:
            return f"## {heading}\n{section}"
    return None


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


def cmd_prompts(
    cfg: Config,
    days: int,
    end_day: date,
    min_count: int,
    *,
    unhandled_only: bool = False,
    roi: bool = False,
) -> None:
    """繰り返し依頼を発掘し、クラスタ台帳へ upsert して表示する。"""
    tz = ZoneInfo(cfg.timezone)
    end = datetime.combine(end_day, time.min, tzinfo=tz) + timedelta(days=1)
    start = end - timedelta(days=days)
    adapters = available_adapters(cfg) if cfg.aiwork.enabled else []
    sessions, prompts, _ = collect_ai_telemetry(adapters, start, end)
    tracking: list[tuple[str, str]] = []
    for exp in load_experiments(cfg.experiments_path):
        if exp.status not in ("running", "adopted"):
            continue
        if not exp.metric.startswith("prompt_cluster:"):
            continue
        rep = exp.cluster_rep
        if exp.cluster_id:
            from .promptledger import representative_for_cluster_id

            rep = representative_for_cluster_id(cfg.memory_path, exp.cluster_id) or rep
        if rep:
            tracking.append((rep, exp.title))
    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
    clusters = [c for c in cluster_prompts(prompts) if c.count >= min_count]
    upsert_clusters(
        cfg.memory_path,
        clusters,
        as_of=end_day,
        redactor=redactor,
    )
    # 表示用: 正規化代表 → (PRM-ID, status)。台帳は redact 後キーなので両方試す
    from .promptmine import normalize as _pm_normalize

    ledger = load_prompt_ledger(cfg.memory_path)
    ledger_by_rep: dict[str, tuple[str, str]] = {}
    for c in clusters:
        candidates = [c.representative]
        if redactor is not None and c.example:
            candidates.append(_pm_normalize(redactor(c.example)))
        hit = None
        for cand in candidates:
            hit = find_matching_entry(ledger, cand)
            if hit:
                break
        if hit:
            ledger_by_rep[c.representative] = (hit.id, hit.status)
    if roi:
        from .promptroi import (
            format_roi_table,
            load_roi_for_paths,
            prompt_roi_scan_start,
        )
        from .promptledger import load_prompt_ledger as _load_pl

        ledger_ents = _load_pl(cfg.memory_path)
        scan_day = prompt_roi_scan_start(ledger_ents, end_day, window_days=30)
        roi_start = datetime.combine(scan_day, time.min, tzinfo=tz)
        if cfg.aiwork.enabled:
            roi_sessions, roi_prompts, _ = collect_ai_telemetry(
                adapters, roi_start, end
            )
        else:
            roi_sessions, roi_prompts = [], []
        rows = load_roi_for_paths(
            cfg.memory_path, roi_prompts, roi_sessions, as_of=end_day
        )
        print(format_roi_table(rows))
        return

    print(
        render_prompt_report(
            prompts,
            days=days,
            min_count=min_count,
            tracking=tracking,
            ledger_by_rep=ledger_by_rep,
            unhandled_only=unhandled_only,
        )
    )


def cmd_guard(cfg: Config, args: argparse.Namespace) -> int:
    """空転ブレーカー: install / status（--hook は main で先に処理）。"""
    import json as _json

    from .guard import (
        build_hook_command,
        build_hooks_snippet,
        format_guard_status,
        install_hooks_write,
    )

    sub = getattr(args, "guard_command", None)
    g = cfg.guard

    if sub == "install":
        py = sys.executable
        cfg_path = None
        try:
            found = find_config_file(getattr(args, "config", None))
            if found:
                cfg_path = str(found)
        except Exception:
            pass
        cmd = build_hook_command(python_exe=py, config_path=cfg_path)
        snippet = build_hooks_snippet(cmd)
        if not getattr(args, "write", False):
            print("# 以下を .claude/settings.json の hooks にマージしてください")
            print("# PostToolUse には登録しないでください（レイテンシ税）")
            print(_json.dumps({"hooks": snippet}, ensure_ascii=False, indent=2))
            print()
            print(f"# コマンド: {cmd}")
            print("# 書き込み: kaizenlog guard install --write --project")
            return 0
        if getattr(args, "user", False):
            target = Path.home() / ".claude" / "settings.json"
        else:
            target = Path.cwd() / ".claude" / "settings.json"
        try:
            bak = install_hooks_write(target, cmd)
            print(f"✅ hooks を書き込みました: {target}")
            if bak != target and Path(bak).is_file():
                print(f"   バックアップ: {bak}")
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        return 0

    # status（既定）
    print(
        format_guard_status(
            enabled=g.enabled,
            retry_threshold=g.retry_threshold,
            tool_error_streak=g.tool_error_streak,
            cooldown_seconds=g.cooldown_seconds,
            debounce_seconds=g.debounce_seconds,
        )
    )
    return 0


def cmd_excavate(
    cfg: Config,
    *,
    days: int = 90,
    write: bool = False,
    card: bool = False,
    as_of: date | None = None,
) -> int:
    """過去ログ発掘監査（読み取り専用）。"""
    from .cardgen import ExcavateCardData, write_excavate_card
    from .excavate import (
        format_excavate_report,
        run_excavate,
        write_excavate_report,
    )

    as_of = as_of or datetime.now(ZoneInfo(cfg.timezone)).date()
    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
    try:
        report = run_excavate(cfg, days=days, as_of=as_of, redactor=redactor)
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    body = format_excavate_report(report)
    print(body)
    if write:
        path = cfg.memory_path / "excavate" / f"{as_of.isoformat()}.md"
        write_excavate_report(path, body)
        print(f"✅ レポートを書き込みました: {path}")
    if card:
        jpy = None
        if report.loop_cost_usd is not None and report.usd_jpy:
            jpy = int(round(report.loop_cost_usd * report.usd_jpy))
        worst = (
            report.worst_days[0].day.isoformat() if report.worst_days else None
        )
        cpath = cfg.memory_path / "cards" / f"excavate-{as_of.isoformat()}.svg"
        write_excavate_card(
            cpath,
            ExcavateCardData(
                period_label=report.period_label,
                loop_cost_usd=report.loop_cost_usd,
                loop_cost_jpy=jpy,
                episode_count=report.loop_episodes,
                worst_day=worst,
                session_count=report.session_count,
            ),
        )
        print(f"✅ カードを書き込みました: {cpath}")
    return 0


def cmd_handoff(
    cfg: Config,
    *,
    targets: list[str] | None = None,
    dry_run: bool = False,
    as_of: date | None = None,
) -> int:
    """実測教訓を CLAUDE.md / AGENTS.md の agent-context 区間へ注入。"""
    from .handoff import build_agent_context_section, run_handoff_for_target

    paths = list(targets or [])
    if not paths:
        paths = list(cfg.handoff.targets or [])
    if not paths:
        print(
            "❌ targets が未設定です。"
            " --target を指定するか config [handoff] targets を設定してください。",
            file=sys.stderr,
        )
        return 1
    as_of = as_of or datetime.now(ZoneInfo(cfg.timezone)).date()
    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
    if dry_run:
        # dry-run: 先頭 target の生成結果を表示（台帳は書かない）
        section = build_agent_context_section(
            stats_dir=cfg.stats_path,
            memory_dir=cfg.memory_path,
            as_of=as_of,
            redactor=redactor,
            target=Path(paths[0]).expanduser(),
        )
        print(section)
        return 0
    for t in paths:
        p = Path(t).expanduser()
        run_handoff_for_target(
            target=p,
            stats_dir=cfg.stats_path,
            memory_dir=cfg.memory_path,
            as_of=as_of,
            redactor=redactor,
            dry_run=False,
        )
        print(f"✅ handoff を書き込みました: {p}")
    return 0


def cmd_handoff_roi(
    cfg: Config,
    *,
    targets: list[str] | None = None,
    suppress: str | None = None,
    unsuppress: str | None = None,
    promote: str | None = None,
    as_of: date | None = None,
) -> int:
    """申し送りROI表・抑制/復帰/昇格（明示CLI=承認）。"""
    from .aiwork import collect_ai_telemetry
    from .handoff import collect_handoff_lessons, run_handoff_for_target
    from .handoffledger import (
        HandoffLesson,
        build_roi_rows,
        format_roi_table,
        inject_promoted_lesson,
        load_handoff_ledger,
        mark_promote_candidates,
        set_lesson_status,
    )

    as_of = as_of or datetime.now(ZoneInfo(cfg.timezone)).date()
    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
    paths = list(targets or [])
    if not paths:
        paths = list(cfg.handoff.targets or [])

    # --- 抑制 / 復帰 / 昇格 ---
    action_id = suppress or unsuppress or promote
    if action_id:
        if suppress and unsuppress:
            print("❌ --suppress と --unsuppress は同時指定できません。", file=sys.stderr)
            return 1
        if promote and (suppress or unsuppress):
            print("❌ --promote は他の status 変更と同時指定できません。", file=sys.stderr)
            return 1

        target_one: Path | None = None
        if targets and len(targets) == 1:
            target_one = Path(targets[0]).expanduser()
        elif targets and len(targets) > 1 and (suppress or unsuppress):
            print(
                "❌ --suppress/--unsuppress では --target を1つに限定するか省略してください。",
                file=sys.stderr,
            )
            return 1

        try:
            if suppress:
                set_lesson_status(
                    cfg.memory_path,
                    suppress,
                    "suppressed",
                    target=target_one,
                    as_of=as_of,
                )
                print(f"✅ suppressed: {suppress}")
            elif unsuppress:
                set_lesson_status(
                    cfg.memory_path,
                    unsuppress,
                    "active",
                    target=target_one,
                    as_of=as_of,
                )
                print(f"✅ unsuppressed (active): {unsuppress}")
            elif promote:
                gt = (cfg.handoff.global_target or "").strip()
                if not gt:
                    print(
                        "❌ [handoff] global_target が未設定です。"
                        " config に global_target を設定してから --promote してください。",
                        file=sys.stderr,
                    )
                    return 1
                lessons = collect_handoff_lessons(
                    stats_dir=cfg.stats_path,
                    memory_dir=cfg.memory_path,
                    as_of=as_of,
                    redactor=redactor,
                    suppress_ids=set(),
                )
                les = next((x for x in lessons if x.lesson_id == promote), None)
                if les is None:
                    led = load_handoff_ledger(cfg.memory_path)
                    hit = next((e for e in led if e.lesson_id == promote), None)
                    if hit is None:
                        print(f"❌ 不明な lesson_id: {promote}", file=sys.stderr)
                        return 1
                    les = HandoffLesson(
                        lesson_id=hit.lesson_id,
                        kind=hit.kind,
                        ref_id=hit.ref_id,
                        text=f"(昇格) {hit.lesson_id}",
                    )
                try:
                    set_lesson_status(
                        cfg.memory_path, promote, "promoted", as_of=as_of
                    )
                except KeyError:
                    print(
                        f"❌ lesson_id が台帳にありません（先に handoff を実行）: {promote}",
                        file=sys.stderr,
                    )
                    return 1
                inject_promoted_lesson(Path(gt).expanduser(), les, as_of=as_of)
                print(f"✅ promoted → {gt}: {promote}")
        except KeyError:
            print(
                f"❌ 不明な lesson_id（台帳に無し）: {action_id}",
                file=sys.stderr,
            )
            return 1
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1

        # 抑制/復帰後は対象 handoff を再実行してセクションから除去/復帰
        if suppress or unsuppress:
            re_paths = [str(target_one)] if target_one else paths
            if not re_paths:
                print(
                    "⚠️ targets 未設定のため handoff 再実行をスキップしました。",
                    file=sys.stderr,
                )
            else:
                for t in re_paths:
                    p = Path(t).expanduser()
                    run_handoff_for_target(
                        target=p,
                        stats_dir=cfg.stats_path,
                        memory_dir=cfg.memory_path,
                        as_of=as_of,
                        redactor=redactor,
                        dry_run=False,
                    )
                    print(f"✅ handoff 再実行: {p}")
        elif promote:
            for t in paths:
                p = Path(t).expanduser()
                run_handoff_for_target(
                    target=p,
                    stats_dir=cfg.stats_path,
                    memory_dir=cfg.memory_path,
                    as_of=as_of,
                    redactor=redactor,
                    dry_run=False,
                )
                print(f"✅ handoff 再実行(昇格除外): {p}")
        return 0

    # --- ROI 表 ---
    if not paths:
        print(
            "❌ targets が未設定です。"
            " --target を指定するか config [handoff] targets を設定してください。",
            file=sys.stderr,
        )
        return 1

    tz = ZoneInfo(cfg.timezone)
    start = datetime.combine(as_of - timedelta(days=29), time.min, tzinfo=tz)
    end = datetime.combine(as_of, time.max, tzinfo=tz)
    adapters = available_adapters(cfg) if cfg.aiwork.enabled else []
    try:
        sessions, prompts, _ = collect_ai_telemetry(adapters, start, end)
    except Exception:
        sessions, prompts = [], []

    ledger = load_handoff_ledger(cfg.memory_path)
    lessons = collect_handoff_lessons(
        stats_dir=cfg.stats_path,
        memory_dir=cfg.memory_path,
        as_of=as_of,
        redactor=redactor,
        suppress_ids=set(),
    )
    rows_by_target: dict[str, list] = {}
    for t in paths:
        p = Path(t).expanduser()
        try:
            tkey = str(p.resolve())
        except OSError:
            tkey = str(p)
        rows = build_roi_rows(
            target=p,
            lessons=lessons,
            ledger=ledger,
            sessions=sessions,
            prompts=prompts,
            memory_dir=cfg.memory_path,
            stats_dir=cfg.stats_path,
            as_of=as_of,
            redactor=redactor,
        )
        rows_by_target[tkey] = rows
    mark_promote_candidates(rows_by_target)
    for t, rows in rows_by_target.items():
        print(f"\n### target: {t}\n")
        print(format_roi_table(rows))
    return 0


def cmd_coach(
    cfg: Config,
    *,
    dry_run: bool = False,
    apply_file: str | None = None,
    as_of: date | None = None,
) -> int:
    """コパイロット調教パック（提案 or 承認適用）。"""
    from .advisor import AdvisorError
    from .coach import (
        apply_proposal,
        build_coach_context,
        proposal_diff_text,
        run_coach_llm,
        save_proposal,
    )

    as_of = as_of or datetime.now(ZoneInfo(cfg.timezone)).date()
    if apply_file:
        targets = [Path(t).expanduser() for t in (cfg.handoff.targets or [])]
        if not targets:
            print(
                "❌ [handoff] targets が未設定です（--apply の書き込み先）。",
                file=sys.stderr,
            )
            return 1
        prop_path = Path(apply_file).expanduser()
        try:
            # 適用前に kind / append を読む（成功後 applied=true になる）
            from .coach import _parse_proposal_text, _parse_rollback_remove_md
            from .vault import read_text_preserve_newlines

            pre_text = read_text_preserve_newlines(prop_path)
            pre_fields, pre_append = _parse_proposal_text(pre_text)
            is_rollback = pre_fields.get("kind", "").strip().lower() == "rollback"
            if is_rollback:
                pre_append = _parse_rollback_remove_md(pre_text)
            written = apply_proposal(prop_path, targets)
        except AdvisorError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        for p in written:
            print(f"✅ coach を適用しました: {p}")
        # 台帳記録
        try:
            from .coachledger import mark_rolled_back, record_coach_application
            import json as _json

            if is_rollback:
                lid = pre_fields.get("ledger_id", "").strip()
                if lid:
                    mark_rolled_back(cfg.memory_path, lid, as_of=as_of)
                    print(f"📒 台帳を rolled_back に更新: {lid}")
            else:
                # evidence は frontmatter の JSON 文字列
                ev_raw = pre_fields.get("evidence", "[]")
                try:
                    evidence = _json.loads(ev_raw)
                except _json.JSONDecodeError:
                    evidence = []
                if not isinstance(evidence, list):
                    evidence = []
                entry = record_coach_application(
                    cfg.memory_path,
                    as_of=as_of,
                    proposal_path=prop_path,
                    targets=written,
                    evidence=evidence,
                    append_md=pre_append,
                )
                print(f"📒 コーチ台帳に記録: {entry.id} (watching)")
        except Exception as e:
            print(f"⚠️  コーチ台帳の記録に失敗: {e}", file=sys.stderr)
        return 0

    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
    context = build_coach_context(cfg, as_of=as_of, redactor=redactor)
    if dry_run:
        print(context)
        return 0
    try:
        data = run_coach_llm(cfg, context)
    except AdvisorError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    path = save_proposal(
        cfg.memory_path,
        as_of=as_of,
        append_md=data["claude_md_append"],
        evidence=data["evidence"],
    )
    print(proposal_diff_text(data["claude_md_append"]))
    print(f"\n提案を保存しました: {path}")
    print("適用: kaizenlog coach --apply <proposal-file>")
    return 0


def cmd_abtest(cfg: Config, args: argparse.Namespace) -> int:
    """パーソナル METR 実験 CLI。"""
    from .cardgen import AbtestCardData, write_abtest_card
    from .experiments import (
        ExperimentError,
        compute_abtest_effect,
        create_abtest,
        finish_abtest,
        format_abtest_journal_line,
        load_abtests,
        parse_predict_pct,
    )
    from .vault import ACTIVITY_MARKER, DailyNoteStore, extract_section, upsert_section

    tz = ZoneInfo(cfg.timezone)
    today = datetime.now(tz).date()
    sub = args.abtest_command

    if sub == "new":
        try:
            predict = parse_predict_pct(str(args.predict))
        except ExperimentError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        path = create_abtest(
            cfg.experiments_path,
            today=today,
            predict_pct=predict,
            days=int(args.days or 28),
        )
        print(f"📊 abtest を開始しました: {path}")
        return 0

    if sub == "status":
        items = load_abtests(cfg.experiments_path)
        if not items:
            print("abtest はまだありません。")
            return 0
        for e in items:
            if e.invalid_reason:
                meas = e.invalid_reason
            elif e.measured_pct is not None:
                meas = f"{e.measured_pct:+g}%"
            else:
                meas = "（未確定）"
            felt = f"{e.felt_pct:+g}%" if e.felt_pct is not None else "—"
            print(
                f"[{e.status}] {e.id}: 予測{e.predict_pct:+g}% / 体感{felt}"
                f" / 実測{meas} / {e.start}〜{e.deadline}"
            )
        return 0

    if sub == "finish":
        try:
            felt = parse_predict_pct(str(args.felt))
        except ExperimentError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        items = [e for e in load_abtests(cfg.experiments_path) if e.status == "running"]
        if not items:
            print("❌ 実行中の abtest がありません", file=sys.stderr)
            return 1
        exp = items[-1]
        if getattr(args, "id", None):
            matched = [e for e in items if e.id == args.id or e.id.endswith(args.id)]
            if not matched:
                print(f"❌ abtest が見つかりません: {args.id}", file=sys.stderr)
                return 1
            exp = matched[-1]
        # 期間内 stats
        days = (exp.deadline - exp.start).days + 1
        stats_list = load_stats(cfg.stats_path, days=days, end_day=exp.deadline)
        pre_stats = load_stats(
            cfg.stats_path, days=28, end_day=exp.start - timedelta(days=1)
        )
        measured, ai_n, non_n, invalid = compute_abtest_effect(
            stats_list,
            start=exp.start,
            end=min(today, exp.deadline),
            pre_stats=pre_stats,
        )
        # SVG カード
        cards_dir = cfg.memory_path / "cards"
        card_name = f"abtest-{exp.id}.svg"
        card_abs = cards_dir / card_name
        period = f"{exp.start.isoformat()} 〜 {min(today, exp.deadline).isoformat()}"
        write_abtest_card(
            card_abs,
            AbtestCardData(
                experiment_id=exp.id,
                period_label=period,
                sample_ai_days=ai_n,
                sample_non_ai_days=non_n,
                predict_pct=exp.predict_pct,
                felt_pct=felt,
                measured_pct=measured,
                invalid_reason=invalid,
            ),
        )
        # 相対パス（memory_dir 基準）
        try:
            card_rel = str(card_abs.relative_to(cfg.vault_dir)).replace("\\", "/")
        except ValueError:
            card_rel = str(card_abs)
        finish_abtest(
            exp,
            felt_pct=felt,
            card_rel_path=card_rel,
            measured_pct=measured,
            invalid_reason=invalid,
            sample_ai=ai_n,
            sample_non=non_n,
            as_of=today,
        )
        line = format_abtest_journal_line(exp)
        print(line)
        print(f"カード: {card_abs}")
        # 終了日の ADVICE/Kaizen 区間へ必ず1行（note 無しでも作成。Activity へは書かない）
        from .vault import ADVICE_MARKER

        store = DailyNoteStore(cfg.daily_notes_path)
        note = store.read(today)
        if note is None:
            store.write_section(today, ADVICE_MARKER, line + "\n")
        else:
            sec = extract_section(note, ADVICE_MARKER)
            if sec is not None:
                # 同一 abtest ID の完了行が既にあれば重複追加しない
                if exp.id in sec and "abtest完了" in sec:
                    pass
                else:
                    new_sec = sec.rstrip() + "\n\n" + line + "\n"
                    store.write_section(today, ADVICE_MARKER, new_sec)
            else:
                store.write_section(today, ADVICE_MARKER, line + "\n")
        print(f"✅ 日誌に追記しました（{today.isoformat()}）")
        return 0

    print(f"❌ 未知の abtest サブコマンド: {sub}", file=sys.stderr)
    return 1


def cmd_prompts_mark(
    cfg: Config,
    action_id: str,
    status: str,
    *,
    skill_name: str | None = None,
) -> int:
    """台帳クラスタを skilled / dismissed に更新する。"""
    status = (status or "").strip().lower()
    if status not in ("skilled", "dismissed"):
        print(
            "❌ status は skilled または dismissed を指定してください",
            file=sys.stderr,
        )
        return 1
    if status == "skilled" and not (skill_name or "").strip():
        print("❌ skilled には --skill NAME が必要です", file=sys.stderr)
        return 1
    entries = load_prompt_ledger(cfg.memory_path)
    resolved = resolve_prm_id(action_id, entries)
    if resolved is None:
        print(f"❌ 該当するクラスタがありません: {action_id}", file=sys.stderr)
        return 1
    if isinstance(resolved, list):
        print("❌ ID が曖昧です。候補:", file=sys.stderr)
        for e in resolved:
            print(f"  {format_ledger_line(e)}", file=sys.stderr)
        return 1
    entry = resolved
    if entry.status == status and (
        status != "skilled" or entry.skill_name == (skill_name or "").strip()
    ):
        print(f"ℹ️  既に {status} です: {entry.id}")
        return 0
    try:
        marked = mark_prompt_entry(entry, status, skill_name=skill_name)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    append_prompt_ledger(cfg.memory_path, [marked])
    extra = f" skill={marked.skill_name}" if marked.skill_name else ""
    print(f"✅ {marked.id} を {status} に更新しました{extra}")
    return 0


def _resolve_pre_start_baseline(cfg: Config, metric: str, today: date) -> float | None:
    """起票日の前日までの直近7日統計から中央値 baseline を求める（3日以上必要）。"""
    prior_end = today - timedelta(days=1)
    stats_list = load_stats(cfg.stats_path, days=7, end_day=prior_end)
    return baseline_median_from_stats(stats_list, metric, min_days=3)


def cmd_experiment_new(cfg: Config, args: argparse.Namespace) -> None:
    tz = ZoneInfo(cfg.timezone)
    today = datetime.now(tz).date()
    deadline = today + timedelta(days=args.days)
    baseline = _resolve_pre_start_baseline(cfg, args.metric, today)
    try:
        path = create_experiment(
            cfg.experiments_path,
            title=args.title,
            metric=args.metric,
            target=args.target,
            today=today,
            deadline=deadline,
            hypothesis=args.hypothesis,
            baseline=baseline,
        )
    except ExperimentError as e:
        raise SystemExit(f"❌ {e}")
    print(f"🧪 実験を起票しました: {path}")
    print(f"   指標: {args.metric} / 目標: {args.target} / 期限: {deadline.isoformat()}")
    if baseline is not None:
        prior_end = today - timedelta(days=1)
        n_days = sum(
            1
            for s in load_stats(cfg.stats_path, days=7, end_day=prior_end)
            if metric_from_stats(args.metric, s) is not None
        )
        print(f"📐 baseline: 直近{n_days}日の中央値 {baseline:g}（開始前実測）")
    else:
        print("   baseline: 開始前の統計が3日未満のため未設定（初回実測で埋まります）")
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
        es = format_effect_size(e)
        es_part = f" / {es}" if es else ""
        print(f"[{e.status:>8}] {e.title} — {e.metric} {e.target_op} {e.target_value:g}"
              f"（期限 {e.deadline or '未設定'}{latest}{es_part}）")


def cmd_eval_record(cfg: Config, day: date, out_dir: Path | None = None) -> int:
    """対象日の advise 入力をケース化して保存（privacy redaction 済み）。"""
    from .evalharness import (
        build_case_from_inputs,
        default_cases_dir,
        redact_case,
        safe_case_filename,
        save_case,
    )

    store = DailyNoteStore(cfg.daily_notes_path)
    content = store.read(day)
    if content is None:
        print(f"❌ デイリーノートがありません: {store.path_for(day)}", file=sys.stderr)
        return 1
    activity_md = extract_section(content, ACTIVITY_MARKER)
    if activity_md is None:
        print("❌ Activity Log がありません。先に generate を実行してください。", file=sys.stderr)
        return 1
    intent = _extract_intent(content)
    recent: list[str] = []
    for i in range(1, cfg.llm.lookback_days + 1):
        past_day = day - timedelta(days=i)
        past = store.read(past_day)
        if not past:
            continue
        sec = extract_section(past, ACTIVITY_MARKER)
        if sec:
            # 先頭数行だけ（フル日誌は肥大化）
            head = "\n".join(sec.splitlines()[:8])
            recent.append(f"{past_day.isoformat()}:\n{head}")
    experiments_ctx = render_experiments_context(load_experiments(cfg.experiments_path))
    memory_ctx = summarize_for_prompt(load_entries(cfg.memory_path), day)
    stats_history = load_stats(
        cfg.stats_path, days=max(1, cfg.llm.lookback_days + 1), end_day=day
    )
    current_stats = next(
        (item for item in reversed(stats_history) if item.get("day") == day.isoformat()),
        None,
    )
    prior_stats = [item for item in stats_history if item.get("day") != day.isoformat()]
    source_status = "missing"
    if current_stats is not None:
        stored_fingerprint = current_stats.get("activity_sha256")
        if isinstance(stored_fingerprint, str):
            source_status = (
                "verified"
                if stored_fingerprint == activity_fingerprint(activity_md)
                else "mismatch"
            )
        else:
            source_status = "unverified"
    case = build_case_from_inputs(
        day=day,
        current_stats=current_stats,
        prior_stats=prior_stats,
        today_md=activity_md,
        recent_summaries=recent,
        intent=intent,
        experiments_ctx=experiments_ctx or None,
        memory_ctx=memory_ctx or None,
        source_status=source_status,
        timezone=cfg.timezone,
        known_categories=list(known_category_names(cfg.rules)),
    )
    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
    case = redact_case(case, redactor)
    dest_dir = out_dir or default_cases_dir(cfg)
    dest = dest_dir / safe_case_filename(case.id, case.day)
    save_case(dest, case)
    print(f"✅ eval ケースを保存しました: {dest}")
    print("   （個人ログ由来のため git にコミットしないでください）")
    return 0


def cmd_eval_run(
    cfg: Config,
    cases_dir: Path | None,
    repeat: int,
    min_pass_rate: float | None,
) -> int:
    """ケースを現在の LLM 設定で繰り返し実行し集計する。"""
    from .evalharness import (
        format_eval_table,
        load_cases_dir,
        resolve_eval_cases_dir,
        run_eval,
    )

    directory, used_samples = resolve_eval_cases_dir(cfg, cases_dir)
    if used_samples:
        print(
            f"ℹ ユーザーケースが無いため同梱サンプルを使います: {directory}"
        )
    cases = load_cases_dir(directory)
    if not cases:
        print(f"❌ ケースがありません: {directory}", file=sys.stderr)
        print(
            "   `kaizenlog eval record` で保存するか、"
            "リポジトリの `eval/samples/` を --cases で指定してください。",
            file=sys.stderr,
        )
        return 1
    print(f"▶ eval: {len(cases)} ケース × {repeat} 回（backend={cfg.llm.backend}）")
    try:
        agg = run_eval(cases, cfg.llm, repeat=repeat)
    except Exception as e:
        # ネットワーク不可・CLI 未導入など
        print(f"❌ eval 実行に失敗しました: {type(e).__name__}: {e}", file=sys.stderr)
        print(
            "   実 LLM が使える環境で再実行してください（pytest ではモックを使います）。",
            file=sys.stderr,
        )
        return 1
    print(format_eval_table(agg))
    if min_pass_rate is not None and agg.final_pass_rate < float(min_pass_rate):
        print(
            f"❌ 修復後合格率 {agg.final_pass_rate * 100:.1f}% が"
            f" --min-pass-rate {float(min_pass_rate) * 100:.1f}% を下回りました",
            file=sys.stderr,
        )
        return 1
    return 0


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
                worst = max(worst, 1)  # スクリプトから検知できるよう非ゼロで返す
            else:
                print(f"⚠️  {name}: 同梱版と差分あり（更新 or ローカル改変）。"
                      "差分を確認して --force で更新できます")
                worst = max(worst, 1)
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


def cmd_init_config(output: str | None = None) -> int:
    from .config import default_config_path, write_config_file
    out = Path(output).expanduser() if output else default_config_path()
    if out.exists():
        print(f"{out} は既に存在します。再構成は `kaizenlog setup` を使ってください。")
        return 1
    write_config_file(out, vault_dir=Path("."), backend="none", model="qwen3:8b", merge=False)
    print(f"✅ 設定ファイルの雛形を作成しました: {out.resolve()}")
    print("次: kaizenlog setup")
    return 0


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
    ev = sub.add_parser("eval", help="LLM日次契約の評価ハーネス（開発者向け）")
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)
    ev_rec = ev_sub.add_parser("record", help="対象日の入力をケース化して保存")
    ev_rec.add_argument("--date", help="対象日 YYYY-MM-DD（省略時は今日）")
    ev_rec.add_argument("--out", help="保存先ディレクトリ（省略時: vault/.kaizenlog/eval/cases）")
    ev_run = ev_sub.add_parser("run", help="ケースを繰り返し実行して合格率を集計")
    ev_run.add_argument(
        "--cases",
        help="ケースディレクトリ（省略時: ユーザーケース or 同梱 samples）",
    )
    ev_run.add_argument("--repeat", type=int, default=1, help="各ケースの反復回数（既定1）")
    ev_run.add_argument(
        "--min-pass-rate",
        type=float,
        default=None,
        help="修復後合格率の下限（下回れば exit 1）。例: 0.8",
    )
    pat = sub.add_parser("patterns", help="繰り返しパターンの検出（自動化候補）")
    pat.add_argument("--days", type=int, default=14, help="遡る日数（デフォルト14）")
    pat.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は今日）")
    rep = sub.add_parser("report", help="提出用の日報ドラフトを生成")
    rep.add_argument("--date", help="対象日 YYYY-MM-DD（省略時は今日）")
    rep.add_argument("--no-llm", action="store_true", help="LLMを使わず事実ベースの箇条書きで生成")
    rep.add_argument("--write", action="store_true", help="デイリーノートにも書き込む")
    pr = sub.add_parser("prompts", help="Claude Codeへの繰り返し依頼の発掘（台帳 upsert）")
    pr_sub = pr.add_subparsers(dest="prompts_command")
    pr.add_argument("--days", type=int, default=14, help="遡る日数（デフォルト14）")
    pr.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は今日）")
    pr.add_argument("--min-count", type=int, default=3, help="レポートする最低反復回数（デフォルト3）")
    pr.add_argument(
        "--unhandled",
        action="store_true",
        help="status=new のクラスタのみ表示（autopilot 向け）",
    )
    pr.add_argument(
        "--roi",
        action="store_true",
        help="プロンプト資産ROIランキングを表示",
    )
    pr_mark = pr_sub.add_parser("mark", help="クラスタを skilled / dismissed に記録")
    pr_mark.add_argument("id", help="PRM-ID（末尾部分一致可）")
    pr_mark.add_argument(
        "status",
        choices=("skilled", "dismissed"),
        help="skilled（スキル化済み）または dismissed（却下）",
    )
    pr_mark.add_argument(
        "--skill",
        dest="skill_name",
        default=None,
        help="skilled 時のスキル名（必須）",
    )
    wc = sub.add_parser(
        "weekly-context",
        help="週次レビュー用の決定論コンテキストを出力（LLM不使用）",
    )
    wc.add_argument("--week", help="対象週 YYYY-Www（月曜始まり）")
    wc.add_argument("--date", help="この日を含む週（--week より優先されない）")
    wc.add_argument(
        "--write",
        action="store_true",
        help="Weekly Reviews/YYYY-Www.md のマーカー区間へ永続化（stdout も出す）",
    )
    blk = sub.add_parser("block", help="時間泥棒からLeechBlockのブロックルールを生成（介入）")
    blk.add_argument("--days", type=int, default=14, help="分析する日数（デフォルト14）")
    blk.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は今日）")
    blk.add_argument("--min-minutes", type=float, default=15.0,
                     help="対象とする平均時間/日の下限（デフォルト15分）")
    blk.add_argument("--write", action="store_true",
                     help="ルールファイルの書き出しと効果測定実験の起票まで行う")
    blk.add_argument("--out", help="ルールファイルの出力先（省略時: <vault>/.kaizenlog/leechblock-options.txt）")
    init = sub.add_parser("init-config", help="設定ファイルの雛形を出力する")
    init.add_argument("--output", help="出力先パス（省略時は AppData/XDG の config.toml）")
    su = sub.add_parser("setup", help="対話式セットアップウィザード")
    su.add_argument("--config", help="読み書きする設定パス（省略時は AppData/XDG）")
    su.add_argument("--vault", help="Obsidian ボールトのパス")
    su.add_argument("--yes", action="store_true", help="安全な既定提案を確認なしで採用")
    su.add_argument("--force", action="store_true", help="OK 済みフェーズも再確認")
    su.add_argument("--skip-aw", action="store_true", help="ActivityWatch フェーズをスキップ")
    su.add_argument("--skip-task", action="store_true", help="タスク登録フェーズをスキップ")
    su.add_argument("--skip-skills", action="store_true", help="スキル導入をスキップ")
    su.add_argument("--install-aw", action="store_true",
                    help="非対話でも winget で ActivityWatch 導入を許可")
    su.add_argument("--register-task", action="store_true",
                    help="非対話でも日次タスク登録を許可")
    su.add_argument("--time", default="21:30", help="日次タスク時刻（既定 21:30）")
    su.add_argument("--morning-time", default="08:30",
                    help="朝タスク時刻（空文字で登録しない、既定 08:30）")
    mor = sub.add_parser(
        "morning",
        help="朝: 追いつき（AW/LLM・書き込みを含む場合あり）→再描画→通知",
    )
    mor.add_argument("--date", help="対象日 YYYY-MM-DD（省略時は今日）")
    mor.add_argument(
        "--skip-catch-up",
        action="store_true",
        help="追いつき（昨日の generate/advise）を行わず表示と通知のみ",
    )
    tod = sub.add_parser(
        "today",
        help="未完了一覧（既定でノートのチェックをMemoryへ同期）",
    )
    tod.add_argument("--date", help="対象日 YYYY-MM-DD（省略時は今日）")
    tod.add_argument(
        "--no-sync",
        action="store_true",
        help="ノート走査とMemory同期を行わず表示のみ",
    )
    tod.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="全未完了を群分けして表示（status は変更しない）",
    )
    don = sub.add_parser("done", help="アクションを消化（KZN ID または末尾）")
    don.add_argument("id", help="KZN-YYYYMMDD-NNN または proposed の ID 末尾")
    don.add_argument("--date", help="done_date とする日（省略時は今日）")
    skp = sub.add_parser(
        "skip",
        help="アクションをスキップ（拒否。消化率の分母から除外）",
    )
    skp.add_argument("id", help="KZN-YYYYMMDD-NNN または proposed の ID 末尾")
    skp.add_argument("--reason", default="", help="スキップ理由（任意）")
    gl = sub.add_parser(
        "goal",
        help="今日の作業目標を設定/表示（goal マーカー区間の唯一の書き手）",
    )
    gl.add_argument(
        "text",
        nargs="?",
        default=None,
        help='目標文（例: "リリースノート下書き @執筆・ノート"）。省略時は表示のみ',
    )
    gl.add_argument("--date", help="対象日 YYYY-MM-DD（省略時は今日）")

    ho = sub.add_parser(
        "handoff",
        help="実測教訓を CLAUDE.md / AGENTS.md の agent-context 区間へ注入",
    )
    ho.add_argument(
        "handoff_action",
        nargs="?",
        default=None,
        choices=["roi"],
        help="roi: 申し送りROI表・抑制/昇格",
    )
    ho.add_argument(
        "--target",
        action="append",
        dest="targets",
        default=None,
        help="書き込み先パス（複数可）。未指定時は config [handoff] targets",
    )
    ho.add_argument(
        "--dry-run",
        action="store_true",
        help="生成セクションを表示するだけでファイルへ書かない",
    )
    ho.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は今日）")
    ho.add_argument(
        "--suppress",
        default=None,
        metavar="LESSON_ID",
        help="handoff roi: レッスンを抑制（台帳+再生成）。CLI実行=承認",
    )
    ho.add_argument(
        "--unsuppress",
        default=None,
        metavar="LESSON_ID",
        help="handoff roi: 抑制を解除して復帰",
    )
    ho.add_argument(
        "--promote",
        default=None,
        metavar="LESSON_ID",
        help="handoff roi: global_target へ昇格（各 target からは除外）",
    )

    ch = sub.add_parser("coach", help="コパイロット調教パック（承認制 diff 提案）")
    ch.add_argument(
        "--dry-run",
        action="store_true",
        help="コンテキストパックのみ表示（LLM 不使用）",
    )
    ch.add_argument(
        "--apply",
        dest="apply_file",
        default=None,
        help="提案ファイルを [handoff] targets の coach 区間へ適用",
    )
    ch.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は今日）")

    ab = sub.add_parser("abtest", help="パーソナル METR 実験（予測/体感/実測）")
    ab_sub = ab.add_subparsers(dest="abtest_command", required=True)
    ab_new = ab_sub.add_parser("new", help="実験開始")
    ab_new.add_argument(
        "--predict",
        required=True,
        help="予測効果 +N または +N%%（例: +30）",
    )
    ab_new.add_argument("--days", type=int, default=28, help="期間日数（既定28）")
    ab_fin = ab_sub.add_parser("finish", help="体感入力と実測確定")
    ab_fin.add_argument(
        "--felt",
        required=True,
        help="体感効果 +N または +N%%",
    )
    ab_fin.add_argument("--id", default=None, help="abtest ID（省略時は最新の running）")
    ab_sub.add_parser("status", help="実験一覧")

    exca = sub.add_parser(
        "excavate",
        help="過去ログ発掘監査（読み取り専用・stats/日誌は書かない）",
    )
    exca.add_argument(
        "--days",
        type=int,
        default=90,
        help="遡る日数（既定90）",
    )
    exca.add_argument(
        "--write",
        action="store_true",
        help="レポートを memory/excavate/YYYY-MM-DD.md へ冪等書き込み",
    )
    exca.add_argument(
        "--card",
        action="store_true",
        help="SVGカードを memory/cards/excavate-YYYY-MM-DD.svg へ出力",
    )
    exca.add_argument("--date", help="基準日 YYYY-MM-DD（省略時は今日）")

    gd = sub.add_parser(
        "guard",
        help="空転ブレーカー（Claude Code フック / install / status）",
    )
    gd.add_argument(
        "--hook",
        action="store_true",
        help="フック本体（stdin JSON）。エラーでも exit 0",
    )
    gd.add_argument(
        "guard_command",
        nargs="?",
        choices=("install", "status"),
        default=None,
        help="install: フック登録 / status: 状態表示",
    )
    gd.add_argument(
        "--write",
        action="store_true",
        help="install 時に settings.json へ書き込む（要バックアップ）",
    )
    gd.add_argument(
        "--project",
        action="store_true",
        help="install 先: カレント .claude/settings.json",
    )
    gd.add_argument(
        "--user",
        action="store_true",
        help="install 先: ~/.claude/settings.json",
    )

    args = parser.parse_args(argv)

    # 古いWindowsコンソール（cp932）での絵文字・日本語の文字化けを防ぐ
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

    if args.command == "init-config":
        return cmd_init_config(getattr(args, "output", None))

    # guard --hook は設定欠落でも沈黙 exit 0（セッションを壊さない）
    if args.command == "guard" and bool(getattr(args, "hook", False)):
        from .guard import run_hook

        try:
            return run_hook(config_path=getattr(args, "config", None))
        except Exception:
            return 0

    # setup bootstraps config — must not require load_config first
    if args.command == "setup":
        from .setup import SetupOptions, run_setup, validate_hhmm
        # setup subparser has its own --config; top-level --config may also apply
        cfg_arg = getattr(args, "config", None)
        try:
            task_time = validate_hhmm(args.time, "日次タスク時刻")
            morning = args.morning_time or ""
            if morning:
                morning = validate_hhmm(morning, "朝タスク時刻")
        except ConfigError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        return run_setup(SetupOptions(
            config_path=Path(cfg_arg).expanduser() if cfg_arg else None,
            vault=Path(args.vault).expanduser() if args.vault else None,
            yes=args.yes,
            force=args.force,
            skip_aw=args.skip_aw,
            skip_task=args.skip_task,
            skip_skills=args.skip_skills,
            install_aw=args.install_aw,
            register_task=args.register_task,
            time=task_time,
            morning_time=morning,
        ))

    # §X2: 設定未作成の通常コマンドは fail-closed（CWD をボールトにしない）
    _NO_CONFIG_STDERR = (
        "❌ KaizenLogの設定がありません。まず `kaizenlog setup` を実行してください。\n"
        "   診断だけ行う場合: `kaizenlog doctor`"
    )
    try:
        found_cfg = find_config_file(args.config)
    except FileNotFoundError as e:
        # 明示 --config / KAIZENLOG_CONFIG の欠落パス: traceback なし
        if args.command == "doctor":
            report, _has_err = run_doctor(
                Config(),
                args.config,
                missing_config_message=str(e),
            )
            print(report)
            return 1
        print(f"❌ {e}", file=sys.stderr)
        print(
            "   例: kaizenlog --config PATH doctor",
            file=sys.stderr,
        )
        return 2
    except ConfigError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if found_cfg is None:
        if args.command == "doctor":
            report, has_error = run_doctor(
                Config(), args.config, config_absent=True
            )
            print(report)
            return 1 if has_error else 0
        print(_NO_CONFIG_STDERR, file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        # load 中の競合欠落も traceback なし
        if args.command == "doctor":
            report, _has_err = run_doctor(
                Config(), args.config, missing_config_message=str(e)
            )
            print(report)
            return 1
        print(f"❌ {e}", file=sys.stderr)
        return 2

    if args.command == "guard":
        return cmd_guard(cfg, args)

    if args.command == "morning":
        tz = ZoneInfo(cfg.timezone)
        day = _parse_date(args.date, tz)
        return cmd_morning(
            cfg, day, skip_catch_up=bool(getattr(args, "skip_catch_up", False))
        )

    if args.command == "today":
        tz = ZoneInfo(cfg.timezone)
        day = _parse_date(args.date, tz)
        return cmd_today(
            cfg,
            day,
            no_sync=bool(getattr(args, "no_sync", False)),
            show_all=bool(getattr(args, "show_all", False)),
        )

    if args.command == "goal":
        tz = ZoneInfo(cfg.timezone)
        day = _parse_date(args.date, tz)
        return cmd_goal(cfg, day, getattr(args, "text", None))

    if args.command == "done":
        tz = ZoneInfo(cfg.timezone)
        day = _parse_date(args.date, tz)
        return cmd_done(cfg, args.id, day)

    if args.command == "skip":
        return cmd_skip(cfg, args.id, reason=getattr(args, "reason", None) or None)

    if args.command == "status":
        print(render_status(load_runs(cfg.logs_path)))
        # 北極星: 消化率 / PASS率（読み込み失敗で status 全体を落とさない）
        try:
            today = datetime.now(ZoneInfo(cfg.timezone)).date()
            mem = load_entries(cfg.memory_path)
            stats = compute_action_stats(mem, today)
            print()
            print(render_action_stats_line(stats, streaks=compute_streaks(mem, today)))
        except Exception:
            pass
        # 当日ループ税1行
        try:
            tz = ZoneInfo(cfg.timezone)
            today = datetime.now(tz).date()
            day_start = datetime.combine(today, time.min, tzinfo=tz)
            day_end = day_start + timedelta(days=1)
            if cfg.aiwork.enabled:
                adapters = available_adapters(cfg)
                sess, prompts, _ = collect_ai_telemetry(adapters, day_start, day_end)
                chains = detect_retry_chains(prompts)
                tax = compute_loop_tax(
                    chains, sess, pricing=cfg.aiwork.pricing or None
                )
                print(
                    format_loop_tax_line(
                        tax, usd_jpy=getattr(cfg.aiwork, "usd_jpy", None)
                    )
                )
        except Exception:
            print("（ループ税: 取得失敗）")
        # 風化 1行（0件なら非表示）
        try:
            from .decay import format_decay_status_line, load_decay_events

            today = datetime.now(ZoneInfo(cfg.timezone)).date()
            ev = load_decay_events(cfg.memory_path, window_days=7, as_of=today)
            line = format_decay_status_line(ev)
            if line:
                print(line)
        except Exception:
            pass
        # コーチ台帳 1行（空なら非表示）
        try:
            from .coachledger import format_coach_status_line, load_coach_ledger

            cline = format_coach_status_line(load_coach_ledger(cfg.memory_path))
            if cline:
                print(cline)
        except Exception:
            pass
        return 0

    if args.command == "excavate":
        return cmd_excavate(
            cfg,
            days=int(getattr(args, "days", 90) or 90),
            write=bool(getattr(args, "write", False)),
            card=bool(getattr(args, "card", False)),
            as_of=_parse_date(getattr(args, "date", None), ZoneInfo(cfg.timezone)),
        )

    if args.command == "handoff":
        tz = ZoneInfo(cfg.timezone)
        day = _parse_date(getattr(args, "date", None), tz)
        if getattr(args, "handoff_action", None) == "roi":
            return cmd_handoff_roi(
                cfg,
                targets=getattr(args, "targets", None),
                suppress=getattr(args, "suppress", None),
                unsuppress=getattr(args, "unsuppress", None),
                promote=getattr(args, "promote", None),
                as_of=day,
            )
        return cmd_handoff(
            cfg,
            targets=getattr(args, "targets", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            as_of=day,
        )

    if args.command == "coach":
        tz = ZoneInfo(cfg.timezone)
        day = _parse_date(getattr(args, "date", None), tz)
        return cmd_coach(
            cfg,
            dry_run=bool(getattr(args, "dry_run", False)),
            apply_file=getattr(args, "apply_file", None),
            as_of=day,
        )

    if args.command == "abtest":
        return cmd_abtest(cfg, args)

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
        # 補完対象が残ったまま中断した場合（ActivityWatch停止等）は
        # スクリプト/スケジューラから検知できるよう非ゼロで返す
        return 1 if missing_days(cfg.stats_path, end_day, args.days) else 0

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
        if getattr(args, "prompts_command", None) == "mark":
            return cmd_prompts_mark(
                cfg,
                args.id,
                args.status,
                skill_name=getattr(args, "skill_name", None),
            )
        tz = ZoneInfo(cfg.timezone)
        end_day = _parse_date(args.date, tz)
        cmd_prompts(
            cfg,
            args.days,
            end_day,
            args.min_count,
            unhandled_only=bool(getattr(args, "unhandled", False)),
            roi=bool(getattr(args, "roi", False)),
        )
        return 0

    if args.command == "weekly-context":
        from .weekly_context import (
            monday_of,
            parse_iso_week,
            render_weekly_context,
            write_weekly_context,
        )

        tz = ZoneInfo(cfg.timezone)
        if args.week:
            try:
                week_start = parse_iso_week(args.week)
            except ValueError as e:
                print(f"❌ {e}", file=sys.stderr)
                return 1
        else:
            ref = _parse_date(args.date, tz)
            week_start = monday_of(ref)
        t0 = monotonic()
        # ROI は CLI 側でテレメトリ収集して渡す（レンダラは暗黙走査しない）
        roi_rows = None
        try:
            from .promptroi import load_roi_for_paths, prompt_roi_scan_start
            from .promptledger import load_prompt_ledger as _lpl

            week_end = week_start + timedelta(days=6)
            ents = _lpl(cfg.memory_path)
            scan_day = prompt_roi_scan_start(ents, week_end, window_days=30)
            if cfg.aiwork.enabled:
                adapters = available_adapters(cfg)
                r_start = datetime.combine(scan_day, time.min, tzinfo=tz)
                r_end = datetime.combine(week_end, time.min, tzinfo=tz) + timedelta(
                    days=1
                )
                r_sess, r_prompts, _ = collect_ai_telemetry(adapters, r_start, r_end)
            else:
                r_sess, r_prompts = [], []
            roi_rows = load_roi_for_paths(
                cfg.memory_path, r_prompts, r_sess, as_of=week_end
            )
        except Exception:
            roi_rows = None
        body = render_weekly_context(
            cfg.stats_path,
            cfg.memory_path,
            cfg.experiments_path,
            week_start,
            roi_rows=roi_rows,
        )
        print(body)
        if getattr(args, "write", False):
            try:
                path = write_weekly_context(
                    cfg.daily_notes_path, body, week_start
                )
                print(f"✅ 週次コンテキストを書き込みました: {path}")
                log_run(
                    cfg.logs_path,
                    "weekly-context",
                    ok=True,
                    duration_seconds=monotonic() - t0,
                    retention_days=cfg.log_retention_days,
                    note="write",
                )
            except Exception as e:
                # 無人実行を止めない: 警告 + runlog
                print(f"⚠️  週次コンテキストの書き込みに失敗: {e}", file=sys.stderr)
                log_run(
                    cfg.logs_path,
                    "weekly-context",
                    ok=False,
                    duration_seconds=monotonic() - t0,
                    error=f"{type(e).__name__}: {e}",
                    retention_days=cfg.log_retention_days,
                )
                return 0
        return 0

    if args.command == "block":
        tz = ZoneInfo(cfg.timezone)
        end_day = _parse_date(args.date, tz)
        return cmd_block(cfg, end_day, days=args.days, min_minutes=args.min_minutes,
                         write=args.write, out=args.out)

    if args.command == "experiment":
        if args.exp_command == "new":
            cmd_experiment_new(cfg, args)
        else:
            cmd_experiment_list(cfg)
        return 0

    if args.command == "eval":
        tz = ZoneInfo(cfg.timezone)
        if args.eval_command == "record":
            day = _parse_date(args.date, tz)
            out = Path(args.out).expanduser() if getattr(args, "out", None) else None
            return cmd_eval_record(cfg, day, out)
        # run
        cases = Path(args.cases).expanduser() if getattr(args, "cases", None) else None
        return cmd_eval_run(
            cfg,
            cases,
            repeat=int(getattr(args, "repeat", 1) or 1),
            min_pass_rate=getattr(args, "min_pass_rate", None),
        )

    dry_run = bool(getattr(args, "dry_run", False))

    start_time = monotonic()
    try:
        # ZoneInfo/日付解釈もtry内で行う。設定のタイムゾーンtypo等で夜間実行が
        # 落ちたとき、実行ログと失敗通知を必ず残すため（外だと素通りで無音になる）
        tz = ZoneInfo(cfg.timezone)
        day = _parse_date(args.date, tz)
        if args.command in ("generate", "run"):
            # 日付指定のない通常実行では、直近の欠損日を先に自動補完する
            # （dry-runは書き込みを伴うため補完しない）
            skip_verdict: set[str] = set()
            if not args.date and cfg.auto_backfill_days > 0 and not dry_run:
                if missing_days(cfg.stats_path, day, cfg.auto_backfill_days):
                    cmd_backfill(cfg, cfg.auto_backfill_days, day)
                # 前夜に advise まで走らなかった日の retro-advise（冪等）
                # 直後 generate で同日 retro 分を即判定しないよう ID を返す
                skip_verdict = catch_up_yesterday(cfg, day).new_ids
            if not dry_run:
                cmd_generate(cfg, day, skip_verdict_ids=skip_verdict or None)
        if args.command in ("advise", "run"):
            cmd_advise(cfg, day, dry_run=dry_run)
    except (ActivityWatchError, AdvisorError, PrivacyError) as e:
        print(f"❌ {e}", file=sys.stderr)
        if not dry_run:
            log_run(cfg.logs_path, args.command, ok=False,
                    duration_seconds=monotonic() - start_time,
                    error=str(e), retention_days=cfg.log_retention_days)
            if cfg.notify_on_failure:
                _notify(cfg, "KaizenLog 失敗", f"{args.command}: {e}")
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
                _notify(cfg, "KaizenLog 失敗", f"{args.command}: {e.__class__.__name__}: {e}")
        return 1
    if not dry_run:
        log_run(cfg.logs_path, args.command, ok=True,
                duration_seconds=monotonic() - start_time,
                retention_days=cfg.log_retention_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
