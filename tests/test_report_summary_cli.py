"""离线摘要保留未知状态、阶段与来源，不依赖 Vivado 或远端配置。"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from vivado_mcp.analysis.report_summary import summarize_directory, timing_snapshots
from vivado_mcp.remote_build import core
from vivado_mcp.report_summary_cli import command_report_summary

FIXTURES = Path(__file__).parent / "fixtures"


def test_reuses_timing_utilization_and_warning_parsers(tmp_path):
    timing = (FIXTURES / "sample_report_timing.txt").read_text()
    (tmp_path / "timing.rpt").write_text("| Design State : Routed\n" + timing)
    utilization = (FIXTURES / "sample_report_utilization.txt").read_text()
    (tmp_path / "utilization.rpt").write_text(utilization)
    (tmp_path / "vivado.log").write_text(
        "CRITICAL WARNING: [Timing 38-282] no clock found\n"
        "CRITICAL WARNING: [Timing 38-282] no clock found\n"
        "CRITICAL WARNING: message without identifier\n"
    )
    result = summarize_directory(tmp_path)
    files = {item["path"]: item for item in result["files"]}
    snapshots = files["timing.rpt"]["timing"]
    assert len(snapshots) == 1  # 不把 Intra Clock Table 误当整体摘要。
    assert snapshots[0]["wns_ns"] == 0.234
    assert snapshots[0]["tns_ns"] == 0
    assert snapshots[0]["whs_ns"] == 0.045
    assert snapshots[0]["ths_ns"] == 0
    assert snapshots[0]["source_stage"] == "post-route"
    resources = files["utilization.rpt"]["utilization"]
    assert resources["lut"]["used"] == 1440
    assert resources["ff"]["used"] == 2200
    assert resources["bram"]["used"] > 0
    assert resources["dsp"]["used"] > 0
    assert result["critical_warning_count"] == 3
    assert result["critical_warnings"][0]["count"] == 2
    assert result["critical_warnings"][0]["files"] == ["vivado.log"]
    assert result["critical_warnings"][1]["warning_id"] == "UNKNOWN"


def test_missing_na_malformed_and_fractional_resources_are_explicit(tmp_path):
    header = next(
        line
        for line in (FIXTURES / "sample_report_timing.txt").read_text().splitlines()
        if line.lstrip().startswith("WNS(ns)")
    )
    (tmp_path / "na.rpt").write_text(header + "\n---\nNA NA 0 0 NA NA 0 0\n")
    (tmp_path / "unknown.rpt").write_text(header + "\n---\nunsupported\n")
    (tmp_path / "empty.log").write_bytes(b"\xff\n")
    (tmp_path / "ram.rpt").write_text(
        "| Site Type | Used | Fixed | Available | Util% |\n| Block RAM Tile | 0.5 | 0 | 50 | 1 |\n"
    )
    files = {item["path"]: item for item in summarize_directory(tmp_path)["files"]}
    assert files["na.rpt"]["timing"][0]["parse_status"] == "no_timing_data"
    assert files["na.rpt"]["timing"][0]["wns_ns"] is None
    assert files["unknown.rpt"]["timing"][0]["parse_status"] == "unrecognized"
    assert files["unknown.rpt"]["timing"][0]["whs_ns"] is None
    assert files["empty.log"]["timing"] == []
    assert all(value is None for value in files["empty.log"]["utilization"].values())
    assert files["ram.rpt"]["utilization"]["bram"]["used"] == 0.5


def test_multiple_stages_and_log_progress_are_not_merged():
    fixture = (FIXTURES / "sample_report_timing.txt").read_text().splitlines()
    index = next(i for i, line in enumerate(fixture) if line.lstrip().startswith("WNS(ns)"))
    table = "\n".join(fixture[index : index + 3])
    snapshots = timing_snapshots(
        "| Design State : Synthesized\n" + table + "\n"
        "| Design State : Routed\n" + table.replace("0.234", "-0.500") + "\n"
        "Phase complete WNS=-0.7 TNS=-3.2 WHS=0.1 THS=0\n"
        "WNS=NA TNS=-4\n"
    )
    assert len(snapshots) == 4
    assert snapshots[0]["source_stage"] == "post-synth"
    assert snapshots[0]["wns_ns"] == 0.234
    assert snapshots[1]["source_stage"] == "post-route"
    assert snapshots[1]["wns_ns"] == -0.5
    assert snapshots[2]["wns_ns"] == -0.7
    assert snapshots[2]["source_stage"] == "unknown"
    assert snapshots[3]["parse_status"] == "partial"
    assert snapshots[3]["wns_ns"] is None
    assert snapshots[3]["whs_ns"] is None


@pytest.mark.parametrize("json_output", [False, True])
def test_offline_command_never_connects_and_keeps_inputs(
    tmp_path, monkeypatch, capsys, json_output
):
    log = tmp_path / "vivado.log"
    log.write_text("WNS=-1 TNS=-2 WHS=0.1 THS=0\n")
    before = log.read_bytes()
    for name in ("load_config", "ssh_bash", "scp_upload", "scp_download"):
        monkeypatch.setattr(core, name, Mock(side_effect=AssertionError("必须离线")))
    assert command_report_summary(str(tmp_path), json_output=json_output) == 0
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(output)["files"][0]["timing"][0]["wns_ns"] == -1
    else:
        assert "WNS=-1.0 ns" in output
        assert "Critical Warning" in output
    assert log.read_bytes() == before
    assert list(tmp_path.iterdir()) == [log]


def test_nested_case_insensitive_files_and_links(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    (nested / "RUN.LOG").write_text("CRITICAL WARNING: [Timing 38-282] no clock\n")
    outside = tmp_path / "outside.rpt"
    outside.write_text("private data")
    (root / "linked.rpt").symlink_to(outside)
    result = summarize_directory(root)
    assert [item["path"] for item in result["files"]] == ["nested/RUN.LOG"]
    assert result["skipped_files"][0]["path"] == "linked.rpt"


def test_root_cli_json_and_invalid_directory_exit_codes(tmp_path):
    (tmp_path / "empty.log").write_text("")
    result = subprocess.run(
        [sys.executable, "-m", "vivado_mcp", "report-summary", "--dir", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["critical_warning_count"] == 0
    assert command_report_summary(str(tmp_path / "missing")) == 2
