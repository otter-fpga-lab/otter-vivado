"""真实 Tcl 解释器执行导出模板；Vivado 命令为明确测试桩。"""

import asyncio
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.analysis.test_debug_artifacts import EXPECTED
from vivado_mcp.analysis.debug_artifacts import check_debug_artifacts
from vivado_mcp.debug_bundle import export_debug_bundle, verify_debug_bundle
from vivado_mcp.vivado.tcl_utils import TclResult, tcl_quote, wrap_command

FIXTURES = Path(__file__).parent / "fixtures"
PART = "xc7k325tffg900-2"
MOCK = r'''
set project ""
set design ""
set fail ""
set design_part xc7k325tffg900-2
proc current_project {args} {return $::project}
proc current_design {args} {return $::design}
proc get_property {property object} {
    if {$property eq "PART"} {return $::design_part}
    if {$property eq "NAME"} {return $::design}
    error "Unsupported property"
}
proc version {args} {return "2022.2-test-double"}
proc open_checkpoint {path} {
    puts "MOCK_CALL:open_checkpoint"
    if {![file isfile $path]} {error "Missing checkpoint"}
    if {$::fail eq "open_checkpoint"} {error "Open failed"}
    set ::design sample_top
    set ::project in_memory
}
proc write_bitstream {path} {
    puts "MOCK_CALL:write_bitstream"
    if {$::fail eq "write_bitstream"} {error "Bit generation failed"}
    if {$::fail eq "no_bit"} {return}
    file copy $::sample_bit $path
    if {$::fail eq "design_changed"} {set ::design another_design}
}
proc write_debug_probes {path} {
    puts "MOCK_CALL:write_debug_probes"
    if {$::fail eq "write_debug_probes"} {error "LTX generation failed"}
    file copy $::sample_ltx $path
}
foreach command {report_timing_summary report_utilization report_drc} {
    proc $command {option path} {
        puts "MOCK_CALL:[lindex [info level 0] 0]"
        if {$option ne "-file"} {error "Unexpected options"}
        if {$::fail eq "reports"} {error "Report failed"}
        set stream [open $path {WRONLY CREAT EXCL}]
        puts $stream "SYNTHETIC REPORT: not real EDA evidence"
        close $stream
    }
}
'''


class TclSession:
    session_id = "dedicated-export"

    def __init__(self, setup="", *, stdio=False):
        self.setup = setup
        self.stdio = stdio
        self.calls = []

    async def execute(self, command, timeout):
        tclsh = shutil.which("tclsh")
        if not tclsh:
            pytest.skip("需要真实 Tcl 解释器")
        setup = (
            f"set sample_bit {tcl_quote(str(FIXTURES / 'sample_header.bit'))}\n"
            f"set sample_ltx {tcl_quote(str(FIXTURES / 'sample_probes.ltx'))}\n"
        )
        if self.stdio:
            command = wrap_command(command, "VMCP_TEST_END")
        result = subprocess.run(
            [tclsh], input=MOCK + setup + self.setup + "\n" + command,
            capture_output=True, text=True, timeout=timeout,
        )
        assert not result.stderr, result.stderr
        self.calls.append(result.stdout)
        return TclResult(result.stdout, result.returncode, result.returncode != 0)


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "路由 $ [source] with spaces.dcp"
    source.write_bytes(b"SYNTHETIC CHECKPOINT FOR TCL TESTS")
    return source, tmp_path / "bundle $ [output] 中文"


async def export(inputs, session=None, **kwargs):
    return await export_debug_bundle(
        session or TclSession(), str(inputs[0]), str(inputs[1]), PART, **kwargs,
    )


@pytest.mark.parametrize("stdio", [False, True])
async def test_export_same_snapshot_and_persist_provenance(inputs, stdio):
    session = TclSession(stdio=stdio)
    result = await export(inputs, session, source_revision="declared-commit")
    assert result["status"] == "exported", result
    assert result["pairing"] == "unverified"
    assert result["timing_signoff"] == "unverified"
    assert result["declared_source_revision"] == "declared-commit"
    assert len(session.calls) == 1
    calls = [line for line in session.calls[0].splitlines() if line.startswith("MOCK_CALL:")]
    assert calls == [f"MOCK_CALL:{c}" for c in (
        "open_checkpoint", "write_bitstream", "write_debug_probes",
        "report_timing_summary", "report_utilization", "report_drc",
    )]
    destination = inputs[1]
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["artifacts"] == result["artifacts"]
    assert (destination / "source.dcp").read_bytes() == inputs[0].read_bytes()
    checked = check_debug_artifacts(
        str(destination / "design.bit"), str(destination / "design.ltx"),
        EXPECTED, str(destination / "manifest.json"),
    )
    assert checked["status"] == "consistent", checked
    assert checked["bundle"]["status"] == "record_matches"
    assert checked["pairing"] == "unverified"
    assert not checked["hardware_verified"]


@pytest.mark.parametrize("setup", ['set project existing', 'set design existing'])
async def test_existing_gui_is_not_replaced(inputs, setup):
    session = TclSession(setup)
    result = await export(inputs, session)
    assert result["status"] == "blocked"
    assert "MOCK_CALL:" not in session.calls[0]
    assert not (inputs[1] / "manifest.json").exists()


@pytest.mark.parametrize("stage", [
    "open_checkpoint", "write_bitstream", "no_bit",
    "write_debug_probes", "reports", "design_changed",
])
async def test_partial_failures_preserve_files_without_manifest(inputs, stage):
    session = TclSession(f"set fail {stage}")
    result = await export(inputs, session)
    assert result["status"] == "partial", result
    assert (inputs[1] / "attempt.json").exists()
    assert (inputs[1] / "source.dcp").exists()
    assert not (inputs[1] / "manifest.json").exists()
    assert json.loads((inputs[1] / "result.json").read_text())["status"] == "partial"
    if stage in {"write_debug_probes", "reports", "design_changed"}:
        assert (inputs[1] / "design.bit").exists()


async def test_part_mismatch_blocks_bit_generation(inputs):
    session = TclSession("set design_part xc7a35tcpg236-1")
    result = await export(inputs, session)
    assert result["status"] == "partial"
    assert "MOCK_CALL:write_bitstream" not in session.calls[0]


@pytest.mark.parametrize("kind", ["timeout", "garbled", "error", "wrong_attempt"])
async def test_unknown_response_never_replays(inputs, kind):
    class Session:
        calls = 0

        async def execute(self, command, timeout):
            self.calls += 1
            if kind == "timeout":
                raise TimeoutError("timeout")
            if kind == "error":
                return TclResult("failure", 1, True)
            if kind == "wrong_attempt":
                return TclResult("VMCP_BUNDLE_FIELD:attempt_id|3030\nVMCP_BUNDLE_DONE:1", 0, False)
            return TclResult("truncated", 0, False)

    session = Session()
    result = await export(inputs, session)
    assert result["status"] == "unknown"
    assert session.calls == 1
    assert not (inputs[1] / "manifest.json").exists()


async def test_cancelled_request_records_unknown(inputs):
    class Session:
        async def execute(self, command, timeout):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await export(inputs, Session())
    assert json.loads((inputs[1] / "result.json").read_text())["status"] == "unknown"


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling"])
async def test_never_overwrite_existing_destination(inputs, kind):
    path = inputs[1]
    if kind == "directory":
        path.mkdir()
    elif kind == "file":
        path.write_text("preserve")
    elif kind == "symlink":
        path.symlink_to(inputs[0])
    else:
        path.symlink_to(path.parent / "absent")
    session = TclSession()
    with pytest.raises(FileExistsError):
        await export(inputs, session)
    assert not session.calls


@pytest.mark.parametrize("filename", ["source.dcp", "design.bit", "design.ltx", "timing.rpt"])
async def test_manifest_detects_tampering(inputs, filename):
    result = await export(inputs)
    path = inputs[1] / filename
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="指纹不符"):
        verify_debug_bundle(
            result["manifest_path"], result["artifacts"]["bit"]["sha256"],
            result["artifacts"]["ltx"]["sha256"],
        )


async def test_manifest_cannot_request_arbitrary_file_read(inputs):
    result = await export(inputs)
    manifest_path = Path(result["manifest_path"])
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"]["checkpoint"]["filename"] = "../elsewhere"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="产物字段无效"):
        verify_debug_bundle(result["manifest_path"], "unused", "unused")


async def test_manifest_rejects_different_bit_ltx_pair(inputs):
    result = await export(inputs)
    with pytest.raises(ValueError, match="不属于"):
        verify_debug_bundle(result["manifest_path"], "another-sha", "another-sha")


@pytest.mark.parametrize("field,value", [("expected_part", ""), ("timeout_seconds", 0),
                                        ("source_revision", {})])
async def test_validate_before_creating_directory(inputs, field, value):
    args = dict(checkpoint_path=str(inputs[0]), output_dir=str(inputs[1]), expected_part=PART)
    args[field] = copy.deepcopy(value)
    with pytest.raises(ValueError):
        await export_debug_bundle(TclSession(), **args)
    assert not inputs[1].exists()


async def test_real_mcp_export_contract(inputs):
    from tests.test_debug_design_tools import call, context
    from vivado_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert len(tools) == 43
    assert set(tools["export_debug_bundle"].input_schema["required"]) == {
        "checkpoint_path", "output_dir", "expected_part",
    }
    result = await call("export_debug_bundle", {
        "checkpoint_path": str(inputs[0]), "output_dir": str(inputs[1]),
        "expected_part": PART, "session_id": "export",
    }, context(TclSession(), "export"))
    assert result["status"] == "exported"
    assert result["pairing"] == "unverified"


async def test_cli_attach_only_and_detach_on_completion(inputs, monkeypatch, capsys):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from vivado_mcp import debug_bundle_cli

    session = TclSession()
    session.start = AsyncMock()
    session.stop = AsyncMock()
    factory = Mock(return_value=session)
    monkeypatch.setattr(debug_bundle_cli, "GuiSession", factory)
    code = await debug_bundle_cli.run_debug_export_cli(SimpleNamespace(
        checkpoint=str(inputs[0]), output_dir=str(inputs[1]), part=PART,
        port=10003, source_revision=None, timeout=20,
    ))
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "exported"
    assert factory.call_args.kwargs["attach_only"] is True
    assert factory.call_args.kwargs["port"] == 10003
    session.stop.assert_awaited_once()


async def test_offline_manifest_cli_and_relocated_bundle(inputs):
    import sys

    result = await export(inputs)
    relocated = inputs[1].parent / "relocated"
    shutil.copytree(inputs[1], relocated)
    completed = subprocess.run([
        sys.executable, "-m", "vivado_mcp", "debug-artifacts",
        "--bit", str(relocated / "design.bit"), "--ltx", str(relocated / "design.ltx"),
        "--expected", str(Path(__file__).parents[1] / "examples/debug/artifacts-expected.json"),
        "--manifest", str(relocated / "manifest.json"),
    ], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    checked = json.loads(completed.stdout)
    assert checked["bundle"]["status"] == "record_matches"
    assert checked["bundle"]["attempt_id"] == result["attempt_id"]


async def test_new_target_conflict_is_detected_before_opening_checkpoint(inputs):
    bit = inputs[1] / "design.bit"
    setup = f'set stream [open {tcl_quote(str(bit))} w]; puts $stream KEEP; close $stream'
    session = TclSession(setup)
    result = await export(inputs, session)
    assert result["status"] == "blocked"
    assert "MOCK_CALL:" not in session.calls[0]
    assert bit.read_text().strip() == "KEEP"


async def test_manifest_missing_report_blocks_offline_check(inputs):
    result = await export(inputs)
    (inputs[1] / "drc.rpt").unlink()
    checked = check_debug_artifacts(
        str(inputs[1] / "design.bit"), str(inputs[1] / "design.ltx"),
        EXPECTED, result["manifest_path"],
    )
    assert checked["status"] == "blocked"
    assert checked["bundle"]["status"] == "blocked"


async def test_receipt_write_failure_does_not_hide_success(inputs, monkeypatch):
    import vivado_mcp.debug_bundle as module

    original = module._save_new

    def save(path, value):
        if path.name == "result.json":
            raise OSError("disk write failure")
        return original(path, value)

    monkeypatch.setattr(module, "_save_new", save)
    result = await export(inputs)
    assert result["status"] == "exported"
    assert "disk write failure" in result["receipt_error"]
    assert (inputs[1] / "manifest.json").exists()


async def test_bad_bit_payload_never_produces_success_manifest(inputs, tmp_path):
    bit = tmp_path / "truncated.bit"
    bit.write_bytes((FIXTURES / "sample_header.bit").read_bytes()[:-1])
    result = await export(inputs, TclSession(f"set sample_bit {tcl_quote(str(bit))}"))
    assert result["status"] == "partial"
    assert "离线核对失败" in result["error"]
    assert not (inputs[1] / "manifest.json").exists()
