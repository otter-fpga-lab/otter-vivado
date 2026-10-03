"""真实 Tcl 检查缺少可选耗时属性时的轮询；Vivado 对象查询用显式桩。"""

import shutil
import subprocess

import pytest

from vivado_mcp.tcl_scripts import POLL_RUN_STATUS


@pytest.mark.skipif(not shutil.which("tclsh"), reason="需要 Tcl 解释器")
@pytest.mark.parametrize("elapsed", ["error {unsupported property}", "return {}",
                                    "return {00:01:25}"])
def test_missing_elapsed_does_not_discard_actual_status_and_progress(elapsed):
    stub = """
proc get_runs {args} {return synth_1}
proc get_property {property object} {
    switch -- $property {
        STATUS {return {synth_design Complete!}}
        PROGRESS {return 100%}
        STATS.ELAPSED {__ELAPSED__}
    }
}
""".replace("__ELAPSED__", elapsed)
    script_hex = (stub + POLL_RUN_STATUS.format(run_name="synth_1")).encode("utf-8").hex()
    result = subprocess.run(
        [shutil.which("tclsh")],
        input=f"eval [encoding convertfrom utf-8 [binary format H* {script_hex}]]\n",
        text=True, capture_output=True, check=True,
    )
    assert result.stderr == ""
    expected_elapsed = "00:01:25" if "00:01:25" in elapsed else ""
    assert result.stdout.strip() == f"VMCP_POLL|synth_design Complete!|100%|{expected_elapsed}"
