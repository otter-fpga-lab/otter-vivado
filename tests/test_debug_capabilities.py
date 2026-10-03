"""独立停止的接口边界；不把上传、重置或本地中止伪装为硬件停止。"""

import pytest

from tests.test_debug_service import select
from tests.test_debug_service import service as service
from vivado_mcp.server import mcp


async def test_stop_capability_is_scoped_readonly_and_cannot_dispatch(service):
    await select(service)
    before = service.snapshot()
    calls = service.backend.inspect.await_count
    caps = before['capabilities']
    assert caps['stop_ila'] is False
    details = caps['stop_ila_details']
    assert details['scope'] == 'plugin_api'
    assert details['status'] == 'unsupported'
    assert details['active_version_probed'] is False
    assert details['hardware_verified'] is False
    assert 'UG835 v2022.2' in details['reference']
    assert details['fallback'] == 'native_hardware_manager_then_refresh'
    # 读取/编辑调用方快照不会探测或改写硬件，也不改变服务的能力声明。
    caps['stop_ila'] = True
    details['status'] = 'supported'
    assert service.snapshot()['capabilities']['stop_ila'] is False
    assert service.snapshot()['capabilities']['stop_ila_details']['status'] == 'unsupported'
    with pytest.raises(ValueError, match='不支持的调试操作'):
        service.submit({'action': 'stop_ila', 'params': {'core': 'ila'},
                        'expected_revision': before['revision']}, source='manual')
    after = service.snapshot()
    assert after['revision'] == before['revision']
    assert after['operations'] == before['operations']
    assert service.backend.inspect.await_count == calls
    service.backend.upload_ila.assert_not_awaited()
    service.backend.arm_ila.assert_not_awaited()
    service.backend.configure_ila.assert_not_awaited()


async def test_mcp_exposes_capability_guidance_without_stop_action():
    tools = {t.name: t for t in await mcp.list_tools()}
    assert len(tools) == 49
    assert 'stop_ila' not in tools
    actions = tools['debug_action'].input_schema['properties']['action']['enum']
    assert 'stop_ila' not in actions
    assert 'stop_ila_details' in tools['get_debug_snapshot'].description
