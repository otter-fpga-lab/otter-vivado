"""通过真实 MCP 注册调用实验；本地人工确认复用同一个服务实例。"""

from tests.test_debug_backend import DEVICE, EXPORT_MOCK, TARGET, backend
from tests.test_debug_design_tools import call, context
from tests.test_debug_experiment import READY, until
from vivado_mcp.server import mcp


async def test_registered_experiment_flow_and_local_human_ready(tmp_path):
    _, session = backend(EXPORT_MOCK)
    session.session_id, session.state = 'board', 'ready'
    ctx = context(session)
    registry = ctx.request_context.lifespan_context.debug_services
    try:
        tools = {t.name: t for t in await mcp.list_tools()}
        assert len(tools) == 47
        assert set(tools['create_debug_experiment'].input_schema['required']) == {
            'spec', 'output_dir', 'expected_revision',
        }
        assert 'confirm_ready' not in (
            tools['debug_experiment_action'].input_schema['properties']['action']['enum']
        )
        snap = await call('get_debug_snapshot', {'session_id': 'board'}, ctx)
        for action, params in [('control', {'owner': 'ai'}),
                               ('select', {'target': TARGET, 'device': DEVICE})]:
            await call('debug_action', {'session_id': 'board', 'action': action, 'params': params,
                                       'expected_revision': snap['revision']}, ctx)
            await registry.last('board').wait_idle()
            snap = await call('get_debug_snapshot', {'session_id': 'board'}, ctx)
        state = await call('create_debug_experiment', {
            'session_id': 'board', 'expected_revision': snap['revision'],
            'output_dir': str(tmp_path / 'mcp experiment'),
            'spec': {'title': 'synthetic', 'steps': [READY,
                {'id': 'export', 'kind': 'debug', 'action': 'export_ila',
                 'params': {'core': 'hw_ila_1'}}]},
        }, ctx)
        assert state['status'] == 'created', state
        exp = registry.last('board').experiment
        await call('debug_experiment_action', {
            'session_id': 'board', 'experiment_id': state['id'], 'action': 'start',
            'params': {}, 'expected_revision': state['revision'],
        }, ctx)
        await until(lambda: exp.status == 'waiting_ready')
        state = await call('get_debug_experiment', {'session_id': 'board'}, ctx)
        assert state['step']['id'] == 'ready'
        # 消费者本地按钮投递到所属事件循环；并未开另一个 Vivado 连接。
        exp.command('confirm_ready', {'step_id': 'ready'}, state['revision'], source='manual')
        await until(lambda: exp.status == 'completed')
        result = await call('get_debug_experiment', {'session_id': 'board'}, ctx)
        assert result['hardware_stopped'] is False
        assert (exp.directory / 'capture-001' / 'manifest.json').is_file()
        wrong = await call('debug_experiment_action', {
            'session_id': 'board', 'experiment_id': 'old', 'action': 'start',
            'params': {}, 'expected_revision': exp.revision,
        }, ctx)
        assert '身份不匹配' in wrong['error']
    finally:
        await registry.close()
