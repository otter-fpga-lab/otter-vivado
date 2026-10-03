"""消费者本地实验运行器；共享硬件服务，单调时钟计时，逐事件留证。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from vivado_mcp.debug_bundle import _save_new
from vivado_mcp.debug_service import _PARAMETERS, _topology

_TERMINAL = {'completed', 'aborted', 'failed', 'unknown'}
_ACTIONS = {'configure_ila', 'write_vio', 'arm_ila', 'export_ila'}


def validate_experiment(spec):
    """只允许有限声明步骤，不接收 Tcl、脚本、烧录或任意导出路径。"""
    if not isinstance(spec, dict) or set(spec) != {'title', 'steps'}:
        raise ValueError('实验必须仅包含 title/steps')
    if not isinstance(spec['title'], str) or not 1 <= len(spec['title']) <= 120:
        raise ValueError('title 必须为 1~120 字符')
    steps = spec['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
        raise ValueError('steps 必须为 1~64 个步骤')
    ids = set()
    fields = {'ready': {'label'}, 'countdown': {'label', 'duration_ms'},
              'cue': {'label', 'tone'}, 'debug': {'action', 'params'},
              'wait_capture': {'core', 'timeout_ms'}}
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get('kind'), str):
            raise ValueError('步骤需要 kind')
        kind = step['kind']
        if kind not in fields or set(step) != {'id', 'kind'} | fields[kind]:
            raise ValueError('步骤字段不符合约定')
        name = step['id']
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or name in ids:
            raise ValueError('步骤 id 必须是唯一的 1~64 字符文本')
        ids.add(name)
        if 'label' in step and (
            not isinstance(step['label'], str) or not 1 <= len(step['label']) <= 512
        ):
            raise ValueError('label 必须为 1~512 字符')
        for key in ('duration_ms', 'timeout_ms'):
            if key in step and (type(step[key]) is not int or not 1 <= step[key] <= 600000):
                raise ValueError('时长必须为 1~600000 毫秒整数')
        if kind == 'cue' and step['tone'] not in ('none', 'beep'):
            raise ValueError('tone 仅支持 none/beep（由消费者播放）')
        if kind == 'wait_capture' and (
            not isinstance(step['core'], str) or not 1 <= len(step['core']) <= 4096
        ):
            raise ValueError('wait_capture 需要精确 core 名')
        if kind == 'debug':
            action, params = step['action'], step['params']
            if (not isinstance(action, str) or action not in _ACTIONS
                    or not isinstance(params, dict)):
                raise ValueError('实验 debug 动作不受支持')
            required, optional = _PARAMETERS[action]
            required = required - {'output_dir'}
            if not required <= params.keys() or params.keys() - required - optional:
                raise ValueError('实验动作参数不符合约定；导出路径由运行器生成')
            for key, value in params.items():
                if key == 'immediate':
                    if type(value) is not bool:
                        raise ValueError('immediate 必须为布尔值')
                elif key == 'trigger_position':
                    if type(value) is not int or value < 0:
                        raise ValueError('trigger_position 必须为非负整数')
                elif not isinstance(value, str) or not 1 <= len(value) <= 4096 or '\x00' in value:
                    raise ValueError('动作参数必须为非空文本，不能含 NUL')
    if steps[0]['kind'] != 'ready':
        raise ValueError('实验首步必须是 ready，由消费者本地人工确认')
    raw = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > 65536:
        raise ValueError('实验描述不能超过 64 KiB')
    return copy.deepcopy(spec), hashlib.sha256(raw).hexdigest()


class _Aborted(Exception):
    pass


class DebugExperiment:
    """单个共享服务上的本地状态机；消费者适配器须在同一事件循环调用。"""

    def __init__(self, service, spec, output_dir, expected_revision, *, owner='ai'):
        if asyncio.get_running_loop() is not service._loop:
            raise RuntimeError('实验必须在共享服务所属事件循环创建')
        self._spec, self.spec_sha = validate_experiment(spec)
        if owner not in ('ai', 'manual'):
            raise ValueError('实验 owner 必须为 ai/manual')
        if service.experiment is not None and service.experiment.active:
            raise RuntimeError('已有活动实验；先中止并等待回执')
        self.service, self.owner = service, owner
        snapshot = service.snapshot()
        service._session_ready()
        if (type(expected_revision) is not int or snapshot['revision'] != expected_revision
                or snapshot['busy'] or service._sampling):
            raise RuntimeError('调试状态已变化或正在执行，请重新读取快照')
        if snapshot['connection'] != 'connected' or not snapshot['hardware']:
            raise RuntimeError('创建实验前必须选择设备并成功刷新')
        if snapshot['control'] != owner:
            raise RuntimeError('实验 owner 与共享控制权不一致')
        self.identity = _topology(snapshot['hardware'])
        self.selection = copy.deepcopy(snapshot['selection'])
        self._check_cores(snapshot['hardware'], idle=True)
        if not isinstance(output_dir, str) or not output_dir or '\x00' in output_dir:
            raise ValueError('output_dir 必须为消费者工程内新目录')
        self.root = Path(output_dir).expanduser().absolute()
        self.root.mkdir(exist_ok=False)
        self.root = self.root.resolve()
        self.id = uuid.uuid4().hex
        self.revision = 0
        self.attempt = 0
        self.status = 'created'
        self.error = None
        self.events = []
        self._task = None
        self._wake = asyncio.Event()
        self._stop = None
        self._pause = False
        self._ready = False
        self._seq = 0
        self._started = time.monotonic_ns()
        self._remaining = None
        self._slice_started = None
        self.step_index = 0
        self.finished_at = None
        self._finished_operation_ids = set()
        _save_new(self.root / 'experiment.json', {
            'schema': 'otter.experiment.v1', 'id': self.id, 'spec': self.spec,
            'spec_sha256': self.spec_sha, 'owner': owner, 'source': snapshot['source'],
            'selection': self.selection, 'hardware_at_creation': snapshot['hardware'],
            'timing': 'host_monotonic_not_fpga',
        })
        self._new_attempt()
        service.experiment = self

    @property
    def spec(self):
        """对外只暴露副本；重做不能静默修改已记录的实验计划。"""
        return copy.deepcopy(self._spec)

    @property
    def active(self):
        return self.status not in _TERMINAL

    def _check_cores(self, hardware, *, idle=False):
        for step in self.spec['steps']:
            if step['kind'] not in ('debug', 'wait_capture'):
                continue
            core = step['params']['core'] if step['kind'] == 'debug' else step['core']
            kind = 'vios' if step.get('action') == 'write_vio' else 'ilas'
            matches = [c for c in hardware[kind] if c['name'] == core]
            if len(matches) != 1 or not matches[0].get('uuid'):
                raise ValueError('实验需要唯一的核和实际 UUID')
            if idle and kind == 'ilas' and matches[0].get('status') != 'IDLE':
                raise RuntimeError('创建或重做前 ILA 必须明确 IDLE；本地中止不会停止 FPGA 采集')

    def validate_hardware(self, hardware):
        if _topology(hardware) != self.identity:
            raise RuntimeError('实验设备或核/探针身份改变，不能继续执行')

    def _guard(self):
        snapshot = self.service.snapshot()
        if snapshot['connection'] not in ('connected', 'busy'):
            raise RuntimeError('实验调试连接不可用；不自动重连或重试')
        if snapshot['selection'] != self.selection or snapshot['control'] != self.owner:
            raise RuntimeError('实验目标或控制权已经改变')
        self.validate_hardware(snapshot['hardware'])
        return snapshot

    def _record(self, kind, **fields):
        self._seq += 1
        self.revision += 1
        event = {'seq': self._seq, 'revision': self.revision, 'kind': kind,
                 'attempt': self.attempt, 'status': self.status,
                 'step_index': self.step_index, 'utc': datetime.now(timezone.utc).isoformat(),
                 'elapsed_ns': str(time.monotonic_ns() - self._started), **fields}
        try:
            if self._seq > 4096:
                raise ValueError('单次实验最多记录 4096 个事件，请保存后新建实验')
            _save_new(self.directory / f'event-{self._seq:06d}.json', event)
        except (OSError, ValueError) as exc:
            self.status, self.error = 'unknown', f'实验记录失败；核对设备与文件：{exc}'
            self._stop = 'journal_failure'
            self._wake.set()
            raise RuntimeError(self.error) from exc
        self.events = (self.events + [event])[-100:]

    def _new_attempt(self):
        directory = self.root / f'attempt-{self.attempt + 1:04d}'
        directory.mkdir(exist_ok=False)
        self.attempt += 1
        self.directory = directory
        self.status, self.error, self.step_index = 'created', None, 0
        self._stop, self._pause, self._ready = None, False, False
        self._remaining, self._slice_started = None, None
        self.finished_at = None
        self._record('attempt_created', spec_sha256=self.spec_sha)

    def snapshot(self):
        """只读快照；计时刷新不改变 revision，不查询 EDA。"""
        remaining = self._remaining
        if remaining is not None and self._slice_started is not None:
            remaining = max(0, remaining - (time.monotonic() - self._slice_started))
        return {'id': self.id, 'revision': self.revision, 'status': self.status,
                'attempt': self.attempt, 'output_dir': str(self.directory),
                'owner': self.owner, 'source': self.service.snapshot()['source'],
                'step_index': self.step_index,
                'step': copy.deepcopy(self.spec['steps'][self.step_index])
                if self.step_index < len(self.spec['steps']) else None,
                'remaining_ms': math.ceil(remaining * 1000) if remaining is not None else None,
                'pause_requested': self._pause, 'abort_requested': self._stop,
                'error': self.error, 'events': copy.deepcopy(self.events),
                'timing': 'host_monotonic_not_fpga', 'hardware_stopped': False}

    def interrupt(self, reason):
        """仅请求停止编排；不能取消已接受的硬件操作。"""
        if self.active:
            self._stop = reason
            self._wake.set()
            if self._task is None or self._task.done():
                self._terminal('aborted', reason)

    def command(self, action, params, expected_revision, *, source):
        if asyncio.get_running_loop() is not self.service._loop:
            raise RuntimeError('实验命令必须投递到共享服务所属事件循环')
        if source not in ('manual', 'ai') or not isinstance(params, dict):
            raise ValueError('实验命令需要明确来源与参数对象')
        if type(expected_revision) is not int or expected_revision != self.revision:
            raise RuntimeError('实验状态已变化，请重新读取实验快照')
        if action == 'mark':
            if not self.active or set(params) - {'label', 'frame_id'} or 'label' not in params:
                raise ValueError('mark 需要活动实验和 label，可选声明的 frame_id')
            if any(not isinstance(v, str) or not 1 <= len(v) <= 512 for v in params.values()):
                raise ValueError('动作标记必须是 1~512 字符文本')
            self._record('marker', source=source, **params, frame_alignment='unverified')
        elif action == 'confirm_ready':
            if source != 'manual':
                raise ValueError('ready 由消费者本地人工入口确认，AI 不能代为确认实际就绪')
            if self.status != 'waiting_ready' or self._pause or params != {
                'step_id': self.spec['steps'][self.step_index]['id'],
            }:
                raise RuntimeError('就绪步骤已变化或暂停，不能接受旧确认')
            self._record('ready_confirmed', source=source)
            self._ready = True
        elif action == 'redo':
            if set(params) != {'expected_debug_revision'} or self.status not in {
                'completed', 'aborted', 'failed',
            } or (self._task is not None and not self._task.done()):
                raise RuntimeError('仅明确终止的实验可重做；unknown 必须先核对现场后新建')
            snapshot = self._guard()
            self.service._session_ready()
            revision = params['expected_debug_revision']
            if (type(revision) is not int or revision != snapshot['revision'] or snapshot['busy']
                    or self.service._sampling):
                raise RuntimeError('调试状态已变化或忙，不能重做')
            refresh = next((op for op in reversed(snapshot['operations'])
                            if op['action'] == 'refresh'), None)
            if not refresh or refresh['status'] != 'succeeded' or (
                refresh['id'] in self._finished_operation_ids
            ):
                raise RuntimeError('重做前须在上轮结束后显式 refresh 核对设备')
            self._check_cores(snapshot['hardware'], idle=True)
            if self.attempt >= 32:
                raise ValueError('最多 32 轮，请保存记录后新建实验')
            self._new_attempt()
        else:
            if params or action not in {'start', 'pause', 'resume', 'abort'}:
                raise ValueError('未知实验命令或多余参数')
            if not self.active:
                raise RuntimeError('实验已终止，不自动恢复')
            if action == 'start':
                if self.status != 'created':
                    raise RuntimeError('实验已启动，不能重复 start')
                self._guard()
                self.status = 'running'
                self._record('started', source=source)
                self._task = asyncio.create_task(self._run())
            elif action == 'pause':
                if self.status == 'created' or self._pause:
                    raise RuntimeError('实验尚未启动或已请求暂停')
                self._record('pause_requested', source=source)
                self._pause = True
            elif action == 'resume':
                if self.status != 'paused' or not self._pause:
                    raise RuntimeError('尚未进入 paused，请等待当前硬件回执')
                self._guard()
                self._record('resume_requested', source=source)
                self._pause = False
            else:
                self._record('abort_requested', source=source)
                self.interrupt('requested')
        self._wake.set()
        return self.snapshot()

    async def _wait(self, seconds=0.1):
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _gate(self):
        if self._stop:
            raise _Aborted(self._stop)
        self._guard()
        if self._pause:
            previous = self.status
            self.status = 'paused'
            self._record('paused')
            while self._pause:
                if self._stop:
                    raise _Aborted(self._stop)
                self._guard()
                await self._wait()
            self.status = previous
            self._record('resumed')
        if self._stop:
            raise _Aborted(self._stop)

    async def _timer(self, milliseconds):
        self._remaining = milliseconds / 1000
        while self._remaining > 0:
            await self._gate()
            self._slice_started = time.monotonic()
            await self._wait(min(0.1, self._remaining))
            elapsed = time.monotonic() - self._slice_started
            late = max(0, elapsed - self._remaining)
            self._remaining = max(0, self._remaining - elapsed)
            self._slice_started = None
        self._record('countdown_finished', late_ms=math.ceil(late * 1000))
        self._remaining = None

    async def _debug(self, step):
        while self.service.snapshot()['busy'] or self.service._sampling:
            await self._gate()
            await self._wait()
        await self._gate()
        snapshot = self._guard()
        params = copy.deepcopy(step['params'])
        if step['action'] == 'export_ila':
            params['output_dir'] = str(self.directory / f'capture-{self.step_index:03d}')
        self._record('operation_intent', action=step['action'], params=params,
                     debug_revision=snapshot['revision'])
        accepted = self.service.submit({
            'action': step['action'], 'params': params, 'expected_revision': snapshot['revision'],
        }, source=self.owner, experiment=self)
        try:
            self._record('operation_accepted', **accepted)
        finally:
            # 即使持久化失败，也先保留已接受操作的最终回执，不发起任何后续步骤。
            await self.service.wait_idle()
        operation = next(op for op in self.service.snapshot()['operations']
                         if op['id'] == accepted['operation_id'])
        self._record('operation_finished', operation=operation)
        if operation['status'] != 'succeeded':
            self._terminal('unknown', '硬件操作结果未确认；核对现场，不自动重试')
            raise _Aborted('hardware_unknown')

    async def _capture(self, step):
        deadline = time.monotonic() + step['timeout_ms'] / 1000
        # 等待采集的超时按实际经过时间计，暂停不会延长 FPGA 采集的可观察期限。
        while True:
            await self._gate()
            if time.monotonic() >= deadline:
                raise TimeoutError('等待采集超时；未停止 FPGA，也不自动重新 arm')
            observed_before = time.time()
            await self.service.sample()
            if time.monotonic() >= deadline:
                raise TimeoutError('等待采集超时；查询返回过晚，未停止 FPGA')
            snapshot = self._guard()
            core = next(c for c in snapshot['hardware']['ilas'] if c['name'] == step['core'])
            if core.get('capture_complete') is True and (
                snapshot['observed_at'] is not None and snapshot['observed_at'] >= observed_before
            ):
                self._record('capture_complete_observed', core=step['core'],
                             observed_at=snapshot['observed_at'])
                return
            await self._wait()

    def _terminal(self, status, error=None):
        if self.status in _TERMINAL:
            return
        self.status, self.error = status, error
        self.finished_at = time.time()
        self._finished_operation_ids = {
            op['id'] for op in self.service.snapshot()['operations']
        }
        self._remaining, self._slice_started = None, None
        try:
            self._record('finished', error=error, hardware_stopped=False)
        except RuntimeError:
            # 日志故障已置为 unknown；不能让释放服务再次失败或伪造成功回执。
            pass

    async def _run(self):
        try:
            for index, step in enumerate(self.spec['steps']):
                self.step_index = index
                await self._gate()
                self.status = 'running'
                self._record('step_started', step=step)
                if step['kind'] == 'ready':
                    self.status, self._ready = 'waiting_ready', False
                    self._record('waiting_ready')
                    while not self._ready:
                        await self._gate()
                        await self._wait()
                elif step['kind'] == 'countdown':
                    await self._timer(step['duration_ms'])
                elif step['kind'] == 'cue':
                    self._record('cue', label=step['label'], tone=step['tone'], sound_played=False)
                elif step['kind'] == 'debug':
                    await self._debug(step)
                else:
                    await self._capture(step)
                await self._gate()
                self._record('step_finished')
            self.step_index = len(self.spec['steps'])
            self._terminal('completed')
        except _Aborted as exc:
            self._terminal('aborted', str(exc))
        except asyncio.CancelledError:
            self._terminal('unknown', '本地执行被取消；不重放硬件操作')
            raise
        except Exception as exc:
            self._terminal('failed', str(exc))

    async def close(self):
        self.interrupt('service_closed')
        if self._task is not None:
            await asyncio.shield(self._task)
