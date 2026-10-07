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
    monkeypatch.setattr(doctor, "run_doctor", lambda *a, **kw: pytest.fail("不应调用完整 doctor"))
    monkeypatch.setattr(doctor, "install", lambda *a, **kw: pytest.fail("不应注入 Vivado"))

    class Distribution:
        def read_text(self, name):
            source = Path(connect.__file__).resolve().parents[2]
            return json.dumps({"dir_info": {"editable": True}, "url": source.as_uri()})

    monkeypatch.setattr(connect.metadata, "distribution", lambda name: Distribution())
    return path


@pytest.mark.parametrize("client", connect.CLIENTS)
def test_connect_preserves_config_and_is_idempotent(home, client):
    if client == "codex":
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = 'model = "user-model"\n[mcp_servers.other]\ncommand = "other-tool"\n'
    elif client == "claude-code":
        config = home / ".claude.json"
        original = '{"theme":"dark","mcpServers":{"other":{"command":"other-tool"}}}\n'
    else:
        config = (
            home / ".cursor" / "mcp.json"
            if client == "cursor"
            else home / ".gemini" / "config" / "mcp_config.json"
        )
        config.parent.mkdir(parents=True)
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
    def fail(*args, **kwargs):
        raise OSError("config denied")

    monkeypatch.setattr(doctor, "_atomic_write_with_backup", fail)
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


def test_all_clients_share_source_changes_without_synchronization(home, tmp_path, monkeypatch):
    source = tmp_path / "product source"
    skill = source / "skills" / "otter-vivado"
    refs = skill / "references"
    refs.mkdir(parents=True)
    (skill / "SKILL.md").write_text("original skill", encoding="utf-8")
    (refs / "flow.md").write_text("original flow", encoding="utf-8")
    monkeypatch.setattr(connect, "_editable_source", lambda: source)
    result = connect.connect_clients("all")
    assert len(result["clients"]) == 4
    links = [Path(client["skill"]) for client in result["clients"]]
    assert len(set(links)) == 3  # Cursor 和 Codex 共用 .agents，不重复创建
    assert all(link.resolve() == skill for link in links)
    (skill / "SKILL.md").write_text("changed once", encoding="utf-8")
    (refs / "flow.md").write_text("new flow", encoding="utf-8")
    for link in links:
        assert (link / "SKILL.md").read_text() == "changed once"
        assert (link / "references" / "flow.md").read_text() == "new flow"
    again = connect.connect_clients("all")
    assert all(not c["skill_changed"] and not c["config_changed"] for c in again["clients"])


def test_check_is_read_only_before_and_after_connect(home):
    before = connect.connect_clients("all", check=True)
    assert before["status"] == "needs_connection"
    assert all(not c["config_changed"] and not c["skill_changed"] for c in before["clients"])
    assert list(home.iterdir()) == []
    connect.connect_clients("all")
    after = connect.connect_clients("all", check=True)
    assert after["status"] == "ready"


def test_batch_conflict_is_found_before_any_write(home):
    target = home / ".gemini" / "config" / "skills" / "otter-vivado"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user content", encoding="utf-8")
    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        connect.connect_clients("all")
    assert not (home / ".agents").exists()
    assert not (home / ".claude.json").exists()
    assert (target / "SKILL.md").read_text() == "user content"


def test_explicit_skills_only_preserves_existing_mcp(home):
    config = home / ".claude.json"
    config.write_text('{"mcpServers":{"vivado":{"command":"existing-tool"}}}')
    before = config.read_bytes()
    result = connect.connect_source("claude-code", skills_only=True)
    assert result["mcp"] == "not_checked"
    assert result["config_changed"] is False
    assert config.read_bytes() == before


def test_cursor_existing_native_link_is_reused(home):
    target = home / ".cursor" / "skills" / "otter-vivado"
    target.parent.mkdir(parents=True)
    target.symlink_to(
        connect._editable_source() / "skills" / "otter-vivado", target_is_directory=True
    )
    result = connect.connect_source("cursor")
    assert result["skill"] == str(target)
    assert not result["skill_changed"]
    assert not (home / ".agents").exists()


def test_antigravity_legacy_detection_and_ambiguous_paths(home):
    old = home / ".gemini" / "antigravity" / "mcp_config.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"mcpServers":{}}')
    legacy = connect.connect_source("antigravity")
    assert legacy["config"] == str(old)
    assert "实际版本" in legacy["note"]
    new = home / ".gemini" / "config" / "mcp_config.json"
    new.parent.mkdir()
    new.write_text('{"mcpServers":{}}')
    with pytest.raises(ValueError, match="同时存在"):
        connect.connect_source("antigravity", check=True)
    chosen = connect.connect_source("antigravity", config_path=new)
    assert chosen["config"] == str(new)
    assert Path(chosen["skill"]).parent == new.parent / "skills"


def test_explicit_paths_allow_antigravity_cli_or_portable_setup(home, tmp_path):
    skills = home / ".gemini" / "antigravity-cli" / "skills"
    config = tmp_path / "custom settings" / "mcp.json"
    result = connect.connect_source("antigravity", skills, config_path=config)
    assert result["config"] == str(config)
    assert result["skill"] == str(skills / "otter-vivado")
    assert not (home / ".gemini" / "config").exists()


def test_existing_config_symlink_preserved(home, tmp_path):
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir()
    real = tmp_path / "shared-mcp.json"
    real.write_text('{"mcpServers":{"other":{"command":"user-tool"}}}')
    config.symlink_to(real)
    connect.connect_source("cursor")
    assert config.is_symlink()
    assert json.loads(real.read_text())["mcpServers"]["other"]["command"] == "user-tool"
    assert "vivado" in json.loads(real.read_text())["mcpServers"]


def test_concurrent_config_change_is_not_overwritten(home, monkeypatch):
    original_apply = connect._apply

    def concurrent(plan, mode):
        plan.config.parent.mkdir(parents=True, exist_ok=True)
        plan.config.write_text('{"concurrent":"user setting"}')
        return original_apply(plan, mode)

    monkeypatch.setattr(connect, "_apply", concurrent)
    with pytest.raises(OSError, match="发生变化"):
        connect.connect_source("cursor")
    assert (home / ".cursor" / "mcp.json").read_text() == '{"concurrent":"user setting"}'
    assert not (home / ".agents" / "skills" / "otter-vivado").exists()


def test_junction_fallback_is_only_for_windows_privilege_failure(home, monkeypatch):
    original_symlink = Path.symlink_to
    created = []

    def denied(*args, **kwargs):
        exc = OSError("privilege missing")
        exc.winerror = 1314
        raise exc

    def junction(target, source):
        created.append((target, source))
        original_symlink(target, source, target_is_directory=True)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "symlink_to", denied)
    monkeypatch.setattr(connect, "_create_junction", junction)
    result = connect.connect_source("cursor")
    assert created and result["skill_changed"]
    assert Path(result["skill"]).resolve() == Path(result["skill_source"])


def test_junction_shell_metacharacters_are_not_expanded(tmp_path, monkeypatch):
    monkeypatch.setattr(connect.subprocess, "run", lambda *a, **kw: pytest.fail("must not execute"))
    with pytest.raises(OSError, match="不能安全"):
        connect._create_junction(tmp_path / "%PATH%", tmp_path / "source")


@pytest.mark.parametrize(
    "clients,options",
    [
        (("all", "unknown"), {}),
        (("all",), {"config_path": Path("custom.json")}),
        (("cursor", "codex"), {"skills_dir": Path("skills")}),
    ],
)
def test_invalid_selection_is_rejected_before_writes(home, clients, options):
    with pytest.raises(ValueError):
        connect.connect_clients(clients, **options)
    assert list(home.iterdir()) == []


def test_multiple_clients_can_link_to_one_existing_mcp_config(home, tmp_path):
    shared = tmp_path / "shared.json"
    shared.write_text('{"mcpServers":{"other":{"command":"existing"}}}')
    paths = [home / ".cursor/mcp.json", home / ".gemini/config/mcp_config.json"]
    for path in paths:
        path.parent.mkdir(parents=True)
        path.symlink_to(shared)
    first = connect.connect_clients(("cursor", "antigravity"))
    assert first["status"] == "connected"
    assert sum(c["config_changed"] for c in first["clients"]) == 1
    assert all(path.is_symlink() for path in paths)
    assert set(json.loads(shared.read_text())["mcpServers"]) == {"other", "vivado"}
    second = connect.connect_clients(("cursor", "antigravity"))
    assert all(not c["config_changed"] and not c["skill_changed"] for c in second["clients"])


def test_skills_only_does_not_require_resolving_unused_mcp_paths(home, tmp_path):
    for folder in ("config", "antigravity"):
        path = home / ".gemini" / folder / "mcp_config.json"
        path.parent.mkdir(parents=True)
        path.write_text("this is not even JSON")
    result = connect.connect_source("antigravity", tmp_path / "chosen-skills", skills_only=True)
    assert result["status"] == "connected" and result["mcp"] == "not_checked"
    for path in (home / ".gemini").glob("*/mcp_config.json"):
        assert path.read_text() == "this is not even JSON"


@pytest.mark.parametrize(
    "client,setting",
    [
        ("cursor", {"disabled": True}),
        ("antigravity", {"disabled": True}),
        ("codex", {"enabled": False}),
        ("claude-code", {"env": {"PYTHONPATH": "/another-source"}}),
    ],
)
def test_check_reports_disabled_or_overridden_entry_without_changing_it(home, client, setting):
    first = connect.connect_source(client)
    path = Path(first["config"])
    if client == "codex":
        content = path.read_text() + "enabled = false\n"
        path.write_text(content)
    else:
        data = json.loads(path.read_text())
        data["mcpServers"]["vivado"].update(setting)
        path.write_text(json.dumps(data))
    before = path.read_bytes()
    check = connect.connect_source(client, check=True)
    applied = connect.connect_source(client)
    assert check["status"] == applied["status"] == "needs_attention"
    assert check["mcp"] in {"disabled", "search_path_override"}
    assert path.read_bytes() == before


def test_junction_command_has_cmd_quotes_without_crt_reescaping(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(
        connect.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0),
    )
    target, source = tmp_path / "client space 中文 & name", tmp_path / "source space"
    connect._create_junction(target, source)
    command = calls[0][0][0]
    assert isinstance(command, str)
    assert command == f'cmd.exe /d /s /c "mklink /J "{target}" "{source}""'
    assert '\\"' not in command
    assert calls[0][1].get("shell", False) is False


def test_non_privilege_link_error_is_not_hidden_by_junction(home, monkeypatch):
    def denied(*args, **kwargs):
        exc = OSError("permission on target directory")
        exc.winerror = 5
        raise exc

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "symlink_to", denied)
    monkeypatch.setattr(
        connect, "_create_junction", lambda *a: pytest.fail("not a privilege error")
    )
    with pytest.raises(OSError, match="无法创建"):
        connect.connect_source("cursor")
    assert not (home / ".cursor" / "mcp.json").exists()


def test_config_link_retargeted_during_connect_is_left_untouched(home, tmp_path, monkeypatch):
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_text('{"mcpServers":{}}')
    new.write_text('{"mcpServers":{}}')
    path = home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.symlink_to(old)
    original_apply = connect._apply

    def retarget(plan, mode):
        path.unlink()
        path.symlink_to(new)
        return original_apply(plan, mode)

    monkeypatch.setattr(connect, "_apply", retarget)
    with pytest.raises(OSError, match="链接在接入期间"):
        connect.connect_source("cursor")
    assert old.read_text() == new.read_text() == '{"mcpServers":{}}'
    assert not (home / ".agents").exists()


def test_cli_all_check_connect_and_idempotent_recheck(home, monkeypatch, capsys):
    from vivado_mcp.__main__ import main

    monkeypatch.setattr(sys, "argv", ["vivado-mcp", "connect", "--client", "all", "--check"])
    main()
    before = json.loads(capsys.readouterr().out)
    assert before["check_only"] and before["status"] == "needs_connection"
    assert list(home.iterdir()) == []
    monkeypatch.setattr(sys, "argv", ["vivado-mcp", "connect", "--client", "all"])
    main()
    installed = json.loads(capsys.readouterr().out)
    assert installed["status"] == "connected"
    assert {c["client"] for c in installed["clients"]} == set(connect.CLIENTS)
    monkeypatch.setattr(sys, "argv", ["vivado-mcp", "connect", "--client", "all", "--check"])
    main()
    after = json.loads(capsys.readouterr().out)
    assert after["status"] == "ready"
    assert all(not c["skill_changed"] and not c["config_changed"] for c in after["clients"])


@pytest.mark.parametrize("skills_only", [False, True])
def test_codex_native_plugin_refuses_duplicate_source_entry_before_writes(home, skills_only):
    config = home / ".codex/config.toml"
    config.parent.mkdir()
    original = b'[plugins."otter-vivado@personal"]\nenabled = true\n'
    config.write_bytes(original)
    with pytest.raises(ValueError, match="Codex .*Skill/MCP"):
        connect.connect_clients(("codex",), skills_only=skills_only)
    assert config.read_bytes() == original
    assert not (home / ".agents/skills").exists()
