"""ブラウザ AI テレメトリ アダプタ。

ブラウザ拡張が chatgpt.com / claude.ai / gemini.google.com の DOM から
ローカル JSONL を書き出し、ここが読み取る。ネットワーク送信は拡張にも
本アダプタにも無い。

JSONL 1行: {ts, site, conversation_id, role, char_count, text?}
  text はオプション（「本文を保存しない」モードでは欠落）。

トークン数は DOM から取得不能なため捏造しない。assistant の文字数は
assistant_chars に保持し、output_tokens / コスト行には混ぜない。
ツールエラー・中断は概念が無いため tools_measurable=False（表では `-`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .aiwork import AISession, UserPrompt, _parse_ts, extract_session_title

# site → source / 表示ラベル
_SITE_META: dict[str, tuple[str, str]] = {
    "chatgpt.com": ("chatgpt-web", "chatgpt"),
    "claude.ai": ("claude-web", "claude"),
    "gemini.google.com": ("gemini-web", "gemini"),
}


def _site_source(site: str) -> str:
    meta = _SITE_META.get(str(site).lower())
    return meta[0] if meta else f"{site}-web"


def _site_label(site: str) -> str:
    meta = _SITE_META.get(str(site).lower())
    return meta[1] if meta else str(site)


@dataclass
class _ConvAccum:
    site: str
    conversation_id: str
    source: str
    start: datetime | None = None
    end: datetime | None = None
    user_turns: int = 0
    assistant_chars: int = 0
    first_user_text: str | None = None
    has_user_text: bool = False
    rows: int = 0
    # §G1: 本文保存モードでのみ prompts_digest を積む
    _user_prompts_raw: list[str] = field(default_factory=list, repr=False)

    def add(self, ts: datetime, role: str, char_count: int, text: str | None) -> None:
        from .aiwork import _is_system_wrapper, normalize_prompt_text

        self.rows += 1
        self.start = ts if self.start is None else min(self.start, ts)
        self.end = ts if self.end is None else max(self.end, ts)
        role_l = (role or "").lower()
        if role_l in ("user", "human"):
            self.user_turns += 1
            if self.first_user_text is None:
                if text and str(text).strip():
                    self.first_user_text = str(text)
                    self.has_user_text = True
                else:
                    # メタデータのみ: 本文なしフラグ
                    self.first_user_text = ""
                    self.has_user_text = False
            # 本文がある発話のみ digest バッファへ（未保存モードは後で捨てる）
            if text and str(text).strip() and not _is_system_wrapper(str(text)):
                cleaned = normalize_prompt_text(str(text))
                if cleaned:
                    self._user_prompts_raw.append(cleaned)
        elif role_l in ("assistant", "model", "ai"):
            self.assistant_chars += max(0, int(char_count or 0))
            if text and not char_count:
                self.assistant_chars += len(str(text))

    def to_session(self) -> AISession | None:
        if self.start is None or self.end is None:
            return None
        if self.user_turns <= 0 and self.assistant_chars <= 0:
            return None
        from .aiwork import finalize_session_io_digest

        title: str | None
        first_len = 0
        prompts: list[str] = []
        if self.has_user_text and self.first_user_text:
            extracted = extract_session_title(self.first_user_text)
            if extracted:
                title, first_len = extracted
            else:
                title = None
            prompts = list(self._user_prompts_raw)
        else:
            # 本文未保存モードでも表・層別以外は動く
            title = "（本文未保存）"
            first_len = 0
            prompts = []  # 推測で埋めない
        sid = f"{self.site}:{self.conversation_id}"
        session = AISession(
            session_id=sid,
            project=_site_label(self.site),
            start=self.start,
            end=self.end,
            user_turns=self.user_turns,
            source=self.source,
            title=title,
            first_prompt_len=first_len,
            tools_measurable=False,
            assistant_chars=self.assistant_chars,
            # トークンは設定しない（0 のまま。コストに混入させない）
            output_tokens=0,
            _user_prompts_raw=prompts,
        )
        finalize_session_io_digest(session)
        return session


def _day_files(export_dir: Path, day_start: datetime, day_end: datetime) -> list[Path]:
    """対象日と前日の YYYY-MM-DD.jsonl（深夜跨ぎ）。"""
    dates = set()
    for dt in (day_start - timedelta(days=1), day_start, day_end - timedelta(seconds=1)):
        local = dt.astimezone() if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone()
        dates.add(local.date())
    files: list[Path] = []
    for d in sorted(dates):
        path = export_dir / f"{d.isoformat()}.jsonl"
        if path.is_file():
            files.append(path)
    return files


def _load_records(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


class BrowserAIAdapter:
    """拡張が書き出した kaizenlog-browser-ai/*.jsonl を読むアダプタ。"""

    name = "browser-ai"

    def __init__(self, export_dir: Path):
        self.export_dir = Path(export_dir)

    def scan_sessions(
        self, day_start: datetime, day_end: datetime
    ) -> list[AISession]:
        if not self.export_dir.is_dir():
            return []
        accums: dict[tuple[str, str], _ConvAccum] = {}
        for path in _day_files(self.export_dir, day_start, day_end):
            for rec in _load_records(path):
                ts = _parse_ts(rec) if "timestamp" in rec else None
                if ts is None and isinstance(rec.get("ts"), str):
                    try:
                        ts = datetime.fromisoformat(
                            str(rec["ts"]).replace("Z", "+00:00")
                        )
                    except ValueError:
                        ts = None
                if ts is None or not (day_start <= ts < day_end):
                    continue
                site = str(rec.get("site") or "").lower()
                if not site:
                    continue
                cid = str(rec.get("conversation_id") or rec.get("conversationId") or "unknown")
                key = (site, cid)
                if key not in accums:
                    accums[key] = _ConvAccum(
                        site=site,
                        conversation_id=cid,
                        source=_site_source(site),
                    )
                role = str(rec.get("role") or "")
                char_count = rec.get("char_count")
                if not isinstance(char_count, (int, float)):
                    char_count = 0
                text = rec.get("text")
                if text is not None and not isinstance(text, str):
                    text = str(text)
                accums[key].add(ts, role, int(char_count), text)
        sessions: list[AISession] = []
        for acc in accums.values():
            s = acc.to_session()
            if s is not None:
                sessions.append(s)
        sessions.sort(key=lambda s: s.start)
        return sessions

    def scan_user_prompts(
        self, start: datetime, end: datetime, min_chars: int = 8
    ) -> list[UserPrompt]:
        """ブラウザ依頼文 → リトライ連鎖・promptmine に参加。"""
        if not self.export_dir.is_dir():
            return []
        out: list[UserPrompt] = []
        for path in _day_files(self.export_dir, start, end):
            for rec in _load_records(path):
                role = str(rec.get("role") or "").lower()
                if role not in ("user", "human"):
                    continue
                text = rec.get("text")
                if not isinstance(text, str) or len(text.strip()) < min_chars:
                    continue
                ts = _parse_ts(rec) if "timestamp" in rec else None
                if ts is None and isinstance(rec.get("ts"), str):
                    try:
                        ts = datetime.fromisoformat(
                            str(rec["ts"]).replace("Z", "+00:00")
                        )
                    except ValueError:
                        ts = None
                if ts is None or not (start <= ts < end):
                    continue
                site = str(rec.get("site") or "").lower()
                out.append(
                    UserPrompt(
                        timestamp=ts,
                        project=_site_label(site),
                        text=text.strip(),
                        source=_site_source(site),
                    )
                )
        out.sort(key=lambda p: p.timestamp)
        return out
