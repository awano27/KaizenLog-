import json
import subprocess
from datetime import date

import pytest

from kaizenlog.advisor import (
    AdvisorError,
    _call_claude_code_cli,
    load_bundled_prompt,
    resolve_system_prompt,
)
from kaizenlog.config import LLMConfig
from kaizenlog.memory import (
    MemoryEntry,
    append_entries,
    assign_action_ids,
    load_entries,
    next_id,
    summarize_for_prompt,
    update_statuses_from_note,
)
from kaizenlog.privacy import PrivacyError, make_redactor
from kaizenlog.skill_manager import (
    bundled_skill_names,
    check_skill,
    install_skill,
    skill_description,
    bundled_skill_content,
)

TODAY = date(2026, 7, 7)

ADVICE = """## 🚀 Kaizen（AIからの改善提案）

### 今日の改善提案
1. 根拠→提案

### 明日の最小アクション
- [ ] Cursorのレビュー指示をテンプレート化する
- [ ] CLAUDE.mdにビルド手順を書く

### AI作業の改善
- 依頼をまとめる
"""


# ---- Kaizen Memory ----

def test_assign_action_ids():
    md, entries = assign_action_ids(ADVICE, TODAY, [])
    assert len(entries) == 2
    assert entries[0].id == "KZN-20260707-001"
    assert entries[1].id == "KZN-20260707-002"
    assert "- [ ] KZN-20260707-001: Cursorのレビュー指示をテンプレート化する" in md
    # アクションセクション外のチェックボックスには付与しない
    assert "1. 根拠→提案" in md


def test_assign_action_ids_skips_existing_and_continues_numbering():
    existing = [MemoryEntry(id="KZN-20260707-001", date="2026-07-07", action="既存")]
    md_with_id = ADVICE.replace(
        "- [ ] Cursorのレビュー指示をテンプレート化する",
        "- [ ] KZN-20260707-001: 既存のアクション",
    )
    md, entries = assign_action_ids(md_with_id, TODAY, existing)
    assert len(entries) == 1  # ID付きの行はスキップ
    assert entries[0].id == "KZN-20260707-002"
    assert md.count("KZN-20260707-001") == 1


def test_memory_roundtrip_and_status_update(tmp_path):
    _, entries = assign_action_ids(ADVICE, TODAY, [])
    append_entries(tmp_path, entries)
    loaded = load_entries(tmp_path)
    assert [e.status for e in loaded] == ["proposed", "proposed"]

    note = "## メモ\n- [x] KZN-20260707-001: Cursorのレビュー指示をテンプレート化する\n"
    updates = update_statuses_from_note(note, loaded, date(2026, 7, 8))
    assert len(updates) == 1 and updates[0].status == "done"
    append_entries(tmp_path, updates)

    final = load_entries(tmp_path)  # 後勝ちマージ
    by_id = {e.id: e for e in final}
    assert by_id["KZN-20260707-001"].status == "done"
    assert by_id["KZN-20260707-001"].done_date == "2026-07-08"
    assert by_id["KZN-20260707-002"].status == "proposed"

    # done済みは再度updatesに含まれない
    assert update_statuses_from_note(note, final, date(2026, 7, 9)) == []


def test_summarize_for_prompt():
    entries = [
        MemoryEntry(id="KZN-20260701-001", date="2026-07-01", action="未完了A"),
        MemoryEntry(id="KZN-20260706-001", date="2026-07-06", action="完了B",
                    status="done", done_date="2026-07-07"),
        MemoryEntry(id="KZN-20260101-001", date="2026-01-01", action="古い未完了"),
    ]
    s = summarize_for_prompt(entries, TODAY)
    assert "未完了A" in s and "完了B" in s
    assert "古い未完了" not in s  # 30日より前は出さない
    assert summarize_for_prompt([], TODAY) == ""


def test_assign_action_ids_idempotent_same_day():
    _, first = assign_action_ids(ADVICE, TODAY, [])
    # 同じ内容で再実行（adviseの再実行を想定）→ 既存IDを再利用し新規エントリなし
    md2, second = assign_action_ids(ADVICE, TODAY, first)
    assert second == []
    assert "KZN-20260707-001: Cursorのレビュー指示をテンプレート化する" in md2


def test_next_id_no_collision():
    existing = [MemoryEntry(id="KZN-20260707-002", date="2026-07-07", action="x")]
    assert next_id(existing, TODAY) == "KZN-20260707-001"
    assert next_id(existing, TODAY, offset=1) == "KZN-20260707-003"


# ---- プライバシーレダクション ----

def test_redactor_masks_patterns():
    r = make_redactor([r"顧客[A-Z]", r"\S+@example\.com"], "[REDACTED]")
    out = r("顧客Aの案件を tanaka@example.com に送付")
    assert "顧客A" not in out and "tanaka@example.com" not in out
    assert out.count("[REDACTED]") == 2


def test_redactor_disabled_when_no_patterns():
    assert make_redactor([]) is None


def test_redactor_invalid_pattern_raises():
    with pytest.raises(PrivacyError, match="正規表現が不正"):
        make_redactor(["(unclosed"])


# ---- スキル同梱・インストール ----

def test_bundled_skills_present():
    names = bundled_skill_names()
    assert {"daily-kaizen", "weekly-kaizen", "kaizen-autopilot"} <= set(names)
    desc = skill_description(bundled_skill_content("daily-kaizen"))
    assert "改善提案" in desc


def test_install_and_check(tmp_path):
    result, dest = install_skill(tmp_path, "daily-kaizen")
    assert result == "installed" and dest.is_file()
    assert check_skill(tmp_path, "daily-kaizen").state == "up-to-date"

    # 再インストールは unchanged
    assert install_skill(tmp_path, "daily-kaizen")[0] == "unchanged"

    # ローカル改変 → 上書きせず skipped
    dest.write_text(dest.read_text(encoding="utf-8") + "\nローカル改変", encoding="utf-8")
    assert check_skill(tmp_path, "daily-kaizen").state == "outdated-or-modified"
    assert install_skill(tmp_path, "daily-kaizen")[0] == "skipped"
    assert "ローカル改変" in dest.read_text(encoding="utf-8")

    # --force で .bak 退避後に上書き
    result, dest = install_skill(tmp_path, "daily-kaizen", force=True)
    assert result == "overwritten"
    assert dest.with_suffix(".md.bak").is_file()
    assert "ローカル改変" in dest.with_suffix(".md.bak").read_text(encoding="utf-8")
    assert check_skill(tmp_path, "daily-kaizen").state == "up-to-date"


# ---- プロンプトテンプレート ----

def test_bundled_prompts_loadable():
    for name in ("daily_advisor", "weekly_review", "ai_work_deep_review", "privacy_safe"):
        assert len(load_bundled_prompt(name)) > 100
    assert "計画と実績の差分" in load_bundled_prompt("daily_advisor")
    assert "[REDACTED]" in load_bundled_prompt("privacy_safe")


def test_resolve_system_prompt(tmp_path):
    assert "明日の最小アクション" in resolve_system_prompt(LLMConfig())
    custom = tmp_path / "my_prompt.md"
    custom.write_text("カスタムプロンプト", encoding="utf-8")
    assert resolve_system_prompt(LLMConfig(system_prompt=str(custom))) == "カスタムプロンプト"
    with pytest.raises(AdvisorError, match="見つかりません"):
        resolve_system_prompt(LLMConfig(system_prompt="no_such_prompt"))


# ---- claude-code-cli バックエンド ----

def _fake_run(stdout="", stderr="", returncode=0):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
    return run


def test_claude_backend_parses_json(monkeypatch):
    payload = json.dumps({"result": "改善提案です", "session_id": "s"})
    monkeypatch.setattr("kaizenlog.advisor.subprocess.run", _fake_run(stdout=payload))
    assert _call_claude_code_cli(LLMConfig(), "sys", "user") == "改善提案です"


def test_claude_backend_falls_back_to_plain_text(monkeypatch):
    monkeypatch.setattr("kaizenlog.advisor.subprocess.run",
                        _fake_run(stdout="プレーンな応答"))
    assert _call_claude_code_cli(LLMConfig(), "sys", "user") == "プレーンな応答"


def test_claude_backend_error_includes_stderr(monkeypatch):
    monkeypatch.setattr("kaizenlog.advisor.subprocess.run",
                        _fake_run(stderr="Not logged in", returncode=1))
    with pytest.raises(AdvisorError, match="Not logged in"):
        _call_claude_code_cli(LLMConfig(), "sys", "user")


def test_claude_backend_missing_cli(monkeypatch):
    def raise_fnf(cmd, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr("kaizenlog.advisor.subprocess.run", raise_fnf)
    with pytest.raises(AdvisorError, match="インストール"):
        _call_claude_code_cli(LLMConfig(), "sys", "user")
