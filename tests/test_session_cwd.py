"""会话工作目录与运行版本核对：不启动真实 Vivado。"""

import asyncio
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


async def test_start_session_tool_returns_chinese_error_for_missing_cwd(
    session_manager, tmp_path,
):
    from vivado_mcp.tools import session_tools

    result = await session_tools.start_session(
        mode="tcl", cwd=str(tmp_path / "missing"), ctx=_fake_ctx(session_manager),
    )
    assert result.startswith("[ERROR]")
    assert "已存在的目录" in result
