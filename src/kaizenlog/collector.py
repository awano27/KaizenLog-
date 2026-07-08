"""ActivityWatch REST API からイベントを取得し、AFK時間を除外した活動イベントに整形する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import requests


@dataclass
class ActivityEvent:
    start: datetime
    end: datetime
    app: str
    title: str

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


class ActivityWatchError(RuntimeError):
    pass


class ActivityWatchClient:
    def __init__(self, base_url: str = "http://localhost:5600", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, **params):
        try:
            r = requests.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        except requests.ConnectionError as e:
            raise ActivityWatchError(
                f"ActivityWatchに接続できません ({self.base_url})。"
                "ActivityWatchが起動しているか確認してください。"
            ) from e
        r.raise_for_status()
        return r.json()

    def buckets(self) -> dict:
        return self._get("/api/0/buckets/")

    def find_bucket(self, bucket_type: str) -> str | None:
        """指定タイプ（currentwindow / afkstatus など）のバケットIDを返す。"""
        for bucket_id, info in self.buckets().items():
            if info.get("type") == bucket_type:
                return bucket_id
        return None

    def events(self, bucket_id: str, start: datetime, end: datetime) -> list[dict]:
        return self._get(
            f"/api/0/buckets/{bucket_id}/events",
            start=start.isoformat(),
            end=end.isoformat(),
            limit=-1,
        )


def _parse_events(raw: list[dict]) -> list[tuple[datetime, datetime, dict]]:
    """AWイベントを (start, end, data) に変換して時系列順に並べる。"""
    out = []
    for ev in raw:
        ts = ev.get("timestamp", "")
        start = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        end = start + timedelta(seconds=float(ev.get("duration", 0)))
        out.append((start, end, ev.get("data", {})))
    out.sort(key=lambda x: x[0])
    return out


def _intersect(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> tuple[datetime, datetime] | None:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return (start, end) if start < end else None


def active_intervals(afk_raw: list[dict]) -> list[tuple[datetime, datetime]]:
    """AFKウォッチャーのイベントから「PCの前にいた」区間を抽出する。"""
    intervals = []
    for start, end, data in _parse_events(afk_raw):
        if data.get("status") == "not-afk":
            intervals.append((start, end))
    return intervals


def clip_to_active(
    window_raw: list[dict], intervals: list[tuple[datetime, datetime]]
) -> list[ActivityEvent]:
    """ウィンドウイベントをアクティブ区間で切り出す。AFK中の「開きっぱなし」を除外する。"""
    events: list[ActivityEvent] = []
    for start, end, data in _parse_events(window_raw):
        for a_start, a_end in intervals:
            if a_end <= start:
                continue
            if a_start >= end:
                break
            clipped = _intersect(start, end, a_start, a_end)
            if clipped:
                events.append(
                    ActivityEvent(
                        start=clipped[0],
                        end=clipped[1],
                        app=str(data.get("app", "")).strip() or "unknown",
                        title=str(data.get("title", "")).strip(),
                    )
                )
    events.sort(key=lambda e: e.start)
    return events


def collect_day(
    client: ActivityWatchClient, day_start: datetime, day_end: datetime
) -> list[ActivityEvent]:
    """1日分のアクティブなウィンドウイベントを取得する。

    AFKウォッチャーが無い環境では、ウィンドウイベントをそのまま使う。
    """
    window_bucket = client.find_bucket("currentwindow")
    if window_bucket is None:
        raise ActivityWatchError(
            "ウィンドウウォッチャーのバケットが見つかりません。"
            "aw-watcher-window が動作しているか確認してください。"
        )
    window_raw = client.events(window_bucket, day_start, day_end)

    afk_bucket = client.find_bucket("afkstatus")
    if afk_bucket is None:
        return clip_to_active(window_raw, [(day_start, day_end)])

    afk_raw = client.events(afk_bucket, day_start, day_end)
    intervals = active_intervals(afk_raw)
    if not intervals:
        # AFKデータが空の日はウィンドウイベントをそのまま採用する
        return clip_to_active(window_raw, [(day_start, day_end)])
    return clip_to_active(window_raw, intervals)
