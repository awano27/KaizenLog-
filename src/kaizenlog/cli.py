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
  kaizenlog morning [--date]               朝の追いつき・アクション再描画・通知
  kaizenlog setup                          対話式セットアップウィザード
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
    AdviceContractError,
    AdvisorError,
    generate_advice,
    prepare_advice_request,
    render_reader_advice,
)
from .advice_evidence import build_advice_evidence
from .aiwork import (
    available_adapters,
    collect_ai_telemetry,
    detect_retry_chains,
    render_aiwork_markdown,
)
from .doctor import run_doctor
from .memory import (
    append_entries,
    assign_action_ids,
    compute_action_stats,
    load_entries,
    render_action_stats_line,
    render_actions_section,
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
from .intervention import detect_time_sinks, render_leechblock_options, render_plan, suggest_rules
from .config import Config, ConfigError, load_config
from .experiments import (
    METRIC_DESCRIPTIONS,
    ExperimentError,
    baseline_median_from_stats,
    compute_metric,
    create_experiment,
    detect_regressions,
    load_experiments,
    metric_from_stats,
    record_measurement,
    render_experiments_context,
    should_measure_experiment,
    target_met,
)
from .patterns import render_patterns_markdown
from .report import render_markdown, summarize
from .stats import activity_fingerprint, load_stats, missing_days, write_stats
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
    apply_verdicts_to_advice_note,
    judge_entries,
    parse_pass_condition,
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
    input_stats = (compute_input_stats(input_raw, day_start=day_start, day_end=day_end)
                   if input_raw is not None else None)

    section = render_markdown(summary, tz, min_block_minutes=cfg.min_block_minutes,
                              input_stats=input_stats)

    ai_sessions = []
    day_retry_chains = []
    retry_chain_count: int | None = None
    if cfg.aiwork.enabled:
        adapters = available_adapters(cfg)
        ai_sessions, day_prompts = collect_ai_telemetry(adapters, day_start, day_end)
        day_retry_chains = detect_retry_chains(day_prompts)
        retry_chain_count = len(day_retry_chains)
        aiwork_md = render_aiwork_markdown(
            ai_sessions, tz, retry_chain_count=retry_chain_count
        )
        if aiwork_md:
            section = section.rstrip() + "\n\n" + aiwork_md

    store = DailyNoteStore(cfg.daily_notes_path)
    path = store.write_section(day, ACTIVITY_MARKER, section)
    print(f"✅ Activity Log を書き込みました: {path}")
    print(f"   合計 {summary.total_minutes:.0f}分 / {len(summary.blocks)}ブロック"
          f" / AI関連画面ブロック {summary.ai_activity_blocks}回"
          f" / AIセッション {len(ai_sessions)}回")

    # パターン検出用の機械可読な統計を蓄積する
    write_stats(
        cfg.stats_path,
        day,
        summary,
        ai_sessions,
        input_stats,
        activity_md=section,
        retry_chains=day_retry_chains if cfg.aiwork.enabled else None,
    )

    # 実験の実測追記: running 全件 + adopted（deadline から30日以内のみ）
    experiments = load_experiments(cfg.experiments_path)
    for exp in experiments:
        if not should_measure_experiment(exp, day):
            continue
        value = compute_metric(
            exp.metric, summary, ai_sessions, input_stats,
            retry_chains=retry_chain_count,
        )
        if value is None:
            print(f"⚠️  実験「{exp.title}」の指標 {exp.metric} は不明のためスキップしました")
            continue
        met = record_measurement(exp, day, value)
        mark = "✅" if met else "❌"
        print(f"🧪 実験「{exp.title}」: {value:g}（目標 {exp.target_op} {exp.target_value:g} {mark}）")

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

    # A1: 前日提案の PASS 機械判定 → Memory と前日ノートへ書き戻し
    memory_entries = load_entries(cfg.memory_path)
    proposal_day = day - timedelta(days=1)
    judged = judge_entries(
        memory_entries, proposal_day, summary, ai_sessions, input_stats, day,
        retry_chains=retry_chain_count,
    )
    if judged:
        append_entries(cfg.memory_path, judged)
        for entry in judged:
            parsed = parse_pass_condition(entry.action)
            if not parsed:
                continue
            metric, op, target_value = parsed
            mark = "✅" if entry.verdict == "pass" else "❌"
            print(
                f"🧪 アクション判定: {entry.id} {mark}"
                f"（実測 {entry.verdict_value:g} / 目標 {metric} {op} {target_value:g}）"
            )
        prev_note = store.read(proposal_day)
        if prev_note is not None:
            updated = apply_verdicts_to_advice_note(prev_note, judged)
            if updated is not None:
                atomic_write_text(store.path_for(proposal_day), updated)

    # A2: 翌日ノートへ未完了アクションを転記（backfill で過去日を汚さない）
    by_id = {e.id: e for e in memory_entries}
    by_id.update({e.id: e for e in judged})
    _write_actions_handoff(cfg, store, day, list(by_id.values()))
    return path


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


def catch_up_yesterday(cfg: Config, today: date) -> None:
    """前夜に走らなかった日の追いつき（昨日のみ）。

    1) 昨日の stats が無ければ generate（一昨日分の判定も generate 内で走る）
    2) ACTIVITY あり ADVICE なしなら advise（retro-advise: 今日向けアクションになる）
    失敗は警告＋runlog に留め、呼び出し元を止めない。
    """
    yesterday = today - timedelta(days=1)
    # generate: stats 欠損時のみ
    try:
        if missing_days(cfg.stats_path, today, 1) == [yesterday]:
            print(f"🔄 追いつき: {yesterday.isoformat()} の generate を実行します")
            t0 = monotonic()
            try:
                cmd_generate(cfg, yesterday)
                log_run(
                    cfg.logs_path, "generate", ok=True,
                    duration_seconds=monotonic() - t0,
                    retention_days=cfg.log_retention_days,
                )
            except Exception as e:
                print(f"⚠️  追いつき generate 失敗: {e}", file=sys.stderr)
                log_run(
                    cfg.logs_path, "generate", ok=False,
                    duration_seconds=monotonic() - t0,
                    error=str(e),
                    retention_days=cfg.log_retention_days,
                )
    except Exception as e:
        print(f"⚠️  追いつき generate 判定に失敗: {e}", file=sys.stderr)

    # retro-advise: ACTIVITY あり・ADVICE なしのときだけ（今日向けアクションが価値あり）
    if cfg.llm.backend == "none":
        return
    try:
        store = DailyNoteStore(cfg.daily_notes_path)
        note = store.read(yesterday)
        if not note:
            return
        if extract_section(note, ACTIVITY_MARKER) is None:
            return
        if extract_section(note, ADVICE_MARKER) is not None:
            return
        print(f"🔄 追いつき: {yesterday.isoformat()} の advise を実行します")
        t0 = monotonic()
        try:
            cmd_advise(cfg, yesterday, dry_run=False)
            log_run(
                cfg.logs_path, "advise", ok=True,
                duration_seconds=monotonic() - t0,
                retention_days=cfg.log_retention_days,
            )
        except Exception as e:
            print(f"⚠️  追いつき advise 失敗: {e}", file=sys.stderr)
            log_run(
                cfg.logs_path, "advise", ok=False,
                duration_seconds=monotonic() - t0,
                error=str(e),
                retention_days=cfg.log_retention_days,
            )
    except Exception as e:
        print(f"⚠️  追いつき advise 判定に失敗: {e}", file=sys.stderr)


def build_morning_notification(
    entries: list,
    today: date,
) -> str | None:
    """朝トースト本文。アクション本文は載せない（ロック画面に固有名詞を出さない）。"""
    window_start = (today - timedelta(days=7)).isoformat()
    window_end = (today - timedelta(days=1)).isoformat()
    open_n = sum(
        1
        for e in entries
        if e.status == "proposed" and window_start <= e.date <= window_end
    )
    yday = (today - timedelta(days=1)).isoformat()
    pass_n = sum(
        1 for e in entries if e.verdict_date == yday and e.verdict == "pass"
    )
    fail_n = sum(
        1 for e in entries if e.verdict_date == yday and e.verdict == "fail"
    )
    if open_n == 0 and pass_n == 0 and fail_n == 0:
        return None
    return (
        f"今日のアクション {open_n}件 / 昨日の判定 ✅{pass_n} ❌{fail_n}"
    )


def cmd_morning(cfg: Config, day: date) -> int:
    """朝の到達性: 追いつき → 📌 再描画 → 通知。"""
    t0 = monotonic()
    try:
        catch_up_yesterday(cfg, day)
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

        msg = build_morning_notification(entries, day)
        if msg:
            print(msg)
            # 本文は件数のみ（固有名詞をロック画面に出さない）
            notify("KaizenLog 朝の確認", msg, icon="Information")
        log_run(
            cfg.logs_path, "morning", ok=True,
            duration_seconds=monotonic() - t0,
            retention_days=cfg.log_retention_days,
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
            notify("KaizenLog 失敗", f"morning: {e}")
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
                hypothesis=f"{rule.evidence}。LeechBlockの制限で目標まで下げられるはず。",
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

    # Kaizen Memory: 提案日のノートのチェックボックスからdoneを検出し、要約をLLMに渡す。
    # KZNチェックボックスは提案日のノートにしか存在しないため、固定の直近N日ではなく
    # 「未完了エントリの提案日」のノートを走査する（4日以上経ってからのチェックも拾う）
    entries = load_entries(cfg.memory_path)
    scan_days = {day} | {
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
    evidence_ctx = build_advice_evidence(
        current_stats,
        prior_stats,
        timezone=ZoneInfo(cfg.timezone),
        source_status=source_status,
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
        )
        print("===== system prompt =====")
        print(system_prompt)
        print("===== user prompt =====")
        print(prompt)
        print("=====")
        print("（--dry-run のためLLMには送信していません。ノートも変更していません）")
        return None

    try:
        advice_md = generate_advice(
            cfg.llm, activity_md, recent,
            intent=intent,
            experiments=experiments_ctx or None,
            memory=memory_ctx or None,
            evidence=evidence_ctx,
            redactor=redactor,
        )
    except AdviceContractError:
        # 契約違反でも確定事実サマリーだけは残す（静かな失敗を防ぐ）。例外は再送出。
        store.write_section(day, ADVICE_MARKER, _degraded_advice_section(evidence_ctx))
        print("⚠️  出力契約を満たせなかったため縮退セクションを保存しました")
        raise
    advice_md = render_reader_advice(advice_md, evidence_ctx)
    # 「明日試すこと」に安定ID（KZN-YYYYMMDD-NNN）を付与して記録する
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
    return path


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
    adapters = available_adapters(cfg) if cfg.aiwork.enabled else []
    _, prompts = collect_ai_telemetry(adapters, start, end)
    print(render_prompt_report(prompts, days=days, min_count=min_count))


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
    mor = sub.add_parser("morning", help="朝の追いつき・アクション再描画・通知")
    mor.add_argument("--date", help="対象日 YYYY-MM-DD（省略時は今日）")

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

    # setup bootstraps config — must not require load_config first
    if args.command == "setup":
        from .setup import SetupOptions, run_setup
        # setup subparser has its own --config; top-level --config may also apply
        cfg_arg = getattr(args, "config", None)
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
            time=args.time,
            morning_time=args.morning_time or "",
        ))

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if args.command == "morning":
        tz = ZoneInfo(cfg.timezone)
        day = _parse_date(args.date, tz)
        return cmd_morning(cfg, day)

    if args.command == "status":
        print(render_status(load_runs(cfg.logs_path)))
        # 北極星: 消化率 / PASS率（読み込み失敗で status 全体を落とさない）
        try:
            today = datetime.now(ZoneInfo(cfg.timezone)).date()
            stats = compute_action_stats(load_entries(cfg.memory_path), today)
            print()
            print(render_action_stats_line(stats))
        except Exception:
            pass
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
        tz = ZoneInfo(cfg.timezone)
        end_day = _parse_date(args.date, tz)
        cmd_prompts(cfg, args.days, end_day, args.min_count)
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
            if not args.date and cfg.auto_backfill_days > 0 and not dry_run:
                if missing_days(cfg.stats_path, day, cfg.auto_backfill_days):
                    cmd_backfill(cfg, cfg.auto_backfill_days, day)
                # 前夜に advise まで走らなかった日の retro-advise（冪等）
                catch_up_yesterday(cfg, day)
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
