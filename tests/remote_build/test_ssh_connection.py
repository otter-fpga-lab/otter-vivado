"""SSH 建连选项与诊断使用进程替身，不连接网络。"""

import json
import subprocess
from unittest.mock import Mock

import pytest

from vivado_mcp.remote_build import core


@pytest.fixture
def profile():
    config = core.SOURCE_ROOT / "config/remote-hosts.example.json"
    return json.loads(config.read_text(encoding="utf-8"))["hosts"]["example"]


def test_default_ssh_options_preserve_existing_behavior():
    assert core.ssh_options({}) == ["-o", "BatchMode=yes"]
    assert core.ssh_options({}, lease=True) == [
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"
    ]


@pytest.mark.parametrize("key,value", [
    ("connect_timeout", 0), ("connect_timeout", True), ("connection_attempts", -1),
    ("connection_attempts", "3"), ("ssh_port", 65536), ("ssh_port", None),
])
def test_invalid_connection_settings_are_rejected(profile, key, value):
    profile[key] = value
    with pytest.raises(core.CliError, match=key):
        core.validate_host_profile("example", profile)


def test_options_apply_to_ssh_scp_and_lease(profile, monkeypatch, tmp_path):
    profile.update(connect_timeout=7, connection_attempts=3, ssh_port=2222)
    core.validate_host_profile("example", profile)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
    monkeypatch.setattr(core, "run_process", run)
    monkeypatch.setattr(core.shutil, "which", lambda name: name)
    assert core.ssh_bash(profile, "echo ok") == "ok"
    core.scp_upload(profile, [tmp_path / "input"], "/remote")
    core.scp_download(profile, "/remote/output", tmp_path / "output")
    for call in run.call_args_list:
        command = call.args[0]
        for option in ("ConnectTimeout=7", "ConnectionAttempts=3", "Port=2222"):
            assert option in command
    assert core.ssh_options(profile, lease=True) == core.ssh_options(profile)
    assert run.call_args_list[0].kwargs["timeout"] == 60


@pytest.mark.parametrize("failure", ["refused", "timeout"])
def test_chinese_connection_error_has_host_port_and_advice(profile, monkeypatch, failure):
    profile.update(ssh_port=2222, connection_attempts=3)
    run = Mock()
    if failure == "timeout":
        run.side_effect = subprocess.TimeoutExpired(["ssh"], 60)
    else:
        run.return_value = subprocess.CompletedProcess([], 255, "", "Connection refused")
    monkeypatch.setattr(core.subprocess, "run", run)
    monkeypatch.setattr(core.shutil, "which", lambda name: name)
    with pytest.raises(core.CliError) as caught:
        core.ssh_bash(profile, "create-build")
    message = str(caught.value)
    assert "example-build-host" in message
    assert "2222" in message
    assert "SSH" in message and "请检查" in message
    assert "connection_attempts" in message
    # 建连重试由 OpenSSH 完成，Python 绝不重放远端脚本。
    run.assert_called_once()


def test_remote_script_failure_is_not_retried_or_called_connection_failure(profile, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 2, "", "script error"))
    monkeypatch.setattr(core, "run_process", run)
    monkeypatch.setattr(core.shutil, "which", lambda name: name)
    with pytest.raises(core.CliError, match="script error") as caught:
        core.ssh_bash(profile, "exit 2")
    assert "SSH 连接失败" not in str(caught.value)
    run.assert_called_once()


def test_follow_connection_failure_has_chinese_diagnostic(profile, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda name: name)
    monkeypatch.setattr(core, "run_process", Mock(
        return_value=subprocess.CompletedProcess([], 255, None, None)
    ))
    args = core.build_parser().parse_args([
        "logs", "example/demo/20261020T120000", "--follow"
    ])
    with pytest.raises(core.CliError, match="主机 example-build-host.*22"):
        core.command_logs(args, {"hosts": {"example": profile}})


def test_workspace_connection_failure_has_chinese_diagnostic(profile, monkeypatch):
    import io

    process = Mock()
    process.stdout = io.BytesIO(b"")
    process.stderr = io.BytesIO(b"Connection refused")
    process.stdin = io.BytesIO()
    process.wait.return_value = 255
    monkeypatch.setattr(core.subprocess, "Popen", Mock(return_value=process))
    with pytest.raises(core.CliError, match="SSH 连接失败"):
        with core.workspace_lease(profile, "demo"):
            pytest.fail("连接失败时不能进入工作区")
