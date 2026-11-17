"""采集未知回执与真实 MCP 入口；不连接商业 EDA 或板卡。"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from tests.analysis.test_ila_waveform import VCD
from tests.test_debug_backend import DEVICE, EXPORT_MOCK, TARGET, backend
from tests.test_debug_design_tools import call, context
from vivado_mcp.server import mcp


@pytest.mark.parametrize('failure', [TimeoutError('late'), asyncio.CancelledError()])
async def test_export_unknown_retained_and_not_replayed(tmp_path, failure):
    api, _ = backend()
    api._mutate = AsyncMock(side_effect=failure)
    directory = tmp_path / 'unknown'
    with pytest.raises(asyncio.CancelledError if isinstance(failure, asyncio.CancelledError)
                       else RuntimeError):
        await api.export_ila(TARGET, DEVICE, 'hw_ila_1', str(directory))
    assert api._mutate.await_count == 1
    assert json.loads((directory / 'result.json').read_text())['status'] == 'unknown'
    assert not (directory / 'manifest.json').exists()


async def test_real_mcp_export_and_offline_read(tmp_path):
    _, session = backend(EXPORT_MOCK)
    session.session_id, session.state = 'board', 'ready'
    ctx = context(session)
    registry = ctx.request_context.lifespan_context.debug_services
    try:
        snapshot = await call('get_debug_snapshot', {'session_id': 'board'}, ctx)
        for name, params in [
            ('control', {'owner': 'ai'}),
            ('select', {'target': TARGET, 'device': DEVICE}),
            ('export_ila', {'core': 'hw_ila_1', 'output_dir': str(tmp_path / 'mcp')}),
        ]:
            accepted = await call('debug_action', {
                'session_id': 'board', 'action': name, 'params': params,
                'expected_revision': snapshot['revision'],
            }, ctx)
            await registry.last('board').wait_idle()
            snapshot = await call('get_debug_snapshot', {'session_id': 'board'}, ctx)
            op = next(v for v in snapshot['operations'] if v['id'] == accepted['operation_id'])
            assert op['status'] == 'succeeded', op
        record = op['result']
        wave = await call('read_ila_waveform', {
            'file_path': str(tmp_path / 'mcp' / 'capture.vcd'),
            'expected_sha256': record['waveform']['sha256'],
        })
        assert wave['events'][0]['value'] == '0'
        assert wave['capture_completeness'] == 'unverified'
    finally:
        await registry.close()


async def test_registered_read_contract_and_blocked_output(tmp_path):
    registered = {v.name: v for v in await mcp.list_tools()}
    assert {'read_ila_waveform', 'debug_action'} <= set(registered)
    assert registered['read_ila_waveform'].input_schema['required'] == ['file_path']
    assert 'export_ila' in registered['debug_action'].input_schema['properties']['action']['enum']
    path = tmp_path / 'test.vcd'
    path.write_text(VCD)
    result = await call('read_ila_waveform', {'file_path': str(path), 'signal_ids': []})
    assert not result['events'] and len(result['signals']) == 3
    result = await call('read_ila_waveform', {'file_path': str(path), 'signal_ids': ['missing']})
    assert result['status'] == 'blocked'


async def test_export_capture_strict_explicit_vs_auto_parents(tmp_path):
    from pathlib import Path
    from unittest.mock import patch

    from vivado_mcp.ila_capture import export_capture

    api, _ = backend()
    with patch.object(api, '_selection', side_effect=ValueError('bad target')):
        with pytest.raises(ValueError, match='bad target'):
            await export_capture(
                api, TARGET, DEVICE, 'hw_ila_1',
                str(tmp_path / 'auto_bad' / 'cap'), create_parents=True,
            )
    assert not (tmp_path / 'auto_bad').exists()

    with pytest.raises(FileNotFoundError):
        await export_capture(api, TARGET, DEVICE, 'hw_ila_1', str(tmp_path / 'missing' / 'cap'))
    assert not (tmp_path / 'missing').exists()

    async def write_vcd(operation, body, target, device, core, expected_uuid, **values):
        Path(values['capture_path']).write_text(VCD, encoding='utf-8')
        return ['hw_ila_1', 'hw_ila_data_1', 'u-1', '2', '0', '2024.2']

    api._mutate = AsyncMock(side_effect=write_vcd)
    auto_dir = tmp_path / 'Debug' / '20261117_000000_ila'
    record = await export_capture(
        api, TARGET, DEVICE, 'hw_ila_1', str(auto_dir), create_parents=True,
    )
    assert record['status'] == 'succeeded'
    assert (auto_dir / 'manifest.json').is_file()
