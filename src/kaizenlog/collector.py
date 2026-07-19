"""ActivityWatch REST API からイベントを取得し、AFK時間を除外した活動イベントに整形する。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

# タブ情報の合成対象とするブラウザのプロセス名
BROWSER_APP_RE = re.compile(r"chrome|msedge|\bedge\b|firefox|brave|vivaldi|opera", re.IGNORECASE)


@dataclass
class ActivityEvent:
    start: datetime
    end: datetime
    app: str
    title: str
    url: str = ""  # aw-watcher-web 導入時のみ入る（ブラウザのタブURL）

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def domain(self) -> str:
        """URLのドメイン部分（www.除去済み）。URLが無ければ空文字。"""
        if not self.url:
            return ""
        netloc = urlparse(self.url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc


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

    def find_buckets(self, bucket_type: str) -> list[str]:
        """指定タイプのバケットIDを全て返す。

        web.tab.current はブラウザごと（Chrome/Edge/Firefox）に別バケットに
        なるため、1つだけ拾うとデータを取りこぼす。
        """
        return [bid for bid, info in self.buckets().items()
                if info.get("type") == bucket_type]

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


def enrich_with_web(
    events: list[ActivityEvent], web_raw: list[dict]
) -> list[ActivityEvent]:
    """ブラウザのウィンドウイベントに、同時刻のタブ情報（URL・タイトル）を合成する。

    ブラウザがアクティブな区間とタブイベント（aw-watcher-web）の交差部分を
    新しいイベントに分割し、タブ側のURL/タイトルを採用する。タブ情報が無い
    残り区間は元のイベントのまま残す。非ブラウザのイベントは変更しない。
    """
    tabs = _parse_events(web_raw)
    out: list[ActivityEvent] = []
    for ev in events:
        if not BROWSER_APP_RE.search(ev.app):
            out.append(ev)
            continue
        cursor = ev.start
        pieces: list[ActivityEvent] = []
        for t_start, t_end, data in tabs:
            if t_end <= cursor:
                continue
            if t_start >= ev.end:
                break
            clipped = _intersect(cursor, ev.end, t_start, t_end)
            if not clipped:
                continue
            c_start, c_end = clipped
            if c_start > cursor:
                pieces.append(ActivityEvent(cursor, c_start, ev.app, ev.title))
            pieces.append(
                ActivityEvent(
                    c_start,
                    c_end,
                    ev.app,
                    str(data.get("title", "")).strip() or ev.title,
                    url=str(data.get("url", "")).strip(),
                )
            )
            cursor = c_end
        if cursor < ev.end:
            pieces.append(ActivityEvent(cursor, ev.end, ev.app, ev.title))
        out.extend(pieces or [ev])
    out.sort(key=lambda e: e.start)
    return out


def collect_day(
    client: ActivityWatchClient, day_start: datetime, day_end: datetime
) -> list[ActivityEvent]:
    """1日分のアクティブなウィンドウイベントを取得する。

    AFKウォッチャーが無い環境では、ウィンドウイベントをそのまま使う。
    aw-watcher-web（ブラウザ拡張）が導入されていれば、ブラウザ時間を
    タブURL粒度に分割して返す。
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
        events = clip_to_active(window_raw, [(day_start, day_end)])
    else:
        afk_raw = client.events(afk_bucket, day_start, day_end)
        intervals = active_intervals(afk_raw)
        # AFKデータが空の日はウィンドウイベントをそのまま採用する
        events = clip_to_active(window_raw, intervals or [(day_start, day_end)])

    web_raw: list[dict] = []
    for web_bucket in client.find_buckets("web.tab.current"):
        web_raw.extend(client.events(web_bucket, day_start, day_end))
    if web_raw:
        events = enrich_with_web(events, web_raw)
    return events


def collect_input(
    client: ActivityWatchClient, day_start: datetime, day_end: datetime
) -> list[dict] | None:
    """入力量イベント（aw-watcher-input）を取得する。watcher未導入ならNone。"""
    bucket = client.find_bucket("os.hid.input")
    if bucket is None:
        return None
    return client.events(bucket, day_start, day_end)
