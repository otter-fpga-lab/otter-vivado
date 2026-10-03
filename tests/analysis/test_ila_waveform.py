"""数字 VCD 的无损分页、未知值和坏数据边界。全部输入为合成样本。"""

import hashlib
import json
import os
import subprocess
import sys

import pytest

from vivado_mcp.analysis.ila_waveform import read_ila_waveform

VCD = '''$date synthetic test $end
$timescale 1 ns $end
$scope module dut $end
$var wire 1 ! clk $end
$var wire 80 bus data [79:0] $end
$var wire 80 bus alias [79:0] $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
bx bus
$end
#5
1!
b1 bus
#5
0!
#9007199254740993
bz bus
'''


@pytest.fixture
def vcd(tmp_path):
    path = tmp_path / '波形 test.vcd'
    path.write_text(VCD)
    return path


def test_wide_four_state_aliases_and_large_ticks(vcd):
    result = read_ila_waveform(str(vcd))
    assert result['timescale'] == {'magnitude': 1, 'unit': 'ns'}
    assert result['events'][1]['value'] == 'x' * 80
    assert result['events'][3]['value'] == '0' * 79 + '1'
    assert result['events'][-1] == {'tick': '9007199254740993', 'id': 'bus', 'value': 'z' * 80}
    assert len(result['signals']) == 3
    assert result['trigger_tick'] is None
    assert result['sample_period_seconds'] is None
    assert result['capture_completeness'] == 'unverified'
    assert result['file']['sha256'] == hashlib.sha256(vcd.read_bytes()).hexdigest()


def test_pagination_keeps_same_tick_order_and_initial_value(vcd):
    full = read_ila_waveform(str(vcd), ['!', 'bus'], '5', '5')
    assert full['initial_values_before_start'] == {'!': '0', 'bus': 'x' * 80}
    collected, offset = [], 0
    while offset is not None:
        page = read_ila_waveform(str(vcd), ['!', 'bus'], '5', '5', offset, 1,
                                 full['file']['sha256'])
        collected.extend(page['events'])
        offset = page['next_offset']
    assert collected == full['events']
    assert len(collected) == 3
    assert read_ila_waveform(str(vcd), [], limit=1)['events'] == []
    assert read_ila_waveform(str(vcd), ['!'], '6')['initial_values_before_start']['!'] == '0'


def test_changed_file_cannot_join_page(vcd):
    first = read_ila_waveform(str(vcd), limit=1)
    vcd.write_text(VCD.replace('1!', '0!'))
    with pytest.raises(ValueError, match='指纹'):
        read_ila_waveform(str(vcd), offset=1, expected_sha256=first['file']['sha256'])


@pytest.mark.parametrize('replace,with_value', [
    ('$timescale 1 ns', '$timescale 2 ns'),
    ('$var wire 1 !', '$var real 1 !'),
    ('$var wire 80 bus alias', '$var wire 79 bus alias'),
    ('#5\n0!', '#4\n0!'),
    ('1!', '1unknown'),
    ('b1 bus', 'b102 bus'),
    ('0!', 'b10 !'),
    ('$enddefinitions $end', '$enddefinitions'),
    ('$upscope $end', ''),
    ('#5\n1!', '$dumpoff $end\n#5\n1!'),
    ('#5\n1!', 'r1.5 bus\n#5\n1!'),
    ('#5\n1!', '$dumpvars\n#5\n1!'),
])
def test_malformed_or_unsupported_is_blocked(vcd, replace, with_value):
    vcd.write_text(VCD.replace(replace, with_value))
    with pytest.raises(ValueError):
        read_ila_waveform(str(vcd))


@pytest.mark.parametrize('kwargs', [
    {'signal_ids': ['missing']}, {'signal_ids': ['!', '!']}, {'signal_ids': 'bus'},
    {'start_tick': None}, {'start_tick': '-1'}, {'start_tick': 5},
    {'end_tick': '4', 'start_tick': '5'}, {'offset': True}, {'limit': 10001},
    {'expected_sha256': 'oops'},
])
def test_request_validation(vcd, kwargs):
    with pytest.raises(ValueError):
        read_ila_waveform(str(vcd), **kwargs)


def test_response_budget_and_full_validation_after_page(tmp_path):
    path = tmp_path / 'wide.vcd'
    header = '$var wire 4096 ! wide $end\n$enddefinitions $end\n'
    path.write_text(header + 'bx !\n' * 100)
    page = read_ila_waveform(str(path), limit=10000)
    assert page['truncated'] and page['next_offset'] < 100
    path.write_text(path.read_text() + 'b2 !\n')
    with pytest.raises(ValueError):
        read_ila_waveform(str(path), limit=1)


def test_size_symlink_encoding_and_read_change(vcd, tmp_path, monkeypatch):
    link = tmp_path / 'link.vcd'
    link.symlink_to(vcd)
    with pytest.raises(ValueError, match='符号链接'):
        read_ila_waveform(str(link))
    vcd.write_bytes(b'\xff')
    with pytest.raises(ValueError):
        read_ila_waveform(str(vcd))
    with vcd.open('wb') as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match='32 MiB'):
        read_ila_waveform(str(vcd))
    vcd.write_text(VCD)
    original = os.fstat
    calls = 0

    def changing(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            vcd.write_text(VCD + '\n')
        return original(fd)

    monkeypatch.setattr(os, 'fstat', changing)
    with pytest.raises(ValueError, match='变化'):
        read_ila_waveform(str(vcd))


def test_cli_catalog_and_error_json(vcd):
    command = [sys.executable, '-m', 'vivado_mcp', 'ila-waveform', '--file', str(vcd)]
    result = subprocess.run(command + ['--catalog'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['events'] == []
    result = subprocess.run(command + ['--signal', 'missing'], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)['status'] == 'blocked'


def test_expanded_catalog_and_selected_initial_values_are_bounded(tmp_path):
    path = tmp_path / 'bounded.vcd'
    declarations = '\n'.join(f'$var wire 4096 s{i} s{i} $end' for i in range(65))
    path.write_text(declarations + '\n$enddefinitions $end\n')
    with pytest.raises(ValueError, match='总位宽'):
        read_ila_waveform(str(path))
    assert len(read_ila_waveform(str(path), [])['signals']) == 65
    path.write_text('$scope module ' + 'a' * 8000 + ' $end\n' + declarations
                    + '\n$upscope $end\n$enddefinitions $end\n')
    with pytest.raises(ValueError, match='目录展开'):
        read_ila_waveform(str(path), [])
