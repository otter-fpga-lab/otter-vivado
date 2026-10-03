"""真实解析链路与模拟传输验证，不需要也不宣称真实 Vivado 运行。"""

import asyncio
import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vivado_mcp.run_monitor import MonitorRegistry, RunMonitor, parse_snapshot, read_reports
from vivado_mcp.tcl_scripts import QUERY_RUN_PROGRESS
from vivado_mcp.vivado.tcl_utils import TclResult

RAW = """VMCP_RUN:status=route_design Running
VMCP_RUN:progress=0%
VMCP_RUN:dir=/missing/demo.runs/impl_1
VMCP_RUN:elapsed=00:00:05
VMCP_RUN_TAIL:1|WARNING: [Test 1-1] sample
VMCP_RUN_DONE
"""


def session():
    return SimpleNamespace(
        session_id="test", mode="tcl", is_alive=True, state="ready",
        execute=AsyncMock(return_value=TclResult(RAW, 0, False)), stop=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_cached_read_never_queries_and_busy_disconnected_keep_evidence():
    source = session()
    monitor = RunMonitor(source, "impl_1", "route_design")
    await monitor.sample()
    original = monitor.snapshot()
    assert original["run"]["progress_percent"] == 0
    assert original["quality"] == {"timing": "unknown", "resources": "unknown"}
    assert original["run"]["diagnostics"]["warnings"] == 1
    monitor.snapshot()["run"]["status"] = "tampered"
    assert monitor.snapshot() == original
    source.state = "busy"
    await monitor.sample()
    assert monitor.snapshot()["connection"] == "busy"
    assert monitor.snapshot()["observed_at"] == original["observed_at"]
    source.is_alive = False
    await monitor.sample()
    assert monitor.snapshot()["connection"] == "disconnected"
    assert monitor.snapshot()["run"] == original["run"]
    source.execute.assert_awaited_once()
    await monitor.close()
    source.stop.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.TimeoutError("pending"), ValueError("query failed")])
async def test_sampling_error_preserves_last_observation(failure):
    source = session()
    monitor = RunMonitor(source, "impl_1", "write_bitstream")
    await monitor.sample()
    before = monitor.snapshot()
    source.execute.side_effect = failure
    await monitor.sample()
    after = monitor.snapshot()
    assert after["observed_at"] == before["observed_at"]
    assert after["connection"] in ("busy", "error")
    assert after["error"]
    await monitor.close()
    source.stop.assert_not_called()


@pytest.mark.parametrize("raw,state,percent", [
    (RAW, "running", 0),
    (RAW.replace("route_design Running", "place_design Complete!"), "stage_complete", 0),
    (RAW.replace("route_design Running", "route_design ERROR"), "failed", 0),
    (RAW.replace("route_design Running", "route_design Complete!"), "completed", 0),
    (RAW.replace("progress=0%", "progress="), "running", None),
])
def test_same_parser_for_live_and_replay(raw, state, percent):
    value = parse_snapshot(raw, "impl_1", "route_design")
    assert (value["state"], value["progress_percent"]) == (state, percent)


@pytest.mark.parametrize("raw", ["", RAW.replace("VMCP_RUN_DONE", ""),
                                 RAW + "VMCP_RUN_ERROR:failure\n"])
def test_incomplete_samples_do_not_become_success(raw):
    with pytest.raises(ValueError):
        parse_snapshot(raw, "impl_1", "route_design")


def test_reports_stale_mismatch_unknown_stage_and_no_live_design_mix(tmp_path):
    begin = tmp_path / ".vivado.begin.rst"
    begin.touch()
    os.utime(begin, (200, 200))
    old = tmp_path / "demo_timing_summary_routed.rpt"
    old.write_text("Design : demo\n", encoding="utf-8")
    os.utime(old, (100, 100))
    wrong = tmp_path / "wrong_timing_summary_placed.rpt"
    wrong.write_text("Design : different\n", encoding="utf-8")
    unknown = tmp_path / "custom.rpt"
    unknown.write_text("Report without provenance", encoding="utf-8")
    reports = {r["name"]: r for r in read_reports({"directory": str(tmp_path), "top": "demo"})}
    assert reports[old.name]["freshness"] == "stale"
    assert reports[wrong.name]["freshness"] == "mismatched"
    assert reports[wrong.name]["stage"] == "post-place"
    assert reports[unknown.name]["stage"] == "unknown"
    assert reports[unknown.name]["freshness"] == "unverified"
    assert reports[old.name]["summary"]["summary"]["parse_status"] == "unrecognized"
    stale = read_reports({"directory": str(tmp_path), "needs_refresh": "1"})
    assert all(r["freshness"] == "stale" for r in stale)


def test_report_reads_are_bounded_and_symlinks_excluded(tmp_path):
    large = tmp_path / "a_timing.rpt"
    large.write_bytes(b"x" * (256 * 1024 + 50))
    (tmp_path / "b_link.rpt").symlink_to(large)
    report, = read_reports({"directory": str(tmp_path)})
    assert len(report["text"]) == 256 * 1024
    assert report["summary"] is None
    assert "256 KiB" in report["reason"]


def test_report_utf8_bom_and_non_ascii_text_preserve_design_check(tmp_path):
    report = tmp_path / "demo_timing_summary_routed.rpt"
    report.write_bytes("Design : other\nPath : D:/项目/实现\n".encode("utf-8-sig"))
    value, = read_reports({"directory": str(tmp_path), "top": "demo"})
    assert value["freshness"] == "mismatched"
    assert "D:/项目/实现" in value["text"]
    assert "\ufeff" not in value["text"]


def test_truncated_report_does_not_reinterpret_utf8_as_ansi(tmp_path):
    prefix = "Path : D:/项目\n".encode("utf-8")
    # 最后一个汉字在读取边界只读到首字节，前面的中文仍必须正确展示。
    data = prefix + b"x" * (256 * 1024 - len(prefix) - 1) + "中more".encode("utf-8")
    (tmp_path / "truncated.rpt").write_bytes(data)
    report, = read_reports({"directory": str(tmp_path)})
    assert report["text"].startswith("Path : D:/项目\n")
    assert "\ufffd" not in report["text"]
    assert report["summary"] is None


@pytest.mark.skipif(not shutil.which("tclsh"), reason="需要 Tcl 解释器验证脚本语法")
def test_tcl_read_only_query_bounds_log_and_handles_real_file_errors(tmp_path):
    log = tmp_path / "runme.log"
    log.write_text("old line\n" * 10000 + "Starting Routing\nWARNING: sample\n", encoding="utf-8")
    begin = tmp_path / ".vivado.begin.rst"
    begin.touch()
    os.utime(begin, (123, 123))
    # Tcl mock 仅替代 Vivado get_*；真实执行模板/文件 I/O/协议标记。
    stub = """
proc get_runs {args} {return impl_1}
proc current_project {} {return demo}
proc get_filesets {args} {return sources_1}
proc version {args} {return mock-tcl}
proc get_property {prop object} {
    switch -- $prop {
        STATUS {return {route_design Running}}
        PROGRESS {return {37.5%}}
        DIRECTORY {return $::env(TEST_RUN_DIR)}
        NAME {return demo}
        TOP {return top}
        default {return {}}
    }
}
"""
    script = stub + QUERY_RUN_PROGRESS.format(run_name="impl_1", tail_n=2)
    result = subprocess.run(["tclsh"], input=script, text=True, capture_output=True,
                            env={**os.environ, "TEST_RUN_DIR": str(tmp_path)}, check=True)
    assert result.stderr == ""
    value = parse_snapshot(result.stdout, "impl_1", "route_design")
    assert value["log_offset"] > 0
    assert value["total_lines"] < 10000
    assert len(value["tail"]) == 2
    assert value["progress_percent"] == 37.5
    assert value["run_started"] == 123
    assert len(result.stdout) < 4096


@pytest.mark.asyncio
async def test_mcp_and_view_use_same_cached_snapshot_even_after_disconnect():
    import json
    from unittest.mock import patch

    from vivado_mcp.tools.monitor_tools import get_run_snapshot

    source = session()
    monitor = RunMonitor(source, "impl_1", "route_design")
    await monitor.sample()
    registry = MonitorRegistry()
    # SimpleNamespace 不可哈希，此处用真实对象模拟 Session 实例身份。
    class Source:
        session_id = "test"
    registry._monitors[(Source(), "impl_1", "route_design")] = monitor
    ctx = SimpleNamespace(request_context=SimpleNamespace(
        lifespan_context=SimpleNamespace(run_monitors=registry)))
    source.is_alive = False
    await monitor.sample()
    with patch("vivado_mcp.tools.monitor_tools._require_session", return_value=None):
        response = await get_run_snapshot(session_id="test", ctx=ctx)
    assert json.loads(response) == monitor.snapshot()
    await registry.close()
    source.stop.assert_not_called()


@pytest.mark.asyncio
async def test_attach_observer_shutdown_preserves_existing_pid_files(tmp_path, monkeypatch):
    from vivado_mcp.vivado.gui_session import GuiSession

    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "vivado_pid123.str"
    marker.write_text("user-owned", encoding="utf-8")
    observer = GuiSession("", session_id="observer", port=9999, attach_only=True)
    await observer.stop()
    assert marker.read_text(encoding="utf-8") == "user-owned"
