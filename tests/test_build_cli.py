"""统一入口复用清单与原有运行器，测试不需要 EDA 或 SSH。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from vivado_mcp import build_cli
from vivado_mcp.remote_build import core


@pytest.fixture
def job(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "run.tcl").write_text("puts $argv\n")
    (root / "data.txt").write_text("input\n")
    data = {
        "schema_version": 1,
        "name": "demo",
        "vivado_version": "2024.2",
        "entry": "run.tcl",
        "inputs": ["data.txt"],
        "arguments": ["", "literal $value [command] {braces}", "中文"],
        "required_outputs": ["result.json"],
    }
    manifest = root / "job.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return manifest, data


def arguments(manifest, workspace, *extra):
    import argparse

    parser = argparse.ArgumentParser()
    build_cli.add_arguments(parser)
    return parser.parse_args(
        ["--target", "local", "--manifest", str(manifest), "--workspace", str(workspace), *extra]
    )


def invoke(*args):
    return subprocess.run(
        [sys.executable, "-m", "vivado_mcp", "build", *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def test_local_manifest_preserves_layout_arguments_environment_and_outputs(
    job, tmp_path, monkeypatch
):
    manifest, data = job
    output = tmp_path / "result"
    args = arguments(manifest, output, "--jobs", "3")
    monkeypatch.setattr(core, "load_config", Mock(side_effect=AssertionError("不能读取 host")))
    parent_env = os.environ.copy()

    def run(tcl, workspace, log, vivado_path, tclargs, *, env):
        work = output / "work"
        assert Path(tcl) == work / "run.tcl"
        assert Path(workspace) == work
        assert (work / "data.txt").read_text() == "input\n"
        assert tclargs == data["arguments"]
        assert env["FPGA_REMOTE_WORK_DIR"] == str(work)
        assert env["FPGA_REMOTE_LOG_DIR"] == str(output / "logs")
        assert env["FPGA_REMOTE_JOBS"] == "3"
        assert env["FPGA_REMOTE_FLOW"] == "tcl"
        (Path(env["FPGA_REMOTE_ARTIFACT_DIR"]) / "result.json").write_text("{}")
        return 0

    monkeypatch.setattr(build_cli, "run_local_batch", run)
    assert build_cli.command_build(args) == 0
    assert os.environ == parent_env
    assert manifest.read_text(encoding="utf-8") == json.dumps(data)
    summary = json.loads((output / "local-build-summary.json").read_text())
    assert summary["quality_status"] == "NOT_EVALUATED"
    assert summary["files"][0]["name"] == "result.json"


def test_cli_runs_local_batch_with_a_fake_executable(job, tmp_path):
    manifest, data = job
    executable = tmp_path / "fake_vivado"
    executable.write_text(
        f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
root = Path(os.environ['FPGA_REMOTE_ARTIFACT_DIR'])
(root / 'result.json').write_text(json.dumps(sys.argv[sys.argv.index('-tclargs')+1:]))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    result = invoke(
        "--target",
        "local",
        "--manifest",
        manifest,
        "--workspace",
        tmp_path / "result",
        "--vivado-path",
        executable,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "result/artifacts/result.json").read_text()) == data["arguments"]


@pytest.mark.parametrize("code,expected", [(7, 7), (0, 2)])
def test_local_failure_and_missing_output_are_failures(job, tmp_path, monkeypatch, code, expected):
    manifest, _ = job
    monkeypatch.setattr(build_cli, "run_local_batch", Mock(return_value=code))
    assert build_cli.command_build(arguments(manifest, tmp_path / "result")) == expected
    assert not (tmp_path / "result/local-build-summary.json").exists()


def test_existing_workspace_is_preserved(job, tmp_path, monkeypatch):
    manifest, _ = job
    output = tmp_path / "result"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep")
    run = Mock(side_effect=AssertionError("不能启动"))
    monkeypatch.setattr(build_cli, "run_local_batch", run)
    assert build_cli.command_build(arguments(manifest, output)) == 2
    assert marker.read_text() == "keep"
    run.assert_not_called()


def test_remote_dispatch_keeps_run_tcl_parameters(job, tmp_path, monkeypatch):
    manifest, _ = job
    args = arguments(manifest, "task-a", "--output", str(tmp_path / "out"), "--jobs", "2")
    args.target = "example"
    args.config = tmp_path / "hosts.json"
    args.remote_cleanup = "trash"
    run = Mock(return_value=13)
    monkeypatch.setattr(core, "main", run)
    assert build_cli.command_build(args) == 13
    forwarded = core.build_parser().parse_args(run.call_args.args[0])
    assert forwarded.command == "run-tcl"
    assert forwarded.host == "example"
    assert forwarded.workspace == "task-a"
    assert forwarded.manifest == manifest
    assert forwarded.config == args.config
    assert forwarded.jobs == 2
    assert forwarded.remote_cleanup == "trash"


@pytest.mark.parametrize("target", ["local", "example"])
def test_cli_dry_run_needs_no_vivado_or_host_config_and_creates_nothing(job, tmp_path, target):
    manifest, data = job
    output = tmp_path / "result" if target == "local" else "task-a"
    result = invoke("--target", target, "--manifest", manifest, "--workspace", output, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["arguments"] == data["arguments"]
    assert not (tmp_path / "result").exists()
