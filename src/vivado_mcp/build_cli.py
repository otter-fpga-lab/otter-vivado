"""通过已有 Tcl 清单选择本地 batch 或远程 run-tcl。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from vivado_mcp.local_batch import run_local_batch
from vivado_mcp.remote_build import core


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """统一入口只增加可选命令，不修改已有 remote 参数。"""
    parser.add_argument("--target", required=True, help="local 或远程 host 配置名。")
    parser.add_argument("--manifest", required=True, type=Path, help="原生 Tcl job.json。")
    parser.add_argument("--workspace", required=True, help="本地新结果目录或远程工作区名称。")
    parser.add_argument("--vivado-path", help="本地 Vivado 可执行文件。")
    parser.add_argument("--config", type=Path, help="远程 host 配置，默认沿用 remote。")
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--output", help="远程交付目录。")
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=14400, help="远程等待任务的总超时秒数。")
    parser.add_argument("--remote-cleanup", choices=("keep", "trash", "purge"), default="keep")
    parser.add_argument(
        "--dry-run", action="store_true", help="只检查清单并输出计划，不创建文件或连接。"
    )


def remote_arguments(args: argparse.Namespace) -> list[str]:
    """让远程路径继续经过原有解析器、锁、执行和交付流程。"""
    command = ["--config", str(args.config)] if args.config else []
    command += [
        "run-tcl",
        "--host",
        args.target,
        "--workspace",
        args.workspace,
        "--manifest",
        str(args.manifest),
        "--poll-seconds",
        str(args.poll_seconds),
        "--timeout",
        str(args.timeout),
        "--remote-cleanup",
        args.remote_cleanup,
    ]
    if args.jobs is not None:
        command += ["--jobs", str(args.jobs)]
    if args.output:
        command += ["--output", args.output]
    return command


def run_local(args: argparse.Namespace, info: core.TclJobInfo) -> int:
    """复制明确输入，保留与远端相同的工作目录和输出环境。"""
    root = Path(args.workspace).expanduser().resolve()
    if root.exists():
        raise core.CliError(f"本地结果目录已存在，请选择新目录：{root}")
    root.mkdir(parents=True)
    work, artifacts, logs = (root / name for name in ("work", "artifacts", "logs"))
    for directory in (work, artifacts, logs):
        directory.mkdir()
    for source in info.files:
        destination = work / source.relative_to(info.root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    shutil.copyfile(info.manifest, root / "native-job.json")
    env = os.environ.copy()
    env.update(
        {
            "FPGA_REMOTE_WORK_DIR": str(work),
            "FPGA_REMOTE_ARTIFACT_DIR": str(artifacts),
            "FPGA_REMOTE_LOG_DIR": str(logs),
            "FPGA_REMOTE_JOBS": str(args.jobs or 1),
            "FPGA_REMOTE_FLOW": "tcl",
        }
    )
    result = run_local_batch(
        str(work / info.entry),
        str(work),
        str(logs / "vivado.log"),
        args.vivado_path,
        list(info.arguments),
        env=env,
    )
    if result != 0:
        return result
    summary = core.summarize_tcl_artifacts(root, info, require_success=True)
    summary.update(flow="tcl", target="local", result="SUCCESS")
    (root / "local-build-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"RUN_OUTPUT={root}")
    print("RUN_QUALITY=NOT_EVALUATED")
    print("RUN_RESULT=SUCCESS")
    return 0


def command_build(args: argparse.Namespace) -> int:
    """离线预检与目标分派；本地路径完全不读取 host 配置。"""
    try:
        if args.jobs is not None and args.jobs < 1:
            raise core.CliError("--jobs 必须为正整数")
        if args.target == "local":
            if args.output or args.config or args.remote_cleanup != "keep":
                raise core.CliError("本地构建不接受 --config/--output/--remote-cleanup")
        else:
            if args.vivado_path:
                raise core.CliError("--vivado-path 只用于本地构建")
            if not core.HOST_NAME_RE.fullmatch(args.target):
                raise core.CliError("--target 必须为 local 或合法的 host 配置名")
            core.require_workspace(args.workspace)
        info = core.inspect_tcl_job(args.manifest)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "target": args.target,
                        "workspace": args.workspace,
                        "entry": info.entry,
                        "arguments": list(info.arguments),
                        "inputs": [p.relative_to(info.root).as_posix() for p in info.files],
                        "required_outputs": list(info.required_outputs),
                        "remote_argv": remote_arguments(args) if args.target != "local" else None,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.target == "local":
            return run_local(args, info)
        return core.main(remote_arguments(args))
    except (core.CliError, OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
