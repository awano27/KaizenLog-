"""Reader-safe fallback diagnostics; machine evidence stays out of daily notes."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .privacy import make_redactor
from .privacy_filter import is_private_block
from .vault import atomic_write_text


def safe_text(text: str, cfg) -> str:
    redact = make_redactor(cfg.privacy.redact_patterns, cfg.privacy.replacement)
    private = getattr(cfg.privacy, 'private_title_patterns', ())
    lines = []
    for line in text.splitlines():
        if is_private_block(title=line, private_title_patterns=private):
            lines.append('（私的・非表示）')
        else:
            lines.append(redact(line) if redact else line)
    return '\n'.join(lines)


def archive_evidence(cfg, day: date, evidence) -> Path:
    """Keep the latest sanitized diagnostic per day, never raw prompts or titles."""
    markdown = getattr(evidence, 'markdown', '')
    if not isinstance(markdown, str):
        markdown = ''
    path = cfg.logs_path / f'advice-evidence-{day.isoformat()}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, safe_text(markdown, cfg))
    return path


def repair_guidance(runs: list[dict]) -> str:
    """Classify only safe runlog tags, never echo arbitrary failure text."""
    latest = next((r for r in reversed(runs)
                   if r.get('command') == 'advise_health'), None)
    if latest is None or latest.get('outcome') not in ('degraded', 'failed'):
        return ''
    raw = latest.get('violations')
    kinds = {tag for tag in raw if isinstance(tag, str)} if isinstance(raw, list) else set()
    if kinds & {'watcher', 'ai_metrics', 'previous_day', 'site', 'semantic'}:
        return '観測データ: `kaizenlog doctor` で取得状態を確認し、復旧後に `kaizenlog generate --date YYYY-MM-DD` を実行してください。'
    if kinds & {'json', 'heading', 'cardinality', 'kzn'}:
        return '出力形式: LLM設定とプロンプトを確認し、`kaizenlog advise --dry-run` で送信予定を確認してください（外部送信なし）。'
    if kinds:
        return '提案条件: `kaizenlog status` で条件違反を確認し、`kaizenlog advise --dry-run` で根拠と条件を照合してください。'
    return '原因未分類: `kaizenlog status` でproviderの状態と原因コードを確認してください。'
