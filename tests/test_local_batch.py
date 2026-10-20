from pathlib import Path

from vivado_mcp.local_batch import build_command


def test_build_command_with_log_and_tclargs():
    command = build_command("vivado", Path("run.tcl"), Path("v.log"), ["synth", "work"])
    assert command[:5] == ["vivado", "-mode", "batch", "-nojournal", "-notrace"]
    assert command[5:7] == ["-log", "v.log"]
    assert command[-3:] == ["-tclargs", "synth", "work"]


def test_build_command_without_log_or_args():
    command = build_command("vivado", Path("run.tcl"), None, [])
    assert "-nolog" in command
    assert "-tclargs" not in command


def test_non_ascii_paths_fail_before_starting_or_creating_workspace(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import pytest

    from vivado_mcp import local_batch

    script = tmp_path / "run.tcl"
    script.write_text("puts ok")
    workspace = tmp_path / "新工作目录"
    log = tmp_path / "日志.log"
    run = Mock(side_effect=AssertionError("不能启动 Vivado"))
    monkeypatch.setattr(local_batch.subprocess, "run", run)
    with pytest.raises(ValueError) as caught:
        local_batch.run_local_batch(str(script), str(workspace), str(log))
    assert "工作目录" in str(caught.value)
    assert "日志路径" in str(caught.value)
    assert "ASCII" in str(caught.value)
    assert not workspace.exists()
    run.assert_not_called()


def test_non_ascii_script_is_reported_even_with_ascii_workspace(tmp_path):
    import pytest

    from vivado_mcp.local_batch import run_local_batch

    script = tmp_path / "脚本.tcl"
    script.write_text("puts ok")
    with pytest.raises(ValueError, match="Tcl 脚本"):
        run_local_batch(str(script), str(tmp_path / "work"))


def test_implicit_current_directory_and_resolved_symlinks_are_checked(tmp_path, monkeypatch):
    import pytest

    from vivado_mcp.local_batch import run_local_batch

    workspace = tmp_path / "中文"
    workspace.mkdir()
    script = tmp_path / "run.tcl"
    script.write_text("puts ok")
    monkeypatch.chdir(workspace)
    with pytest.raises(ValueError, match="工作目录"):
        run_local_batch(str(script))
    link = tmp_path / "ascii"
    try:
        link.symlink_to(workspace, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统账户无创建符号链接权限")
    with pytest.raises(ValueError, match="工作目录"):
        run_local_batch(str(script), str(link))


def test_ascii_paths_and_literal_arguments_keep_existing_execution(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from vivado_mcp import local_batch

    script = tmp_path / "run script.tcl"
    script.write_text("puts $argv")
    run = Mock(return_value=Mock(returncode=9))
    monkeypatch.setattr(local_batch, "find_vivado", lambda path: "vivado")
    monkeypatch.setattr(local_batch.subprocess, "run", run)
    assert (
        local_batch.run_local_batch(
            str(script), str(tmp_path / "work"), tclargs=["中文", "$literal", ""]
        )
        == 9
    )
    assert run.call_args.args[0][-3:] == ["中文", "$literal", ""]


def test_cli_non_ascii_path_returns_nonzero_without_traceback(tmp_path):
    import subprocess
    import sys

    script = tmp_path / "脚本.tcl"
    script.write_text("puts ok")
    result = subprocess.run(
        [sys.executable, "-m", "vivado_mcp", "local-batch", "--tcl", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 2
    assert "非 ASCII" in result.stderr
    assert "Traceback" not in result.stderr
