"""LLM 日次契約の評価ハーネス（プロンプト回帰の実測用）。

`kaizenlog eval record` で実入力をケース化、`eval run` で現バックエンドを
繰り返し実行し、一発合格率・修復後合格率・縮退率を集計する。

pytest はモックバックエンドのみ。実 LLM は手動 `eval run` 時のみ。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .advice_evidence import AdviceEvidence, build_advice_evidence
from .advisor import (
    PipelineReport,
    _run_daily_pipeline,
    prepare_advice_request,
    requires_daily_contract,
)
from .config import Config, LLMConfig
from .privacy import make_redactor


CASE_SCHEMA_VERSION = 1


@dataclass
class EvalCase:
    """advise 入力スナップショット。"""

    id: str
    day: str
    current_stats: dict[str, Any] | None
    prior_stats: list[dict[str, Any]]
    today_md: str
    recent_summaries: list[str] = field(default_factory=list)
    intent: str | None = None
    experiments_ctx: str | None = None
    memory_ctx: str | None = None
    source_status: str = "verified"
    timezone: str = "Asia/Tokyo"
    known_categories: list[str] = field(default_factory=list)
    schema_version: int = CASE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalCase:
        return cls(
            id=str(data.get("id") or "case"),
            day=str(data.get("day") or ""),
            current_stats=data.get("current_stats"),
            prior_stats=list(data.get("prior_stats") or []),
            today_md=str(data.get("today_md") or ""),
            recent_summaries=list(data.get("recent_summaries") or []),
            intent=data.get("intent"),
            experiments_ctx=data.get("experiments_ctx"),
            memory_ctx=data.get("memory_ctx"),
            source_status=str(data.get("source_status") or "verified"),
            timezone=str(data.get("timezone") or "Asia/Tokyo"),
            known_categories=list(data.get("known_categories") or []),
            schema_version=int(data.get("schema_version") or CASE_SCHEMA_VERSION),
        )


@dataclass
class CaseRunResult:
    case_id: str
    report: PipelineReport
    error: str | None = None


@dataclass
class EvalAggregate:
    total_runs: int = 0
    first_pass: int = 0
    final_ok: int = 0
    degraded: int = 0
    repaired_success: int = 0
    duration_sum: float = 0.0
    by_case: dict[str, list[CaseRunResult]] = field(default_factory=dict)

    @property
    def first_pass_rate(self) -> float:
        return self.first_pass / self.total_runs if self.total_runs else 0.0

    @property
    def final_pass_rate(self) -> float:
        return self.final_ok / self.total_runs if self.total_runs else 0.0

    @property
    def degraded_rate(self) -> float:
        return self.degraded / self.total_runs if self.total_runs else 0.0

    @property
    def mean_duration(self) -> float:
        return self.duration_sum / self.total_runs if self.total_runs else 0.0


def _deep_redact(obj: Any, redactor: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return redactor(obj)
    if isinstance(obj, dict):
        return {k: _deep_redact(v, redactor) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_redact(v, redactor) for v in obj]
    return obj


def redact_case(case: EvalCase, redactor: Callable[[str], str]) -> EvalCase:
    """ケース全体に privacy redactor を適用したコピーを返す。"""
    d = _deep_redact(case.to_dict(), redactor)
    return EvalCase.from_dict(d)


def default_cases_dir(cfg: Config) -> Path:
    """ユーザーが record したケースの既定先（vault 配下、gitignore 対象）。"""
    return Path(cfg.vault_dir).expanduser() / ".kaizenlog" / "eval" / "cases"


def cwd_eval_cases_dir() -> Path:
    """リポジトリ/CWD 直下の eval/cases（gitignore 対象）。"""
    return Path("eval") / "cases"


def repo_eval_samples_dir() -> Path | None:
    """ソースツリーの eval/samples（リポジトリ同梱）。無ければ None。"""
    # src/kaizenlog/evalharness.py → parents[2] == リポジトリルート
    cand = Path(__file__).resolve().parents[2] / "eval" / "samples"
    if cand.is_dir() and any(cand.glob("*.json")):
        return cand
    # CWD からの相対（クローン直下で実行時）
    cwd_cand = Path("eval") / "samples"
    if cwd_cand.is_dir() and any(cwd_cand.glob("*.json")):
        return cwd_cand.resolve()
    return None


def package_samples_dir() -> Path:
    """同梱サンプル: リポジトリ eval/samples を優先、なければパッケージ内。"""
    from importlib import resources

    repo = repo_eval_samples_dir()
    if repo is not None:
        return repo
    return Path(resources.files("kaizenlog") / "eval_samples")


def _dir_has_cases(directory: Path) -> bool:
    return directory.is_dir() and any(directory.glob("*.json"))


def resolve_eval_cases_dir(
    cfg: Config, explicit: Path | None = None
) -> tuple[Path, bool]:
    """評価ケースディレクトリを解決する。

    戻り値: (path, used_bundled_samples)
    優先順: 明示 --cases → vault 既定 cases → CWD eval/cases → 同梱 samples
    """
    if explicit is not None:
        return explicit, False
    user = default_cases_dir(cfg)
    if _dir_has_cases(user):
        return user, False
    cwd_cases = cwd_eval_cases_dir()
    if _dir_has_cases(cwd_cases):
        return cwd_cases.resolve(), False
    return package_samples_dir(), True


def save_case(path: Path, case: EvalCase) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(case.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_case(path: Path) -> EvalCase:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"ケースがオブジェクトではありません: {path}")
    return EvalCase.from_dict(data)


def load_cases_dir(directory: Path) -> list[EvalCase]:
    if not directory.is_dir():
        return []
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.json")):
        try:
            cases.append(load_case(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return cases


def build_case_from_inputs(
    *,
    day: date,
    current_stats: dict | None,
    prior_stats: list[dict],
    today_md: str,
    recent_summaries: list[str],
    intent: str | None,
    experiments_ctx: str | None,
    memory_ctx: str | None,
    source_status: str,
    timezone: str,
    known_categories: list[str],
    case_id: str | None = None,
) -> EvalCase:
    cid = case_id or f"case-{day.isoformat()}"
    return EvalCase(
        id=cid,
        day=day.isoformat(),
        current_stats=current_stats,
        prior_stats=prior_stats,
        today_md=today_md,
        recent_summaries=recent_summaries,
        intent=intent,
        experiments_ctx=experiments_ctx,
        memory_ctx=memory_ctx,
        source_status=source_status,
        timezone=timezone,
        known_categories=known_categories,
    )


def _evidence_for_case(case: EvalCase) -> AdviceEvidence:
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(case.timezone)
    except Exception:
        tz = ZoneInfo("Asia/Tokyo")
    return build_advice_evidence(
        case.current_stats,
        case.prior_stats,
        timezone=tz,
        source_status=case.source_status,
        known_categories=case.known_categories or None,
    )


def run_case(
    case: EvalCase,
    llm: LLMConfig,
    *,
    generate_fn: Callable[[LLMConfig, str, str], str] | None = None,
    redactor: Callable[[str], str] | None = None,
) -> CaseRunResult:
    """1ケースを1回実行。"""
    evidence = _evidence_for_case(case)
    system_prompt, prompt, evidence_ctx = prepare_advice_request(
        llm,
        case.today_md,
        case.recent_summaries,
        case.intent,
        case.experiments_ctx,
        case.memory_ctx,
        redactor,
        evidence,
    )
    if not requires_daily_contract(llm):
        # 日次契約外は passthrough 扱い（集計では final_ok=True としない）
        report = PipelineReport(
            outcome="passthrough",
            duration_seconds=0.0,
        )
        return CaseRunResult(case_id=case.id, report=report, error="not daily contract")

    assert evidence_ctx is not None
    try:
        _md, report = _run_daily_pipeline(
            llm,
            system_prompt,
            prompt,
            evidence_ctx,
            redactor=redactor,
            generate_fn=generate_fn,
        )
        return CaseRunResult(case_id=case.id, report=report)
    except Exception as e:
        report = PipelineReport(
            outcome="degraded",
            final_ok=False,
            final_violations=[type(e).__name__],
        )
        return CaseRunResult(case_id=case.id, report=report, error=type(e).__name__)


def run_eval(
    cases: list[EvalCase],
    llm: LLMConfig,
    *,
    repeat: int = 1,
    generate_fn: Callable[[LLMConfig, str, str], str] | None = None,
    redactor: Callable[[str], str] | None = None,
) -> EvalAggregate:
    agg = EvalAggregate()
    n = max(1, int(repeat))
    for case in cases:
        agg.by_case.setdefault(case.id, [])
        for _ in range(n):
            result = run_case(case, llm, generate_fn=generate_fn, redactor=redactor)
            agg.by_case[case.id].append(result)
            agg.total_runs += 1
            r = result.report
            agg.duration_sum += float(r.duration_seconds or 0.0)
            if r.first_pass:
                agg.first_pass += 1
            if r.final_ok:
                agg.final_ok += 1
                if r.repaired:
                    agg.repaired_success += 1
            if r.outcome == "degraded" or not r.final_ok:
                if not r.final_ok:
                    agg.degraded += 1
    return agg


def format_eval_table(agg: EvalAggregate) -> str:
    lines = [
        "# KaizenLog eval 結果",
        "",
        f"実行回数: {agg.total_runs}",
        f"一発合格率: {agg.first_pass_rate * 100:.1f}% ({agg.first_pass}/{agg.total_runs})",
        f"修復後合格率: {agg.final_pass_rate * 100:.1f}% ({agg.final_ok}/{agg.total_runs})",
        f"縮退率: {agg.degraded_rate * 100:.1f}% ({agg.degraded}/{agg.total_runs})",
        f"平均所要秒: {agg.mean_duration:.2f}",
        "",
        "| ケース | 一発合格 | 最終合格 | 縮退 | 平均秒 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for cid, results in sorted(agg.by_case.items()):
        n = len(results)
        fp = sum(1 for r in results if r.report.first_pass)
        fo = sum(1 for r in results if r.report.final_ok)
        dg = sum(1 for r in results if not r.report.final_ok)
        dur = sum(r.report.duration_seconds for r in results) / n if n else 0.0
        lines.append(
            f"| {cid} | {fp}/{n} | {fo}/{n} | {dg}/{n} | {dur:.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def safe_case_filename(case_id: str, day: str) -> str:
    raw = f"{day}_{case_id}"
    return re.sub(r"[^\w.\-]+", "_", raw)[:80] + ".json"
