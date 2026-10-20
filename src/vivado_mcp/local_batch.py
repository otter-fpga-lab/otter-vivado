"""本地批处理入口：沿用插件的 Vivado 查找逻辑，同步运行 vivado -mode batch -source。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from vivado_mcp.config import find_vivado


def build_command(vivado: str, tcl: Path, log: Path | None, tclargs: list[str]) -> list[str]:
    command = [vivado, "-mode", "batch", "-nojournal", "-notrace"]
    command += ["-log", str(log)] if log else ["-nolog"]
    command += ["-source", str(tcl)]
    if tclargs:
        command += ["-tclargs", *tclargs]
    return command


def run_local_batch(
    tcl: str,
    workspace: str | None = None,
    log: str | None = None,
    vivado_path: str | None = None,
    tclargs: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> int:
    script = Path(tcl).resolve()
    if not script.is_file():
        raise FileNotFoundError(f"Tcl 脚本不存在: {script}")
    cwd = Path(workspace).resolve() if workspace else Path.cwd()
    cwd.mkdir(parents=True, exist_ok=True)
    log_path = Path(log).resolve() if log else None
    command = build_command(find_vivado(vivado_path), script, log_path, list(tclargs or []))
    # Windows 下 .bat 需经 shell 解释；参数仍按列表传递。
    options = {"env": env} if env is not None else {}
    return subprocess.run(
        command, cwd=cwd, shell=command[0].lower().endswith(".bat"), **options
    ).returncode
