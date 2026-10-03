"""验证 CLI attach 边界与合成演示，全部测试不连接商业 EDA。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vivado_mcp import debug_cli
from vivado_mcp.debug_demo import DEMO_DEVICE, DEMO_TARGET, DemoDebugBackend
from vivado_mcp.debug_service import DebugService


def arguments(**overrides):
    values = dict(port=9999, demo=False, panel=None, json=True, target=None, device=None)
    return SimpleNamespace(**(values | overrides))


@pytest.mark.asyncio
async def test_demo_json_loads_panel_without_creating_gui(monkeypatch, capsys):
    gui = Mock(side_effect=AssertionError("demo 不应连接 GUI"))
    monkeypatch.setattr(debug_cli, "GuiSession", gui)
    panel = Path(__file__).parents[1] / "examples" / "debug" / "panel.json"
    await debug_cli.run_debug_cli(arguments(demo=True, panel=str(panel)))
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["source"] == "demo"
    assert snapshot["selection"] == {"target": DEMO_TARGET, "device": DEMO_DEVICE}
    assert snapshot["hardware"]["part"] == "SYNTHETIC-NO-FPGA"
    assert len(snapshot["panel"]["controls"]) == 2
    assert all(op["status"] == "succeeded" for op in snapshot["operations"])
    gui.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"target": "target"}, {"device": "device"}, {"port": 0}])
async def test_invalid_selection_and_port_fail_before_connecting(monkeypatch, overrides):
    gui = Mock()
    monkeypatch.setattr(debug_cli, "GuiSession", gui)
    with pytest.raises(ValueError):
        await debug_cli.run_debug_cli(arguments(**overrides))
    gui.assert_not_called()


@pytest.mark.asyncio
async def test_live_cli_attaches_without_selecting_device(monkeypatch, capsys):
    session = SimpleNamespace(
        session_id="debug", is_alive=True, state="ready", start=AsyncMock(), stop=AsyncMock(),
    )
    gui = Mock(return_value=session)
    backend = DemoDebugBackend()
    backend.inspect = AsyncMock(wraps=backend.inspect)
    monkeypatch.setattr(debug_cli, "GuiSession", gui)
    monkeypatch.setattr(
        debug_cli, "DebugService",
        lambda session, **kwargs: DebugService(session, backend=backend, source=kwargs["source"]),
    )
    await debug_cli.run_debug_cli(arguments(port=10001))
    snapshot = json.loads(capsys.readouterr().out)
    gui.assert_called_once_with("", session_id="debug", port=10001, attach_only=True)
    session.start.assert_awaited_once_with(timeout=5)
    session.stop.assert_awaited_once()
    backend.inspect.assert_not_awaited()
    assert snapshot["selection"] is None
    assert snapshot["hardware"] is None
    assert snapshot["source"] == "live"


@pytest.mark.asyncio
async def test_live_cli_disconnects_if_attach_fails(monkeypatch):
    session = SimpleNamespace(start=AsyncMock(side_effect=OSError("断开")), stop=AsyncMock())
    monkeypatch.setattr(debug_cli, "GuiSession", Mock(return_value=session))
    with pytest.raises(OSError, match="断开"):
        await debug_cli.run_debug_cli(arguments())
    session.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_demo_writes_are_local_and_validate_direction_width_and_uuid():
    backend = DemoDebugBackend()
    result = await backend.write_vio(DEMO_TARGET, DEMO_DEVICE, "demo_vio", "gain", "0xabc")
    assert result["value"] == "ABC"
    assert result["source"] == "demo"
    assert result["functional_effect_verified"] is False
    snapshot = await backend.inspect(DEMO_TARGET, DEMO_DEVICE)
    assert snapshot["vios"][0]["probes"][0]["value"] == "ABC"
    snapshot["vios"][0]["probes"][0]["value"] = "tampered"
    unchanged = await backend.inspect(DEMO_TARGET, DEMO_DEVICE)
    assert unchanged["vios"][0]["probes"][0]["value"] == "ABC"
    for probe, value, uuid in [
        ("frame_count", "1", None), ("gain", "4096", None), ("gain", "1", "wrong"),
    ]:
        with pytest.raises(ValueError):
            await backend.write_vio(DEMO_TARGET, DEMO_DEVICE, "demo_vio", probe, value, uuid)


@pytest.mark.asyncio
async def test_demo_waiting_capture_does_not_invent_samples():
    backend = DemoDebugBackend()
    await backend.arm_ila(DEMO_TARGET, DEMO_DEVICE, "demo_ila")
    await asyncio.sleep(0)
    for _ in range(3):
        core = (await backend.inspect(DEMO_TARGET, DEMO_DEVICE))["ilas"][0]
        assert core["status"] == "WAITING_FOR_TRIGGER"
        assert core["capture_complete"] is False
        assert core["sample_count"] == 0
    with pytest.raises(ValueError, match="尚无完整采集"):
        await backend.upload_ila(DEMO_TARGET, DEMO_DEVICE, "demo_ila")


@pytest.mark.asyncio
async def test_demo_immediate_capture_is_explicitly_synthetic():
    backend = DemoDebugBackend()
    await backend.configure_ila(
        DEMO_TARGET, DEMO_DEVICE, "demo_ila", "frame_valid", "eq1'b0", trigger_position=3,
    )
    await backend.arm_ila(DEMO_TARGET, DEMO_DEVICE, "demo_ila", immediate=True)
    result = await backend.upload_ila(DEMO_TARGET, DEMO_DEVICE, "demo_ila")
    assert result["source"] == "demo"
    assert result["capture_complete"] is True
    assert result["exported"] is False
    assert "不存在于 Vivado" in result["note"]
