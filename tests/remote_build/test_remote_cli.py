"""CLI integration checks use local fixtures and reject every network entry."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vivado_mcp.remote_build import core


def invoke(*args, config):
    env = os.environ.copy()
    env["OTTER_VIVADO_REMOTE_CONFIG"] = str(config)
    return subprocess.run(
        [sys.executable, "-m", "vivado_mcp", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=20,
    )


def guarded_offline(*args, config):
    script = """
import sys
from unittest.mock import patch
from vivado_mcp import __main__ as cli
from vivado_mcp.remote_build import core
sys.argv = ['vivado-mcp', *sys.argv[1:]]
with (
    patch.object(core, 'load_config', side_effect=AssertionError('No host config read')),
    patch.object(core, 'ssh_bash', side_effect=AssertionError('No SSH')),
    patch.object(core, 'scp_upload', side_effect=AssertionError('No upload')),
    patch.object(core, 'scp_download', side_effect=AssertionError('No download')),
):
    cli.main()
"""
    env = os.environ.copy()
    env["OTTER_VIVADO_REMOTE_CONFIG"] = str(config)
    return subprocess.run(
        [sys.executable, "-c", script, "remote", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=20,
    )


def test_root_cli_keeps_version_and_lists_remote(tmp_path):
    missing = tmp_path / "missing.json"
    assert "remote" in invoke("--help", config=missing).stdout
    assert invoke("version", config=missing).returncode == 0
    result = invoke("remote", "--help", config=missing)
    assert result.returncode == 0, result.stderr
    assert "run-tcl" in result.stdout
    assert "workspace-clean" in result.stdout


def test_missing_host_config_stops_before_network(tmp_path):
    result = invoke("remote", "hosts", config=tmp_path / "missing.json")
    assert result.returncode == 1
    assert "OTTER_VIVADO_REMOTE_CONFIG" in result.stderr
    assert "remote-hosts.example.json" in result.stderr


def test_explicit_config_overrides_environment(tmp_path, monkeypatch):
    selected = tmp_path / "explicit.json"
    monkeypatch.setenv("OTTER_VIVADO_REMOTE_CONFIG", str(tmp_path / "environment.json"))
    assert core.build_parser().parse_args(["hosts"]).config == tmp_path / "environment.json"
    assert core.build_parser().parse_args(["--config", str(selected), "hosts"]).config == selected
    monkeypatch.delenv("OTTER_VIVADO_REMOTE_CONFIG")
    assert core.build_parser().parse_args(["hosts"]).config == core.DEFAULT_CONFIG
    assert core.DEFAULT_CONFIG.name == "remote-hosts.local.json"


def test_offline_report_never_reads_config_or_connects(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    report = artifacts / "fixture.rpt"
    report.write_text(
        "| Command : report_cdc -file fixture.rpt\n| Design : top\n"
        "| Design State : Routed\nunrecognized fixture format\n",
        encoding="utf-8",
    )
    original = report.read_bytes()
    result = guarded_offline("report", "--results", str(tmp_path), config=tmp_path / "missing.json")
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["reports"]["fixture.rpt"]["metadata"]["design_state"] == "Routed"
    assert report.read_bytes() == original


def test_offline_inspect_never_reads_config_or_connects(tmp_path):
    source = tmp_path / "demo.srcs/sources_1/new/top.v"
    source.parent.mkdir(parents=True)
    source.write_text("module top; endmodule\n", encoding="utf-8")
    project = tmp_path / "demo.xpr"
    project.write_text(
        "<!-- Product Version: Vivado v2024.2 -->\n"
        '<Project><Configuration><Option Name="Part" Val="fake-part"/></Configuration>'
        '<FileSets><FileSet Name="sources_1">'
        '<File Path="$PSRCDIR/sources_1/new/top.v"/>'
        '<Config><Option Name="TopModule" Val="top"/></Config>'
        "</FileSet></FileSets></Project>",
        encoding="utf-8",
    )
    result = guarded_offline(
        "inspect", "--project", str(project), "--json", config=tmp_path / "missing.json"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["top"] == "top"
    assert source.read_text(encoding="utf-8") == "module top; endmodule\n"


def test_assets_are_package_relative_and_lf():
    for name in ("run_vivado_build.sh", "vivado_project_build.tcl"):
        data = (core.ASSET_ROOT / name).read_bytes()
        assert data
        assert b"\r" not in data
    assert core.ASSET_ROOT == Path(core.__file__).resolve().parent / "assets"


def test_archive_extraction_without_data_filter_stops_before_transfer(tmp_path, monkeypatch):
    monkeypatch.delattr(core.tarfile, "data_filter")

    def unexpected_transfer(*args, **kwargs):
        raise AssertionError("Unsupported extraction must stop before download")

    monkeypatch.setattr(core, "scp_download", unexpected_transfer)
    handle = core.BuildHandle("example", "demo", "20260908T120000", "test")
    with pytest.raises(core.CliError, match="tarfile.data_filter"):
        core.fetch_remote_build({"work_root": "/srv/builds"}, handle, output=tmp_path, extract=True)
    assert not list(tmp_path.iterdir())
