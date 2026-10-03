"""真实 asyncio 本地流程与共享服务测试；硬件为明确替身。"""

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from tests.test_debug_service import action, select
from tests.test_debug_service import service as service
from vivado_mcp.debug_experiment import DebugExperiment, validate_experiment

READY = {'id': 'ready', 'kind': 'ready', 'label': '请确认准备好'}
CUE = {'id': 'move', 'kind': 'cue', 'label': '移动目标', 'tone': 'beep'}
WAIT = {'id': 'count', 'kind': 'countdown', 'label': '静止', 'duration_ms': 120}
ARM = {'id': 'arm', 'kind': 'debug', 'action': 'arm_ila', 'params': {'core': 'ila'}}
EXPORT = {'id': 'export', 'kind': 'debug', 'action': 'export_ila', 'params': {'core': 'ila'}}


async def setup(service, tmp_path, steps=None):
    await select(service)
    return DebugExperiment(service, {'title': '合成实验', 'steps': steps or [READY, WAIT, CUE]},
                           str(tmp_path / '实验 [a]'), service.snapshot()['revision'],
                           owner='manual')


def command(exp, name, params=None, source='manual'):
    return exp.command(name, params or {}, exp.revision, source=source)


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout=3)


async def ready(exp):
    command(exp, 'start')
    await until(lambda: exp.status == 'waiting_ready')
    command(exp, 'confirm_ready', {'step_id': 'ready'})


def records(exp):
    return [json.loads(p.read_text()) for p in sorted(exp.directory.glob('event-*.json'))]


async def test_local_run_countdown_cue_mark_and_persisted_provenance(service, tmp_path):
    exp = await setup(service, tmp_path)
    await ready(exp)
    await until(lambda: exp.snapshot()['remaining_ms'] is not None)
    command(exp, 'mark', {'label': '开始移动', 'frame_id': 'declared-123'})
    await until(lambda: exp.status == 'completed')
    events = records(exp)
    assert any(e['kind'] == 'cue' and e['sound_played'] is False for e in events)
    assert any(e['kind'] == 'marker' and e['frame_alignment'] == 'unverified' for e in events)
    assert events[-1]['hardware_stopped'] is False
    assert [e['seq'] for e in events] == sorted({e['seq'] for e in events})
    assert all(isinstance(e['elapsed_ns'], str) for e in events)
    plan = json.loads((exp.root / 'experiment.json').read_text())
    assert plan['spec_sha256'] == exp.spec_sha
    assert plan['timing'] == 'host_monotonic_not_fpga'
    assert plan['hardware_at_creation']['target'] == 'cable/serial'
    service.backend.arm_ila.assert_not_called()


async def test_ready_requires_local_human_and_current_revision(service, tmp_path):
    exp = await setup(service, tmp_path)
    revision = exp.revision
    command(exp, 'start')
    await until(lambda: exp.status == 'waiting_ready')
    with pytest.raises(ValueError, match='人工'):
        command(exp, 'confirm_ready', {'step_id': 'ready'}, source='ai')
    with pytest.raises(RuntimeError, match='状态已变化'):
        exp.command('confirm_ready', {'step_id': 'ready'}, revision, source='manual')
    with pytest.raises(RuntimeError, match='旧确认'):
        command(exp, 'confirm_ready', {'step_id': 'old'})
    assert exp.status == 'waiting_ready'
    command(exp, 'abort')
    await until(lambda: exp.status == 'aborted')


async def test_pause_freezes_remaining_and_resume_does_not_repeat_cues(service, tmp_path):
    exp = await setup(service, tmp_path, [READY, CUE, WAIT])
    await ready(exp)
    await until(lambda: exp.snapshot()['remaining_ms'] is not None)
    command(exp, 'pause')
    await until(lambda: exp.status == 'paused')
    remaining = exp.snapshot()['remaining_ms']
    await asyncio.sleep(0.15)
    assert exp.snapshot()['remaining_ms'] == remaining and remaining > 0
    command(exp, 'resume')
    await until(lambda: exp.status == 'completed')
    assert sum(e['kind'] == 'cue' for e in records(exp)) == 1


async def test_hardware_step_uses_service_and_export_new_attempt_paths(service, tmp_path):
    service.backend.export_ila = AsyncMock(return_value={'waveform': {'sha256': 'synthetic'}})
    exp = await setup(service, tmp_path, [READY, ARM, EXPORT])
    await ready(exp)
    await until(lambda: exp.status == 'completed')
    service.backend.arm_ila.assert_awaited_once_with(
        'cable/serial', 'xc7_0', core='ila', expected_uuid='ila-uuid',
    )
    first = str(exp.directory / 'capture-002')
    service.backend.export_ila.assert_awaited_once_with(
        'cable/serial', 'xc7_0', core='ila', expected_uuid='ila-uuid', output_dir=first,
    )
    old_directory = exp.directory
    with pytest.raises(RuntimeError, match='显式 refresh'):
        command(exp, 'redo', {'expected_debug_revision': service.snapshot()['revision']})
    await action(service, 'refresh')
    command(exp, 'redo', {'expected_debug_revision': service.snapshot()['revision']})
    assert exp.attempt == 2 and exp.status == 'created'
    assert exp.directory != old_directory and old_directory.is_dir()
    await ready(exp)
    await until(lambda: exp.status == 'completed')
    assert service.backend.export_ila.call_args.kwargs['output_dir'] != first
    assert service.backend.arm_ila.await_count == 2


async def test_abort_drains_accepted_operation_without_export_or_replay(service, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'command_accepted': True}

    service.backend.arm_ila.side_effect = slow
    service.backend.export_ila = AsyncMock()
    exp = await setup(service, tmp_path, [READY, ARM, EXPORT])
    await ready(exp)
    await entered.wait()
    command(exp, 'abort')
    assert exp.status not in {'completed', 'aborted'}
    release.set()
    await until(lambda: exp.status == 'aborted')
    service.backend.arm_ila.assert_awaited_once()
    service.backend.export_ila.assert_not_called()
    assert any(e['kind'] == 'operation_finished' for e in records(exp))


async def test_pause_during_hardware_waits_for_receipt_then_resumes_next_step(service, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'command_accepted': True}

    service.backend.arm_ila.side_effect = slow
    exp = await setup(service, tmp_path, [READY, ARM, CUE])
    await ready(exp)
    await entered.wait()
    command(exp, 'pause')
    assert exp.status != 'paused'
    with pytest.raises(RuntimeError, match='回执'):
        command(exp, 'resume')
    release.set()
    await until(lambda: exp.status == 'paused')
    assert not any(e['kind'] == 'cue' for e in records(exp))
    command(exp, 'resume')
    await until(lambda: exp.status == 'completed')
    service.backend.arm_ila.assert_awaited_once()


async def test_unknown_stops_followups_and_disallows_redo(service, tmp_path):
    service.backend.arm_ila.side_effect = TimeoutError('hardware uncertainty')
    exp = await setup(service, tmp_path, [READY, ARM, CUE])
    await ready(exp)
    await until(lambda: exp.status == 'unknown')
    assert not any(e['kind'] == 'cue' for e in records(exp))
    with pytest.raises(RuntimeError, match='unknown'):
        command(exp, 'redo', {'expected_debug_revision': service.snapshot()['revision']})
    service.backend.arm_ila.assert_awaited_once()


async def test_lease_blocks_other_writes_selection_and_duplicate_experiment(service, tmp_path):
    exp = await setup(service, tmp_path)
    for name, params in [('arm_ila', {'core': 'ila'}), ('select', {'target': 'x', 'device': 'y'})]:
        with pytest.raises(RuntimeError, match='活动实验'):
            service.submit({'action': name, 'params': params,
                            'expected_revision': service.snapshot()['revision']}, source='manual')
    with pytest.raises(RuntimeError, match='活动实验'):
        DebugExperiment(service, exp.spec, str(tmp_path / 'other'), service.snapshot()['revision'],
                        owner='manual')
    await action(service, 'refresh')
    await action(service, 'control', {'owner': 'ai'}, source='ai')
    assert exp.status == 'aborted'
    assert exp.error == 'control_handoff'


async def test_connection_loss_and_topology_change_do_not_run_later_steps(service, tmp_path):
    exp = await setup(service, tmp_path, [READY, ARM])
    command(exp, 'start')
    await until(lambda: exp.status == 'waiting_ready')
    service.fixture_hardware['ilas'][0]['uuid'] = 'changed'
    # 缓存尚未更新；硬件动作执行前的现场检查也必须阻断。
    command(exp, 'confirm_ready', {'step_id': 'ready'})
    await until(lambda: exp.status in {'failed', 'unknown'})
    service.backend.arm_ila.assert_not_called()


async def test_capture_wait_complete_and_timeout_dont_stop_hardware(service, tmp_path):
    step = {'id': 'capture', 'kind': 'wait_capture', 'core': 'ila', 'timeout_ms': 1000}
    exp = await setup(service, tmp_path, [READY, step, CUE])
    await ready(exp)
    await until(lambda: exp.step_index == 1)
    service.fixture_hardware['ilas'][0]['capture_complete'] = True
    await until(lambda: exp.status == 'completed')
    assert any(e['kind'] == 'capture_complete_observed' for e in records(exp))
    service.fixture_hardware['ilas'][0]['capture_complete'] = False
    exp = DebugExperiment(
        service, {'title': '超时实验', 'steps': [READY, {**step, 'timeout_ms': 20}, CUE]},
        str(tmp_path / 'timeout'), service.snapshot()['revision'], owner='manual',
    )
    await ready(exp)
    await until(lambda: exp.status == 'failed')
    assert '未停止 FPGA' in exp.error
    assert not any(e['kind'] == 'cue' for e in records(exp))


async def test_journal_failure_prevents_hardware_and_service_close_is_safe(service, tmp_path,
                                                                         monkeypatch):
    import vivado_mcp.debug_experiment as module

    exp = await setup(service, tmp_path, [READY, ARM])
    command(exp, 'start')
    await until(lambda: exp.status == 'waiting_ready')
    original = module._save_new

    def fail(path, value):
        if value.get('kind') == 'operation_intent':
            raise OSError('disk full')
        original(path, value)

    monkeypatch.setattr(module, '_save_new', fail)
    command(exp, 'confirm_ready', {'step_id': 'ready'})
    await until(lambda: exp.status == 'unknown')
    service.backend.arm_ila.assert_not_called()
    await service.close()
    service.session.stop.assert_not_called()


async def test_close_wakes_local_ready_without_stopping_session(service, tmp_path):
    exp = await setup(service, tmp_path)
    command(exp, 'start')
    await until(lambda: exp.status == 'waiting_ready')
    await service.close()
    assert exp.status == 'aborted'
    assert exp._task.done()
    service.session.stop.assert_not_called()


@pytest.mark.parametrize('change', [
    lambda s: s.update(extra=True),
    lambda s: s.update(steps=[CUE]),
    lambda s: s['steps'].append(READY),
    lambda s: s['steps'].append({'id': 'bad', 'kind': 'tcl', 'script': 'exit'}),
    lambda s: s['steps'].append({**WAIT, 'duration_ms': True}),
    lambda s: s['steps'].append({**CUE, 'tone': 'shell:play'}),
    lambda s: s['steps'].append({**ARM, 'action': 'program_device'}),
    lambda s: s['steps'].append({**EXPORT, 'params': {'core': 'ila', 'output_dir': '/arbitrary'}}),
])
def test_invalid_plans_rejected(change):
    spec = {'title': 'test', 'steps': [copy.deepcopy(READY)]}
    change(spec)
    with pytest.raises(ValueError):
        validate_experiment(spec)


async def test_immutable_spec_disconnect_and_stale_redo(service, tmp_path):
    exp = await setup(service, tmp_path, [READY, CUE])
    spec = exp.spec
    spec['steps'][1]['label'] = 'tampered'
    assert exp.spec['steps'][1]['label'] == '移动目标'
    command(exp, 'start')
    await until(lambda: exp.status == 'waiting_ready')
    service.session.is_alive = False
    await until(lambda: exp.status == 'failed')
    assert not any(e['kind'] == 'cue' for e in records(exp))
    service.session.is_alive = True
    await action(service, 'refresh')
    with pytest.raises(RuntimeError, match='调试状态已变化'):
        command(exp, 'redo', {'expected_debug_revision': -1})


async def test_accepted_operation_survives_journal_failure(service, tmp_path, monkeypatch):
    import vivado_mcp.debug_experiment as module

    exp = await setup(service, tmp_path, [READY, ARM, CUE])
    original = module._save_new

    def fail(path, value):
        if value.get('kind') == 'operation_accepted':
            raise OSError('disk disappeared')
        original(path, value)

    monkeypatch.setattr(module, '_save_new', fail)
    await ready(exp)
    await until(lambda: exp._task.done())
    assert exp.status == 'unknown'
    service.backend.arm_ila.assert_awaited_once()
    assert service.snapshot()['operations'][-1]['status'] == 'succeeded'
    assert not any(e['kind'] == 'cue' for e in records(exp))


async def test_pinned_identity_checked_inside_queued_hardware_operation(service, tmp_path,
                                                                      monkeypatch):
    exp = await setup(service, tmp_path, [READY, ARM])
    original = service.submit

    def submit(request, **kwargs):
        result = original(request, **kwargs)
        if kwargs.get('experiment') is exp:
            service.fixture_hardware['ilas'][0]['uuid'] = 'replacement'
            service._publish(copy.deepcopy(service.fixture_hardware))
        return result

    monkeypatch.setattr(service, 'submit', submit)
    await ready(exp)
    await until(lambda: not exp.active)
    assert exp.status == 'unknown'
    service.backend.arm_ila.assert_not_called()
    assert '身份改变' in service.snapshot()['operations'][-1]['error']


async def test_redo_refuses_still_armed_ila(service, tmp_path):
    exp = await setup(service, tmp_path, [READY, ARM])
    await ready(exp)
    await until(lambda: exp.status == 'completed')
    service.fixture_hardware['ilas'][0]['status'] = 'WAITING_FOR_TRIGGER'
    await action(service, 'refresh')
    with pytest.raises(RuntimeError, match='明确 IDLE'):
        command(exp, 'redo', {'expected_debug_revision': service.snapshot()['revision']})
    assert exp.attempt == 1


async def test_capture_response_after_deadline_cannot_advance(service, tmp_path):
    exp = await setup(service, tmp_path, [READY,
        {'id': 'capture', 'kind': 'wait_capture', 'core': 'ila', 'timeout_ms': 10}, CUE])

    async def delayed(*args):
        await asyncio.sleep(0.03)
        value = copy.deepcopy(service.fixture_hardware)
        value['ilas'][0]['capture_complete'] = True
        return value

    service.backend.inspect.side_effect = delayed
    await ready(exp)
    await until(lambda: exp.status == 'failed')
    assert '返回过晚' in exp.error
    assert not any(e['kind'] == 'cue' for e in records(exp))


async def test_wall_clock_jump_cannot_bypass_post_run_refresh(service, tmp_path, monkeypatch):
    import time

    exp = await setup(service, tmp_path, [READY, WAIT])
    await action(service, 'refresh')
    before_jump = time.time()
    monkeypatch.setattr(time, 'time', lambda: before_jump - 10000)
    await ready(exp)
    await until(lambda: exp.status == 'completed')
    with pytest.raises(RuntimeError, match='显式 refresh'):
        command(exp, 'redo', {'expected_debug_revision': service.snapshot()['revision']})
    await action(service, 'refresh')
    command(exp, 'redo', {'expected_debug_revision': service.snapshot()['revision']})
    assert exp.status == 'created' and exp.attempt == 2
