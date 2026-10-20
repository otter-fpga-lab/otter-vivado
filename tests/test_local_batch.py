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