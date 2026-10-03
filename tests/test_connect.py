"""源码接入只操作临时客户端目录，验证幂等、路径占用与既有配置保护。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from vivado_mcp import connect, doctor


@pytest.fixture
def home(monkeypatch, tmp_path):
    """隔离用户目录，禁止接入测试隐式执行完整 doctor 修复。"""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(
        doctor, "run_doctor", lambda *a, **kw: pytest.fail("不应调用完整 doctor")
    )
    monkeypatch.setattr(doctor, "install", lambda *a, **kw: pytest.fail("不应注入 Vivado"))

    class Distribution:
        def read_text(self, name):
            source = Path(connect.__file__).resolve().parents[2]
            return json.dumps({"dir_info": {"editable": True}, "url": source.as_uri()})

    monkeypatch.setattr(connect.metadata, "distribution", lambda name: Distribution())
    return path


@pytest.mark.parametrize("client", ["codex", "claude-code"])
def test_connect_preserves_config_and_is_idempotent(home, client):
    if client == "codex":
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = 'model = "user-model"\n[mcp_servers.other]\ncommand = "other-tool"\n'
    else:
        config = home / ".claude.json"
        original = '{"theme":"dark","mcpServers":{"other":{"command":"other-tool"}}}\n'
    config.write_text(original, encoding="utf-8")

    first = connect.connect_source(client)
    link = Path(first["skill"])
    assert link.is_symlink()
    assert link.resolve() == Path(first["skill_source"])
    assert first["config_changed"] is True
    assert first["skill_changed"] is True
    written = config.read_bytes()
    if client == "codex":
        data = doctor.tomllib.loads(written.decode())
        assert data["model"] == "user-model"
        servers = data["mcp_servers"]
    else:
        data = json.loads(written)
        assert data["theme"] == "dark"
        servers = data["mcpServers"]
    assert servers["other"]["command"] == "other-tool"
    assert servers["vivado"]["command"] == sys.executable
    assert servers["vivado"]["args"] == ["-m", "vivado_mcp"]
    assert "env" not in servers["vivado"]
    assert config.with_suffix(config.suffix + ".vmcp_backup").read_text() == original

    second = connect.connect_source(client)
    assert second["config_changed"] is False
    assert second["skill_changed"] is False
    assert config.read_bytes() == written


@pytest.mark.parametrize("kind", ["directory", "file", "foreign-link", "broken-link"])
def test_occupied_skill_never_overwritten(home, tmp_path, kind):
    skills = tmp_path / "skills"
    skills.mkdir()
    target = skills / "otter-vivado"
    if kind == "directory":
        target.mkdir()
    elif kind == "file":
        target.write_text("user content")
    else:
        destination = tmp_path / "foreign"
        if kind == "foreign-link":
            destination.mkdir()
        target.symlink_to(destination, target_is_directory=True)
    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        connect.connect_source("codex", skills)
    assert not (home / ".codex").exists()
    assert target.exists() or target.is_symlink()


def test_other_interpreter_is_not_claimed_as_this_source(home):
    config = home / ".claude.json"
    original = '{"mcpServers":{"vivado":{"command":"python","args":["-m","vivado_mcp"]}}}'
    config.write_text(original)
    with pytest.raises(ValueError, match="其他启动入口"):
        connect.connect_source("claude-code")
    assert config.read_text() == original
    assert not (home / ".claude").exists()


def test_non_editable_install_refused_before_writes(home, monkeypatch):
    class Distribution:
        def read_text(self, name):
            return '{"dir_info":{"editable":false},"url":"file:///wheel"}'

    monkeypatch.setattr(connect.metadata, "distribution", lambda name: Distribution())
    with pytest.raises(RuntimeError, match="pip install -e"):
        connect.connect_source()
    assert list(home.iterdir()) == []


def test_codex_home_and_explicit_skill_directory(home, tmp_path, monkeypatch):
    config_dir = tmp_path / "codex-config"
    skills = tmp_path / "native-skills"
    monkeypatch.setenv("CODEX_HOME", str(config_dir))
    result = connect.connect_source("codex", skills)
    assert result["config"] == str(config_dir / "config.toml")
    assert result["skill"] == str(skills / "otter-vivado")
    assert not (home / ".codex").exists()


def test_config_write_failure_removes_only_new_link(home, monkeypatch):
    def fail(*args):
        raise OSError("config denied")

    monkeypatch.setattr(doctor, "_fix_codex_config", fail)
    with pytest.raises(OSError, match="config denied"):
        connect.connect_source("codex")
    assert not (home / ".agents" / "skills" / "otter-vivado").is_symlink()


def test_invalid_config_refused_before_skill_write(home):
    config = home / ".claude.json"
    config.write_text("not JSON")
    with pytest.raises(ValueError, match="无法验证"):
        connect.connect_source("claude-code")
    assert config.read_text() == "not JSON"
    assert not (home / ".claude").exists()


def test_versioned_python_command_is_valid_current_interpreter(home, monkeypatch):
    monkeypatch.setattr(sys, "executable", "/source-venv/bin/python3.12")
    first = connect.connect_source("claude-code")
    second = connect.connect_source("claude-code")
    assert first["python"] == "/source-venv/bin/python3.12"
    assert second["config_changed"] is False
