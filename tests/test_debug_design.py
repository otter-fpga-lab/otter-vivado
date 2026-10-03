"""真实 Tcl 解释器与模拟 Vivado 对象验证；不代表商业 EDA 或板卡实测。"""

from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from vivado_mcp.debug_design import (
    DebugDesignError,
    DebugDesignPreparation,
    plan_debug_design,
)
from vivado_mcp.vivado.tcl_utils import TclResult, tcl_quote

TCLSH = shutil.which("tclsh")
ILA = {
    "kind": "ila_netlist", "name": "ila_camera", "clock": "top/pixel_clk", "depth": 4096,
    "probes": [{"nets": ["top/frame_valid"]}, {"nets": ["top/pixel[0]", "top/pixel[1]"]}],
}
MARK = {"kind": "mark_debug", "name": "observe", "nets": ["top/frame_valid"]}


def run_tcl(script: str, timeout: float = 5.0) -> str:
    if TCLSH is None:
        pytest.skip("需要 tclsh 验证真实 Tcl 语法")
    process = subprocess.run(
        [TCLSH], input=script, text=True, capture_output=True, timeout=timeout, check=False,
    )
    assert process.returncode == 0, process.stderr
    assert not process.stderr, process.stderr
    return process.stdout


def records(output: str, prefix: str) -> list[list[str]]:
    return [
        [bytes.fromhex(field).decode("utf-8") for field in line[len(prefix):].split("|")]
        for line in output.splitlines() if line.startswith(prefix)
    ]


COMMON = r"""
proc emit {event args} {
    set fields [list]
    foreach value [list $event {*}$args] {
        lappend fields [binary encode hex [encoding convertto utf-8 $value]]
    }
    puts "EVENT:[join $fields |]"
}
proc get_property {property object} {return [dict get $::__props $object $property]}
"""

NETLIST_MOCK = COMMON + r"""
set __props [dict create]
set __nets [list]
set __cores [list]
set __ports [list]
set __next_probe 1
proc get_nets {args} {
    if {[lrange $args 0 1] ne {-hierarchical -regexp} || [llength $args] != 3} {
        error "Unexpected get_nets arguments"
    }
    set matches [list]
    foreach net $::__nets {
        if {[regexp -- [lindex $args 2] [get_property NAME $net]]} {lappend matches $net}
    }
    return $matches
}
proc get_debug_cores {args} {
    if {$args ne {-quiet}} {error "Unexpected get_debug_cores arguments"}
    return $::__cores
}
proc get_debug_ports {args} {
    if {$args ne {-of_objects core_new}} {error "Unexpected get_debug_ports arguments"}
    return $::__ports
}
proc set_property {property value objects} {
    foreach object $objects {
        dict set ::__props $object $property $value
        emit property $property $value [get_property NAME $object]
    }
}
proc create_debug_core {name kind} {
    emit create_core $name $kind
    dict set ::__props core_new [dict create NAME $name]
    dict set ::__props clk_port [dict create NAME $name/CLK]
    dict set ::__props probe0_port [dict create NAME $name/PROBE0]
    set ::__cores {core_new}
    set ::__ports {clk_port probe0_port}
    return core_new
}
proc create_debug_port {core kind} {
    if {$core ne "core_new" || $kind ne "probe"} {error "Unexpected create_debug_port arguments"}
    set handle probe$::__next_probe
    dict set ::__props $handle [dict create NAME [get_property NAME $core]/PROBE$::__next_probe]
    lappend ::__ports $handle
    incr ::__next_probe
    emit create_port $handle
    return $handle
}
proc connect_debug_port {args} {
    if {[llength $args] == 2} {
        lassign $args port net
        set bit clock
    } elseif {[llength $args] == 4 && [lindex $args 0] eq "-channel_start_index"} {
        lassign $args option bit port net
    } else {
        error "Unexpected connect_debug_port arguments"
    }
    emit connect [get_property NAME $port] $bit [get_property NAME $net]
}
"""


def execute_constraint(spec: dict, *, nets: list[str] | None = None, extra: str = "") -> dict:
    plan = plan_debug_design(spec)
    names = plan["required_nets"] if nets is None else nets
    setup = []
    for index, name in enumerate(names):
        setup += [
            f"dict set ::__props net{index} [dict create NAME {tcl_quote(name)}]",
            f"lappend ::__nets net{index}",
        ]
    content = plan["artifacts"][0]["content"]
    script = (
        NETLIST_MOCK + "\n" + "\n".join(setup) + "\n" + extra
        + f"\nset __script {tcl_quote(content)}\n"
        + "set __code [catch {uplevel #0 $__script} __result]\n"
        + "emit outcome $__code $__result\n"
    )
    events = records(run_tcl(script), "EVENT:")
    return {"code": int(events[-1][1]), "result": events[-1][2], "events": events[:-1]}


def test_special_net_names_and_probe_bit_order_survive_actual_tcl_parsing():
    clock = "top/时钟$clk[0]"
    first = "top/{像素};[set injected 1]$data[0]"
    second = "top/data\\escaped\nline[1]"
    spec = {**ILA, "clock": clock, "probes": [{"nets": [first, second]}]}
    assert plan_debug_design(spec)["artifacts"][0]["content"].isascii()
    result = execute_constraint(spec)
    assert result["code"] == 0, result
    assert [event for event in result["events"] if event[0] == "connect"] == [
        ["connect", "ila_camera/CLK", "clock", clock],
        ["connect", "ila_camera/PROBE0", "0", first],
        ["connect", "ila_camera/PROBE0", "1", second],
    ]


def test_multiple_probes_set_widths_and_reset_bit_index():
    result = execute_constraint(ILA)
    assert result["code"] == 0, result
    assert [event[1:] for event in result["events"] if event[0] == "connect"] == [
        ["ila_camera/CLK", "clock", "top/pixel_clk"],
        ["ila_camera/PROBE0", "0", "top/frame_valid"],
        ["ila_camera/PROBE1", "0", "top/pixel[0]"],
        ["ila_camera/PROBE1", "1", "top/pixel[1]"],
    ]
    assert [event[2:] for event in result["events"] if event[:2] == ["property", "PORT_WIDTH"]] == [
        ["1", "ila_camera/PROBE0"], ["2", "ila_camera/PROBE1"],
    ]


@pytest.mark.parametrize("mode", ["missing_last", "ambiguous_last", "prefix_only"])
def test_all_nets_preflight_before_any_design_change(mode):
    names = plan_debug_design(ILA)["required_nets"]
    if mode == "missing_last":
        names = names[:-1]
    elif mode == "ambiguous_last":
        names = names + [names[-1]]
    else:
        names = names[:-1] + [names[-1] + "/other"]
    result = execute_constraint(ILA, nets=names)
    assert result["code"] == 1
    assert "exactly one net" in result["result"]
    assert not result["events"]


def test_existing_core_blocks_before_mark_debug_or_create():
    result = execute_constraint(ILA, extra=(
        "dict set ::__props existing [dict create NAME ila_camera]\n"
        "set ::__cores {existing}\n"
    ))
    assert result["code"] == 1
    assert "already exists" in result["result"]
    assert not result["events"]


def test_mark_debug_checks_whole_net_set_before_writing_properties():
    spec = {**MARK, "nets": ["top/frame_valid", "top/pixel[0]"]}
    failed = execute_constraint(spec, nets=["top/frame_valid"])
    assert failed["code"] == 1
    assert not failed["events"]
    result = execute_constraint(spec)
    assert result["code"] == 0
    assert result["events"] == [
        ["property", "MARK_DEBUG", "true", "top/frame_valid"],
        ["property", "MARK_DEBUG", "true", "top/pixel[0]"],
    ]


PREPARE_MOCK = COMMON + r"""
set __props [dict create \
    project [dict create NAME camera DIRECTORY $::project_dir PART xc7a35tcpg236-1] \
    synth [dict create NAME synth_1 CONSTRSET constrs_1 IS_SYNTHESIS 1 IS_IMPLEMENTATION 0 \
        STATUS {synth_design Complete!}] \
    impl [dict create NAME impl_1 CONSTRSET constrs_1 IS_SYNTHESIS 0 IS_IMPLEMENTATION 1 \
        STATUS {route_design Complete!}] \
    other [dict create NAME impl_2 CONSTRSET constrs_2 IS_SYNTHESIS 0 IS_IMPLEMENTATION 1 \
        STATUS {Not started}] \
    constraints [dict create NAME constrs_1] \
    other_constraints [dict create NAME constrs_2]]
set __runs {synth impl other}
set __filesets {constraints other_constraints}
set __files [dict create constraints {} other_constraints {}]
set __failure ""
set __current project
proc current_project {args} {
    if {$args ne {-quiet}} {error "Unexpected current_project arguments"}
    return $::__current
}
proc get_runs {args} {
    if {$args ne {-quiet}} {error "Unexpected get_runs arguments"}
    return $::__runs
}
proc get_filesets {args} {
    if {$args ne {-quiet}} {error "Unexpected get_filesets arguments"}
    return $::__filesets
}
proc get_files {args} {
    if {[lrange $args 0 1] ne {-quiet -of_objects} || [llength $args] != 3} {
        error "Unexpected get_files arguments"
    }
    return [dict get $::__files [lindex $args 2]]
}
proc add_files {args} {
    emit add_files {*}$args
    if {$::__failure eq "add_files"} {error "Cannot register file"}
    lassign $args option set_name path
    if {$option ne "-fileset" || $set_name ne "constrs_1"} {
        error "Unexpected add_files arguments"
    }
    dict set ::__props constraint_file [dict create NAME $path USED_IN_SYNTHESIS 1 \
        USED_IN_IMPLEMENTATION 1 PROCESSING_ORDER EARLY]
    dict lappend ::__files constraints constraint_file
}
proc set_property {property value object} {
    emit property $property $value $object
    if {$::__failure eq $property} {error "Cannot set property $property"}
    if {$::__failure ne "readback"} {dict set ::__props $object $property $value}
}
rename open __real_open
proc open {args} {
    emit open {*}$args
    return [__real_open {*}$args]
}
"""


class PreparationSession:
    def __init__(self, directory: Path, setup: str = ""):
        self.directory, self.setup = directory, setup
        self.output = ""

    async def execute(self, command: str, timeout: float = 30.0) -> TclResult:
        setup = f"set ::project_dir {tcl_quote(str(self.directory))}\n"
        self.output = run_tcl(setup + PREPARE_MOCK + "\n" + self.setup + "\n" + command, timeout)
        return TclResult(self.output, 0, False)

    @property
    def project(self) -> dict:
        return {"name": "camera", "directory": str(self.directory), "part": "xc7a35tcpg236-1"}

    @property
    def events(self) -> list[list[str]]:
        return records(self.output, "EVENT:")


async def test_inspect_observes_shared_constraint_set_without_writing(tmp_path):
    session = PreparationSession(tmp_path)
    result = await DebugDesignPreparation(session).inspect(ILA)
    assert result["status"] == "ready", result
    assert result["project"] == session.project
    assert result["constraint_set"] == "constrs_1"
    assert result["affected_runs"] == ["synth_1", "impl_1"]
    assert not result["file_created"] and not result["registered"]
    assert not result["design_modified"] and not result["built"]
    assert not session.events
    assert not (tmp_path / "otter_debug").exists()


@pytest.mark.parametrize("spec, synthesis, implementation", [(ILA, "0", "1"), (MARK, "1", "0")])
async def test_apply_writes_only_generated_file_and_sets_verified_stage_flags(
    tmp_path, spec, synthesis, implementation,
):
    session = PreparationSession(tmp_path)
    result = await DebugDesignPreparation(session).apply(spec, expected_project=session.project)
    assert result["status"] == "constraints_added", result
    assert result["file_created"] and result["registered"]
    assert not result["design_modified"] and not result["connections_verified"]
    artifact = result["plan"]["artifacts"][0]
    path = tmp_path / "otter_debug" / artifact["filename"]
    assert path.read_text(encoding="utf-8") == artifact["content"]
    assert session.events == [
        ["open", str(path), "WRONLY CREAT EXCL"],
        ["add_files", "-fileset", "constrs_1", str(path)],
        ["property", "USED_IN_SYNTHESIS", synthesis, "constraint_file"],
        ["property", "USED_IN_IMPLEMENTATION", implementation, "constraint_file"],
        ["property", "PROCESSING_ORDER", "LATE", "constraint_file"],
    ]


@pytest.mark.parametrize("key, value", [
    ("NAME", "different"), ("DIRECTORY", "/other/project"), ("PART", "other_part"),
])
async def test_apply_blocks_project_switch_before_creating_files(tmp_path, key, value):
    session = PreparationSession(tmp_path, f"dict set ::__props project {key} {tcl_quote(value)}")
    result = await DebugDesignPreparation(session).apply(ILA, expected_project=session.project)
    assert result["status"] == "blocked"
    assert "changed since inspection" in result["error"]
    assert not session.events
    assert not (tmp_path / "otter_debug").exists()


@pytest.mark.parametrize("setup, error", [
    ("set ::__current {}", "current project"),
    ("set ::__runs {synth other}", "Run name"),
    ("dict set ::__props other NAME impl_1", "ambiguous"),
    ("dict set ::__props impl IS_IMPLEMENTATION 0", "Run type"),
    ("set ::__filesets {}", "constraint set"),
    ("dict set ::__props synth STATUS {synth_design Running}", "sharing"),
    ("dict set ::__props synth STATUS {Queued}", "sharing"),
])
async def test_run_and_shared_constraint_preflight_blocks_without_side_effects(
    tmp_path, setup, error,
):
    session = PreparationSession(tmp_path, setup)
    result = await DebugDesignPreparation(session).apply(ILA, expected_project=session.project)
    assert result["status"] == "blocked", result
    assert error in result["error"]
    assert not session.events
    assert not (tmp_path / "otter_debug").exists()


async def test_existing_destination_is_preserved_without_any_registration(tmp_path):
    directory = tmp_path / "otter_debug"
    directory.mkdir()
    path = directory / "ila_camera_ila.tcl"
    path.write_text("用户已有约束", encoding="utf-8")
    session = PreparationSession(tmp_path)
    result = await DebugDesignPreparation(session).apply(ILA, expected_project=session.project)
    assert result["status"] == "blocked"
    assert path.read_text(encoding="utf-8") == "用户已有约束"
    assert not session.events


async def test_registered_missing_file_still_blocks_before_write(tmp_path):
    path = tmp_path / "otter_debug" / "ila_camera_ila.tcl"
    session = PreparationSession(tmp_path, (
        f"dict set ::__props old [dict create NAME {tcl_quote(str(path))}]\n"
        "dict set ::__files constraints {old}\n"
    ))
    result = await DebugDesignPreparation(session).apply(ILA, expected_project=session.project)
    assert result["status"] == "blocked"
    assert "already registered" in result["error"]
    assert not session.events
    assert not path.exists()


@pytest.mark.parametrize("target_exists", [False, True])
async def test_existing_or_dangling_debug_directory_symlink_blocks_before_write(
    tmp_path, target_exists,
):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    if target_exists:
        external.mkdir()
    try:
        (project / "otter_debug").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统没有创建目录符号链接的权限")
    session = PreparationSession(project)
    result = await DebugDesignPreparation(session).apply(ILA, expected_project=session.project)
    assert result["status"] == "blocked", result
    assert "not a plain directory" in result["error"]
    assert not session.events
    assert not (external / "ila_camera_ila.tcl").exists()


async def test_non_ascii_project_path_and_exact_run_name_roundtrip(tmp_path):
    directory = tmp_path / "消费$者[工程]\n目录"
    directory.mkdir()
    run = "impl[2]$调试\n分支"
    session = PreparationSession(directory, f"dict set ::__props impl NAME {tcl_quote(run)}")
    spec = {**ILA, "run": run}
    inspected = await DebugDesignPreparation(session).inspect(spec)
    assert inspected["status"] == "ready", inspected
    assert inspected["project"] == session.project
    result = await DebugDesignPreparation(session).apply(spec, inspected["project"])
    assert result["status"] == "constraints_added", result
    assert result["run"] == run
    assert result["affected_runs"] == ["synth_1", run]
    assert Path(result["path"]).parent == directory / "otter_debug"


@pytest.mark.parametrize("failure", ["add_files", "USED_IN_IMPLEMENTATION", "readback"])
async def test_registration_failures_preserve_file_and_report_partial(tmp_path, failure):
    session = PreparationSession(tmp_path, f"set ::__failure {failure}")
    result = await DebugDesignPreparation(session).apply(ILA, expected_project=session.project)
    assert result["status"] == "partial", result
    assert result["file_created"]
    assert result["registered"] is (failure != "add_files")
    assert Path(result["path"]).exists()
    assert result["error"]
    if failure == "readback":
        assert "property verification failed" in result["error"]
    assert not result["built"] and not result["design_modified"]


@pytest.mark.parametrize("spec", [
    {**ILA, "extra": "source evil.tcl"}, {**ILA, "probes": []},
    {**ILA, "depth": 1025}, {**ILA, "depth": True}, {**ILA, "clock": ""},
    {**ILA, "name": "../outside"}, {**ILA, "run": ""},
    {**ILA, "probes": [{"nets": ["top/signal", "top/signal"]}]},
    {**ILA, "probes": [{"nets": ["top/signal"]}] * 2},
    {**MARK, "nets": []}, {**MARK, "nets": ["with\x00null"]}, None, [],
])
def test_invalid_structured_descriptions_are_rejected_without_tcl(spec):
    with pytest.raises(ValueError):
        plan_debug_design(spec)


def test_plan_does_not_mutate_input_and_reports_deferred_connections():
    original = copy.deepcopy(ILA)
    plan = plan_debug_design(ILA)
    assert ILA == original
    assert plan["spec"]["run"] == "impl_1"
    assert plan["probe_map"][1]["bits"] == [
        {"bit": 0, "net": "top/pixel[0]"}, {"bit": 1, "net": "top/pixel[1]"},
    ]
    assert plan["limitations"] and plan["remaining_steps"]


@pytest.mark.parametrize("output", [
    "", "VMCP_DESIGN_DONE:1\n", "VMCP_DESIGN_FIELD:status|nothex\nVMCP_DESIGN_DONE:1\n",
    "VMCP_DESIGN_FIELD:status|7265616479\nVMCP_DESIGN_DONE:1\n",
    "VMCP_DESIGN_FIELD:status|7265616479\nVMCP_DESIGN_FIELD:status|7265616479\n",
])
async def test_incomplete_or_damaged_response_never_claims_ready(output):
    session = AsyncMock()
    session.execute.return_value = TclResult(output, 0, False)
    with pytest.raises(DebugDesignError):
        await DebugDesignPreparation(session).inspect(ILA)


async def test_transport_failure_is_not_interpreted_as_rolled_back():
    session = AsyncMock()
    session.execute.side_effect = TimeoutError("after send")
    project = {"name": "camera", "directory": "/consumer", "part": "xc7a35tcpg236-1"}
    with pytest.raises(DebugDesignError, match="未知"):
        await DebugDesignPreparation(session).apply(ILA, project)
    session.execute.assert_awaited_once()
