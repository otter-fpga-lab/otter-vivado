"""会话工作目录与运行版本核对：不启动真实 Vivado。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from vivado_mcp.vivado import gui_session
from vivado_mcp.vivado import session_manager as sm_mod
from vivado_mcp.vivado.base_session import SessionState
from vivado_mcp.vivado.session import SubprocessSession
from vivado_mcp.vivado.session_manager import SessionManager, resolve_spawn_cwd
from vivado_mcp.vivado.tcl_utils import TclResult


def _result(output: str, error: bool = False) -> TclResult:
    return TclResult(output=output, return_code=int(error), is_error=error)


@pytest.fixture(autouse=True)
def _no_vivado_env(monkeypatch):
    monkeypatch.delenv("VIVADO_PATH", raising=False)


class TestResolveSpawnCwd:
    def test_explicit_existing_directory_is_used(self, tmp_path):
        assert resolve_spawn_cwd(str(tmp_path)) == str(tmp_path.resolve())

    @pytest.mark.parametrize("kind", ["missing", "file"])
    def test_explicit_invalid_directory_is_rejected_without_creating(self, tmp_path, kind):
        target = tmp_path / "not-a-dir"
        if kind == "file":
            target.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="已存在的目录"):
            resolve_spawn_cwd(str(target))
        assert target.is_file() if kind == "file" else not target.exists()

    def test_default_directory_is_created(self, tmp_path, monkeypatch):
        default = tmp_path / "AppData" / "Roaming" / "Xilinx" / "Vivado"
        monkeypatch.setattr(sm_mod, "default_vivado_cwd", lambda: default)
        assert resolve_spawn_cwd("") == str(default)
        assert default.is_dir()


class _CaptureSession:
    """记录构造参数的会话替身。"""

    created: list[dict] = []

    def __init__(self, **kwargs):
        type(self).created.append(kwargs)
        self.connected_port = None
        self.pid = None

    async def start(self, timeout):
        return "fake banner"

    @property
    def is_alive(self):
        return True


@pytest.mark.parametrize("mode,cls_name", [("tcl", "SubprocessSession"), ("gui", "GuiSession")])
async def test_manager_passes_spawn_cwd(session_manager, monkeypatch, tmp_path, mode, cls_name):
    _CaptureSession.created = []
    monkeypatch.setattr(sm_mod, cls_name, _CaptureSession)
    await session_manager.start_session("cwd-test", mode=mode, cwd=str(tmp_path))
    assert _CaptureSession.created[-1]["cwd"] == str(tmp_path.resolve())


async def test_manager_default_cwd_uses_vivado_directory(session_manager, monkeypatch):
    _CaptureSession.created = []
    monkeypatch.setattr(sm_mod, "SubprocessSession", _CaptureSession)
    await session_manager.start_session("cwd-default", mode="tcl")
    expected = sm_mod.default_vivado_cwd()
    assert _CaptureSession.created[-1]["cwd"] == str(expected)
    assert expected.is_dir()


async def test_manager_attach_does_not_apply_cwd(session_manager, monkeypatch, tmp_path):
    _CaptureSession.created = []
    monkeypatch.setattr(sm_mod, "GuiSession", _CaptureSession)
    # attach 不 spawn：即使给了不存在的 cwd 也不校验、不传递。
    await session_manager.start_session(
        "cwd-attach", mode="attach", port=9999, cwd=str(tmp_path / "missing"),
    )
    assert "cwd" not in _CaptureSession.created[-1]


async def test_manager_rejects_missing_cwd_before_spawn(session_manager, monkeypatch, tmp_path):
    _CaptureSession.created = []
    monkeypatch.setattr(sm_mod, "SubprocessSession", _CaptureSession)
    with pytest.raises(ValueError, match="已存在的目录"):
        await session_manager.start_session(
            "cwd-missing", mode="tcl", cwd=str(tmp_path / "missing"),
        )
    assert _CaptureSession.created == []
    assert session_manager.get("cwd-missing") is None


async def test_tcl_spawn_passes_cwd_and_reads_banner_version(monkeypatch, tmp_path):
    captured = {}

    class Process:
        returncode = None
        stdin = stdout = stderr = None

    async def capture_launch(*args, **kwargs):
        captured.update(kwargs)
        return Process()

    async def banner(self, timeout):
        return "****** Vivado v2024.2 (64-bit)\n  **** SW Build 5239630"

    async def drain(self):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)
    monkeypatch.setattr(SubprocessSession, "_read_startup_banner", banner)
    monkeypatch.setattr(SubprocessSession, "_drain_stderr", drain)
    session = SubprocessSession("unused-vivado", "tcl-cwd", cwd=str(tmp_path))
    await session.start()
    assert captured["cwd"] == str(tmp_path)
    status = session.status_dict()
    assert status["runtime_version"] == "2024.2"
    assert status["cwd"] == str(tmp_path)


async def test_gui_spawn_passes_cwd(monkeypatch, tmp_path):
    source = tmp_path / "server.tcl"
    source.write_text("puts ok\n", encoding="utf-8")
    monkeypatch.setattr(gui_session, "_locate_server_script", lambda: source)
    captured = {}

    async def capture_launch(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("test captured launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)
    session = gui_session.GuiSession("unused-vivado", port=0, cwd=str(tmp_path))
    try:
        with pytest.raises(RuntimeError, match="test captured launch"):
            await session.start()
        assert captured["cwd"] == str(tmp_path)
    finally:
        await session.stop()


async def test_gui_stop_cleans_pid_file_in_vivado_cwd(tmp_path):
    pid_file = tmp_path / "vivado_pid4242.str"
    pid_file.write_text("", encoding="utf-8")
    session = gui_session.GuiSession("unused-vivado", port=0, cwd=str(tmp_path))
    await session.stop()
    assert not pid_file.exists()


class TestProbeRuntimeInfo:
    async def test_reads_version_and_pwd(self):
        session = SubprocessSession("/fake/vivado", "probe")
        session.execute = lambda *a, **k: asyncio.sleep(0, _result(
            "VMCP_RT_VERSION:2024.2\nVMCP_RT_PWD:C:/Users/me/AppData/Roaming/Xilinx/Vivado"
        ))
        await session.probe_runtime_info()
        assert session.runtime_version == "2024.2"
        assert session.status_dict()["runtime_pwd"].endswith("Xilinx/Vivado")

    @pytest.mark.parametrize("output", ["VMCP_RT_VERSION:version -short", "garbage"])
    async def test_unrecognized_output_keeps_unknown(self, output):
        session = SubprocessSession("/fake/vivado", "probe")
        session.execute = lambda *a, **k: asyncio.sleep(0, _result(output))
        await session.probe_runtime_info()
        assert session.status_dict()["runtime_version"] == "unknown"

    async def test_query_failure_is_not_raised(self):
        session = SubprocessSession("/fake/vivado", "probe")

        async def boom(*args, **kwargs):
            raise asyncio.TimeoutError("busy")

        session.execute = boom
        await session.probe_runtime_info()
        assert session.runtime_version is None


async def _start_recording_vmcp_server():
    """启动本地长度前缀伪造服务端，记录收到的所有 Tcl 命令。"""
    commands: list[str] = []
    writers: list[asyncio.StreamWriter] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        try:
            while True:
                header = await reader.readexactly(4)
                size = int.from_bytes(header, "big")
                request = (await reader.readexactly(size)).decode("utf-8")
                commands.append(request)
                if request == "version -short":
                    output = "2024.2"
                elif "VMCP_CURPROJ" in request:
                    output = "VMCP_CURPROJ:demo\n"
                else:
                    output = request
                body = json.dumps({"rc": 0, "output": output}).encode("utf-8")
                writer.write(len(body).to_bytes(4, "big") + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def close() -> None:
        server.close()
        await server.wait_closed()
        for w in writers:
            w.close()

    return port, commands, close


@pytest.mark.parametrize("mode,cls_name", [("tcl", "SubprocessSession"), ("gui", "GuiSession")])
async def test_manager_probes_runtime_only_for_spawned_sessions(
    session_manager, monkeypatch, tmp_path, mode, cls_name,
):
    probe_calls = 0

    class _SpawnStub:
        def __init__(self, **kwargs):
            self.vivado_path = kwargs["vivado_path"]
            self.session_id = kwargs["session_id"]
            self.cwd = kwargs.get("cwd")
            self.mode = mode
            self.connected_port = None
            self.pid = None
            self._runtime_version = None
            self._runtime_pwd = None

        @property
        def is_alive(self):
            return True

        async def start(self, timeout):
            return "fake banner"

        async def probe_runtime_info(self):
            nonlocal probe_calls
            probe_calls += 1
            self._runtime_version = "2024.2"
            self._runtime_pwd = self.cwd

        def status_dict(self):
            d = {
                "session_id": self.session_id,
                "mode": self.mode,
                "state": SessionState.READY.value,
                "vivado_path": self.vivado_path,
                "is_alive": True,
                "uptime_seconds": 0.0,
                "runtime_version": self._runtime_version or "unknown",
            }
            if self.cwd:
                d["cwd"] = self.cwd
            if self._runtime_pwd:
                d["runtime_pwd"] = self._runtime_pwd
            return d

    monkeypatch.setattr(sm_mod, cls_name, _SpawnStub)
    session, _ = await session_manager.start_session("spawned", mode=mode, cwd=str(tmp_path))
    assert probe_calls == 1
    assert session.status_dict()["runtime_version"] == "2024.2"
    assert session.status_dict()["runtime_pwd"] == str(tmp_path.resolve())

    # 复用已有会话不再重复发 version -short / pwd 查询。
    reused_session, _ = await session_manager.start_session("spawned", mode=mode)
    assert reused_session is session
    assert probe_calls == 1


async def test_manager_skips_runtime_probe_for_attach_external_and_reuse(session_manager):
    port, commands, close = await _start_recording_vmcp_server()
    created_ids: list[str] = []
    try:
        # 1. 显式 attach + 复用
        att, _ = await session_manager.start_session("att", mode="attach", port=port)
        created_ids.append("att")
        assert att.mode == "attach"
        assert att.status_dict()["runtime_version"] == "unknown"
        assert "runtime_pwd" not in att.status_dict()
        att_again, _ = await session_manager.start_session("att", mode="attach", port=port)
        assert att_again is att

        # 2. gui 端口命中已有 GUI（probe-then-attach）
        hit, _ = await session_manager.start_session("probe-hit", mode="gui", port=port)
        created_ids.append("probe-hit")
        assert hit.mode == "attach"
        assert hit.status_dict()["runtime_version"] == "unknown"
        assert "runtime_pwd" not in hit.status_dict()

        # 3. external@ 接管 + 复用
        ext, _ = await session_manager.start_session(f"external@{port}")
        created_ids.append(f"external@{port}")
        assert ext.mode == "attach"
        assert ext.status_dict()["runtime_version"] == "unknown"
        assert "runtime_pwd" not in ext.status_dict()
        ext_again, _ = await session_manager.start_session(f"external@{port}")
        assert ext_again is ext

        assert not any(
            "VMCP_RT_" in cmd or "version" in cmd or "pwd" in cmd
            for cmd in commands
        ), f"不应向已有 GUI 发送 version/pwd 查询: {commands}"
    finally:
        for sid in created_ids:
            if session_manager.get(sid) is not None:
                await session_manager.stop_session(sid)
        await close()


async def test_manager_probe_then_attach_with_explicit_path_keeps_version_without_pwd_probe(
    session_manager, tmp_path,
):
    exe = tmp_path / "Vivado" / "2024.2" / "bin" / "vivado.bat"
    exe.parent.mkdir(parents=True)
    exe.touch()
    port, commands, close = await _start_recording_vmcp_server()
    try:
        session, _ = await session_manager.start_session(
            "sel-hit", mode="gui", port=port, vivado_path=str(exe),
        )
        assert session.mode == "attach"
        status = session.status_dict()
        assert status["runtime_version"] == "2024.2"
        assert "runtime_pwd" not in status
        assert not any("VMCP_RT_" in cmd or "pwd" in cmd for cmd in commands)
    finally:
        if session_manager.get("sel-hit") is not None:
            await session_manager.stop_session("sel-hit")
        await close()


def _fake_ctx(manager: SessionManager):
    from vivado_mcp.server import AppContext

    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=AppContext(session_manager=manager))
    )


def _status(**extra):
    base = {
        "session_id": "default", "mode": "tcl", "state": SessionState.READY.value,
        "vivado_path": "C:/Xilinx/Vivado/2024.2/bin/vivado.bat", "is_alive": True,
        "uptime_seconds": 0.0, "runtime_version": "unknown",
    }
    base.update(extra)
    return base


@pytest.mark.parametrize(
    "version,policy_shown",
    [("2024.2", False), ("2019.1", True), ("unknown", True)],
)
async def test_start_session_tool_reports_version_cwd_and_gates_2019_warning(
    session_manager, monkeypatch, version, policy_shown,
):
    from vivado_mcp.tools import session_tools

    cwd = "C:/Users/me/AppData/Roaming/Xilinx/Vivado"
    fake = SimpleNamespace(status_dict=lambda: _status(
        runtime_version=version, cwd=cwd, runtime_pwd=cwd,
    ))

    async def start(**kwargs):
        return fake, "****** Vivado v2024.2 (64-bit)"

    monkeypatch.setattr(session_manager, "start_session", start)
    monkeypatch.setattr(session_tools, "_check_win_curdir_policy", lambda: "\nPOLICY-2019")
    result = await session_tools.start_session(mode="tcl", ctx=_fake_ctx(session_manager))
    assert f"工作目录: {cwd}（Vivado pwd 已核对）" in result
    if version == "unknown":
        assert "运行版本: 未核对" in result
    else:
        assert f"已核对运行版本: {version}" in result
    assert ("POLICY-2019" in result) is policy_shown


async def test_start_session_tool_attach_reports_actual_pwd_without_changing(
    session_manager, monkeypatch,
):
    from vivado_mcp.tools import session_tools

    fake = SimpleNamespace(status_dict=lambda: _status(
        mode="attach", runtime_version="2024.2", runtime_pwd="D:/work",
    ))

    async def start(**kwargs):
        assert kwargs["cwd"] == "E:/elsewhere"
        return fake, "已接管外部 Vivado 会话"

    monkeypatch.setattr(session_manager, "start_session", start)
    result = await session_tools.start_session(
        session_id="external@9999", cwd="E:/elsewhere", ctx=_fake_ctx(session_manager),
    )
    assert "工作目录: 未改变（attach 已有 Vivado，实际 pwd: D:/work" in result
    assert "未应用传入的 cwd" in result


async def test_start_session_tool_attach_without_query_says_not_checked(
    session_manager, monkeypatch,
):
    from vivado_mcp.tools import session_tools

    fake = SimpleNamespace(status_dict=lambda: _status(mode="attach"))

    async def start(**kwargs):
        return fake, "已接管外部 Vivado 会话"

    monkeypatch.setattr(session_manager, "start_session", start)
    result = await session_tools.start_session(
        session_id="external@9999", ctx=_fake_ctx(session_manager),
    )
    assert "工作目录: 未改变（attach 已有 Vivado，实际 pwd 未查询）" in result
    assert "运行版本: 未核对（未附带查询）" in result
    assert "version -short 未返回可识别版本" not in result


async def test_start_session_tool_attach_keeps_known_version_and_marks_pwd_not_queried(
    session_manager, monkeypatch,
):
    from vivado_mcp.tools import session_tools

    fake = SimpleNamespace(status_dict=lambda: _status(
        mode="attach", runtime_version="2024.2",
    ))

    async def start(**kwargs):
        return fake, "GUI 会话就绪(attach 到现有 GUI)"

    monkeypatch.setattr(session_manager, "start_session", start)
    result = await session_tools.start_session(
        session_id="gui-hit", mode="gui", ctx=_fake_ctx(session_manager),
    )
    assert "已核对运行版本: 2024.2" in result
    assert "工作目录: 未改变（attach 已有 Vivado，实际 pwd 未查询）" in result


@pytest.mark.parametrize(
    "version,pwd_known",
    [("2024.2", True), ("unknown", False)],
)
async def test_start_session_tool_reused_session_keeps_known_or_marks_unchecked(
    session_manager, version, pwd_known,
):
    from vivado_mcp.tools import session_tools

    cwd = "C:/Users/me/AppData/Roaming/Xilinx/Vivado"
    extra = {"runtime_version": version, "cwd": cwd}
    if pwd_known:
        extra["runtime_pwd"] = cwd
    existing = SimpleNamespace(
        is_alive=True,
        mode="tcl",
        vivado_path="C:/Xilinx/Vivado/2024.2/bin/vivado.bat",
        status_dict=lambda: _status(**extra),
    )
    session_manager._sessions["reused"] = existing

    result = await session_tools.start_session(
        session_id="reused", mode="tcl", ctx=_fake_ctx(session_manager),
    )
    if version == "unknown":
        assert "运行版本: 未核对（未附带查询）" in result
        assert "version -short 未返回可识别版本" not in result
    else:
        assert f"已核对运行版本: {version}" in result
    expected_pwd = f"实际 pwd: {cwd}" if pwd_known else "实际 pwd 未查询"
    assert f"工作目录: {cwd}（复用已有会话，不改变工作目录；{expected_pwd}）" in result


async def test_start_session_tool_returns_chinese_error_for_missing_cwd(
    session_manager, tmp_path,
):
    from vivado_mcp.tools import session_tools

    result = await session_tools.start_session(
        mode="tcl", cwd=str(tmp_path / "missing"), ctx=_fake_ctx(session_manager),
    )
    assert result.startswith("[ERROR]")
    assert "已存在的目录" in result
