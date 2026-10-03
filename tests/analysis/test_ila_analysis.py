"""合成 VCD 的采样证据与不确定性边界；不宣称板卡或协议认证。"""

import copy
import hashlib

import pytest

from vivado_mcp.analysis.ila_analysis import analyze_ila_capture

WIDTHS = {'c': 1, 'v': 1, 'r': 1, 'd': 80, 's': 2, 'reset': 1}
SPEC = {
    'signals': list(WIDTHS),
    'sample_clock': {'id': 'c', 'edge': 'rising', 'values': 'after_tick'},
    'reset': {'id': 'reset', 'active': '1'},
    'checks': [
        {'name': 'states', 'kind': 'allowed_values', 'signal': 's', 'values': ['00', '01']},
        {'name': 'path', 'kind': 'state_transitions', 'signal': 's',
         'allowed_pairs': [['01', '10']]},
        {'name': 'hold', 'kind': 'stable_while_stalled', 'valid': 'v', 'ready': 'r', 'data': ['d']},
    ],
}


def write_vcd(path, events=None, *, widths=None):
    widths = WIDTHS if widths is None else widths
    events = [(0, {'c': '0', 'v': '0', 'r': '0', 'd': '1', 's': '00', 'reset': '0'}),
              (5, {'c': '1', 'v': '1', 'd': '1', 's': '01'}),
              (10, {'c': '0'}),
              (15, {'c': '1', 'r': '1', 'd': '10', 's': '10'}),
              (20, {'c': '0'})] if events is None else events
    text = '$timescale 1 ns $end\n' + '\n'.join(
        f'$var wire {width} {code} signal_{code} $end' for code, width in widths.items()
    ) + '\n$enddefinitions $end\n'
    for tick, values in events:
        text += f'#{tick}\n'
        for code, value in values.items():
            text += f'b{value} {code}\n'
    path.write_text(text)
    return path


@pytest.fixture
def capture(tmp_path):
    return write_vcd(tmp_path / '合成 capture.vcd')


def test_precise_evidence_and_declarative_fsm_handshake(capture):
    result = analyze_ila_capture(str(capture), SPEC)
    assert result['status'] == 'violated'
    assert result['file']['sha256'] == hashlib.sha256(capture.read_bytes()).hexdigest()
    assert [r['status'] for r in result['checks']] == ['violated', 'consistent', 'violated']
    evidence = result['checks'][2]['evidence'][0]
    assert evidence['tick'] == '15' and evidence['previous_tick'] == '5'
    assert evidence['values']['d'] == '0' * 78 + '10'
    assert evidence['previous_values']['d'] == '0' * 79 + '1'
    assert result['sampling']['edges'] == 2
    assert result['hardware_pairing'] == result['capture_completeness'] == 'unverified'
    assert all(s['executable'] is False for s in result['next_capture_suggestions'])
    assert result['next_capture_suggestions'][0]['focus_tick'] == '15'
    assert SPEC['checks'][0]['values'] == ['00', '01']


def test_statistics_exact_ticks_no_clock_and_wide_unsigned(tmp_path):
    big = '1' * 80
    path = write_vcd(tmp_path / 'stats.vcd', [
        (0, {'b': 'x'}), (5, {'b': '0'}), (9, {'b': big}), (12, {'b': 'z'}), (15, {}),
    ], widths={'b': 80})
    result = analyze_ila_capture(str(path), {'signals': ['b']}, '2', '14')
    stat = result['statistics'][0]
    assert result['status'] == 'observed'
    assert stat['known_ticks'] == '7' and stat['unknown_ticks'] == '5'
    assert stat['min_unsigned'] == '0' and stat['max_unsigned'] == str(2 ** 80 - 1)
    assert stat['changes'] == 3 and stat['unknown_assignments'] == 1
    assert stat['initial_before_start'] == 'x' * 80
    assert stat['first_change_tick'] == '5'


def test_before_vs_after_tick_is_explicit_not_file_order(capture):
    spec = copy.deepcopy(SPEC)
    spec['checks'] = [spec['checks'][0]]
    after = analyze_ila_capture(str(capture), spec)
    spec['sample_clock']['values'] = 'before_tick'
    before = analyze_ila_capture(str(capture), spec)
    assert before['status'] == 'consistent'
    assert after['status'] == 'violated'
    assert before['spec_sha256'] != after['spec_sha256']
    lines = capture.read_text().splitlines()
    # 将上升沿赋值移到同 tick 的其它值之后，不改变 after_tick 结果。
    pos = lines.index('b1 c')
    clock_line = lines.pop(pos)
    lines.insert(lines.index('#10'), clock_line)
    capture.write_text('\n'.join(lines) + '\n')
    spec['sample_clock']['values'] = 'after_tick'
    assert analyze_ila_capture(str(capture), spec)['checks'] == after['checks']


def test_unknown_values_never_become_zero_or_bridge_state(tmp_path):
    path = write_vcd(tmp_path / 'x.vcd', [
        (0, {'c': '0', 's': '00'}), (5, {'c': '1', 's': '00'}), (6, {'c': '0'}),
        (10, {'c': '1', 's': 'x'}), (11, {'c': '0'}), (15, {'c': '1', 's': '10'}),
    ], widths={'c': 1, 's': 2})
    spec = {'signals': ['c', 's'], 'sample_clock': SPEC['sample_clock'], 'checks': [
        {'name': 'fsm', 'kind': 'state_transitions', 'signal': 's',
         'allowed_pairs': [['00', '10']]},
    ]}
    result = analyze_ila_capture(str(path), spec)
    assert result['status'] == 'inconclusive'
    assert result['checks'][0]['evaluated'] == 0
    assert result['checks'][0]['unknown'] == 2


@pytest.mark.parametrize('clock_values', [['1', '0', '1'], ['x', '1']])
def test_clock_ambiguity_skips_tick_and_breaks_history(tmp_path, clock_values):
    path = write_vcd(tmp_path / 'ambiguous.vcd')
    insertion = '#12\n' + '\n'.join(f'b{v} c' for v in clock_values) + '\n'
    path.write_text(path.read_text().replace('#15\n', insertion + '#15\n'))
    result = analyze_ila_capture(str(path), SPEC)
    assert result['sampling']['ambiguous_clock_ticks'] == 1
    assert result['checks'][1]['evaluated'] == 0
    assert result['checks'][1]['status'] == 'inconclusive'


@pytest.mark.parametrize('reset_value', ['1', 'x'])
def test_reset_clears_pending_obligation_and_unknown_reset_is_visible(tmp_path, reset_value):
    path = write_vcd(tmp_path / 'reset.vcd')
    path.write_text(path.read_text().replace('#15\n', f'#15\nb{reset_value} reset\n'))
    result = analyze_ila_capture(str(path), SPEC)
    assert result['checks'][2]['violations'] == 0
    assert result['checks'][2]['pending_at_end'] is None
    assert result['sampling']['reset_samples' if reset_value == '1'
                              else 'unknown_reset_samples'] == 1


def test_no_clock_no_applicable_obligation_and_outside_window_are_inconclusive(capture):
    spec = copy.deepcopy(SPEC)
    for start, end in [('0', '4'), ('21', None), ('0', '25')]:
        result = analyze_ila_capture(str(capture), spec, start, end)
        assert result['checks'][1]['status'] == 'inconclusive'
    spec['checks'] = [spec['checks'][2]]
    capture.write_text(capture.read_text().replace('b1 v', 'b0 v'))
    result = analyze_ila_capture(str(capture), spec)
    assert result['status'] == 'inconclusive'
    assert result['checks'][0]['inactive'] == 1


def test_pending_stall_at_end_is_not_liveness_pass(capture):
    spec = copy.deepcopy(SPEC)
    spec['checks'] = [spec['checks'][2]]
    capture.write_text(capture.read_text().replace('b1 r', 'b0 r').replace('b10 d', 'b1 d'))
    result = analyze_ila_capture(str(capture), spec)
    assert result['status'] == 'consistent'
    assert result['checks'][0]['pending_at_end'] is True
    assert result['next_capture_suggestions'][0]['intent'] == 'capture_missing_context'


def test_large_tick_and_window_initial_state_are_exact(tmp_path):
    base = 2 ** 53 + 1
    path = write_vcd(tmp_path / 'large.vcd', [
        (0, {'c': '0', 's': '00'}), (base, {'c': '1'}), (base + 1, {'c': '0'}),
        (base + 2, {'c': '1', 's': '01'}),
    ], widths={'c': 1, 's': 2})
    spec = {'signals': ['c', 's'], 'sample_clock': SPEC['sample_clock'], 'checks': [
        {'name': 'zero', 'kind': 'allowed_values', 'signal': 's', 'values': ['00']},
    ]}
    result = analyze_ila_capture(str(path), spec, str(base))
    assert result['checks'][0]['evidence'][0]['tick'] == str(base + 2)
    assert result['sampling']['edges'] == 2
    assert result['statistics'][0]['known_ticks'] == '2'


@pytest.mark.parametrize('spec', [
    {}, {'signals': []}, {'signals': ['c', 'c']}, {'signals': ['c'], 'extra': True},
    {'signals': ['c'], 'sample_clock': None}, {'signals': ['c'], 'checks': [None]},
    {'signals': ['c'], 'checks': [{'kind': 'arbitrary_python'}]},
    {'signals': ['c'], 'reset': {'id': 'c', 'active': 1}},
    {'signals': ['c'], 'sample_clock': {'id': 'c', 'edge': 'rising'}},
    {'signals': ['c'], 'sample_clock': {'id': 'missing', 'edge': 'rising', 'values': 'after_tick'}},
])
def test_invalid_description_rejected(capture, spec):
    with pytest.raises(ValueError):
        analyze_ila_capture(str(capture), spec)


@pytest.mark.parametrize('edit', [
    lambda s: s['checks'][0].update(values=['1']),
    lambda s: s['checks'][0].update(values=['xx']),
    lambda s: s['checks'][1].update(allowed_pairs=['00']),
    lambda s: s['sample_clock'].update(id='s'),
    lambda s: s['checks'][2].update(ready='d'),
    lambda s: s['checks'][2].update(data=[]),
    lambda s: s['checks'][1].update(name='states'),
    lambda s: s.update(checks=s['checks'] * 6),
])
def test_invalid_rule_semantics_rejected(capture, edit):
    spec = copy.deepcopy(SPEC)
    edit(spec)
    with pytest.raises(ValueError):
        analyze_ila_capture(str(capture), spec)


def test_outside_window_bad_data_and_fingerprint_mismatch_block(capture):
    sha = hashlib.sha256(capture.read_bytes()).hexdigest()
    capture.write_text(capture.read_text() + '#100\nb2 s\n')
    with pytest.raises(ValueError, match='指纹'):
        analyze_ila_capture(str(capture), SPEC, end_tick='4', expected_sha256=sha)
    with pytest.raises(ValueError, match='值无效'):
        analyze_ila_capture(str(capture), SPEC, end_tick='4')


def test_evidence_limit_does_not_hide_violation_count(tmp_path):
    events = [(0, {'c': '0', 's': '01'})]
    for i in range(1, 41):
        events.append((i, {'c': str(i % 2)}))
    path = write_vcd(tmp_path / 'many.vcd', events, widths={'c': 1, 's': 2})
    spec = {'signals': ['c', 's'], 'sample_clock': SPEC['sample_clock'], 'checks': [
        {'name': 'zero', 'kind': 'allowed_values', 'signal': 's', 'values': ['00']},
    ]}
    result = analyze_ila_capture(str(path), spec)
    assert result['checks'][0]['violations'] == 20
    assert len(result['checks'][0]['evidence']) == 8
    assert result['checks'][0]['evidence_truncated']


def test_large_event_window_blocks_without_partial_success(tmp_path):
    path = write_vcd(tmp_path / 'budget.vcd', [], widths={'c': 1})
    with path.open('a') as stream:
        stream.write('b0 c\n' * 200001)
    with pytest.raises(ValueError, match='200000'):
        analyze_ila_capture(str(path), {'signals': ['c']})


def test_unknown_clock_prefix_prevents_complete_verdict(tmp_path):
    path = write_vcd(tmp_path / 'late-clock.vcd', [
        (5, {'c': '0', 's': '00'}), (10, {'c': '1'}),
    ], widths={'c': 1, 's': 2})
    spec = {'signals': ['c', 's'], 'sample_clock': SPEC['sample_clock'], 'checks': [
        {'name': 'zero', 'kind': 'allowed_values', 'signal': 's', 'values': ['00']},
    ]}
    result = analyze_ila_capture(str(path), spec)
    assert result['checks'][0]['evaluated'] == 1
    assert result['status'] == 'inconclusive'
    assert result['sampling']['unknown_clock_ticks'] == '5'


@pytest.mark.parametrize('valid,data,status', [
    ('0', 'x', 'violated'), ('x', '1', 'inconclusive'), ('1', 'x', 'inconclusive'),
    ('1', '1', 'consistent'),
])
def test_stall_release_does_not_erase_obligation(capture, valid, data, status):
    spec = copy.deepcopy(SPEC)
    spec['checks'] = [spec['checks'][2]]
    capture.write_text(capture.read_text().replace('#15\n', f'#15\nb{valid} v\n')
                      .replace('b10 d', f'b{data} d'))
    result = analyze_ila_capture(str(capture), spec)
    assert result['checks'][0]['status'] == status


def test_state_self_transition_requires_explicit_pair(capture):
    spec = copy.deepcopy(SPEC)
    spec['checks'] = [spec['checks'][1]]
    capture.write_text(capture.read_text().replace('b10 s', 'b01 s'))
    assert analyze_ila_capture(str(capture), spec)['status'] == 'violated'
    spec['checks'][0]['allowed_pairs'] = [['01', '01']]
    assert analyze_ila_capture(str(capture), spec)['status'] == 'consistent'
