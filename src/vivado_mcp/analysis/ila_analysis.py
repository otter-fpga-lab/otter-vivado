"""根据消费者声明分析有限 VCD；不推测协议语义，不操作设备。"""

from __future__ import annotations

import copy
import hashlib
import json
import re

from vivado_mcp.analysis.ila_waveform import _scan_ila_waveform


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or (
        value.keys() - set(required) - set(optional)
    ):
        raise ValueError(f"分析描述需要字段 {sorted(required)}，可选 {sorted(optional)}")


def _ids(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 64 or any(
        not isinstance(v, str) or not 1 <= len(v) <= 256 for v in value
    ) or len(set(value)) != len(value):
        raise ValueError("signals/data 必须是 1~64 个不重复的 VCD 标识符")
    return value


def _known(value):
    return value is not None and 'x' not in value and 'z' not in value


def _validate(spec):
    _object(spec, {'signals'}, {'sample_clock', 'reset', 'checks'})
    selected = _ids(spec['signals'])
    checks = spec.get('checks', [])
    if not isinstance(checks, list) or len(checks) > 16:
        raise ValueError("checks 必须是最多 16 项列表")
    clock = spec.get('sample_clock')
    if 'sample_clock' in spec:
        _object(clock, {'id', 'edge', 'values'})
        if clock['edge'] not in ('rising', 'falling') or clock['values'] not in (
            'before_tick', 'after_tick',
        ):
            raise ValueError("sample_clock 必须显式选择 edge 和 before_tick/after_tick")
    if (checks or 'reset' in spec) and clock is None:
        raise ValueError("规则检查和 reset 需要显式 sample_clock")
    references = [clock['id']] if clock else []
    reset = spec.get('reset')
    if 'reset' in spec:
        _object(reset, {'id', 'active'})
        if reset['active'] not in ('0', '1'):
            raise ValueError("reset.active 必须是 0/1 字符串")
        references.append(reset['id'])
    names = set()
    for rule in checks:
        if not isinstance(rule, dict):
            raise ValueError("check 必须是对象")
        kind = rule.get('kind')
        fields = {
            'allowed_values': {'signal', 'values'},
            'state_transitions': {'signal', 'allowed_pairs'},
            'stable_while_stalled': {'valid', 'ready', 'data'},
        }
        if not isinstance(kind, str) or kind not in fields:
            raise ValueError("不支持的检查类型")
        _object(rule, {'name', 'kind'} | fields[kind])
        name = rule['name']
        if not isinstance(name, str) or not 1 <= len(name) <= 80 or name in names:
            raise ValueError("check.name 必须是唯一的 1~80 字符文本")
        names.add(name)
        if kind == 'stable_while_stalled':
            references += [rule['valid'], rule['ready']] + _ids(rule['data'])
        else:
            references.append(rule['signal'])
            values = rule['values'] if kind == 'allowed_values' else rule['allowed_pairs']
            if not isinstance(values, list) or not 1 <= len(values) <= 256:
                raise ValueError("values/allowed_pairs 必须是 1~256 项列表")
            for value in values:
                bits = [value] if kind == 'allowed_values' else value
                if not isinstance(bits, list) or len(bits) != (
                    1 if kind == 'allowed_values' else 2
                ):
                    raise ValueError("allowed_pairs 每项必须是两个完整二进制状态")
                if any(not isinstance(v, str) or not re.fullmatch('[01]{1,4096}', v) for v in bits):
                    raise ValueError("规则值必须是已知的完整二进制字符串，不使用 x/z 通配符")
    if any(not isinstance(v, str) or v not in selected for v in references):
        raise ValueError("时钟、复位和规则的全部标识符必须在 signals 中")
    canonical = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(canonical.encode()) > 65536:
        raise ValueError("分析描述不能超过 64 KiB")
    return copy.deepcopy(spec), hashlib.sha256(canonical.encode()).hexdigest()


def _rule_ids(rule):
    if rule['kind'] == 'stable_while_stalled':
        return list(dict.fromkeys([rule['valid'], rule['ready']] + rule['data']))
    return [rule['signal']]


class _Analysis:
    """按时刻聚合快照，流式统计；规则按消费者选择的时钟采样。"""

    def __init__(self, spec, start, end):
        self.spec, self.start, self.end = spec, start, end
        self.state = dict.fromkeys(spec['signals'])
        self.clock = spec.get('sample_clock')
        self.rules = spec.get('checks', [])
        self.results = [dict(name=r['name'], kind=r['kind'], evaluated=0, violations=0,
                             unknown=0, no_predecessor=0, inactive=0, evidence=[])
                        for r in self.rules]
        self.allowed = [set(r['values']) if r['kind'] == 'allowed_values' else
                        {tuple(p) for p in r['allowed_pairs']}
                        if r['kind'] == 'state_transitions' else None for r in self.rules]
        self.stats = {}
        self.tick = None
        self.before = None
        self.previous = None
        self.clock_changes = []
        self.clock_uncertain = False
        self.events = self.samples = self.clock_ambiguities = self.reset_samples = 0
        self.unknown_reset_samples = 0
        self.started = False
        self.evidence_bytes = 0

    def begin(self, signals, widths):
        if sum(widths[s] for s in self.state) > 16384:
            raise ValueError("分析所选信号总位宽不能超过 16384")
        single = [self.clock['id']] if self.clock else []
        if self.spec.get('reset'):
            single.append(self.spec['reset']['id'])
        for r in self.rules:
            if r['kind'] == 'stable_while_stalled':
                single += [r['valid'], r['ready']]
            else:
                values = r['values'] if r['kind'] == 'allowed_values' else [
                    v for pair in r['allowed_pairs'] for v in pair
                ]
                if any(len(v) != widths[r['signal']] for v in values):
                    raise ValueError("规则二进制值的长度必须等于信号位宽")
        if any(widths[s] != 1 for s in single):
            raise ValueError("时钟、复位、valid 和 ready 必须是一位信号")
        self.signals = [s for s in signals if s['id'] in self.state]
        self.widths = widths

    def _start(self):
        if self.started:
            return
        self.started = True
        for code, value in self.state.items():
            self.stats[code] = {
                'id': code, 'width': self.widths[code], 'initial_before_start': value,
                'assignments': 0, 'changes': 0, 'rising_edges': 0, 'falling_edges': 0,
                'unknown_assignments': 0, 'known_ticks': 0, 'unknown_ticks': 0,
                'first_change_tick': None, 'last_change_tick': None,
                '_last_tick': self.start, '_min': None, '_max': None,
            }

    def _hold(self, code, tick):
        stat, value = self.stats[code], self.state[code]
        duration = max(0, tick - stat['_last_tick'])
        stat['known_ticks' if _known(value) else 'unknown_ticks'] += duration
        if duration and _known(value):
            self._range(stat, value)
        stat['_last_tick'] = tick

    @staticmethod
    def _range(stat, value):
        number = int(value, 2)
        stat['_min'] = number if stat['_min'] is None else min(stat['_min'], number)
        stat['_max'] = number if stat['_max'] is None else max(stat['_max'], number)

    def event(self, tick, code, value):
        if tick < self.start:
            self.state[code] = value
            return
        self.events += 1
        if self.events > 200000:
            raise ValueError("分析窗口超过 200000 个所选事件，请缩小窗口或信号集合")
        self._start()
        if self.tick != tick:
            self._flush()
            self.tick, self.before = tick, self.state.copy()
            self.clock_changes, self.clock_uncertain = [], False
        old = self.state[code]
        stat = self.stats[code]
        self._hold(code, tick)
        stat['assignments'] += 1
        if old is not None and old != value:
            stat['changes'] += 1
            stat['first_change_tick'] = stat['first_change_tick'] or str(tick)
            stat['last_change_tick'] = str(tick)
        if old == '0' and value == '1':
            stat['rising_edges'] += 1
        elif old == '1' and value == '0':
            stat['falling_edges'] += 1
        if _known(value):
            self._range(stat, value)
        else:
            stat['unknown_assignments'] += 1
        if self.clock and code == self.clock['id'] and old != value:
            if old in ('0', '1') and value in ('0', '1'):
                # 同刻仅需区分 0/1/多次变化，不能把大文件缓存在列表里。
                if len(self.clock_changes) < 2:
                    self.clock_changes.append((old, value))
            elif old is not None or value not in ('0', '1'):
                self.clock_uncertain = True
        self.state[code] = value

    def _flush(self):
        if self.tick is None or not self.clock:
            return
        if self.clock_uncertain or len(self.clock_changes) > 1:
            self.clock_ambiguities += 1
            self.previous = None
            return
        edge = ('0', '1') if self.clock['edge'] == 'rising' else ('1', '0')
        if self.clock_changes != [edge]:
            return
        values = self.before if self.clock['values'] == 'before_tick' else self.state.copy()
        self.samples += 1
        reset = self.spec.get('reset')
        if reset and values[reset['id']] not in ('0', '1'):
            self.unknown_reset_samples += 1
            self.previous = None
            return
        if reset and values[reset['id']] == reset['active']:
            self.reset_samples += 1
            self.previous = None
            return
        for rule, result, allowed in zip(self.rules, self.results, self.allowed):
            verdict = self._check(rule, values, allowed)
            if verdict in ('unknown', 'no_predecessor', 'inactive'):
                result[verdict] += 1
                continue
            result['evaluated'] += 1
            if verdict:
                continue
            result['violations'] += 1
            if len(result['evidence']) < 8:
                ids = _rule_ids(rule)
                evidence = {
                    'tick': str(self.tick),
                    'values': {s: values[s] for s in ids},
                    'previous_tick': str(self.previous[0]) if self.previous else None,
                    'previous_values': {s: self.previous[1][s] for s in ids}
                    if self.previous else None,
                }
                size = len(json.dumps(evidence, ensure_ascii=False).encode())
                if self.evidence_bytes + size <= 262144:
                    result['evidence'].append(evidence)
                    self.evidence_bytes += size
        self.previous = self.tick, values.copy()

    def _check(self, rule, current, allowed):
        kind = rule['kind']
        if kind == 'allowed_values':
            value = current[rule['signal']]
            return value in allowed if _known(value) else 'unknown'
        if self.previous is None:
            return 'no_predecessor'
        previous = self.previous[1]
        if kind == 'state_transitions':
            pair = previous[rule['signal']], current[rule['signal']]
            return pair in allowed if all(_known(v) for v in pair) else 'unknown'
        valid, ready = previous[rule['valid']], previous[rule['ready']]
        if valid == '0' or (valid == '1' and ready == '1'):
            return 'inactive'
        if valid != '1' or ready != '0':
            return 'unknown'
        if current[rule['valid']] == '0':
            return False
        pairs = [(previous[s], current[s]) for s in rule['data']]
        if any(_known(a) and _known(b) and a != b for a, b in pairs):
            return False
        if current[rule['valid']] != '1' or any(
            not _known(a) or not _known(b) for a, b in pairs
        ):
            return 'unknown'
        return True

    def finish(self, last_tick):
        self._start()
        self._flush()
        self.effective_end = min(self.end, last_tick) if self.end is not None else last_tick
        self.covered = self.start <= last_tick and (self.end is None or self.end <= last_tick)
        for code, stat in self.stats.items():
            self._hold(code, max(self.start, self.effective_end))
            stat['final_value'] = self.state[code]
            if self.start <= last_tick and _known(self.state[code]):
                self._range(stat, self.state[code])
            stat['known_ticks'], stat['unknown_ticks'] = (
                str(stat['known_ticks']), str(stat['unknown_ticks']),
            )
            stat['min_unsigned'] = str(stat['_min']) if stat['_min'] is not None else None
            stat['max_unsigned'] = str(stat['_max']) if stat['_max'] is not None else None
            for key in ('_min', '_max', '_last_tick'):
                del stat[key]
        self.unknown_clock_ticks = (self.stats[self.clock['id']]['unknown_ticks']
                                    if self.clock else None)
        for rule, result in zip(self.rules, self.results):
            insufficient = (result['unknown'] or self.clock_ambiguities
                            or self.unknown_reset_samples or not self.covered
                            or self.unknown_clock_ticks not in (None, '0'))
            result['status'] = ('violated' if result['violations'] else 'inconclusive'
                                if insufficient or not result['evaluated'] else 'consistent')
            result['evidence_truncated'] = result['violations'] > len(result['evidence'])
            if rule['kind'] == 'stable_while_stalled':
                value = self.previous[1] if self.previous else {}
                result['pending_at_end'] = (
                    value.get(rule['valid']) == '1' and value.get(rule['ready']) == '0'
                ) if self.previous else None


def analyze_ila_capture(
    file_path: str, spec: dict, start_tick: str = '0', end_tick: str | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """只读分析：统计与声明规则的证据；结论仅适用于观察窗口和声明采样方式。"""
    spec, spec_sha = _validate(spec)
    # 时间校验仍由共享读取器完成，此处转换前仅保留有界的十进制字符串。
    for value in (start_tick, end_tick):
        if value is not None and (not isinstance(value, str)
                                  or not re.fullmatch('[0-9]{1,40}', value)):
            raise ValueError("时间必须是最多 40 位非负十进制字符串")
    if start_tick is None:
        raise ValueError("start_tick 不能为 null")
    analysis = _Analysis(spec, int(start_tick), int(end_tick) if end_tick is not None else None)
    data = _scan_ila_waveform(
        file_path, spec['signals'], start_tick, end_tick,
        expected_sha256=expected_sha256, visitor=analysis,
    )
    rules = analysis.results
    status = ('violated' if any(r['status'] == 'violated' for r in rules) else
              'inconclusive' if any(r['status'] == 'inconclusive' for r in rules) else
              'consistent' if rules else 'observed')
    suggestions = []
    for rule, result in zip(analysis.rules, rules):
        if result['status'] != 'consistent' or result.get('pending_at_end'):
            suggestions.append({
                'rule': rule['name'], 'signal_ids': _rule_ids(rule),
                'focus_tick': result['evidence'][0]['tick'] if result['evidence'] else None,
                'intent': ('review_semantics_and_capture_predecessor' if result['violations'] else
                           'capture_missing_context'),
                'executable': False,
            })
    return {
        'schema': 'otter.ila-analysis.v1', 'source': 'file', 'status': status,
        'file': data['file'], 'spec': spec, 'spec_sha256': spec_sha,
        'window': {'start_tick': start_tick, 'requested_end_tick': end_tick,
                   'effective_end_tick': str(analysis.effective_end)
                   if analysis.effective_end >= analysis.start else None,
                   'requested_window_covered': analysis.covered,
                   'selected_events': analysis.events},
        'timescale': data['timescale'], 'time_interpretation': 'vcd_ticks_only',
        'capture_completeness': 'unverified', 'hardware_pairing': 'unverified',
        'signals': analysis.signals, 'statistics': list(analysis.stats.values()),
        'sampling': {'edges': analysis.samples, 'ambiguous_clock_ticks': analysis.clock_ambiguities,
                     'unknown_clock_ticks': analysis.unknown_clock_ticks,
                     'reset_samples': analysis.reset_samples,
                     'unknown_reset_samples': analysis.unknown_reset_samples},
        'checks': rules, 'next_capture_suggestions': suggestions,
    }
