"""用真实 Tcl/文件 I/O 验证样例交付契约；Vivado 命令为桩，不证明 EDA/GUI 成功。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[1] / "examples" / "progress"
pytestmark = pytest.mark.skipif(not shutil.which("tclsh"), reason="需要 Tcl 解释器")

STUB = r"""
fconfigure stdout -encoding utf-8
fconfigure stderr -encoding utf-8
proc get_projects {args} {return $::env(OTTER_TEST_OPEN_PROJECT)}
proc get_parts {args} {return $::env(OTTER_TEST_PARTS)}
proc create_project {name dir args} {
    if {$args ne {-part test-part}} {error {Unexpected project creation options}}
    if {$::env(OTTER_TEST_GUI_DIRECTORY)} {set dir [file join $dir $name]}
    set ::__project_name $name
    set ::__project_dir [file normalize $dir]
    file mkdir $::__project_dir
    set __file [open [file join $::__project_dir "$name.xpr"] w]
    puts $__file {MOCK PROJECT: not a Vivado-generated XPR}
    close $__file
}
proc import_files {args} {
    if {$::env(OTTER_TEST_IMPORT_FAILURE)} {error {Mock import failure}}
    if {[lindex $args 0] ne {-fileset} || [lindex $args 2] ne {-flat}} {
        error {Expected explicit fileset and local flat imports}
    }
    set __fileset [lindex $args 1]
    set __dest [file join $::__project_dir "$::__project_name.srcs" $__fileset imports]
    file mkdir $__dest
    foreach __source [lindex $args 3] {file copy $__source $__dest}
}
proc get_filesets {name} {return $name}
proc set_property {property value object} {
    if {$property ne {top} || $value ne {demo} || $object ne {sources_1}} {
        error {Unexpected top setting}
    }
}
proc update_compile_order {args} {}
proc current_project {} {return $::__project_name}
proc get_property {property object} {
    switch -- $property {
        DIRECTORY {return $::__project_dir}
        NAME {return $::__project_name}
        default {error "Unexpected property: $property"}
    }
}
proc add_files {args} {error {External source references are forbidden in this demo}}
proc close_project {args} {error {An existing project must not be closed}}
proc launch_runs {args} {error {Creating the example must not launch runs}}
proc program_hw_devices {args} {error {Creating the example must not program devices}}
if {!$::env(OTTER_TEST_MISSING_INPUT)} {
    set __otter_demo_dir $::env(OTTER_TEST_DEST)
    set __otter_demo_part test-part
}
source -encoding utf-8 [file join $::env(OTTER_TEST_SOURCE) create_demo.tcl]
"""


def run_demo(tmp_path, source, destination, **overrides):
    """在真实 Tcl 解释器中 source 脚本；仅 Vivado API 由上述桩提供。"""
    script = tmp_path / "driver.tcl"
    script.write_text(STUB, encoding="utf-8")
    env = {
        **os.environ,
        "OTTER_TEST_SOURCE": str(source),
        "OTTER_TEST_DEST": str(destination),
        "OTTER_TEST_OPEN_PROJECT": "",
        "OTTER_TEST_PARTS": "test-part",
        "OTTER_TEST_GUI_DIRECTORY": "0",
        "OTTER_TEST_IMPORT_FAILURE": "0",
        "OTTER_TEST_MISSING_INPUT": "0",
        **overrides,
    }
    return subprocess.run(
        [shutil.which("tclsh"), str(script)],
        capture_output=True, text=True, encoding="utf-8", env=env, check=True,
    )


@pytest.mark.parametrize("gui_directory", ["0", "1"])
def test_demo_imports_inputs_and_reports_actual_native_project(tmp_path, gui_directory):
    source = tmp_path / "tool source 中文 [literal]"
    shutil.copytree(DEMO, source)
    destination = tmp_path / "consumer project 中文 [literal]"
    result = run_demo(tmp_path, source, destination, OTTER_TEST_GUI_DIRECTORY=gui_directory)
    assert result.stderr == ""
    assert "VMCP_DEMO_ERR:" not in result.stdout
    project_dir = destination / "otter_progress_demo" if gui_directory == "1" else destination
    project_file = project_dir / "otter_progress_demo.xpr"
    assert project_file.is_file()
    assert f"VMCP_DEMO:project_file={project_file.as_posix()}" in result.stdout
    assert "VMCP_DEMO:sources=imported" in result.stdout

    # 真实文件复制/移动后仍保留两个消费输入；此断言不模拟或宣称 Vivado 重开成功。
    expected = {"demo.v": (source / "demo.v").read_bytes(),
                "demo.xdc": (source / "demo.xdc").read_bytes()}
    shutil.rmtree(source)
    moved = tmp_path / "moved consumer"
    shutil.move(str(project_dir), moved)
    for fileset, filename in [("sources_1", "demo.v"), ("constrs_1", "demo.xdc")]:
        assert (moved / "otter_progress_demo.srcs" / fileset / "imports" / filename
                ).read_bytes() == expected[filename]


@pytest.mark.parametrize("condition,expected", [
    ("missing_input", "Set __otter_demo_dir"),
    ("open_project", "existing project will not be closed"),
    ("existing_output", "Output already exists"),
    ("unknown_part", "Select one exact installed Vivado part"),
    ("missing_source", "Missing demo input: demo.v"),
])
def test_demo_preflight_does_not_mutate_project(tmp_path, condition, expected):
    source = tmp_path / "source"
    shutil.copytree(DEMO, source)
    destination = tmp_path / "destination"
    options = {}
    if condition == "missing_input":
        options["OTTER_TEST_MISSING_INPUT"] = "1"
    elif condition == "open_project":
        options["OTTER_TEST_OPEN_PROJECT"] = "existing-project"
    elif condition == "existing_output":
        destination.mkdir()
        (destination / "keep.txt").write_text("user data", encoding="utf-8")
    elif condition == "unknown_part":
        options["OTTER_TEST_PARTS"] = ""
    else:
        (source / "demo.v").unlink()
    result = run_demo(tmp_path, source, destination, **options)
    assert result.stderr == ""
    assert result.stdout.startswith("VMCP_DEMO_ERR:")
    assert expected in result.stdout
    assert "VMCP_DEMO:created=" not in result.stdout
    if condition == "existing_output":
        assert list(destination.iterdir()) == [destination / "keep.txt"]
        assert (destination / "keep.txt").read_text(encoding="utf-8") == "user data"
    else:
        assert not destination.exists()


def test_import_failure_is_reported_without_deleting_partial_project(tmp_path):
    destination = tmp_path / "destination"
    result = run_demo(tmp_path, DEMO, destination, OTTER_TEST_IMPORT_FAILURE="1")
    assert "VMCP_DEMO_ERR:Mock import failure" in result.stdout
    assert "VMCP_DEMO:created=" not in result.stdout
    assert (destination / "otter_progress_demo.xpr").is_file()
