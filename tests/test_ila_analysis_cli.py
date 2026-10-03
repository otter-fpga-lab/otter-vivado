"""真实 MCP/CLI 使用同一分析器，观察/证据不足/违规分别返回。"""

import copy
import json
import subprocess
import sys

import pytest

from tests.analysis.test_ila_analysis import SPEC, write_vcd
from vivado_mcp.server import mcp


async def test_mcp_registration_and_read_only_analysis(tmp_path):
    path = write_vcd(tmp_path / 'sample.vcd')
    before = path.read_bytes()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert len(tools) == 47
    assert set(tools['analyze_ila_capture'].input_schema['required']) == {'file_path', 'spec'}
    result = await mcp.call_tool('analyze_ila_capture', {'file_path': str(path), 'spec': SPEC})
    assert not result.is_error
    report = json.loads(result.content[0].text)
    assert report['status'] == 'violated'
    assert report['checks'][2]['evidence'][0]['tick'] == '15'
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
    result = await mcp.call_tool('analyze_ila_capture', {
        'file_path': str(path), 'spec': {'signals': ['missing']},
    })
    assert json.loads(result.content[0].text)['status'] == 'blocked'


@pytest.mark.parametrize('mode,exit_code', [
    ('violated', 3), ('inconclusive', 2), ('consistent', 0), ('observed', 0), ('blocked', 1),
])
def test_cli_status_and_evidence_contract(tmp_path, mode, exit_code):
    path = write_vcd(tmp_path / 'sample.vcd')
    spec = copy.deepcopy(SPEC)
    if mode == 'observed':
        spec = {'signals': ['s']}
    elif mode == 'consistent':
        spec['checks'] = [spec['checks'][1]]
    elif mode == 'inconclusive':
        spec['sample_clock']['edge'] = 'falling'
        spec['checks'] = [spec['checks'][2]]
        path.write_text(path.read_text().replace('b1 v', 'b0 v'))
    elif mode == 'blocked':
        spec = {'signals': ['missing']}
    description = tmp_path / 'analysis.json'
    description.write_text(json.dumps(spec))
    result = subprocess.run([
        sys.executable, '-m', 'vivado_mcp', 'ila-analyze', '--file', str(path),
        '--spec', str(description),
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == exit_code, result.stderr or result.stdout
    assert json.loads(result.stdout)['status'] == mode


@pytest.mark.parametrize('content', ['{"signals":["c"],"signals":["d"]}', ' ' * 65537,
                                    '{bad', 'null'])
def test_cli_rejects_ambiguous_or_invalid_spec(tmp_path, content):
    description = tmp_path / 'bad.json'
    description.write_text(content)
    result = subprocess.run([
        sys.executable, '-m', 'vivado_mcp', 'ila-analyze', '--file', 'unused',
        '--spec', str(description),
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert json.loads(result.stdout)['status'] == 'blocked'
