"""离线产物、显式 attach 和消费者文件保护；不连接商业 EDA。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vivado_mcp import debug_prepare_cli

PROJECT = {"name": "camera", "directory": "/consumer/camera", "part": "xc7a35tcpg236-1"}
SPEC = {
    "kind": "ila_ip", "name": "ila_isp", "clock": "pixel_clk", "depth": 4096,
    "probes": [{"name": "frame_valid", "width": 1}],
}


@pytest.fixture
def arguments(tmp_path):
    spec_file = tmp_path / "design.json"
    spec_file.write_text(json.dumps(SPEC), encoding="utf-8")

    def build(**overrides):
        return SimpleNamespace(**{
            "spec": str(spec_file), "apply": False, "port": 9999, "output_dir": None,
            **overrides,
        })

    return build


@pytest.fixture
def fake_plan(monkeypatch):
    plan = {
        "spec": SPEC,
        "artifacts": [
            {"filename": "ila_isp.tcl", "content": "# 准备 ILA\n"},
            {"filename": "ila_isp.v", "content": "// 例化片段\n"},
        ],
        "remaining_steps": ["连接业务 RTL 后综合"],
    }
    monkeypatch.setattr(debug_prepare_cli, "plan_debug_design", Mock(return_value=plan))
    return plan


@pytest.fixture
def live(monkeypatch):
    session = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    gui = Mock(return_value=session)
    preparation = SimpleNamespace(
        inspect=AsyncMock(return_value={"status": "ready", "project": PROJECT}),
        apply=AsyncMock(return_value={"status": "created", "project": PROJECT}),
    )
    monkeypatch.setattr(debug_prepare_cli, "GuiSession", gui)
    monkeypatch.setattr(debug_prepare_cli, "DebugDesignPreparation", Mock(return_value=preparation))
    return session, gui, preparation


async def test_default_plans_without_connecting_or_writing(arguments, monkeypatch, capsys):
    gui = Mock(side_effect=AssertionError("离线规划不应连接 Vivado"))
    monkeypatch.setattr(debug_prepare_cli, "GuiSession", gui)
    await debug_prepare_cli.run_debug_prepare_cli(arguments())
    plan = json.loads(capsys.readouterr().out)
    assert plan["spec"]["kind"] == "ila_ip"
    assert plan["artifacts"]
    assert plan["remaining_steps"]
    gui.assert_not_called()


async def test_output_dir_saves_exact_plan_and_all_artifacts(
    arguments, tmp_path, fake_plan, capsys,
):
    destination = tmp_path / "consumer" / "debug"
    await debug_prepare_cli.run_debug_prepare_cli(arguments(output_dir=str(destination)))
    result = json.loads(capsys.readouterr().out)
    assert json.loads((destination / "plan.json").read_text(encoding="utf-8")) == fake_plan
    for artifact in fake_plan["artifacts"]:
        assert (destination / artifact["filename"]).read_text(encoding="utf-8") == (
            artifact["content"]
        )
    assert len(result["saved_files"]) == len(fake_plan["artifacts"]) + 1


async def test_existing_later_artifact_prevents_every_write(arguments, tmp_path, fake_plan):
    destination = tmp_path / "consumer"
    destination.mkdir()
    preserved = destination / "ila_isp.v"
    preserved.write_text("用户已有 RTL", encoding="utf-8")
    with pytest.raises(FileExistsError, match="未写入任何产物"):
        await debug_prepare_cli.run_debug_prepare_cli(arguments(output_dir=str(destination)))
    assert preserved.read_text(encoding="utf-8") == "用户已有 RTL"
    assert sorted(path.name for path in destination.iterdir()) == ["ila_isp.v"]


@pytest.mark.parametrize("filename", ["../outside.tcl", "/outside.tcl", "a\\b.tcl", "plan.json"])
async def test_invalid_artifact_paths_are_rejected_before_writing(
    arguments, tmp_path, fake_plan, filename,
):
    fake_plan["artifacts"][1]["filename"] = filename
    destination = tmp_path / "consumer"
    with pytest.raises(ValueError, match="普通文件名"):
        await debug_prepare_cli.run_debug_prepare_cli(arguments(output_dir=str(destination)))
    assert not destination.exists()


@pytest.mark.parametrize("overrides", [
    {"apply": True, "output_dir": "unused"}, {"apply": True, "port": 0},
])
async def test_invalid_options_fail_before_read_or_attach(arguments, live, overrides):
    with pytest.raises(ValueError):
        await debug_prepare_cli.run_debug_prepare_cli(
            arguments(spec="file-does-not-exist.json", **overrides)
        )
    live[1].assert_not_called()


@pytest.mark.parametrize("data, message", [
    (b" " * (64 * 1024 + 1), "64 KiB"),
    (b"[]", "JSON 对象"),
    (b"{", "UTF-8 JSON"),
    (b"\xff", "UTF-8 JSON"),
])
async def test_bad_spec_fails_before_attach(arguments, tmp_path, live, data, message):
    source = tmp_path / "bad.json"
    source.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        await debug_prepare_cli.run_debug_prepare_cli(arguments(spec=str(source), apply=True))
    live[1].assert_not_called()


async def test_apply_pins_inspected_project_and_disconnects(arguments, fake_plan, live, capsys):
    session, gui, preparation = live
    await debug_prepare_cli.run_debug_prepare_cli(arguments(apply=True, port=10001))
    gui.assert_called_once_with("", session_id="debug-prepare", port=10001, attach_only=True)
    session.start.assert_awaited_once_with(timeout=5)
    preparation.inspect.assert_awaited_once_with(SPEC)
    preparation.apply.assert_awaited_once_with(SPEC, expected_project=PROJECT)
    session.stop.assert_awaited_once()
    assert json.loads(capsys.readouterr().out)["status"] == "created"


@pytest.mark.parametrize("inspection", [
    {"status": "blocked", "project": PROJECT, "reason": "已有同名 IP"},
    {"status": "ready", "project": None},
    {"status": "ready", "project": {"name": "camera"}},
    {"status": "unknown", "project": PROJECT},
])
async def test_failed_preflight_prints_evidence_and_never_applies(
    arguments, fake_plan, live, capsys, inspection,
):
    session, _, preparation = live
    preparation.inspect.return_value = inspection
    with pytest.raises(RuntimeError):
        await debug_prepare_cli.run_debug_prepare_cli(arguments(apply=True))
    preparation.apply.assert_not_awaited()
    session.stop.assert_awaited_once()
    assert json.loads(capsys.readouterr().out) == inspection


@pytest.mark.parametrize("status", ["blocked", "partial", "unknown"])
async def test_apply_failure_is_reported_without_retry(arguments, fake_plan, live, capsys, status):
    session, _, preparation = live
    preparation.apply.return_value = {"status": status, "reason": "请检查 Vivado 状态"}
    with pytest.raises(RuntimeError, match="不要直接重放"):
        await debug_prepare_cli.run_debug_prepare_cli(arguments(apply=True))
    preparation.apply.assert_awaited_once()
    session.stop.assert_awaited_once()
    assert json.loads(capsys.readouterr().out)["status"] == status


async def test_disconnect_when_attach_fails(arguments, fake_plan, live):
    session, _, preparation = live
    session.start.side_effect = OSError("连接失败")
    with pytest.raises(OSError, match="连接失败"):
        await debug_prepare_cli.run_debug_prepare_cli(arguments(apply=True))
    session.stop.assert_awaited_once()
    preparation.inspect.assert_not_awaited()
