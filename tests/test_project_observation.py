"""真实 Tcl 验证可选工程路径观察；Vivado 对象查询由显式 stub 提供。"""

import os
import shutil
import subprocess

import pytest

from vivado_mcp.run_monitor import parse_snapshot
from vivado_mcp.tcl_scripts import QUERY_RUN_PROGRESS


@pytest.mark.skipif(not shutil.which("tclsh"), reason="需要 Tcl 解释器")
@pytest.mark.parametrize("mode", ["disk", "missing", "memory", "unavailable"])
def test_optional_project_metadata_never_invalidates_run_or_invents_xpr(tmp_path, mode):
    project = tmp_path / "工程 space [literal]"
    project.mkdir()
    xpr = project / "demo.xpr"
    if mode != "missing":
        # 文件仅验证存在性；这个 stub 不是可交付的真实 Vivado 工程。
        xpr.write_text("test-only existence marker", encoding="utf-8")
    script = """
fconfigure stdout -encoding utf-8
proc get_runs {args} {return impl_1}
proc current_project {} {return demo}
proc version {args} {return mock-tcl}
proc get_filesets {args} {error {no conventional sources_1}}
proc list_property {args} {return {NAME DIRECTORY IS_IN_MEMORY}}
proc get_property {prop object} {
    if {$object eq "demo"} {
        if {$::env(TEST_MODE) eq "unavailable"} {error {metadata unavailable}}
        switch -- $prop {
            NAME {return demo}
            DIRECTORY {return $::env(TEST_PROJECT_DIR)}
            IS_IN_MEMORY {return [expr {$::env(TEST_MODE) eq "memory"}]}
        }
    }
    switch -- $prop {
        STATUS {return {route_design Running}}
        PROGRESS {return {0%}}
        DIRECTORY {return $::env(TEST_RUN_DIR)}
        default {return {}}
    }
}
""" + QUERY_RUN_PROGRESS.format(run_name="impl_1", tail_n=2)
    script_hex = script.encode("utf-8").hex()
    driver = f"eval [encoding convertfrom utf-8 [binary format H* {script_hex}]]\n"
    result = subprocess.run(
        [shutil.which("tclsh")], input=driver.encode("ascii"), capture_output=True,
        env={**os.environ, "TEST_MODE": mode, "TEST_PROJECT_DIR": str(project),
             "TEST_RUN_DIR": str(tmp_path / "absent-run")}, check=True,
    )
    assert result.stderr == b""
    value = parse_snapshot(result.stdout.decode("utf-8"), "impl_1", "route_design")
    assert value["progress_percent"] == 0
    assert value["state"] == "running"
    assert value["top"] == ""
    if mode == "disk":
        assert value["project_file"] == xpr.as_posix()
    else:
        assert value["project_file"] == ""
    assert value["project_mode"] == ("in_memory" if mode == "memory" else "unknown")
