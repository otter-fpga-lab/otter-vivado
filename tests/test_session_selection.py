"""多版本选择不静默复用其他安装，不替换活动会话；不启动真实 EDA。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vivado_mcp.vivado.base_session import SessionState
from vivado_mcp.vivado.gui_session import GuiSession
from vivado_mcp.vivado.session_manager import SessionManager


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["explicit", "environment"])
async def test_existing_session_with_different_explicit_path_is_kept(tmp_path, monkeypatch, choice):
    manager = SessionManager("default-vivado")
    original = SimpleNamespace(
        is_alive=True, mode="tcl", vivado_path=str(tmp_path / "Vivado/2018.3/bin/vivado"),
        stop=AsyncMock(),
    )
    manager._sessions["work"] = original
    selected = str(tmp_path / "Vivado/2024.2/bin/vivado")
    if choice == "environment":
        monkeypatch.setenv("VIVADO_PATH", selected)
    with pytest.raises(ValueError, match="原会话保持运行"):
        await manager.start_session("work", vivado_path=selected if choice == "explicit" else None)
    assert manager.get("work") is original
    original.stop.assert_not_awaited()
    same, _ = await manager.start_session("work", vivado_path=original.vivado_path)
    assert same is original


@pytest.mark.asyncio
async def test_environment_selection_is_passed_to_gui_version_guard(tmp_path, monkeypatch):
    from vivado_mcp.vivado import session_manager as module

    exe = tmp_path / "Vivado/2024.2/bin/vivado.bat"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setenv("VIVADO_PATH", str(exe))
    captured = {}

    class Session:
        connected_port = None
        pid = None

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self, **kwargs):
            return "test stub"

    monkeypatch.setattr(module, "GuiSession", Session)
    manager = SessionManager("older-detected-install")
    await manager.start_session("env-select", mode="gui", port=9999)
    assert captured["vivado_path"] == exe.as_posix()
    assert captured["expected_version"] == "2024.2"


@pytest.mark.asyncio
async def test_explicit_missing_path_does_not_spawn_or_attach(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock())
    manager = SessionManager("some-other-install")
    with pytest.raises(FileNotFoundError, match="不会自动改用其他版本"):
        await manager.start_session("selected", vivado_path=str(tmp_path / "missing"), mode="gui")
    asyncio.create_subprocess_exec.assert_not_awaited()
    assert manager.get("selected") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,actual,return_code,accepted", [
    ("2024.2", "2018.3", 0, False),
    ("2024.2", "unknown", 0, False),
    ("2024.2", "2024.2", 0, True),
    ("2019.1", "2019.1", 0, True),  # 维护目标不是运行版本白名单。
    ("2024.2", "2024.2.1", 0, False),  # 已知版本仍精确核对，不放宽补丁。
    ("unknown", "2023.1", 0, True),  # 自定义路径使用实际查询版本。
    ("unknown", "", 0, False),
    ("unknown", "invalid version", 0, False),
    ("unknown", "2023.1", 1, False),
])
async def test_explicit_version_guard_uses_real_protocol_and_preserves_existing_gui(
    expected, actual, return_code, accepted, monkeypatch, tmp_path,
):
    commands = []
    writers = []

    async def handle(reader, writer):
        writers.append(writer)
        try:
            while True:
                header = await reader.readexactly(4)
                request = (await reader.readexactly(int.from_bytes(header, "big"))).decode()
                commands.append(request)
                output = actual if request == "version -short" else request
                rc = return_code if request == "version -short" else 0
                body = json.dumps({"rc": rc, "output": output}).encode()
                writer.write(len(body).to_bytes(4, "big") + body)
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    installation = "eda/current" if expected == "unknown" else f"Vivado/{expected}"
    executable = tmp_path / installation / "bin/vivado.bat"
    executable.parent.mkdir(parents=True)
    executable.touch()  # 仅真实路径夹具；现有协议测试服务替代 EDA，不执行此文件。
    session = GuiSession(str(executable), port=port, expected_version=expected)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock())
    monkeypatch.setattr(session, "_current_project_hint", AsyncMock(return_value=""))
    try:
        if accepted:
            banner = await session.start(timeout=2)
            assert session.mode == "attach"
            assert session.status_dict()["runtime_version"] == actual
            assert session.status_dict()["vivado_path_is_launch_candidate"]
            if expected == "unknown":
                assert "安装路径未提供版本约束" in banner
                assert f"实际版本 {actual}" in banner
        else:
            with pytest.raises(RuntimeError, match="port=0"):
                await session.start(timeout=2)
            assert session.state == SessionState.ERROR
            assert not session.is_alive
        assert commands[1] == "version -short"
        asyncio.create_subprocess_exec.assert_not_awaited()
        await session.stop()
        assert "exit" not in commands
        # 原 listener 仍可连接，拒绝错版没有停止用户 GUI。
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
    finally:
        await session.stop()
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()


@pytest.mark.asyncio
async def test_selected_version_does_not_spawn_on_busy_or_unverified_existing_port(monkeypatch):
    session = GuiSession("selected-install", port=9876, expected_version="2024.2")
    writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(return_value=(object(), writer)))
    monkeypatch.setattr(session, "_handshake", AsyncMock(return_value=False))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock())
    with pytest.raises(RuntimeError, match="不会在该端口自动启动另一实例"):
        await session.start(timeout=1)
    asyncio.create_subprocess_exec.assert_not_awaited()
    assert not session.is_alive


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", ["2024.2", "unknown"])
async def test_version_query_timeout_only_closes_probe_connection(monkeypatch, expected):
    release = asyncio.Event()
    commands = []
    handlers = []

    async def handle(reader, writer):
        handlers.append(asyncio.current_task())
        try:
            while True:
                header = await reader.readexactly(4)
                request = (await reader.readexactly(int.from_bytes(header, "big"))).decode()
                commands.append(request)
                if request == "version -short":
                    await release.wait()
                output = "2024.2" if request == "version -short" else request
                body = json.dumps({"rc": 0, "output": output}).encode()
                writer.write(len(body).to_bytes(4, "big") + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    session = GuiSession("selected-install", expected_version=expected)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock())
    try:
        with pytest.raises(RuntimeError, match="原 GUI 保持运行"):
            await session._try_attach_existing(port, timeout=0.02)
        assert commands[-1] == "version -short"
        assert not session.is_alive
        asyncio.create_subprocess_exec.assert_not_awaited()
        await session.stop()
        assert "exit" not in commands
        # 原 listener 依然活着，不因查询超时被重启或终止。
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)
