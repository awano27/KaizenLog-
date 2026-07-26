"""Config path trust and template writes for setup / init-config."""
from __future__ import annotations

from pathlib import Path

import pytest

from kaizenlog.config import (
    default_config_path,
    find_config_file,
    render_config_template,
    write_config_file,
    load_config,
)


def test_default_config_path_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("kaizenlog.config.sys.platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    p = default_config_path()
    assert p == tmp_path / "AppData" / "kaizenlog" / "config.toml"


def test_default_config_path_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr("kaizenlog.config.sys.platform", "linux")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # implementation may use Path.home()/.config if no XDG — either is fine if documented
    p = default_config_path()
    assert p.name == "config.toml"
    assert "kaizenlog" in p.parts


def test_find_prefers_appdata_over_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAIZENLOG_CONFIG", raising=False)
    app = tmp_path / "AppData" / "kaizenlog"
    app.mkdir(parents=True)
    app_cfg = app / "config.toml"
    app_cfg.write_text('[general]\nvault_dir = "from-app"\n', encoding="utf-8")
    cwd_cfg = tmp_path / "kaizenlog.toml"
    cwd_cfg.write_text('[general]\nvault_dir = "from-cwd"\n', encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr("kaizenlog.config.sys.platform", "win32")
    found = find_config_file()
    assert found == app_cfg


def test_find_falls_back_to_cwd_with_file_present(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAIZENLOG_CONFIG", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-appdata"))
    monkeypatch.setattr("kaizenlog.config.sys.platform", "win32")
    cwd_cfg = tmp_path / "kaizenlog.toml"
    cwd_cfg.write_text("[general]\n", encoding="utf-8")
    assert find_config_file() == cwd_cfg.resolve()


def test_missing_env_config_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("KAIZENLOG_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(FileNotFoundError):
        find_config_file()


def test_render_template_embeds_vault_backend_model():
    text = render_config_template(
        vault_dir=Path("C:/vault"),
        backend="openai-compatible",
        model="gemma4:latest",
    )
    assert "C:/vault" in text or "C:\\\\vault" in text or "C:/vault" in text.replace("\\", "/")
    assert 'backend = "openai-compatible"' in text
    assert 'model = "gemma4:latest"' in text
    assert 'reasoning_effort = "none"' in text


def test_write_config_creates_parents_atomically(tmp_path):
    dest = tmp_path / "a" / "b" / "config.toml"
    write_config_file(
        dest,
        vault_dir=tmp_path / "vault",
        backend="none",
        model="qwen3:8b",
        merge=False,
    )
    assert dest.is_file()
    cfg = load_config(str(dest))
    assert cfg.vault_dir == tmp_path / "vault"
    assert cfg.llm.backend == "none"


def test_write_config_merge_preserves_extra_text(tmp_path):
    dest = tmp_path / "config.toml"
    dest.write_text(
        '[general]\nvault_dir = "old"\n\n# keep-me\n[[categories.rules]]\n'
        'name = "Custom"\npatterns = ["foo"]\n',
        encoding="utf-8",
    )
    write_config_file(
        dest,
        vault_dir=tmp_path / "newvault",
        backend="copilot-cli",
        model="gemma4:latest",
        merge=True,
    )
    text = dest.read_text(encoding="utf-8")
    assert "keep-me" in text
    assert "Custom" in text
    assert "newvault" in text.replace("\\", "/")
