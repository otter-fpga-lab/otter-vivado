"""真实 CLI 与 MCP 入口，全部使用合成工件。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.analysis.test_debug_artifacts import EXPECTED
from vivado_mcp.server import mcp

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("mode,code", [("consistent", 0), ("incomplete", 2), ("blocked", 1)])
def test_cli_exit_codes_and_read_only(tmp_path, mode, code):
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(EXPECTED))
    command = [sys.executable, "-m", "vivado_mcp", "debug-artifacts", "--bit",
               str(FIXTURES / ("absent.bit" if mode == "blocked" else "sample_header.bit")),
               "--ltx", str(FIXTURES / "sample_probes.ltx")]
    if mode != "incomplete":
        command += ["--expected", str(expected)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == code, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == mode
    assert data["pairing"] == "unverified"
    assert list(tmp_path.iterdir()) == [expected]


async def test_real_mcp_registration_and_offline_call():
    registered = {tool.name: tool for tool in await mcp.list_tools()}
    assert len(registered) == 42
    schema = registered["check_debug_artifacts"].input_schema
    assert set(schema["required"]) == {"bit_path", "ltx_path"}
    result = await mcp.call_tool("check_debug_artifacts", {
        "bit_path": str(FIXTURES / "sample_header.bit"),
        "ltx_path": str(FIXTURES / "sample_probes.ltx"),
        "expected": EXPECTED,
    })
    assert not result.is_error
    data = json.loads(result.content[0].text)
    assert data["status"] == "consistent"
    assert data["pairing"] == "unverified"


async def test_mcp_invalid_expectations_are_structured():
    result = await mcp.call_tool("check_debug_artifacts", {
        "bit_path": "unused", "ltx_path": "unused", "expected": {"unexpected": True},
    })
    data = json.loads(result.content[0].text)
    assert data["status"] == "blocked"
    assert "error" in data
