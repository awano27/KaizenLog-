"""第42弾: screenpipe 画面内容連携（fixtures・monkeypatch のみ）。"""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from kaizenlog.config import Config, ScreenpipeConfig, load_config
from kaizenlog.doctor import Check, _check_screenpipe
from kaizenlog.report import (
    Block,
    DailySummary,
    SessionSpan,
    render_markdown,
)
from kaizenlog.screenpipe_source import (
    ScreenText,
    ScreenpipeClient,
    UI_CHROME_STOPLIST,
    block_fill_key,
    collect_screen_fills_for_ai_blocks,
    is_localhost_url,
    normalize_app_name,
    resolve_api_key,
    summarize_screen_texts,
)

TZ = ZoneInfo("Asia/Tokyo")
FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FakeResp:
    def __init__(self, payload: dict, code: int = 200):
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = code

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_s1_disabled_no_http(monkeypatch, tmp_path):
    """既定 OFF＋キー未設定では HTTP が一切出ない（urlopen を実結線して検証）。"""
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("urlopen must not be called when disabled")

    cfg = Config(vault_dir=tmp_path)
    assert cfg.screenpipe.enabled is False
    monkeypatch.delenv(cfg.screenpipe.api_key_env, raising=False)
    assert resolve_api_key(cfg.screenpipe.api_key_env) is None

    # キー未設定のクライアントは何度呼んでも urlopen に到達しない
    client = ScreenpipeClient(
        "http://localhost:3030", api_key=None, urlopen=boom, tz=TZ
    )
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    for _ in range(3):
        assert client.search_text("ChatGPT", start, start + timedelta(minutes=5)) == []
    assert calls == []
    assert client.last_warning


def test_s2_circuit_breaker_stops_after_first_failure():
    """1回失敗したら以降は照会しない（停止中に generate が固まらない）。"""
    attempts = []

    def raise_conn(*a, **k):
        attempts.append(1)
        raise OSError("refused")

    client = ScreenpipeClient(
        "http://localhost:3030", api_key="tok", urlopen=raise_conn, tz=TZ
    )
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    for _ in range(5):
        assert client.search_text("ChatGPT", start, start + timedelta(minutes=5)) == []
    # 初回の1リクエストで打ち切り（5ブロック×2 content_type = 10 にならない）
    assert len(attempts) == 1
    # 警告は最初の1件のみ保持
    assert client.last_warning == "screenpipe 照会失敗: OSError"


def test_s2_connection_errors_return_empty():
    def raise_conn(*a, **k):
        raise OSError("refused")

    client = ScreenpipeClient(
        "http://localhost:3030",
        api_key="tok",
        urlopen=raise_conn,
        tz=TZ,
    )
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    assert client.search_text("ChatGPT", start, start + timedelta(minutes=5)) == []
    assert client.health() is None
    assert client.last_warning


def test_s2_parse_nested_and_flat():
    nested = _load("screenpipe_search_ocr.json")
    flat = {
        "data": [
            {
                "text": "flat body that is long enough for min length gate ok",
                "timestamp": "2026-08-02T00:00:10Z",
                "app_name": "ChatGPT",
                "window_name": "w",
            }
        ]
    }
    payloads = {"ocr": nested, "accessibility": flat}
    urls: list[str] = []

    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        urls.append(url)
        if "/health" in url:
            return _FakeResp(_load("screenpipe_health.json"))
        if "content_type=ocr" in url:
            return _FakeResp(payloads["ocr"])
        return _FakeResp({"data": [], "pagination": {}})

    client = ScreenpipeClient(
        "http://localhost:3030", api_key="tok", urlopen=urlopen, tz=TZ
    )
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    items = client.search_text("ChatGPT", start, start + timedelta(minutes=10))
    assert items
    assert items[0].ts_local.tzinfo is not None
    # JST = UTC+9 → 00:00:10Z → 09:00:10 JST
    assert items[0].ts_local.hour == 9
    assert all("content_type=input" not in u for u in urls)
    assert all("include_cloud" not in u for u in urls)

    # OCR が 0 件のときは accessibility へフォールバックし、フラット形も解釈できる
    urls2: list[str] = []

    def urlopen_flat(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        urls2.append(url)
        if "content_type=ocr" in url:
            return _FakeResp({"data": [], "pagination": {}})
        return _FakeResp(flat)

    client2 = ScreenpipeClient(
        "http://localhost:3030", api_key="tok", urlopen=urlopen_flat, tz=TZ
    )
    items2 = client2.search_text("ChatGPT", start, start + timedelta(minutes=10))
    assert len(items2) == 1
    assert items2[0].text.startswith("flat body")
    assert items2[0].ts_local.hour == 9
    assert any("content_type=accessibility" in u for u in urls2)


def test_s2_ocr_preferred_then_accessibility_fallback():
    order: list[str] = []

    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "content_type=ocr" in url:
            order.append("ocr")
            return _FakeResp({"data": [], "pagination": {}})
        if "content_type=accessibility" in url:
            order.append("accessibility")
            return _FakeResp(_load("screenpipe_search_accessibility.json"))
        return _FakeResp({})

    client = ScreenpipeClient(
        "http://localhost:3030", api_key="tok", urlopen=urlopen, tz=TZ
    )
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    # accessibility only has short UI chrome → may empty after summarize;
    # but search_text should still call accessibility after empty ocr
    client.search_text("ChatGPT", start, start + timedelta(minutes=5))
    assert order == ["ocr", "accessibility"]


def test_s3_summarize_noise_and_self_ref():
    noisy = ScreenText(
        ts_local=datetime(2026, 8, 2, 9, 0, tzinfo=TZ),
        app_name="ChatGPT",
        window_name="w",
        text=(
            "最小化 復元 閉じる\n"
            "SHORT_RECORD_MIN_MINUTES = 120.0 の単一定数を確認する長い本文です\n"
            "バ ッ ク グ ラ ウ ン ド 処理\n"
            "[URL_WITH_CREDENTIALS]\n"
            "kaizenlog rehumanize 第42弾\n"
            "コードをコピー\n"
            "短い\n"
            "12345 !!!\n"
            "C:/develop/obsidian/2026/01 Daily Notes/x.md を開く\n"
            "SHORT_RECORD_MIN_MINUTES = 120.0 の単一定数を確認する長い本文です\n"
        ),
    )
    out = summarize_screen_texts(
        [noisy],
        max_lines=3,
        max_chars=80,
        self_paths=["C:/develop/obsidian/2026"],
    )
    assert out
    joined = "\n".join(out)
    assert "kaizenlog" not in joined.lower()
    assert "最小化" not in joined
    assert "バックグラウンド" in joined or "SHORT_RECORD" in joined
    assert "[URL_WITH_CREDENTIALS]" not in joined
    # deterministic
    assert out == summarize_screen_texts([noisy], max_lines=3, max_chars=80, self_paths=["C:/develop/obsidian/2026"])
    assert "最小化" in UI_CHROME_STOPLIST


def test_s4_only_ai_unmatched_queried():
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    ai = Block(
        start, start + timedelta(minutes=6), "AI作業", "ChatGPT.exe", ["ChatGPT"],
        ai=True, tool="chatgpt",
    )
    browse = Block(
        start + timedelta(hours=1),
        start + timedelta(hours=1, minutes=20),
        "ブラウジング",
        "chrome.exe",
        ["x"],
        ai=False,
    )
    queries: list[str | None] = []

    class FakeClient:
        last_warning = None

        def search_text(self, app_name, start_local, end_local, **k):
            queries.append(app_name)
            return [
                ScreenText(
                    ts_local=start_local,
                    app_name=app_name or "",
                    window_name="",
                    text="十分に長い画面テキストの要約候補をここに置きますよ",
                )
            ]

    fills, stats, samples = collect_screen_fills_for_ai_blocks(
        [ai, browse],
        spans=[],
        client=FakeClient(),  # type: ignore[arg-type]
        redactor=lambda s: s,
        min_block_minutes=3.0,
    )
    assert queries == ["ChatGPT"]  # only AI
    assert stats["queried_blocks"] == 1
    assert stats["filled_blocks"] == 1
    assert samples


def test_s4_timeline_fill_or_no_log():
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    block = Block(
        start, start + timedelta(minutes=6), "AI作業", "ChatGPT.exe", ["ChatGPT"],
        ai=True, tool="chatgpt",
    )
    s = DailySummary(
        day=date(2026, 8, 2),
        total_minutes=6.0,
        by_category={"AI作業": 6.0},
        by_app={},
        blocks=[block],
        ai_tool_minutes={},
        ai_sessions=1,
        context_switches=0,
        by_site={},
    )
    key = block_fill_key(block.start, block.end, block.app)
    md_fill = render_markdown(
        s, TZ, min_block_minutes=3.0, screen_fills={key: "secret-token-work"}
    )
    assert "画面テキスト: secret-token-work" in md_fill
    assert "（ログなし）" not in md_fill

    md_empty = render_markdown(s, TZ, min_block_minutes=3.0, screen_fills={})
    assert "（ログなし）" in md_empty


def test_s4_matched_session_skips_screenpipe():
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    block = Block(
        start, start + timedelta(minutes=10), "AI作業", "ChatGPT.exe", ["ChatGPT"],
        ai=True, tool="chatgpt",
    )
    spans = [
        SessionSpan(
            start - timedelta(minutes=1),
            start + timedelta(minutes=11),
            "chatgpt",
            "proj: session label",
        )
    ]
    calls = []

    class FakeClient:
        last_warning = None

        def search_text(self, *a, **k):
            calls.append(1)
            return []

    fills, stats, _ = collect_screen_fills_for_ai_blocks(
        [block], spans, FakeClient(), min_block_minutes=3.0  # type: ignore[arg-type]
    )
    assert calls == []
    assert stats["queried_blocks"] == 0
    assert fills == {}


def test_s9_redactor_applied():
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    block = Block(
        start, start + timedelta(minutes=6), "AI作業", "ChatGPT.exe", ["t"],
        ai=True, tool="chatgpt",
    )

    class FakeClient:
        last_warning = None

        def search_text(self, *a, **k):
            return [
                ScreenText(
                    ts_local=start,
                    app_name="ChatGPT",
                    window_name="",
                    text="プロジェクト SECRET_TOKEN を修正する長い文章です",
                )
            ]

    fills, _, samples = collect_screen_fills_for_ai_blocks(
        [block],
        [],
        FakeClient(),  # type: ignore[arg-type]
        redactor=lambda s: s.replace("SECRET_TOKEN", "[REDACTED]"),
    )
    assert fills
    assert "SECRET_TOKEN" not in next(iter(fills.values()))
    assert "[REDACTED]" in samples[0]["summary"]


def test_s10_forbidden_params_never_sent():
    urls: list[str] = []

    def urlopen(req, timeout=None):
        url = getattr(req, "full_url", str(req))
        urls.append(url)
        return _FakeResp({"data": [], "pagination": {}})

    client = ScreenpipeClient(
        "http://127.0.0.1:3030", api_key="x", urlopen=urlopen, tz=TZ
    )
    start = datetime(2026, 8, 2, 9, 0, tzinfo=TZ)
    client.search_text("ChatGPT", start, start + timedelta(minutes=1))
    assert urls
    for u in urls:
        assert "content_type=input" not in u
        assert "content_type=memory" not in u
        assert "include_cloud" not in u


def test_s11_non_localhost_disabled_on_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        """
[general]
vault_dir = "."
[screenpipe]
enabled = true
base_url = "http://example.com:3030"
""",
        encoding="utf-8",
    )
    with pytest.warns(UserWarning):
        cfg = load_config(str(p))
    assert cfg.screenpipe.enabled is False


def test_s12_doctor_three_states():
    c = Check()
    cfg = Config()
    cfg.screenpipe = ScreenpipeConfig(enabled=False)
    _check_screenpipe(c, cfg)
    assert any("disabled" in ln for ln in c.lines)

    c2 = Check()
    cfg2 = Config()
    cfg2.screenpipe = ScreenpipeConfig(enabled=True, base_url="http://localhost:3030")

    def raise_conn(*a, **k):
        raise OSError("down")

    # monkeypatch client via urlopen by patching module used inside doctor
    import kaizenlog.screenpipe_source as sp

    orig = sp.urllib.request.urlopen
    sp.urllib.request.urlopen = raise_conn  # type: ignore[assignment]
    try:
        _check_screenpipe(c2, cfg2)
    finally:
        sp.urllib.request.urlopen = orig
    assert any("unreachable" in ln or "disabled" in ln or "未設定" in ln for ln in c2.lines)


def test_normalize_app_name():
    assert normalize_app_name("ChatGPT.exe") == "ChatGPT"


def test_is_localhost_url_allows_true_localhost_only():
    assert is_localhost_url("http://localhost:3030")
    assert is_localhost_url("http://127.0.0.1:3030")
    assert is_localhost_url("http://[::1]:3030")
    assert is_localhost_url("http://localhost")
    assert is_localhost_url("  HTTP://127.0.0.1:3030/path  ")


def test_is_localhost_url_rejects_prefix_and_userinfo_bypass():
    assert not is_localhost_url("https://evil.example")
    assert not is_localhost_url("http://localhost.evil.com")
    assert not is_localhost_url("http://localhost.evil.com:3030")
    assert not is_localhost_url("http://127.0.0.1.attacker")
    assert not is_localhost_url("http://127.0.0.1:3030@evil.example/")
    assert not is_localhost_url("http://user:pass@localhost:3030/")
    assert not is_localhost_url("https://localhost:3030")
    assert not is_localhost_url("http://192.168.1.1:3030")
    assert not is_localhost_url("")
    assert not is_localhost_url("not-a-url")


def test_extract_screen_text_excerpt_handles_nested_fullwidth_parens():
    from kaizenlog.screenpipe_source import extract_screen_text_excerpt

    nested = "（画面テキスト: 関数（foo）の実装を進めた）"
    assert extract_screen_text_excerpt(nested) == "関数（foo）の実装を進めた"

    table = (
        "| 10:00 | 15 | AI作業 | ChatGPT | "
        "（画面テキスト: 関数（foo）の実装を進めた） |"
    )
    assert extract_screen_text_excerpt(table) == "関数（foo）の実装を進めた"

    plain = "（画面テキスト: 単純な要約）"
    assert extract_screen_text_excerpt(plain) == "単純な要約"
    assert extract_screen_text_excerpt("画面テキストなし") is None
