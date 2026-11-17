"""output_dir 省略时按工程目录约定生成 Debug/<时间>_<ila|experiment|bundle>。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_debug_design_tools import call, context
from vivado_mcp.debug_bundle import export_debug_bundle
from vivado_mcp.vivado.tcl_utils import TclResult

PART = "xc7k325tffg900-2"


def _project(tmp_path):
    root = tmp_path / "board proj"
    root.mkdir()
    xpr = root / "board.xpr"
    xpr.write_text("<Project/>", encoding="utf-8")
    dcp = root / "board.runs" / "impl_1" / "top_routed.dcp"
    dcp.parent.mkdir(parents=True)
    dcp.write_bytes(b"SYNTHETIC CHECKPOINT")
    return root, xpr, dcp


class LayoutSession:
    """只回答工程查询；其它命令返回 Tcl 错误（导出结果因此为 unknown）。"""

    session_id = "board"
    state = "ready"

    def __init__(self, xpr: Path | None):
        self.xpr = xpr
        self.commands: list[str] = []
        self.is_alive = True

    async def execute(self, command, timeout=None):
        self.commands.append(command)
        if "VMCP_LAYOUT_XPR" in command:
            value = self.xpr.as_posix() if self.xpr else "NONE"
            return TclResult(f"VMCP_LAYOUT_XPR:{value}", 0, False)
        return TclResult("ERROR: synthetic", 1, True)


def _bundle_dirs(root: Path):
    debug = root / "Debug"
    return sorted(p for p in debug.iterdir()) if debug.is_dir() else []


async def test_bundle_default_from_checkpoint_project(tmp_path):
    root, _, dcp = _project(tmp_path)
    session = LayoutSession(None)
    result = await export_debug_bundle(session, str(dcp), None, PART)
    created = _bundle_dirs(root)
    assert len(created) == 1 and created[0].name.endswith("_bundle")
    assert result["bundle_directory"] == str(created[0].resolve())
    assert (created[0] / "attempt.json").is_file()
    # 由 DCP 推出工程，不需要询问会话当前工程。
    assert not any("VMCP_LAYOUT_XPR" in c for c in session.commands)


async def test_bundle_default_falls_back_to_session_project(tmp_path):
    root, xpr, _ = _project(tmp_path)
    loose = tmp_path / "loose" / "a.dcp"
    loose.parent.mkdir()
    loose.write_bytes(b"SYNTHETIC CHECKPOINT")
    result = await export_debug_bundle(LayoutSession(xpr), str(loose), None, PART)
    assert Path(result["bundle_directory"]).parent == (root / "Debug").resolve()


async def test_bundle_without_project_requires_explicit_dir(tmp_path):
    loose = tmp_path / "loose" / "a.dcp"
    loose.parent.mkdir()
    loose.write_bytes(b"SYNTHETIC CHECKPOINT")
    with pytest.raises(ValueError, match="显式传入 output_dir"):
        await export_debug_bundle(LayoutSession(None), str(loose), None, PART)
    assert sorted(p.name for p in loose.parent.iterdir()) == ["a.dcp"]
    with pytest.raises(ValueError):
        await export_debug_bundle(LayoutSession(None), str(loose), "", PART)


async def test_bundle_explicit_dir_keeps_strict_semantics(tmp_path):
    root, _, dcp = _project(tmp_path)
    existing = tmp_path / "exists"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        await export_debug_bundle(LayoutSession(None), str(dcp), str(existing), PART)
    with pytest.raises(FileNotFoundError):
        await export_debug_bundle(
            LayoutSession(None), str(dcp), str(tmp_path / "no" / "parent"), PART,
        )
    assert not (root / "Debug").exists()


async def test_bundle_mcp_and_cli_output_dir_optional(tmp_path, monkeypatch, capsys):
    from vivado_mcp import debug_bundle_cli
    from vivado_mcp.server import mcp

    root, _, dcp = _project(tmp_path)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "output_dir" not in tools["export_debug_bundle"].input_schema["required"]
    session = LayoutSession(None)
    result = await call("export_debug_bundle", {
        "checkpoint_path": str(dcp), "expected_part": PART, "session_id": "board",
    }, context(session))
    assert Path(result["bundle_directory"]).parent == (root / "Debug").resolve()

    cli_session = LayoutSession(None)
    cli_session.start = AsyncMock()
    cli_session.stop = AsyncMock()
    monkeypatch.setattr(debug_bundle_cli, "GuiSession", lambda *a, **k: cli_session)
    code = await debug_bundle_cli.run_debug_export_cli(SimpleNamespace(
        checkpoint=str(dcp), output_dir=None, part=PART, port=10003,
        source_revision=None, timeout=20,
    ))
    assert code == 1  # 合成会话不执行导出；只核对目录约定
    printed = json.loads(capsys.readouterr().out)
    assert Path(printed["bundle_directory"]).parent == (root / "Debug").resolve()
    assert len(_bundle_dirs(root)) == 2


def test_cli_parser_output_dir_optional(monkeypatch):
    from vivado_mcp import __main__ as cli
    from vivado_mcp import debug_bundle_cli

    seen = {}

    async def fake(args):
        seen["output_dir"] = args.output_dir
        return 0

    monkeypatch.setattr(debug_bundle_cli, "run_debug_export_cli", fake)
    monkeypatch.setattr("sys.argv", [
        "vivado-mcp", "debug-export", "--checkpoint", "a.dcp", "--part", PART, "--port", "1",
    ])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert seen == {"output_dir": None}


class FakeService:
    def __init__(self, session):
        self.session = session
        self.requests = []

    def submit(self, request, *, source):
        self.requests.append(request)
        return {"operation_id": "op", "status": "running"}


async def _debug_action(service, params):
    from vivado_mcp.tools import debug_tools

    with patch.object(debug_tools, "_service", AsyncMock(return_value=service)):
        return json.loads(await debug_tools.debug_action(
            action="export_ila", params=params, expected_revision=3,
            session_id="board", ctx=None,
        ))


async def test_export_ila_default_and_explicit_output_dir(tmp_path):
    root, xpr, _ = _project(tmp_path)
    service = FakeService(LayoutSession(xpr))
    result = await _debug_action(service, {"core": "hw_ila_1"})
    output = Path(result["output_dir"])
    assert output.parent == root / "Debug" and output.name.endswith("_ila")
    assert not output.exists()  # 由导出执行时以排他语义创建
    assert service.requests[0]["params"] == {"core": "hw_ila_1", "output_dir": str(output)}

    explicit = tmp_path / "explicit"
    result = await _debug_action(service, {"core": "hw_ila_1", "output_dir": str(explicit)})
    assert result["output_dir"] == str(explicit)
    assert service.requests[1]["params"]["output_dir"] == str(explicit)


async def test_export_ila_without_project_reports_chinese_error(tmp_path):
    service = FakeService(LayoutSession(None))
    result = await _debug_action(service, {"core": "hw_ila_1"})
    assert "显式传入 output_dir" in result["error"]
    assert service.requests == []


class FakeExperiment:
    created: list[str] = []

    def __init__(self, service, spec, output_dir, expected_revision, *, owner):
        self.root = Path(output_dir)
        self.root.mkdir(exist_ok=False)
        FakeExperiment.created.append(output_dir)

    def snapshot(self):
        return {"id": "exp", "status": "created", "output_dir": str(self.root / "attempt-0001")}


async def test_create_experiment_default_output_dir(tmp_path):
    from vivado_mcp.tools import debug_tools

    root, xpr, _ = _project(tmp_path)
    tools = {tool.name: tool for tool in await debug_tools.mcp.list_tools()}
    assert set(tools["create_debug_experiment"].input_schema["required"]) == {
        "spec", "expected_revision",
    }
    valid_spec = {"title": "default dir", "steps": [{"id": "s1", "kind": "ready", "label": "go"}]}
    service = FakeService(LayoutSession(xpr))
    with patch.object(debug_tools, "_service", AsyncMock(return_value=service)), \
            patch("vivado_mcp.debug_experiment.DebugExperiment", FakeExperiment):
        result = json.loads(await debug_tools.create_debug_experiment(
            spec=valid_spec, expected_revision=1, session_id="board", ctx=None,
        ))
        experiment_dir = Path(result["experiment_dir"])
        assert experiment_dir.parent == root / "Debug"
        assert experiment_dir.name.endswith("_experiment") and experiment_dir.is_dir()

        missing = FakeService(LayoutSession(None))
        with patch.object(debug_tools, "_service", AsyncMock(return_value=missing)):
            result = json.loads(await debug_tools.create_debug_experiment(
                spec=valid_spec, expected_revision=1, session_id="board", ctx=None,
            ))
        assert "显式传入 output_dir" in result["error"]


async def _make_real_service(xpr: Path, *, control: str = "ai", connected: bool = True):
    from vivado_mcp.debug_service import DebugService

    session = LayoutSession(xpr)
    backend = SimpleNamespace(inspect=AsyncMock(return_value={
        "target": "t0", "device": "xc7k325t", "part": PART,
        "ilas": [{"name": "hw_ila_1", "uuid": "u-1", "status": "IDLE", "probes": []}],
        "vios": [],
    }))
    service = DebugService(session, backend=backend, poll_interval=60.0)
    if control == "ai":
        service._state["control"] = "ai"
    if connected:
        service._state["connection"] = "connected"
        service._state["selection"] = {"target": "t0", "device": "xc7k325t"}
        service._state["hardware"] = await backend.inspect("t0", "xc7k325t")
    return service


async def test_rejected_export_ila_and_experiment_do_not_create_debug_dir(tmp_path):
    from vivado_mcp.tools import debug_tools

    root, xpr, dcp = _project(tmp_path)
    valid_spec = {
        "title": "check preflight",
        "steps": [
            {"id": "r", "kind": "ready", "label": "ready"},
            {"id": "e", "kind": "debug", "action": "export_ila", "params": {"core": "hw_ila_1"}},
        ],
    }

    # 1. export_ila 在权限/状态/参数校验失败时不创建 Debug/
    service = await _make_real_service(xpr, control="manual", connected=False)
    try:
        rev = service.snapshot()["revision"]
        with patch.object(debug_tools, "_service", AsyncMock(return_value=service)):
            bad_rev = json.loads(await debug_tools.debug_action(
                action="export_ila", params={"core": "hw_ila_1"},
                expected_revision=rev + 99, session_id="board",
            ))
            assert "状态已变化" in bad_rev["error"]
            bad_params = json.loads(await debug_tools.debug_action(
                action="export_ila", params={}, expected_revision=rev, session_id="board",
            ))
            assert "参数不符合约定" in bad_params["error"]
            no_ctrl = json.loads(await debug_tools.debug_action(
                action="export_ila", params={"core": "hw_ila_1"},
                expected_revision=rev, session_id="board",
            ))
            assert "控制权" in no_ctrl["error"]

        service._state["control"] = "ai"
        with patch.object(debug_tools, "_service", AsyncMock(return_value=service)):
            no_hw = json.loads(await debug_tools.debug_action(
                action="export_ila", params={"core": "hw_ila_1"},
                expected_revision=rev, session_id="board",
            ))
            assert "硬件状态" in no_hw["error"]
        assert not (root / "Debug").exists()

        # 2. create_debug_experiment 在 spec/revision/control/hardware/ILA 状态失败时不创建 Debug/
        with patch.object(debug_tools, "_service", AsyncMock(return_value=service)):
            bad_spec = json.loads(await debug_tools.create_debug_experiment(
                spec={}, expected_revision=rev, session_id="board",
            ))
            assert "title/steps" in bad_spec["error"]
            no_hw_exp = json.loads(await debug_tools.create_debug_experiment(
                spec=valid_spec, expected_revision=rev, session_id="board",
            ))
            assert "选择设备" in no_hw_exp["error"]
        assert not (root / "Debug").exists()
    finally:
        await service.close()

    # 3. ILA 非 IDLE 或核不存在时，create_debug_experiment 同样不创建 Debug/
    service2 = await _make_real_service(xpr, control="ai", connected=True)
    try:
        service2._state["hardware"]["ilas"][0]["status"] = "WAITING"
        rev2 = service2.snapshot()["revision"]
        with patch.object(debug_tools, "_service", AsyncMock(return_value=service2)):
            not_idle = json.loads(await debug_tools.create_debug_experiment(
                spec=valid_spec, expected_revision=rev2, session_id="board",
            ))
            assert "IDLE" in not_idle["error"]
        assert not (root / "Debug").exists()
    finally:
        await service2.close()

    # 4. export_debug_bundle 会话不可用时也不提前创建 Debug/
    dead_session = LayoutSession(xpr)
    dead_session.is_alive = False
    with pytest.raises(RuntimeError, match="会话不可用"):
        await export_debug_bundle(dead_session, str(dcp), None, PART)
    assert not (root / "Debug").exists()
