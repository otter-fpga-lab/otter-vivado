"""真实 MCP 注册到共享服务，再经 Tcl 替身验证语义写入端到端。"""

from tests.test_debug_backend import DEVICE, TARGET, backend
from tests.test_debug_controls import profile
from tests.test_debug_design_tools import call, context
from vivado_mcp.server import mcp


async def test_registered_offline_preview_and_guarded_hardware_write():
    spec = profile()
    spec.update(target=TARGET, device=DEVICE)
    spec['controls'][0].update(core='hw_vio_1')
    tools = {t.name: t for t in await mcp.list_tools()}
    assert len(tools) == 49
    assert set(tools['write_debug_control'].input_schema['required']) == {
        'spec', 'control_id', 'value', 'expected_profile_sha256', 'expected_revision',
    }
    preview = await call('resolve_debug_controls', {'spec': spec, 'preset': 'idle'})
    assert preview['writes'][0]['wire_hex'] == '0xF8'
    assert preview['executed'] is False
    _, session = backend()
    session.session_id, session.state = 'board', 'ready'
    ctx = context(session)
    registry = ctx.request_context.lifespan_context.debug_services
    try:
        service = await registry.get(session)
        for action, params in [('control', {'owner': 'ai'}),
                               ('select', {'target': TARGET, 'device': DEVICE})]:
            accepted = await call('debug_action', {
                'action': action, 'params': params, 'session_id': 'board',
                'expected_revision': service.snapshot()['revision'],
            }, ctx)
            assert 'operation_id' in accepted
            await service.wait_idle()
        request = {'spec': spec, 'control_id': 'gain', 'value': '0', 'session_id': 'board',
                   'expected_profile_sha256': preview['profile_sha256'],
                   'expected_revision': service.snapshot()['revision']}
        bad = await call('write_debug_control', {**request, 'value': '0.01'}, ctx)
        assert 'error' in bad
        accepted = await call('write_debug_control', request, ctx)
        assert accepted['status'] == 'running', accepted
        await service.wait_idle()
        snap = await call('get_debug_snapshot', {'session_id': 'board'}, ctx)
        op = snap['operations'][-1]
        assert op['status'] == 'succeeded', op
        assert op['semantic_request']['profile_sha256'] == preview['profile_sha256']
        assert op['result']['semantic']['readback']['value'] == '0'
        assert op['result']['semantic']['readback_verified'] is True
        assert op['result']['previous_staged_value'] == 'AA'
        other = next(p for p in snap['hardware']['vios'][0]['probes'] if p['name'] == 'other')
        assert other['value'] == '34' and other['staged_value'] == 'BB'
        stale = await call('write_debug_control', request, ctx)
        assert '状态已变化' in stale['error']
        spec['controls'][0]['unit'] = 'mV'
        changed = await call('write_debug_control', {
            **request, 'expected_revision': snap['revision'],
        }, ctx)
        assert '指纹' in changed['error']
    finally:
        await registry.close()
