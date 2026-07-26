"""バグ監査 第2ラウンド（2026-07-20）の回帰テスト。"""

import json
import subprocess
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from kaizenlog.advisor import (
    AdvisorError,
    BackendUnavailable,
    _call_claude_code_cli,
    _call_copilot_cli,
    _call_openai_compatible,
    _check_cmdline_length,
)
from kaizenlog.collector import active_intervals, clip_to_active, collect_day
from kaizenlog.config import ConfigError, LLMConfig, load_config
from kaizenlog.promptmine import cluster_prompts, normalize
from kaizenlog.aiwork import UserPrompt
from kaizenlog.runlog import load_runs, log_run
from kaizenlog.memory import load_entries
from kaizenlog.vault import extract_heading_section

T0 = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)


# ---- advisor: OpenAI互換の応答異常はAdvisorError（リトライ/フォールバック対象）----

class _FakeResp:
    def __init__(self, payload, text="{}"):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_openai_null_content_raises_advisor_error(monkeypatch):
    resp = _FakeResp({"choices": [{"message": {"content": None}}]})
    monkeypatch.setattr("kaizenlog.advisor.requests.post", lambda *a, **k: resp)
    with pytest.raises(AdvisorError):
        _call_openai_compatible(LLMConfig(), "sys", "user")


def test_openai_read_timeout_raises_advisor_error(monkeypatch):
    def raise_timeout(*a, **k):
        raise requests.exceptions.ReadTimeout("slow model load")
    monkeypatch.setattr("kaizenlog.advisor.requests.post", raise_timeout)
    with pytest.raises(AdvisorError, match="タイムアウト"):
        _call_openai_compatible(LLMConfig(), "sys", "user")


def test_openai_non_json_body_raises_advisor_error(monkeypatch):
    resp = _FakeResp(ValueError("not json"))
    monkeypatch.setattr("kaizenlog.advisor.requests.post", lambda *a, **k: resp)
    with pytest.raises(AdvisorError):
        _call_openai_compatible(LLMConfig(), "sys", "user")


def test_openai_payload_disables_reasoning_by_default(monkeypatch):
    captured = {}
    resp = _FakeResp({"choices": [{"message": {"content": "改善提案"}}]})

    def post(*args, **kwargs):
        captured["json"] = kwargs["json"]
        return resp

    monkeypatch.setattr("kaizenlog.advisor.requests.post", post)
    assert _call_openai_compatible(LLMConfig(), "sys", "user") == "改善提案"
    assert captured["json"]["reasoning_effort"] == "none"


# ---- advisor: Claude CLIのJSONエラー封筒をノートに書かない ----

def _fake_run(stdout="", stderr="", returncode=0):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
    return run


def test_claude_json_error_envelope_raises(monkeypatch):
    monkeypatch.setattr("kaizenlog.advisor.shutil.which", lambda c: "C:/fake/claude.exe")
    envelope = json.dumps({"type": "result", "subtype": "error_during_execution",
                           "is_error": True, "result": None})
    monkeypatch.setattr("kaizenlog.advisor.subprocess.run", _fake_run(stdout=envelope))
    with pytest.raises(AdvisorError, match="error_during_execution"):
        _call_claude_code_cli(LLMConfig(), "sys", "user")


def test_claude_prompt_passed_via_stdin(monkeypatch):
    monkeypatch.setattr("kaizenlog.advisor.shutil.which", lambda c: "C:/fake/claude.exe")
    captured = {}

    def run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"result": "ok"}), stderr="")

    monkeypatch.setattr("kaizenlog.advisor.subprocess.run", run)
    assert _call_claude_code_cli(LLMConfig(), "sys", "user prompt") == "ok"
    assert "user prompt" in captured["input"]        # stdin経由
    assert all("user prompt" not in c for c in captured["cmd"])  # 引数には載らない


# ---- advisor: 認証切れは即フォールバック（B-1: 実CLIで採取した401封筒）----
# 実CLI(claude 2.1.199)の実挙動: 認証切れは stderr 空・stdout のJSONに 401 が入り、
# returncode は 1(subprocess経由) にも 0(TTY) にもなりうる。両方を検出できること。

_AUTH_401_ENVELOPE = json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 401,
    "result": "Failed to authenticate. API Error: 401 OAuth access token has expired. "
              "Re-authenticate to continue.",
})


def test_claude_auth_error_returncode1_stdout_is_backend_unavailable(monkeypatch):
    monkeypatch.setattr("kaizenlog.advisor.shutil.which", lambda c: "C:/fake/claude.exe")
    # stderr は空、401情報は stdout にのみ入る（旧コードは stderr だけ見て取りこぼした）
    monkeypatch.setattr("kaizenlog.advisor.subprocess.run",
                        _fake_run(stdout=_AUTH_401_ENVELOPE, stderr="", returncode=1))
    with pytest.raises(BackendUnavailable, match="未認証"):
        _call_claude_code_cli(LLMConfig(), "sys", "user")


def test_claude_auth_error_exit0_envelope_is_backend_unavailable(monkeypatch):
    monkeypatch.setattr("kaizenlog.advisor.shutil.which", lambda c: "C:/fake/claude.exe")
    monkeypatch.setattr("kaizenlog.advisor.subprocess.run",
                        _fake_run(stdout=_AUTH_401_ENVELOPE, returncode=0))
    with pytest.raises(BackendUnavailable, match="未認証"):
        _call_claude_code_cli(LLMConfig(), "sys", "user")


def test_claude_auth_error_triggers_fallback_not_retry(monkeypatch):
    # 未認証(BackendUnavailable)はリトライせず即座に次バックエンドへ落ちる
    monkeypatch.setattr("kaizenlog.advisor.shutil.which", lambda c: "C:/fake/claude.exe")
    monkeypatch.setattr("kaizenlog.advisor.subprocess.run",
                        _fake_run(stdout=_AUTH_401_ENVELOPE, returncode=1))
    monkeypatch.setattr("kaizenlog.advisor._call_openai_compatible",
                        lambda cfg, s, u: "fallback advice")
    sleeps = []
    from kaizenlog.advisor import generate_text
    cfg = LLMConfig(backend="claude-code-cli", fallback_to_local=True,
                    retries=2, retry_wait_seconds=20)
    assert generate_text(cfg, "s", "u", sleep=sleeps.append) == "fallback advice"
    assert sleeps == []  # 認証切れで20秒リトライを空回りしない


# ---- advisor: Windowsコマンドライン上限とCopilot npm shim迂回 ----

def test_cmdline_length_guard_raises_backend_unavailable():
    with pytest.raises(BackendUnavailable, match="上限"):
        _check_cmdline_length(["copilot.CMD", "-p", "x" * 40000], "Copilot CLI")


def test_copilot_cmd_uses_node_loader_and_preserves_multiline_prompt(
        monkeypatch, tmp_path):
    npm_root = tmp_path / "npm"
    shim = npm_root / "copilot.CMD"
    shim.parent.mkdir()
    shim.touch()
    loader = npm_root / "node_modules" / "@github" / "copilot" / "npm-loader.js"
    loader.parent.mkdir(parents=True)
    loader.touch()
    node = tmp_path / "node.exe"
    node.touch()

    def which(command):
        return str(shim) if command == "copilot" else str(node) if command == "node" else None

    captured = {}

    def run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("kaizenlog.advisor.shutil.which", which)
    monkeypatch.setattr("kaizenlog.advisor.subprocess.run", run)
    prompt = 'line 1\nline 2: "quoted" 100% ^caret'

    assert _call_copilot_cli(LLMConfig(), "system", prompt) == "ok"
    assert captured["cmd"][:2] == [str(node), str(loader)]
    assert captured["cmd"][captured["cmd"].index("-p") + 1] == f"system\n\n{prompt}"
    assert "--silent" in captured["cmd"]
    assert "--no-custom-instructions" in captured["cmd"]
    assert "--disable-builtin-mcps" in captured["cmd"]


def test_copilot_cmd_without_official_loader_is_unavailable(monkeypatch, tmp_path):
    shim = tmp_path / "copilot.CMD"
    shim.touch()
    monkeypatch.setattr(
        "kaizenlog.advisor.shutil.which",
        lambda command: str(shim) if command == "copilot" else "C:/node.exe",
    )

    with pytest.raises(BackendUnavailable, match="npm-loader.js"):
        _call_copilot_cli(LLMConfig(), "system", "user")


# ---- collector: 終日AFKの日はゼロ活動（全日フォールバックしない）----

class _FakeClient:
    def __init__(self, buckets, events_by_bucket):
        self._buckets = buckets
        self._events = events_by_bucket

    def find_bucket(self, t):
        ids = self.find_buckets(t)
        return ids[0] if ids else None

    def find_buckets(self, t):
        return [bid for bid, info in self._buckets.items() if info["type"] == t]

    def events(self, bucket_id, start, end):
        return self._events.get(bucket_id, [])


def _raw(start, minutes, data):
    return {"timestamp": start.isoformat(), "duration": minutes * 60, "data": data}


def test_fully_afk_day_yields_no_events():
    day_start, day_end = T0, T0 + timedelta(hours=24)
    client = _FakeClient(
        {"w": {"type": "currentwindow"}, "a": {"type": "afkstatus"}},
        {
            "w": [_raw(T0, 480, {"app": "chrome.exe", "title": "開きっぱなし"})],
            "a": [_raw(T0, 480, {"status": "afk"})],  # AFKデータはあるが全て離席
        },
    )
    assert collect_day(client, day_start, day_end) == []


def test_no_afk_data_still_falls_back_to_full_day():
    day_start, day_end = T0, T0 + timedelta(hours=24)
    client = _FakeClient(
        {"w": {"type": "currentwindow"}, "a": {"type": "afkstatus"}},
        {"w": [_raw(T0, 60, {"app": "code.exe", "title": "main.py"})], "a": []},
    )
    events = collect_day(client, day_start, day_end)
    assert len(events) == 1


# ---- promptmine: 日本語文がパス正規化で丸呑みされない ----

def test_normalize_does_not_swallow_japanese_sentence():
    a = normalize("src/kaizenlog/nippou.pyの_is_privateのバグを修正してテストも追加して")
    b = normalize("src/kaizenlog/vault.pyのextract_heading_sectionを高速化して")
    assert a != b  # パス以降の日本語が保持され、別の依頼として区別される
    assert "修正" in a and "高速化" in b


def test_distinct_japanese_prompts_do_not_merge():
    prompts = [
        UserPrompt(T0, "p", "src/kaizenlog/nippou.pyの_is_privateのバグを修正してテストも追加して"),
        UserPrompt(T0, "p", "src/kaizenlog/vault.pyのextract_heading_sectionを高速化して計測結果を教えて"),
        UserPrompt(T0, "p", "docs/design.mdとsrc/foo/bar.tsのリファクタをお願いします"),
    ]
    clusters = cluster_prompts(prompts)
    assert len(clusters) == 3


# ---- runlog / memory: 不正UTF-8でもクラッシュしない ----

def test_load_runs_survives_invalid_utf8(tmp_path):
    good = json.dumps({"ts": "2026-07-19T00:00:00+00:00", "command": "run", "ok": True})
    (tmp_path / "runs.jsonl").write_bytes(good.encode() + b"\n\x93\xfa\x96{ broken\n")
    runs = load_runs(tmp_path)
    assert len(runs) == 1  # 壊れた行だけ落ち、正常な行は生きる


def test_log_run_after_corruption_recovers(tmp_path):
    (tmp_path / "runs.jsonl").write_bytes(b"\x93\xfa\x96{ broken\n")
    log_run(tmp_path, "run", ok=True, duration_seconds=1.0)  # クラッシュしない
    assert len(load_runs(tmp_path)) == 1


def test_load_entries_survives_invalid_utf8(tmp_path):
    good = json.dumps({"id": "KZN-20260719-001", "date": "2026-07-19",
                       "action": "a", "status": "proposed"}, ensure_ascii=False)
    (tmp_path / "suggestions.jsonl").write_bytes(good.encode() + b"\n\x93\xfa broken\n")
    assert len(load_entries(tmp_path)) == 1


# ---- vault: コードフェンス・Obsidianタグで見出し抽出が切れない ----

def test_heading_section_survives_code_fence():
    content = (
        "## Tasks\n\n- [ ] レビュー対応\n\n```bash\n# run tests\npytest\n```\n\n"
        "- [ ] デプロイ\n\n## Next\n"
    )
    section = extract_heading_section(content, "tasks")
    assert "- [ ] デプロイ" in section
    assert "pytest" in section


def test_heading_section_ignores_obsidian_tags():
    content = "## Tasks\n\n- [ ] 仕事\n#work\n- [ ] 続き\n\n## Next\n"
    section = extract_heading_section(content, "tasks")
    assert "- [ ] 続き" in section


# ---- config: 型間違いが分かりやすいエラー/寛容な解釈になる ----

def test_config_string_for_list_becomes_single_item(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[privacy]\nredact_patterns = "secret-project"\n', encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.privacy.redact_patterns == ["secret-project"]  # 1文字ずつに爆発しない


def test_config_bad_int_raises_config_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[llm]\nretries = "2 times"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="llm.retries"):
        load_config(str(p))


def test_config_loads_openai_reasoning_effort(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[llm.openai_compatible]\nreasoning_effort = "low"\n',
        encoding="utf-8",
    )
    assert load_config(str(p)).llm.reasoning_effort == "low"


def test_config_rejects_invalid_openai_reasoning_effort(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[llm.openai_compatible]\nreasoning_effort = "off"\n',
        encoding="utf-8",
    )
    with pytest.raises(
        ConfigError,
        match="llm.openai_compatible.reasoning_effort",
    ):
        load_config(str(p))
