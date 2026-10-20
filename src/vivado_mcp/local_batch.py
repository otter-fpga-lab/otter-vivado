"""本地批处理入口：沿用插件的 Vivado 查找逻辑，同步运行 vivado -mode batch -source。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from vivado_mcp.config import find_vivado


def validate_batch_paths(tcl: Path, workspace: Path, log: Path | None) -> None:
    """在创建输出或启动 Vivado 前拒绝可能触发 Tcl 内部崩溃的路径。"""
    paths = {"Tcl 脚本": tcl, "工作目录": workspace, "日志路径": log}
    problems = [
        f"{label}: {path.resolve()}"
        for label, path in paths.items()
        if path is not None and not str(path.resolve()).isascii()
    ]
    if problems:
        raise ValueError(
            "检测到非 ASCII 路径，已停止本地 Vivado 启动：\n"
            + "\n".join(problems)
            + "\nVivado 在中文/非 ASCII 路径下可能触发 TclStackFree 等内部错误。"
            "请使用纯 ASCII 脚本、工作目录和日志路径；也可将依赖写入 Tcl manifest，"
            "通过 build --target local --manifest <job.json> --workspace <新的ASCII目录>"
            "复制输入快照后运行。脚本内部的绝对路径不会被自动改写。"
        )


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
    cwd = Path(workspace).resolve() if workspace else Path.cwd().resolve()
    log_path = Path(log).resolve() if log else None
    validate_batch_paths(script, cwd, log_path)
    command = build_command(find_vivado(vivado_path), script, log_path, list(tclargs or []))
    cwd.mkdir(parents=True, exist_ok=True)
    # Windows 下 .bat 需经 shell 解释；参数仍按列表传递。
    options = {"env": env} if env is not None else {}
    return subprocess.run(
        command, cwd=cwd, shell=command[0].lower().endswith(".bat"), **options
    ).returncode
