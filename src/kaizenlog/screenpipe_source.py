"""screenpipe REST API クライアント（read-only・fail-closed）。

使用可: GET /health, GET /search, GET /activity-summary のみ。
content_type=input/memory・cloud・書き込み系は禁止。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

# ---- 自己参照除外（日誌・指示書・自プロダクトの再取り込みを防ぐ） ----
SELF_REFERENCE_PATTERNS: tuple[str, ...] = (
    "kaizenlog",
    "KZN-",
    "効果指標:",
    "📌 今日のアクション",
    "今日のアクション",
    "kaizenlog:advice",
    "kaizenlog:actions",
    "第42弾",
    "screenpipe画面観測",
)

# 実測で毎フレーム付く UI 定型語
UI_CHROME_STOPLIST: frozenset[str] = frozenset(
    {
        "最小化",
        "復元",
        "閉じる",
        "ファイル",
        "編集",
        "表示",
        "ヘルプ",
        "検索",
        "送信",
        "新しいチャット",
        "前へ",
        "進む",
        "ターミナルで実行",
        "コードをコピー",
        "サイドバーを非表示にする",
        "優先度でフィルター",
    }
)

_CJK_SPACE_RE = re.compile(
    r"(?<=[\u3040-\u30ff\u3400-\u9fff])\s(?=[\u3040-\u30ff\u3400-\u9fff])"
)
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_DIGITS_ONLY_RE = re.compile(r"^[\d\W_]+$", re.UNICODE)
_PII_MARKERS_RE = re.compile(
    r"\[URL_WITH_CREDENTIALS\]|\[REDACTED\]|\(truncated\s+\d+\s+chars\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScreenText:
    ts_local: datetime
    app_name: str
    window_name: str
    text: str
    browser_url: str = ""


def normalize_app_name(app: str | None) -> str:
    """ChatGPT.exe → ChatGPT。照合用に拡張子を外す。"""
    s = (app or "").strip()
    if s.lower().endswith(".exe"):
        s = s[:-4]
    return s


def is_localhost_url(url: str) -> bool:
    """True only for plain http:// loopback hosts (no userinfo / no prefix tricks)."""
    u = (url or "").strip()
    if not u:
        return False
    try:
        parsed = urllib.parse.urlparse(u)
    except ValueError:
        return False
    if parsed.scheme.lower() != "http":
        return False
    # Reject credentials that could re-route or smuggle via userinfo.
    if parsed.username is not None or parsed.password is not None:
        return False
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


_SCREEN_TEXT_EXCERPT_RE = re.compile(r"（画面テキスト:\s*(.+)）\s*(?:\||$)")


def extract_screen_text_excerpt(line: str) -> str | None:
    """日誌行 / 表セルから「画面テキスト」要約を取り出す。

    全角括弧の入れ子（例: 関数（foo））でも、閉じは行末または次セル `|` 手前に合わせる。
    """
    if not line or "画面テキスト:" not in line:
        return None
    m = _SCREEN_TEXT_EXCERPT_RE.search(line)
    if not m:
        return None
    text = m.group(1).strip()
    return text or None


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(raw: object, tz: tzinfo) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _extract_content_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """入れ子 {"type","content"} とフラット形の両方を dict リストへ。"""
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        if "content" in item and isinstance(item.get("content"), Mapping):
            content = dict(item["content"])
            content["_type"] = str(item.get("type") or "")
            out.append(content)
        else:
            out.append(dict(item))
    return out


def _item_to_screen_text(item: Mapping[str, Any], tz: tzinfo) -> ScreenText | None:
    text = str(item.get("text") or "").strip()
    if not text:
        return None
    ts = _parse_ts(item.get("timestamp"), tz)
    if ts is None:
        return None
    return ScreenText(
        ts_local=ts,
        app_name=str(item.get("app_name") or ""),
        window_name=str(item.get("window_name") or ""),
        text=text,
        browser_url=str(item.get("browser_url") or ""),
    )


def summarize_screen_texts(
    items: Sequence[ScreenText],
    max_lines: int = 3,
    max_chars: int = 120,
    *,
    self_paths: Sequence[str] = (),
) -> list[str]:
    """画面テキストを決定論要約する（ネットワーク非依存）。"""
    lines: list[str] = []
    for it in items:
        raw = it.text or ""
        # 行分割: 改行 + 連続2space以上
        chunks = re.split(r"\n+|\s{2,}", raw)
        for ch in chunks:
            line = ch.strip()
            if not line:
                continue
            line = _PII_MARKERS_RE.sub("", line)
            line = _CJK_SPACE_RE.sub("", line)
            line = _MULTI_SPACE_RE.sub(" ", line).strip()
            if len(line) < 12:
                continue
            if _DIGITS_ONLY_RE.match(line):
                continue
            # UI 定型句のみの行
            tokens = [t for t in re.split(r"\s+", line) if t]
            if tokens and all(t in UI_CHROME_STOPLIST for t in tokens):
                continue
            if any(p.lower() in line.lower() for p in SELF_REFERENCE_PATTERNS):
                continue
            # パス自己参照
            low = line.lower().replace("\\", "/")
            skip_path = False
            for p in self_paths:
                pp = str(p or "").strip().replace("\\", "/").lower()
                if pp and pp in low:
                    skip_path = True
                    break
            if skip_path:
                continue
            # 行内の UI 語を除去しすぎないが、行全体が定型なら既に除外済み
            lines.append(line)

    if not lines:
        return []

    # 頻度×長さでランク
    counts = Counter(lines)
    ranked = sorted(
        counts.keys(),
        key=lambda s: (-counts[s] * max(len(s), 1), -len(s), s),
    )
    # 重複・包含除去
    selected: list[str] = []
    for cand in ranked:
        if any(cand == s or cand in s or s in cand for s in selected):
            continue
        selected.append(cand)
        if len(selected) >= max_lines:
            break
    return [s if len(s) <= max_chars else s[: max_chars - 1] + "…" for s in selected]


class ScreenpipeClient:
    """localhost screenpipe への read-only クライアント。"""

    def __init__(
        self,
        base_url: str = "http://localhost:3030",
        *,
        api_key: str | None = None,
        timeout_seconds: float = 3.0,
        tz: tzinfo | None = None,
        urlopen: Callable[..., Any] | None = None,
        max_content_length: int = 2000,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip() or None
        self.timeout_seconds = float(timeout_seconds)
        self.tz = tz or ZoneInfo("Asia/Tokyo")
        self._urlopen = urlopen or urllib.request.urlopen
        self.max_content_length = int(max_content_length)
        self._warned = False
        self.last_warning: str | None = None
        # 一度でも照会に失敗したら以降の照会を打ち切る（サービス停止時に
        # ブロック数×timeout で generate が止まるのを防ぐ）
        self._dead = False

    def _warn_once(self, msg: str) -> None:
        # 最初の警告だけを残す（generate 1回につき1行の契約）
        if not self._warned:
            self._warned = True
            self.last_warning = msg

    def health(self) -> dict[str, Any] | None:
        """認証不要。失敗時 None。"""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/health",
                method="GET",
                headers={"Accept": "application/json"},
            )
            with self._urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001 — fail-closed
            self._warn_once(f"screenpipe health 失敗: {type(e).__name__}")
            return None

    def _get_json(self, path: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        if self._dead:
            return None
        if not self.api_key:
            self._warn_once("screenpipe: API キー未設定")
            self._dead = True
            return None
        # 禁止パラメータの混入を防ぐ
        for banned in ("include_cloud",):
            if banned in params:
                raise ValueError(f"forbidden param: {banned}")
        ct = params.get("content_type")
        if ct in ("input", "memory"):
            raise ValueError(f"forbidden content_type: {ct}")

        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.base_url}{path}"
        if q:
            url = f"{url}?{q}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            req = urllib.request.Request(url, method="GET", headers=headers)
            with self._urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
        except urllib.error.HTTPError as e:
            if e.code == 403:
                self._warn_once("screenpipe: 認証エラー（API キーを確認）")
            else:
                self._warn_once(f"screenpipe HTTP {e.code}")
            self._dead = True
            return None
        except Exception as e:  # noqa: BLE001
            self._warn_once(f"screenpipe 照会失敗: {type(e).__name__}")
            self._dead = True
            return None

    def search_text(
        self,
        app_name: str | None,
        start_local: datetime,
        end_local: datetime,
        *,
        min_length: int = 25,
        limit: int = 60,
    ) -> list[ScreenText]:
        """OCR 優先、0件なら accessibility。失敗時は空リスト。"""
        base_params: dict[str, Any] = {
            "start_time": _to_utc_iso(start_local),
            "end_time": _to_utc_iso(end_local),
            "min_length": int(min_length),
            "limit": int(limit),
            "max_content_length": self.max_content_length,
            "focused": "true",
        }
        app = normalize_app_name(app_name)
        if app:
            base_params["app_name"] = app

        items: list[ScreenText] = []
        for ctype in ("ocr", "accessibility"):
            params = {**base_params, "content_type": ctype}
            payload = self._get_json("/search", params)
            if payload is None:
                continue
            for raw_item in _extract_content_items(payload):
                st = _item_to_screen_text(raw_item, self.tz)
                if st is not None:
                    items.append(st)
            if items:
                break
        return items


def resolve_api_key(api_key_env: str) -> str | None:
    name = (api_key_env or "").strip()
    if not name:
        return None
    val = os.environ.get(name, "").strip()
    return val or None


def block_fill_key(start: datetime, end: datetime, app: str) -> str:
    return f"{start.isoformat()}|{end.isoformat()}|{app}"


def collect_screen_fills_for_ai_blocks(
    blocks: Sequence[Any],
    spans: Sequence[Any],
    client: ScreenpipeClient,
    *,
    redactor: Callable[[str], str] | None = None,
    max_lines: int = 3,
    max_chars: int = 120,
    self_paths: Sequence[str] = (),
    min_block_minutes: float = 3.0,
) -> tuple[dict[str, str], dict[str, int], list[dict[str, Any]]]:
    """未突合 AI ブロックだけ screenpipe 照会。

    Returns:
        fills: block_key → redact 済み要約1行
        stats: queried_blocks / filled_blocks
        samples: S5/S6 用メタ（start/end/app/summary）最大3
    """
    from .report import _best_session_label  # local import avoid cycle at module load

    fills: dict[str, str] = {}
    samples: list[dict[str, Any]] = []
    queried = 0
    filled = 0
    for b in blocks:
        if not getattr(b, "ai", False):
            continue
        minutes = float(getattr(b, "minutes", 0) or 0)
        if minutes < min_block_minutes:
            continue
        if _best_session_label(b, spans) is not None:
            continue
        queried += 1
        app = str(getattr(b, "app", "") or "")
        start = getattr(b, "start", None)
        end = getattr(b, "end", None)
        if start is None or end is None:
            continue
        texts = client.search_text(normalize_app_name(app) or app, start, end)
        summaries = summarize_screen_texts(
            texts,
            max_lines=max_lines,
            max_chars=max_chars,
            self_paths=self_paths,
        )
        if not summaries:
            continue
        summary = summaries[0]
        if redactor is not None:
            summary = redactor(summary)
        if not summary:
            continue
        key = block_fill_key(start, end, app)
        fills[key] = summary
        filled += 1
        if len(samples) < 3:
            samples.append(
                {
                    "start": start,
                    "end": end,
                    "app": normalize_app_name(app) or app,
                    "summary": summary,
                }
            )
    return fills, {"queried_blocks": queried, "filled_blocks": filled}, samples
