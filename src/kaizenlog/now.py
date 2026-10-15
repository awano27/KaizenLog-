"""Read-only view of the current day's observations; no ledger or note writes."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from .advice_fallback import safe_text
from .aiwork import available_adapters
from .classifier import Classifier
from .collector import ActivityWatchClient, collect_day, collect_input_observation
from .focus import compute_input_stats
from .reliability import QualityState
from .report import render_markdown, summarize


def render_now(cfg, *, now: datetime | None = None, client=None) -> str:
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz) if now is None else now.astimezone(tz)
    start = datetime.combine(now.date(), time.min, tzinfo=tz)
    client = client if client is not None else ActivityWatchClient(cfg.aw_base_url)
    events, afk_ok = collect_day(client, start, now)
    summary = summarize(now.date(), Classifier(cfg.rules).classify_all(events),
                        gap_minutes=cfg.session_gap_minutes)
    observation = collect_input_observation(client, start, now)
    observed = observation.state is QualityState.OBSERVED
    input_stats = compute_input_stats(observation.events, day_start=start, day_end=now) if observed else None
    lines = [f'# 今日の途中経過（{now.isoformat(timespec="minutes")} まで）', '',
             render_markdown(summary, tz, min_block_minutes=cfg.min_block_minutes,
                             max_timeline_rows=10, input_stats=input_stats,
                             private_title_patterns=getattr(cfg.privacy, 'private_title_patterns', ())),
             '', '## 計測の状態', '']
    if not afk_ok:
        lines.append('- 離席: 未判定（aw-watcher-afk の当日イベントが必要）。表示時間には離席が含まれ得ます。')
    if observed:
        lines.append('- 集中ブロック: 当日の入力イベントから計測済み（達成判定ではありません）。')
    else:
        lines.append('- 集中ブロック: 未判定。aw-watcher-input の当日イベントを受信すると表示できます。')
    adapters = available_adapters(cfg)
    source_names = ', '.join(a.name for a in adapters)
    lines.append('- AI作業の質: 未判定。' + (f'収集先を検出: {source_names}（ログ内容の計測は日次generate時）。'
                                           if adapters else '対応するローカルセッションログを設定すると計測できます。'))
    lines.append('- screenpipe: ' + ('有効設定（この画面では接続未確認）。' if cfg.screenpipe.enabled
                                      else '無効。利用する場合は接続先を設定して明示的に有効化してください。'))
    return safe_text('\n'.join(lines), cfg)
