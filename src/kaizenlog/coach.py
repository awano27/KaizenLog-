"""コパイロット調教パック: 実測コンテキスト→LLM提案→承認制適用。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .advisor import (
    AdvisorError,
    apply_internal_sentinel,
    generate_text,
    load_bundled_prompt,
)
from .aiwork import available_adapters, collect_ai_telemetry
from .config import Config
from .memory import load_entries, metric_pass_rates
from .privacy import make_redactor
from .promptroi import format_roi_table, load_roi_for_paths
from .stats import load_stats
from .vault import (
    COACH_MARKER,
    atomic_write_bytes,
    atomic_write_text,
    read_text_preserve_newlines,
    upsert_section,
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _aggregate_loop_tax_30d(
    stats_dir: Path, as_of: date
) -> list[str]:
    """直近30日 stats の loop_tax を表示行リストで返す。"""
    lines: list[str] = []
    days = [as_of - timedelta(days=i) for i in range(29, -1, -1)]
    covered = 0
    total_eps = 0
    total_tokens = 0
    total_cost = 0.0
    any_tok_unknown = False
    any_cost_unknown = False
    max_ep: tuple[str, dict] | None = None

    for d in days:
        loaded = load_stats(stats_dir, 1, d)
        if not loaded:
            continue
        ai = loaded[0].get("ai") if isinstance(loaded[0].get("ai"), dict) else {}
        lt = ai.get("loop_tax")
        if not isinstance(lt, dict):
            continue
        covered += 1
        try:
            total_eps += int(lt.get("episode_count") or 0)
        except (TypeError, ValueError):
            pass
        tw = lt.get("total_wasted_tokens")
        if tw is None:
            any_tok_unknown = True
        else:
            try:
                total_tokens += int(tw)
            except (TypeError, ValueError):
                any_tok_unknown = True
        cost = lt.get("est_cost_usd")
        if cost is None:
            any_cost_unknown = True
        else:
            try:
                total_cost += float(cost)
            except (TypeError, ValueError):
                any_cost_unknown = True
        me = lt.get("max_episode")
        if isinstance(me, dict) and me.get("length"):
            cand = (d.isoformat(), me)
            if max_ep is None:
                max_ep = cand
            else:
                cur_len = int(max_ep[1].get("length") or 0)
                new_len = int(me.get("length") or 0)
                cur_wt = max_ep[1].get("wasted_tokens")
                new_wt = me.get("wasted_tokens")
                cur_k = int(cur_wt) if isinstance(cur_wt, (int, float)) else -1
                new_k = int(new_wt) if isinstance(new_wt, (int, float)) else -1
                if (new_len, new_k) > (cur_len, cur_k):
                    max_ep = cand

    if covered < 30:
        lines.append(f"計測不成立（loop_tax coverage: {covered}/30日）")
        return lines

    # coverage 30/30
    if total_eps == 0 and max_ep is None:
        lines.append("30日episode合計: 0")
        lines.append("総tokens: 0")
        lines.append("総額: $0.00")
        lines.append("最大episode: なし（ループなし）")
        return lines

    lines.append(f"30日episode合計: {total_eps}")
    if any_tok_unknown:
        lines.append("総tokens: 不明")
    else:
        lines.append(f"総tokens: {total_tokens}")
    if any_cost_unknown or any_tok_unknown:
        lines.append("総額: 不明")
    else:
        lines.append(f"総額: ${total_cost:.4f}")
    if max_ep is None:
        lines.append("最大episode: なし（ループなし）")
    else:
        day_s, me = max_ep
        wt = me.get("wasted_tokens")
        tok = "不明" if wt is None else str(int(wt))
        err = "あり" if me.get("has_tool_error") else "なし"
        excerpt = (me.get("excerpt") or "").strip()
        lines.append(
            f"最大episode: {day_s} length={me.get('length')} "
            f"tokens={tok} tool_error={err}"
        )
        if excerpt:
            lines.append(f"  excerpt: {excerpt}")
    return lines


def build_coach_context(
    cfg: Config,
    *,
    as_of: date | None = None,
    redactor: Callable[[str], str] | None = None,
) -> str:
    """決定論の機械可読 Markdown コンテキスト（redact 済み）。"""
    as_of = as_of or date.today()
    tz = ZoneInfo(cfg.timezone)
    stats_list = load_stats(cfg.stats_path, days=30, end_day=as_of)
    entries = load_entries(cfg.memory_path)
    mpr = metric_pass_rates(entries, as_of, window_days=30, min_judged=1)

    end = datetime.combine(as_of, time.min, tzinfo=tz) + timedelta(days=1)
    start = end - timedelta(days=30)
    prompts = []
    sessions = []
    if cfg.aiwork.enabled:
        adapters = available_adapters(cfg)
        sessions, prompts, _ = collect_ai_telemetry(adapters, start, end)
    roi_rows = load_roi_for_paths(
        cfg.memory_path, prompts, sessions, as_of=as_of
    )

    lines: list[str] = [
        f"# Coach Context {as_of.isoformat()}",
        "",
        "## stats v2（直近30日・存在する日のみ）",
        f"- 日数: {len(stats_list)}",
    ]
    total_retry = 0
    total_err = 0
    total_sessions = 0
    for s in stats_list:
        ai = s.get("ai") if isinstance(s.get("ai"), dict) else {}
        total_retry += int(ai.get("retry_chains") or 0)
        total_err += int(ai.get("tool_errors") or 0)
        total_sessions += int(ai.get("sessions") or 0)
    if stats_list:
        lines.append(f"- AIセッション合計: {total_sessions}")
        lines.append(f"- リトライ連鎖合計: {total_retry}")
        lines.append(f"- ツールエラー合計: {total_err}")
    else:
        lines.append("- （計測なし）")

    lines.extend(["", "## 指標別PASS率（実行済み）", ""])
    if mpr:
        for metric, passed, judged in mpr[:10]:
            lines.append(f"- {metric}: {passed}/{judged}")
    else:
        lines.append("- （計測なし）")

    lines.extend(["", "## プロンプト資産ROI", ""])
    if roi_rows:
        lines.append(format_roi_table(roi_rows[:10]))
    else:
        lines.append("- （計測なし）")

    lines.extend(["", "## ループ税（直近30日・stats loop_tax）", ""])
    for ln in _aggregate_loop_tax_30d(cfg.stats_path, as_of):
        lines.append(f"- {ln}")
    lines.append("")

    text = "\n".join(lines)
    if redactor is not None:
        text = redactor(text)
    return text


def _validate_evidence_item(item: object) -> str | None:
    """不正なら理由文字列、OKなら None。"""
    if not isinstance(item, dict):
        return "evidence 要素は dict である必要がある"
    fid = item.get("fact_id")
    val = item.get("value")
    if not isinstance(fid, str) or not fid.strip():
        return "fact_id が空でない str でない"
    if isinstance(val, bool):
        return "value に bool は不可"
    if isinstance(val, str):
        if not val.strip():
            return "value が空"
    elif isinstance(val, (int, float)):
        pass
    else:
        return "value は str/int/float のみ"
    return None


def parse_coach_json(text: str) -> dict:
    """LLM 出力から JSON 契約を抽出。失敗時 AdvisorError。"""
    raw = (text or "").strip()
    if not raw:
        raise AdvisorError("coach 出力が空です")
    candidates = [raw]
    m = _JSON_FENCE_RE.search(raw)
    if m:
        candidates.insert(0, m.group(1).strip())
    brace = re.search(r"\{[\s\S]*\}", raw)
    if brace:
        candidates.append(brace.group(0))
    last_err = "JSON を解析できません"
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError as e:
            last_err = str(e)
            continue
        if not isinstance(data, dict):
            last_err = "JSON がオブジェクトではありません"
            continue
        append = data.get("claude_md_append")
        evidence = data.get("evidence")
        if not isinstance(append, str) or not append.strip():
            last_err = "claude_md_append が無い/空"
            continue
        lines = [ln for ln in append.strip().splitlines() if ln.strip()]
        if not (3 <= len(lines) <= 7):
            last_err = f"claude_md_append は3-7行（実際 {len(lines)} 行）"
            continue
        if not isinstance(evidence, list) or not evidence:
            last_err = "evidence が無い/空"
            continue
        bad = None
        for item in evidence:
            bad = _validate_evidence_item(item)
            if bad:
                break
        if bad:
            last_err = bad
            continue
        return {
            "claude_md_append": append.strip(),
            "evidence": evidence,
        }
    raise AdvisorError(f"coach 出力契約違反: {last_err}")


def run_coach_llm(
    cfg: Config,
    context_md: str,
    *,
    generate_fn: Callable | None = None,
) -> dict:
    """LLM で提案 JSON を得る。契約違反時は retries 回まで再試行。"""
    system = load_bundled_prompt("coach")
    system = apply_internal_sentinel(system, cfg.llm.backend)
    redactor = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
    if redactor:
        system = redactor(system)
        context_md = redactor(context_md)
    user = (
        "以下の決定論コンテキストから CLAUDE.md 追記案を JSON で出力してください。\n\n"
        + context_md
    )
    gen = generate_fn or generate_text
    attempts = max(1, int(cfg.llm.retries) + 1)
    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            raw = gen(cfg.llm, system, user)
            result = parse_coach_json(raw)
            if redactor:
                # LLM 出力にも保存前 redact を通す（入力 redact の取りこぼし対策）
                result["claude_md_append"] = redactor(result["claude_md_append"])
            return result
        except (AdvisorError, Exception) as e:
            last_err = e
            user = (
                "前回の出力が契約違反でした。"
                f"エラー: {e}\n"
                "JSON のみ再出力してください。"
                "evidence 各要素は fact_id(str) と value(str|int|float) 必須。\n\n"
                + context_md
            )
    raise AdvisorError(f"coach が契約を満たせませんでした: {last_err}")


def _proposal_frontmatter(
    *,
    as_of: date,
    applied: bool,
    append_md: str,
    evidence: list,
) -> str:
    ev_json = json.dumps(evidence, ensure_ascii=False)
    return (
        "---\n"
        f"date: {as_of.isoformat()}\n"
        f"applied: {'true' if applied else 'false'}\n"
        f"evidence: {ev_json}\n"
        "---\n\n"
        "## CLAUDE.md 追記案\n\n"
        f"{append_md.rstrip()}\n"
    )


def save_proposal(
    memory_dir: Path,
    *,
    as_of: date,
    append_md: str,
    evidence: list,
) -> Path:
    out_dir = Path(memory_dir) / "coach"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{as_of.isoformat()}-proposal.md"
    atomic_write_text(
        path,
        _proposal_frontmatter(
            as_of=as_of, applied=False, append_md=append_md, evidence=evidence
        ),
    )
    return path


def _parse_proposal_text(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    body_start = 0
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_start = i + 1
            break
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    body = "\n".join(lines[body_start:]).strip()
    m = re.search(r"##\s*CLAUDE\.md\s*追記案\s*\n+([\s\S]*)", body)
    append = m.group(1).strip() if m else body
    return fields, append


def proposal_diff_text(append_md: str) -> str:
    """stdout 用 diff 形式（追記先マーカー明示）。"""
    lines = [
        f"--- a/（追記先: <!-- {COACH_MARKER}:start/end -->）",
        f"+++ b/（追記先: <!-- {COACH_MARKER}:start/end -->）",
    ]
    for ln in append_md.splitlines():
        lines.append(f"+{ln}")
    return "\n".join(lines)


def _validate_proposal_for_apply(
    proposal_path: Path,
    targets: list[Path],
) -> tuple[bytes, str, str, list[tuple[Path, bytes, str]]]:
    """事前検証。成功時は (prop_bytes, prop_text, append_md, [(path, orig_bytes, new_content)...])。

    失敗時は AdvisorError（副作用なし）。
    """
    proposal_path = Path(proposal_path)
    if not proposal_path.exists():
        raise AdvisorError(f"提案ファイルが存在しません: {proposal_path}")
    if not proposal_path.is_file():
        raise AdvisorError(f"提案が通常ファイルではありません: {proposal_path}")
    try:
        prop_bytes = proposal_path.read_bytes()
        prop_text = prop_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise AdvisorError(f"提案が UTF-8 ではありません: {proposal_path}") from e
    except OSError as e:
        raise AdvisorError(f"提案を読めません: {e}") from e

    fields, append_md = _parse_proposal_text(prop_text)
    if not fields:
        raise AdvisorError("提案に frontmatter がありません")
    if "applied" not in fields:
        raise AdvisorError("提案ファイルに applied フィールドがありません")
    applied_raw = fields.get("applied", "").strip().lower()
    if applied_raw in ("true", "yes", "1"):
        raise AdvisorError(f"既に適用済みです: {proposal_path}")
    if applied_raw not in ("false", "no", "0"):
        raise AdvisorError(f"applied は false である必要があります: {applied_raw!r}")
    if not append_md.strip():
        raise AdvisorError("提案本文が空です")
    non_empty = [ln for ln in append_md.splitlines() if ln.strip()]
    if not (3 <= len(non_empty) <= 7):
        raise AdvisorError(
            f"追記本文は3-7非空行である必要があります（実際 {len(non_empty)} 行）"
        )
    if not targets:
        raise AdvisorError("targets が空です")

    prepared: list[tuple[Path, bytes, str]] = []
    for t in targets:
        t = Path(t)
        if not t.exists():
            raise AdvisorError(f"target が存在しません: {t}")
        if not t.is_file():
            raise AdvisorError(f"target が通常ファイルではありません: {t}")
        try:
            orig = t.read_bytes()
            content = orig.decode("utf-8")
        except UnicodeDecodeError as e:
            raise AdvisorError(f"target が UTF-8 ではありません: {t}") from e
        except OSError as e:
            raise AdvisorError(f"target を読めません: {t}: {e}") from e
        updated = upsert_section(content, COACH_MARKER, append_md, position="bottom")
        prepared.append((t, orig, updated))
    return prop_bytes, prop_text, append_md, prepared


def apply_proposal(
    proposal_path: Path,
    targets: list[Path],
) -> list[Path]:
    """提案を targets の coach マーカー区間へ書き込む。

    事前検証失敗時は1バイトも書かない。書き込み途中失敗は rollback。
    """
    prop_bytes, prop_text, _append, prepared = _validate_proposal_for_apply(
        proposal_path, targets
    )
    # applied: true に更新した proposal content
    new_prop = re.sub(
        r"(?m)^applied:\s*\S+",
        "applied: true",
        prop_text,
        count=1,
    )
    if new_prop == prop_text:
        raise AdvisorError("applied フィールドの更新に失敗しました")

    try:
        for t, _orig, updated in prepared:
            atomic_write_text(t, updated)
        atomic_write_text(Path(proposal_path), new_prop)
    except Exception as e:
        failed_restore: list[str] = []
        for t, orig, _ in prepared:
            try:
                atomic_write_bytes(t, orig)
            except Exception:
                failed_restore.append(str(t))
        try:
            atomic_write_bytes(Path(proposal_path), prop_bytes)
        except Exception:
            failed_restore.append(str(proposal_path))
        if failed_restore:
            raise AdvisorError(
                f"書き込み失敗後の rollback に失敗: {', '.join(failed_restore)}"
            ) from e
        raise AdvisorError(f"書き込みに失敗し rollback しました: {e}") from e
    return [t for t, _, _ in prepared]
