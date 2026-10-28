"""IDE/Electron 环境泄漏：spawn Vivado 前必须丢掉宿主变量。"""

import asyncio

import pytest

from vivado_mcp.vivado.child_env import sanitize_vivado_child_env
from vivado_mcp.vivado import gui_session
from vivado_mcp.vivado.session import SubprocessSession


def test_drops_electron_and_vscode_keys(monkeypatch):
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setenv("VSCODE_IPC_HOOK", r"\\.\pipe\test")
    monkeypatch.setenv("CURSOR_SESSION_ID", "abc")
    monkeypatch.setenv("PATH", "C:\\Windows")
    env = sanitize_vivado_child_env()
    assert "ELECTRON_RUN_AS_NODE" not in env
    assert "VSCODE_IPC_HOOK" not in env
    assert "CURSOR_SESSION_ID" not in env
    assert env["PATH"] == "C:\\Windows"


def test_does_not_mutate_source_mapping():
    source = {"ELECTRON_RUN_AS_NODE": "1", "FOO": "bar"}
    env = sanitize_vivado_child_env(source)
    assert source["ELECTRON_RUN_AS_NODE"] == "1"
    assert "ELECTRON_RUN_AS_NODE" not in env
    assert env["FOO"] == "bar"


@pytest.mark.asyncio
async def test_gui_spawn_passes_sanitized_env(tmp_path, monkeypatch):
    source = tmp_path / "server.tcl"
    source.write_text("puts ok\n", encoding="utf-8")
    monkeypatch.setattr(gui_session, "_locate_server_script", lambda: source)
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    captured = {}

    async def capture_launch(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise OSError("test captured launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)
    session = gui_session.GuiSession("unused-vivado", port=0)
    try:
        with pytest.raises(RuntimeError, match="test captured launch"):
            await session.start()
        assert captured["env"] is not None
        assert "ELECTRON_RUN_AS_NODE" not in captured["env"]
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_tcl_spawn_passes_sanitized_env(monkeypatch):
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    captured = {}

    async def capture_launch(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise OSError("test captured launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)
    session = SubprocessSession("unused-vivado", "tcl-env")
    with pytest.raises(RuntimeError, match="无法启动 Vivado"):
        await session.start()
    assert captured["env"] is not None
    assert "ELECTRON_RUN_AS_NODE" not in captured["env"]
