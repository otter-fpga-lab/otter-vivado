"""用真实 Tcl 解释器检查 IP 准备协议与副作用；不代替真实 Vivado 验收。"""

from __future__ import annotations

import copy
import shutil
import subprocess

import pytest

from vivado_mcp.debug_ip import (
    DebugIPError,
    DebugIPPreparation,
    plan_debug_ip,
    validate_debug_ip_spec,
)
from vivado_mcp.vivado.tcl_utils import TclResult, tcl_quote

TCLSH = shutil.which("tclsh")
ILA = {
    "kind": "ila_ip",
    "name": "ila_pixels",
    "clock": "pixel_clk",
    "depth": 4096,
    "probes": [{"name": "pixel_data", "width": 24}, {"name": "frame_valid", "width": 1}],
}
VIO = {
    "kind": "vio_ip",
    "name": "vio_controls",
    "clock": "pixel_clk",
    "inputs": [{"name": "frame_count", "width": 16}],
    "outputs": [{"name": "gain", "width": 12, "initial": "0x100"}, {"name": "bypass", "width": 1}],
}
PROJECT = {"name": "isp", "directory": "/workspace/isp", "part": "xc7a35tcpg236-1"}


def test_offline_plan_is_independent_of_vivado_and_does_not_change_spec():
    original = copy.deepcopy(VIO)
    plan = plan_debug_ip(VIO)
    assert VIO == original
    assert plan["spec"]["outputs"][0]["initial"] == "0x100"
    assert plan["spec"]["outputs"][1]["initial"] == "0x0"
    assert plan["configuration"] == {
        "CONFIG.C_NUM_PROBE_IN": 1,
        "CONFIG.C_NUM_PROBE_OUT": 2,
        "CONFIG.C_PROBE_IN0_WIDTH": 16,
        "CONFIG.C_PROBE_OUT0_WIDTH": 12,
        "CONFIG.C_PROBE_OUT0_INIT_VAL": "0x100",
        "CONFIG.C_PROBE_OUT1_WIDTH": 1,
        "CONFIG.C_PROBE_OUT1_INIT_VAL": "0x0",
    }
    assert plan["verilog"] == (
        "vio_controls u_vio_controls (\n    .clk(pixel_clk),\n"
        "    .probe_in0(frame_count),\n    .probe_out0(gain),\n    .probe_out1(bypass)\n);\n"
    )
    assert "create_ip -vlnv $__vlnv -module_name $__name" in plan["tcl"]
    assert "generate_target all $__ip" in plan["tcl"]
    assert len(plan["remaining_steps"]) >= 4


def test_ila_plan_connects_entire_bus_without_reversing_or_slicing_bits():
    plan = plan_debug_ip(ILA)
    assert plan["configuration"]["CONFIG.C_PROBE0_WIDTH"] == 24
    assert plan["configuration"]["CONFIG.C_NUM_OF_PROBES"] == 2
    assert ".probe0(pixel_data)" in plan["verilog"]
    assert "[23:0]" not in plan["verilog"]
    assert "{" not in plan["verilog"]


@pytest.mark.parametrize(
    "name",
    [
        "",
        "3abc",
        "gain[7:0]",
        "top.gain",
        "module",
        "logic",
        "pixel;exec whoami",
        "[set injected 1]",
        "gain\nmodule x;",
        "a b",
        "\\escaped",
        "a$",
        "信号",
        None,
        2,
    ],
)
@pytest.mark.parametrize("field", ["name", "clock", "probe"])
def test_rejects_unsafe_or_unsupported_identifiers(name, field):
    spec = copy.deepcopy(ILA)
    if field == "probe":
        spec["probes"][0]["name"] = name
    else:
        spec[field] = name
    with pytest.raises(ValueError, match="标识符"):
        plan_debug_ip(spec)


@pytest.mark.parametrize("width", [0, -1, 1025, True, 2.5, "8", None])
def test_rejects_ila_widths_outside_documented_range(width):
    spec = copy.deepcopy(ILA)
    spec["probes"][0]["width"] = width
    with pytest.raises(ValueError, match="width"):
        plan_debug_ip(spec)


@pytest.mark.parametrize("depth", [0, 512, 1025, 2049, 262144, True, "4096"])
def test_rejects_invalid_ila_depth(depth):
    with pytest.raises(ValueError, match="depth"):
        plan_debug_ip({**ILA, "depth": depth})


@pytest.mark.parametrize(
    "initial", [-1, 4096, "0x1000", "1.2", "+1", "FF", "0b11", True, "[exec bad]", None, "1" * 100]
)
def test_rejects_invalid_output_initial(initial):
    spec = copy.deepcopy(VIO)
    spec["outputs"][0]["initial"] = initial
    with pytest.raises(ValueError, match="initial"):
        plan_debug_ip(spec)


@pytest.mark.parametrize(
    "spec",
    [
        {**ILA, "CONFIG.C_ADV_TRIGGER": 1},
        {**ILA, "probes": [{"name": "gain", "width": 8, "initial": 1}]},
        {**ILA, "outputs": []},
        {**ILA, "probes": []},
        {**ILA, "probes": [{"name": "gain", "width": 8}] * 2},
        {**ILA, "probes": [{"name": "a", "width": 1}] * 1025},
        {**VIO, "probes": []},
        {**VIO, "inputs": [], "outputs": []},
        {**VIO, "inputs": [{"name": "a", "width": 257}]},
        {**VIO, "outputs": [{"name": "a", "width": 1}] * 257},
        {**VIO, "outputs": [{"name": "pixel_clk", "width": 1}]},
        {**VIO, "outputs": [{"name": "gain", "width": 1, "CONFIG.X": 3}]},
        {"kind": "vio_ip", "name": "test", "clock": "clk"},
        {"kind": "ila_ip", "clock": "clk", "probes": []},
        {"kind": "other"},
        [],
        None,
    ],
)
def test_rejects_unknown_fields_missing_fields_counts_and_duplicate_signals(spec):
    with pytest.raises(ValueError):
        plan_debug_ip(spec)


def test_defaults_and_256_bit_vio_values_are_lossless():
    spec = {
        "kind": "vio_ip",
        "name": "vio_wide",
        "clock": "clk",
        "outputs": [{"name": "wide_control", "width": 256, "initial": str((1 << 256) - 1)}],
    }
    normalized = validate_debug_ip_spec(spec)
    assert normalized["inputs"] == []
    assert normalized["outputs"][0]["initial"] == "0x" + "F" * 64
    plan = plan_debug_ip(spec)
    assert plan["configuration"]["CONFIG.C_PROBE_OUT0_INIT_VAL"] == "0x" + "F" * 64
    assert (
        validate_debug_ip_spec({key: value for key, value in ILA.items() if key != "depth"})[
            "depth"
        ]
        == 1024
    )


def test_shared_vio_signal_requires_consistent_full_width_and_clock_is_one_bit():
    spec = copy.deepcopy(VIO)
    spec["inputs"] = [{"name": "gain", "width": 8}]
    with pytest.raises(ValueError, match="width 必须一致"):
        plan_debug_ip(spec)
    spec["inputs"][0]["width"] = 12
    assert ".probe_in0(gain)" in plan_debug_ip(spec)["verilog"]
    spec["inputs"] = [{"name": "pixel_clk", "width": 8}]
    with pytest.raises(ValueError, match="采样时钟宽度为 1"):
        plan_debug_ip(spec)


MOCK = r"""
set __props [dict create \
    project [dict create NAME isp DIRECTORY /workspace/isp PART xc7a35tcpg236-1] \
    ila_def [dict create VLNV xilinx.com:ip:ila:6.2] \
    vio_def [dict create VLNV xilinx.com:ip:vio:3.0]]
set __current project
set __defs {ila_def vio_def}
set __ips {}
set __fail ""
set __unsupported ""
proc current_project {args} {
    if {$args ne "-quiet"} {error "Unexpected current_project arguments"}
    return $::__current
}
proc get_property {property object} { return [dict get $::__props $object $property] }
proc get_ips {args} {
    if {$args ne "-quiet"} {error "Unexpected get_ips arguments"}
    return $::__ips
}
proc get_ipdefs {args} {
    if {$args ne {-filter {UPGRADE_VERSIONS == ""}}} {error "Unexpected get_ipdefs arguments"}
    return $::__defs
}
proc list_property {object} { return [dict keys [dict get $::__props $object]] }
proc create_ip {args} {
    puts "MOCK_CALL:[list create_ip {*}$args]"
    if {[llength $args] != 4 || [lindex $args 0] ne "-vlnv" || \
        [lindex $args 2] ne "-module_name"} {error "Unexpected create_ip arguments"}
    set name [lindex $args 3]
    if {$::__fail eq "create"} {error "IP creation failed"}
    set ::__ips {new_ip}
    dict set ::__props new_ip [dict create NAME $name]
    foreach property {CONFIG.C_NUM_OF_PROBES CONFIG.C_DATA_DEPTH CONFIG.C_NUM_PROBE_IN \
        CONFIG.C_NUM_PROBE_OUT} {
        if {$property ne $::__unsupported} {dict set ::__props new_ip $property 1}
    }
    for {set index 0} {$index < 3} {incr index} {
        foreach property [list CONFIG.C_PROBE${index}_WIDTH CONFIG.C_PROBE_IN${index}_WIDTH \
            CONFIG.C_PROBE_OUT${index}_WIDTH CONFIG.C_PROBE_OUT${index}_INIT_VAL] {
            if {$property ne $::__unsupported} {dict set ::__props new_ip $property 1}
        }
    }
}
proc set_property {args} {
    puts "MOCK_CALL:[list set_property {*}$args]"
    if {[llength $args] != 3 || [lindex $args 0] ne "-dict" || \
        [lindex $args 2] ne "new_ip"} {error "Unexpected set_property arguments"}
    dict for {property value} [lindex $args 1] {
        if {![dict exists $::__props new_ip $property]} {error "Unknown config property"}
        dict set ::__props new_ip $property $value
    }
    if {$::__fail eq "set"} {error "Vendor validation failed after partial changes"}
    if {$::__fail eq "readback"} {dict set ::__props new_ip CONFIG.C_PROBE0_WIDTH 4}
    if {$::__fail eq "normalize"} {
        dict set ::__props new_ip CONFIG.C_PROBE_OUT0_INIT_VAL 0x00000100
    }
}
proc generate_target {args} {
    puts "MOCK_CALL:[list generate_target {*}$args]"
    if {$args ne "all new_ip"} {error "Unexpected generate_target arguments"}
    if {$::__fail eq "generate"} {error "Output generation failed"}
}
"""


class TclMockSession:
    """每次执行隔离的真实 Tcl，保存输出供副作用断言使用。"""

    def __init__(self, setup: str = ""):
        self.setup = setup
        self.command = self.output = ""

    async def execute(self, command: str, timeout: float = 30.0) -> TclResult:
        if TCLSH is None:
            pytest.skip("需要 tclsh 执行真实 Tcl 语法")
        self.command = command
        result = subprocess.run(
            [TCLSH],
            input=MOCK + "\n" + self.setup + "\n" + command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        assert not result.stderr, result.stderr
        assert result.returncode == 0
        self.output = result.stdout
        return TclResult(result.stdout, 0, False)


async def test_inspect_returns_identity_and_explicitly_defers_config_check():
    session = TclMockSession()
    result = await DebugIPPreparation(session).inspect(ILA)
    assert result["status"] == "ready"
    assert result["project"] == PROJECT
    assert result["ipdef"] == "xilinx.com:ip:ila:6.2"
    assert result["configuration_validation"] == "requires_creation"
    assert not result["mutation_attempted"]
    assert "MOCK_CALL:" not in session.output


@pytest.mark.parametrize("spec", [ILA, VIO])
async def test_apply_creates_configures_reads_back_then_generates_without_build(spec):
    session = TclMockSession()
    result = await DebugIPPreparation(session).apply(spec, PROJECT)
    assert result["status"] == "created", result
    assert result["created"] and result["generated"] and result["mutation_attempted"]
    assert result["configuration_validation"] == "verified"
    assert result["readback"] == {key: str(value) for key, value in result["configuration"].items()}
    assert session.output.index("MOCK_CALL:create_ip") < session.output.index(
        "MOCK_CALL:set_property"
    )
    assert session.output.index("VMCP_DEBUG_IP_RECORD:readback") < session.output.index(
        "MOCK_CALL:generate_target all new_ip"
    )
    for command in (
        "launch_runs",
        "synth_design",
        "open_run",
        "program_hw_devices",
        "upgrade_ip",
        "remove_files",
        "delete_ip",
        "-force",
    ):
        assert command not in session.command


@pytest.mark.parametrize(
    "setup,message",
    [
        ("set __current {}", "current project"),
        ("dict set __props project PART {}", "empty part"),
        ("dict set __props project DIRECTORY {}", "empty directory"),
        ("set __ips old_ip; dict set __props old_ip NAME ila_pixels", "already exists"),
        ("set __defs {}", "exactly one current"),
        ("lappend __defs ila_def", "exactly one current"),
        ("dict set __props ila_def VLNV thirdparty.com:ip:ila:6.2", "exactly one current"),
        ("dict set __props ila_def VLNV xilinx.com:ip:system_ila:6.2", "exactly one current"),
    ],
)
async def test_failed_preflight_never_creates_or_reuses_ip(setup, message):
    session = TclMockSession(setup)
    result = await DebugIPPreparation(session).apply(ILA, PROJECT)
    assert result["status"] == "blocked"
    assert message in result["error"]
    assert not result["created"] and not result["mutation_attempted"]
    assert "MOCK_CALL:" not in session.output


@pytest.mark.parametrize(
    "field,value", [("name", "different"), ("directory", "/different"), ("part", "xc7z020clg400-1")]
)
async def test_project_changed_since_inspect_blocks_apply(field, value):
    session = TclMockSession()
    result = await DebugIPPreparation(session).apply(ILA, {**PROJECT, field: value})
    assert result["status"] == "blocked"
    assert "changed" in result["error"]
    assert "MOCK_CALL:" not in session.output


async def test_installed_version_is_discovered_not_hardcoded():
    session = TclMockSession("dict set __props ila_def VLNV xilinx.com:ip:ila:999.1")
    result = await DebugIPPreparation(session).apply(ILA, PROJECT)
    assert result["status"] == "created"
    assert result["ipdef"] == "xilinx.com:ip:ila:999.1"
    assert "create_ip -vlnv xilinx.com:ip:ila:999.1 -module_name ila_pixels" in session.output


async def test_similar_existing_name_does_not_match_glob():
    session = TclMockSession("set __ips old_ip; dict set __props old_ip NAME ila_pixels_backup")
    result = await DebugIPPreparation(session).apply(ILA, PROJECT)
    assert result["status"] == "created"


@pytest.mark.parametrize(
    "failure,stage,created",
    [
        ("create", "create_ip", None),
        ("set", "set_configuration", True),
        ("readback", "verify_configuration", True),
        ("generate", "generate_target", True),
    ],
)
async def test_mutation_failure_preserves_partial_state_without_retry_or_delete(
    failure, stage, created
):
    session = TclMockSession(f"set __fail {failure}")
    result = await DebugIPPreparation(session).apply(ILA, PROJECT)
    assert result["status"] == "partial"
    assert result["created"] is created
    assert not result["generated"]
    assert result["stage"] == stage
    assert result["recovery"]
    assert session.output.count("MOCK_CALL:create_ip") == 1
    if failure != "generate":
        assert "MOCK_CALL:generate_target" not in session.output


async def test_unsupported_config_is_partial_before_setting_any_properties():
    session = TclMockSession("set __unsupported CONFIG.C_PROBE_OUT0_INIT_VAL")
    result = await DebugIPPreparation(session).apply(VIO, PROJECT)
    assert result["status"] == "partial" and result["created"]
    assert "does not expose" in result["error"]
    assert "MOCK_CALL:set_property" not in session.output
    assert "MOCK_CALL:generate_target" not in session.output


async def test_numerically_equal_vio_initial_readback_is_accepted():
    session = TclMockSession("set __fail normalize")
    result = await DebugIPPreparation(session).apply(VIO, PROJECT)
    assert result["status"] == "created"
    assert result["readback"]["CONFIG.C_PROBE_OUT0_INIT_VAL"] == "0x00000100"


async def test_success_marker_without_complete_config_readback_is_unknown_partial():
    class LostRecordSession(TclMockSession):
        async def execute(self, command, timeout):
            result = await super().execute(command, timeout)
            output = "\n".join(
                line
                for line in result.output.splitlines()
                if not line.startswith("VMCP_DEBUG_IP_RECORD:readback")
            )
            return TclResult(output, 0, False)

    result = await DebugIPPreparation(LostRecordSession()).apply(ILA, PROJECT)
    assert result["status"] == "partial"
    assert result["created"] is None
    assert "读回" in result["error"]


async def test_256_bit_initial_roundtrips_real_tcl_bignum_validation():
    spec = {
        "kind": "vio_ip",
        "name": "vio_wide",
        "clock": "clk",
        "outputs": [{"name": "wide_control", "width": 256, "initial": (1 << 256) - 1}],
    }
    result = await DebugIPPreparation(TclMockSession()).apply(spec, PROJECT)
    assert result["status"] == "created"
    assert result["readback"]["CONFIG.C_PROBE_OUT0_INIT_VAL"] == "0x" + "F" * 64


async def test_project_identity_roundtrips_tcl_metacharacters():
    project = {
        **PROJECT,
        "name": '工程 [set injected 1] {$env(HOME)} "x"|\nVMCP_DEBUG_IP_ERR:x',
        "directory": "/workspace/path [set injected 1] $env(HOME) {a}",
    }
    setup = "\n".join(
        f"dict set __props project {key.upper()} {tcl_quote(value)}"
        for key, value in project.items()
    )
    session = TclMockSession(setup)
    result = await DebugIPPreparation(session).apply(ILA, project)
    assert result["status"] == "created"
    assert result["project"] == project
    assert "VMCP_DEBUG_IP_ERR:x" not in session.output


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {},
        {**PROJECT, "extra": 1},
        {**PROJECT, "part": 1},
        {**PROJECT, "name": ""},
        {**PROJECT, "name": "a\x00b"},
    ],
)
async def test_apply_rejects_invalid_project_identity_before_execute(identity):
    session = TclMockSession()
    with pytest.raises(ValueError):
        await DebugIPPreparation(session).apply(ILA, identity)
    assert not session.command


@pytest.mark.parametrize(
    "output,is_error",
    [
        ("", False),
        ("VMCP_DEBUG_IP_DONE:1", False),
        ("VMCP_DEBUG_IP_RECORD:result|wronghex\nVMCP_DEBUG_IP_DONE:1", False),
        ("disconnected", True),
    ],
)
async def test_broken_response_is_not_success_or_safe_to_retry(output, is_error):
    class BrokenSession:
        async def execute(self, command, timeout):
            return TclResult(output, int(is_error), is_error)

    api = DebugIPPreparation(BrokenSession())
    with pytest.raises(DebugIPError):
        await api.inspect(ILA)
    result = await api.apply(ILA, PROJECT)
    assert result["status"] == "partial"
    assert result["created"] is None and result["generated"] is None
    assert result["stage"] == "transport"


async def test_timeout_during_apply_reports_unknown_partial_state():
    class TimeoutSession:
        async def execute(self, command, timeout):
            raise TimeoutError("Timed out after sending command")

    result = await DebugIPPreparation(TimeoutSession()).apply(ILA, PROJECT)
    assert result["status"] == "partial"
    assert result["mutation_attempted"] is None
    assert result["created"] is None


async def test_offline_tcl_is_runnable_and_uses_identical_preflight():
    session = TclMockSession()
    await session.execute(plan_debug_ip(VIO)["tcl"])
    assert "MOCK_CALL:create_ip" in session.output
    assert "MOCK_CALL:generate_target all new_ip" in session.output


async def test_tcl_locals_do_not_clobber_user_global_variables():
    session = TclMockSession("set __name user_value; set __stage user_stage")
    plan = plan_debug_ip(ILA)
    await session.execute(plan["tcl"] + '\nputs "MOCK_PRESERVED:$__name|$__stage"\n')
    assert "MOCK_PRESERVED:user_value|user_stage" in session.output
