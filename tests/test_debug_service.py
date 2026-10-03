"""共享控制、并发、失败证据与消费者面板验证；硬件输入为明确测试桩。"""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vivado_mcp.debug_service import DebugRegistry, DebugService, load_panel, validate_panel


def hardware():
    return {
        "target": "cable/serial", "device": "xc7_0", "part": "xc7",
        "ilas": [{"name": "ila", "uuid": "ila-uuid", "status": "IDLE", "probes": []}],
        "vios": [{"name": "vio", "uuid": "vio-uuid", "probes": [
            {"name": "gain", "width": 8, "direction": "out", "value": "00"},
        ]}],
    }


@pytest.fixture
async def service():
    value = hardware()
    backend = SimpleNamespace(
        inspect=AsyncMock(side_effect=lambda *args: copy.deepcopy(value)),
        inventory=AsyncMock(return_value={"targets": []}),
        write_vio=AsyncMock(return_value={"value": "02"}),
        arm_ila=AsyncMock(return_value={"status": "armed"}),
        configure_ila=AsyncMock(return_value={"configured": True}),
        upload_ila=AsyncMock(return_value={"data": "hw_ila_data_1"}),
    )
    session = SimpleNamespace(
        session_id="test", state="ready", is_alive=True, stop=AsyncMock(),
    )
    result = DebugService(session, backend=backend)
    result.fixture_hardware = value
    try:
        yield result
    finally:
        await result.close()
        session.stop.assert_not_called()


async def action(service, name, params=None, source="manual"):
    accepted = service.submit({
        "action": name, "params": params or {},
        "expected_revision": service.snapshot()["revision"],
    }, source=source)
    await service.wait_idle()
    return next(
        op for op in service.snapshot()["operations"] if op["id"] == accepted["operation_id"]
    )


async def select(service):
    result = await action(service, "select", {"target": "cable/serial", "device": "xc7_0"})
    assert result["status"] == "succeeded"


async def test_explicit_selection_shared_cache_and_control_handoff(service):
    await select(service)
    before = service.backend.inspect.await_count
    snapshot = service.snapshot()
    snapshot["hardware"]["vios"].clear()
    assert service.snapshot()["hardware"]["vios"]
    assert service.backend.inspect.await_count == before
    request = {"action": "write_vio", "params": {"core": "vio", "probe": "gain", "value": "2"},
               "expected_revision": service.snapshot()["revision"]}
    with pytest.raises(RuntimeError, match="控制权"):
        service.submit(request, source="ai")
    await action(service, "control", {"owner": "ai"}, source="ai")
    result = await action(service, "write_vio", request["params"], source="ai")
    assert result["status"] == "succeeded"
    service.backend.write_vio.assert_awaited_once_with(
        "cable/serial", "xc7_0", core="vio", probe="gain", value="2", expected_uuid="vio-uuid",
    )
    await action(service, "control", {"owner": "manual"})
    with pytest.raises(RuntimeError, match="控制权"):
        service.submit(
            {**request, "expected_revision": service.snapshot()["revision"]}, source="ai",
        )


async def test_no_default_device_or_unknown_current_state_write(service):
    with pytest.raises(RuntimeError, match="最新硬件状态"):
        service.submit({"action": "write_vio", "params": {
            "core": "vio", "probe": "gain", "value": "2",
        }, "expected_revision": 0}, source="manual")
    service.backend.write_vio.assert_not_awaited()


async def test_busy_requests_not_queued_and_stale_revisions_not_replayed(service):
    await select(service)
    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        await gate.wait()
        return {"value": "02"}

    service.backend.write_vio.side_effect = slow
    request = {"action": "write_vio", "params": {"core": "vio", "probe": "gain", "value": "2"},
               "expected_revision": service.snapshot()["revision"]}
    service.submit(request, source="manual")
    await asyncio.sleep(0)
    assert service.snapshot()["busy"]
    with pytest.raises(RuntimeError, match="已有调试"):
        service.submit(request, source="manual")
    await service.sample()
    gate.set()
    await service.wait_idle()
    with pytest.raises(RuntimeError, match="状态已变化"):
        service.submit(request, source="manual")
    assert service.backend.write_vio.await_count == 1


async def test_changed_device_core_uuid_blocks_write_before_mutation(service):
    await select(service)
    service.fixture_hardware["vios"][0]["uuid"] = "reprogrammed"
    result = await action(service, "write_vio", {"core": "vio", "probe": "gain", "value": "2"})
    assert result["status"] == "failed"
    assert "结构已变化" in result["error"]
    service.backend.write_vio.assert_not_awaited()


async def test_timeout_is_unknown_no_auto_retry_and_explicit_refresh_required(service):
    await select(service)
    observed = service.snapshot()["observed_at"]
    service.backend.write_vio.side_effect = TimeoutError("仍在执行")
    result = await action(service, "write_vio", {"core": "vio", "probe": "gain", "value": "2"})
    assert result["status"] == "unknown"
    assert service.snapshot()["connection"] == "error"
    assert service.snapshot()["observed_at"] >= observed
    await service.sample()
    assert service.backend.write_vio.await_count == 1
    # 仅枚举不能掩盖目标设备的未知状态。
    await action(service, "inventory")
    assert service.snapshot()["connection"] == "error"
    await action(service, "refresh")
    assert service.snapshot()["connection"] == "connected"


async def test_successful_write_then_read_failure_retains_result_as_unknown(service):
    await select(service)
    service.backend.inspect.side_effect = [hardware(), OSError("lost")]
    result = await action(service, "write_vio", {"core": "vio", "probe": "gain", "value": "2"})
    assert result["status"] == "unknown"
    assert result["result"] == {"value": "02"}
    assert service.snapshot()["connection"] == "error"


async def test_vio_readback_mismatch_is_not_success(service):
    await select(service)
    service.backend.write_vio.return_value = {"value": "03", "readback_matches": False}
    result = await action(service, "write_vio", {"core": "vio", "probe": "gain", "value": "2"})
    assert result["status"] == "unknown"
    assert result["result"]["value"] == "03"
    assert service.snapshot()["connection"] == "error"


async def test_failed_selection_discards_previous_device(service):
    await select(service)
    service.backend.inspect.side_effect = ValueError("not found")
    await action(service, "select", {"target": "other", "device": "other"})
    assert service.snapshot()["selection"] is None
    assert service.snapshot()["hardware"] is None


async def test_external_busy_or_disconnected_preserves_old_observation(service):
    await select(service)
    before = service.snapshot()
    service.session.state = "busy"
    await service.sample()
    assert service.snapshot()["connection"] == "busy"
    assert service.snapshot()["hardware"] == before["hardware"]
    assert service.snapshot()["observed_at"] == before["observed_at"]
    service.session.is_alive = False
    await service.sample()
    assert service.snapshot()["connection"] == "disconnected"
    assert service.backend.inspect.await_count == 1


@pytest.mark.parametrize("invalid_request", [
    {"action": "run_tcl", "params": {"code": "exit"}, "expected_revision": 0},
    {"action": "inventory", "params": {"surprise": True}, "expected_revision": 0},
    {"action": "inventory", "params": {}, "expected_revision": True},
    {"action": "arm_ila", "params": {"core": "ila", "immediate": "false"},
     "expected_revision": 0},
    {"action": "write_vio", "params": {"core": "vio", "probe": "gain", "value": 2},
     "expected_revision": 0},
])
async def test_invalid_requests_never_reach_backend(service, invalid_request):
    with pytest.raises(ValueError):
        service.submit(invalid_request, source="manual")
    service.backend.inspect.assert_not_awaited()
    service.backend.write_vio.assert_not_awaited()


async def test_http_bridge_runs_operation_on_owner_loop(service):
    request = {"action": "inventory", "params": {}, "expected_revision": 0}
    accepted = await asyncio.to_thread(service._http_submit, request)
    await service.wait_idle()
    assert service.snapshot()["operations"][-1]["id"] == accepted["operation_id"]
    assert service.snapshot()["operations"][-1]["source"] == "manual"


async def test_registry_close_waits_for_accepted_operation_before_reopening(service):
    registry = DebugRegistry()
    registry._services["test"] = service
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return {"targets": []}

    service.backend.inventory.side_effect = slow
    service.submit({"action": "inventory", "params": {}, "expected_revision": 0},
                   source="manual")
    closing = asyncio.create_task(registry.release("test"))
    await asyncio.sleep(0)
    reopening = asyncio.create_task(registry.get(service.session))
    await asyncio.sleep(0)
    assert not reopening.done()
    assert registry.last("test") is service
    gate.set()
    assert await closing is True
    new_service = await reopening
    assert new_service is not service
    assert service.snapshot()["operations"][-1]["status"] == "succeeded"
    assert new_service.snapshot()["selection"] is None
    await registry.close()
    with pytest.raises(RuntimeError, match="关闭"):
        await registry.get(service.session)


def test_panel_description_size_shape_and_no_script_fields(tmp_path):
    panel = {"title": "ISP", "controls": [{
        "kind": "slider", "label": "增益", "core": "vio", "probe": "gain",
        "min": 0, "max": 255, "step": 1,
    }]}
    path = tmp_path / "panel.json"
    path.write_text(json.dumps(panel), encoding="utf-8")
    assert load_panel(str(path)) == panel
    with pytest.raises(ValueError):
        validate_panel({**panel, "script": "exit"})
    with pytest.raises(ValueError):
        validate_panel({**panel, "controls": panel["controls"] * 2})
    path.write_bytes(b" " * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        load_panel(str(path))
