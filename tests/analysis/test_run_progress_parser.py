"""run_progress_parser 单元测试。"""

import time

import pytest

from vivado_mcp.analysis.run_progress_parser import (
    RunProgress,
    format_run_progress,
    parse_run_progress,
    progress_percent,
    run_state,
)

# 模拟 impl_1 正在运行时 QUERY_RUN_PROGRESS 的输出
RUNNING_SAMPLE = """\
VMCP_RUN:status=route_design Running
VMCP_RUN:progress=60%
VMCP_RUN:dir=C:/proj/basys3.runs/impl_1
VMCP_RUN:log_exists=1
VMCP_RUN:log_size=2048576
VMCP_RUN:log_mtime=1729123456
VMCP_RUN:total_lines=1234
VMCP_RUN_PHASE:120|Starting Placer
VMCP_RUN_PHASE:256|Phase 1 Placer Initialization
VMCP_RUN_PHASE:312|Phase 2 Global Placement
VMCP_RUN_PHASE:389|Phase 3 Detail Placement
VMCP_RUN_PHASE:421|Starting Routing
VMCP_RUN_PHASE:456|Phase 1 Build RT Design
VMCP_RUN_TAIL:1220|Phase 2 Router Initialization
VMCP_RUN_TAIL:1221|Elapsed: 00:05:43
VMCP_RUN_TAIL:1222|INFO: [Route 35-5] Router utilization...
VMCP_RUN_DONE
"""

COMPLETE_SAMPLE = """\
VMCP_RUN:status=write_bitstream Complete!
VMCP_RUN:progress=100%
VMCP_RUN:dir=C:/proj/basys3.runs/impl_1
VMCP_RUN:log_exists=1
VMCP_RUN:log_size=3145728
VMCP_RUN:log_mtime=1729120000
VMCP_RUN:total_lines=2000
VMCP_RUN_PHASE:100|Starting Placer
VMCP_RUN_PHASE:500|Starting Routing
VMCP_RUN_PHASE:1800|Finished Writing Bitstream
VMCP_RUN_DONE
"""

ERROR_SAMPLE = """\
VMCP_RUN:status=place_design ERROR
VMCP_RUN:progress=30%
VMCP_RUN:dir=C:/proj/basys3.runs/impl_1
VMCP_RUN:log_exists=1
VMCP_RUN:log_size=512000
VMCP_RUN:log_mtime=1729123000
VMCP_RUN:total_lines=400
VMCP_RUN_PHASE:120|Starting Placer
VMCP_RUN_PHASE:390|ERROR: [Place 30-58] IO pin constraint conflict
VMCP_RUN_DONE
"""

NOT_FOUND_SAMPLE = "VMCP_RUN_ERROR:run 'impl_42' not found\n"


def test_parses_running_status():
    rp = parse_run_progress(RUNNING_SAMPLE, run_name="impl_1")
    assert rp.found is True
    assert rp.is_running() is True
    assert rp.is_complete() is False
    assert rp.progress == "60%"
    assert rp.log_exists is True
    assert rp.log_size == 2048576
    assert rp.total_lines == 1234


def test_parses_phases_and_tail():
    rp = parse_run_progress(RUNNING_SAMPLE)
    assert len(rp.phases) == 6
    assert rp.phases[0].lineno == 120
    assert rp.phases[-1].text == "Phase 1 Build RT Design"
    assert len(rp.tail) == 3
    assert "Router Initialization" in rp.tail[0].text


def test_current_phase_is_last():
    rp = parse_run_progress(RUNNING_SAMPLE)
    assert "Phase 1 Build RT Design" in rp.current_phase()


def test_complete_sample_detected():
    rp = parse_run_progress(COMPLETE_SAMPLE)
    assert rp.is_complete() is True
    assert rp.is_running() is False
    assert rp.is_error() is False


def test_error_sample_detected():
    rp = parse_run_progress(ERROR_SAMPLE)
    assert rp.is_error() is True
    assert rp.is_running() is False


def test_run_not_found():
    rp = parse_run_progress(NOT_FOUND_SAMPLE, run_name="impl_42")
    assert rp.found is False
    assert "impl_42" in rp.error


def test_format_shows_key_info():
    rp = parse_run_progress(RUNNING_SAMPLE, run_name="impl_1")
    text = format_run_progress(rp)
    assert "impl_1" in text
    assert "运行中" in text
    assert "60%" in text
    assert "Phase" in text


def test_format_error_path():
    rp = parse_run_progress(NOT_FOUND_SAMPLE, run_name="impl_42")
    text = format_run_progress(rp)
    assert "ERROR" in text
    assert "impl_42" in text


def test_elapsed_age_nonnegative_when_mtime_present():
    rp = RunProgress(log_mtime=int(time.time()) - 120)
    assert rp.elapsed_since_last_update() >= 119


def test_elapsed_age_minus_one_when_mtime_missing():
    rp = RunProgress()
    assert rp.elapsed_since_last_update() == -1


def test_empty_input_no_exception():
    rp = parse_run_progress("")
    assert rp.found is False
    assert rp.phases == []


@pytest.mark.parametrize(
    ("status", "target", "expected"),
    [
        ("synth_design Complete!", "synth_design", "completed"),
        ("place_design Complete!", None, "stage_complete"),
        ("place_design Complete!", "route_design", "stage_complete"),
        ("route_design Complete!", "write_bitstream", "stage_complete"),
        ("synth_design Complete!", "write_bitstream", "stage_complete"),
        ("write_bitstream Complete!", "route_design", "completed"),
        ("write_bitstream Complete!", "write_bitstream", "completed"),
        ("write_bitstream Complete! ERROR", "write_bitstream", "failed"),
        ("write_bitstream Failed", None, "failed"),
        ("write_bitstream Complete! Cancelled", None, "cancelled"),
        ("route_design Canceled", None, "cancelled"),
        ("route_design Aborted", None, "cancelled"),
        ("Queued", None, "running"),
        ("route_design Running", None, "running"),
        ("Not started", None, "idle"),
        ("Reset", None, "idle"),
        ("Complete!", "write_bitstream", "stage_complete"),
        ("write_bitstream Complete! (Needs refresh)", None, "unknown"),
        ("unknown", None, "unknown"),
        ("", None, "unknown"),
    ],
)
def test_run_state_is_target_aware(status, target, expected):
    assert run_state(status, target) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0%", 0.0),
        ("100%", 100.0),
        (" 12.5% ", 12.5),
        (".5 %", 0.5),
        ("", None),
        ("unknown", None),
        ("nan%", None),
        ("50", None),
        ("101%", None),
        ("-1%", None),
        ("50% extra", None),
    ],
)
def test_progress_percent_requires_actual_bounded_percent(raw, expected):
    assert progress_percent(raw) == expected


def test_snapshot_error_wins_over_later_success_fields():
    rp = parse_run_progress("VMCP_RUN_ERROR:read failed\n" + COMPLETE_SAMPLE)
    assert not rp.found
    assert rp.is_error()
    assert not rp.is_complete()
    assert "read failed" in format_run_progress(rp)


@pytest.mark.parametrize(
    "raw",
    [
        "VMCP_RUN:status=write_bitstream Complete!\nVMCP_RUN:progress=100%",
        "VMCP_RUN:progress=100%\nVMCP_RUN_DONE",
        "VMCP_RUN:log_exists=1",
    ],
)
def test_incomplete_snapshot_is_not_success(raw):
    rp = parse_run_progress(raw)
    assert not rp.found
    assert not rp.is_complete()
    assert "ERROR" in format_run_progress(rp)


def test_stage_complete_format_does_not_claim_run_completion():
    rp = parse_run_progress(COMPLETE_SAMPLE.replace("write_bitstream", "place_design"))
    assert not rp.is_complete()
    assert "阶段已完成" in format_run_progress(rp)


def test_error_status_has_priority_over_complete_keyword():
    rp = parse_run_progress(COMPLETE_SAMPLE.replace("Complete!", "Complete! ERROR"))
    assert rp.is_error()
    assert not rp.is_complete()
    assert "运行进度: 失败" in format_run_progress(rp)


def test_log_window_offset_and_relative_line_numbers_are_explicit():
    rp = parse_run_progress(RUNNING_SAMPLE + "VMCP_RUN:log_offset=1983040\n")
    assert rp.log_offset == 1983040
    text = format_run_progress(rp)
    assert "仅末尾 64 KiB" in text
    assert "起始字节 1983040" in text
    assert "窗口内 1234 行" in text
    assert "窗口内相对行号" in text
    assert "← 最近观测" in text
    assert "← 当前" not in text


@pytest.mark.parametrize("value", ["unknown", "-12"])
def test_invalid_log_window_offset_does_not_invent_a_window(value):
    rp = parse_run_progress(RUNNING_SAMPLE + f"VMCP_RUN:log_offset={value}\n")
    assert rp.log_offset == 0
    assert "仅末尾 64 KiB" not in format_run_progress(rp)
