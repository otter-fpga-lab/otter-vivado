"""零 Token 后台只读观察 CLI 子命令（`vivado-mcp watch`）。

退出码契约：
  0  completed / reached
  1  参数无效或目标不存在
  2  failed / silent_exit / early_error
  3  stalled（确认卡死，未杀 Vivado）
  4  timeout（健康运行达到观察上限，未杀 Vivado）
  5  memory_critical（主机内存危急，未杀 Vivado）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

from vivado_mcp.vivado.tcl_utils import validate_identifier
from vivado_mcp.watch_engine import WATCH_UNTIL_CHOICES, watch_target_async


class _WatchArgumentParser(argparse.ArgumentParser):
    """确保参数错误以退出码 1 退出，避免与失败退出码 2 冲突。"""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


def add_watch_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """为 `watch` 子命令注册全部 CLI 参数。"""
    target_grp = parser.add_mutually_exclusive_group(required=False)
    target_grp.add_argument("--job", help="批处理 / OOC 任务 job_id。")
    target_grp.add_argument("--run-dir", help=".runs/<run_name> 运行目录路径。")
    target_grp.add_argument("--xpr", help="Vivado .xpr 工程文件路径。")

    parser.add_argument("--run", default="impl_1", help="配合 --xpr 使用的 run 名（默认 impl_1）。")
    parser.add_argument(
        "--until",
        default="complete",
        choices=WATCH_UNTIL_CHOICES,
        help="等待到达的目标步骤或 complete（默认 complete）。",
    )
    parser.add_argument(
        "--stall-seconds",
        default="auto",
        help="卡死阈值秒数，auto 按工程规模估算，0 关闭（默认 auto）。",
    )
    parser.add_argument(
        "--timeout",
        default="auto",
        help="观察超时秒数，auto 按工程规模估算，范围 1..3600（默认 auto）。",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=2,
        help="轮询间隔秒数，范围 1..60（默认 2）。",
    )
    parser.add_argument(
        "--no-fail-fast",
        action="store_true",
        help="禁用日志 ^ERROR: [ 早错提前唤醒。",
    )
    parser.add_argument(
        "--no-mem-guard",
        action="store_true",
        help="禁用主机内存危急提前唤醒。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出单行紧凑 JSON（<700 B，默认行为）。",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="输出人类可读多行摘要。",
    )
    return parser


def _parse_auto_or_int(raw: str, *, flag: str, allow_zero: bool) -> int | str:
    text = str(raw).strip().lower()
    if text == "auto":
        return "auto"
    try:
        val = int(text)
    except ValueError as exc:
        raise ValueError(f"{flag} 必须是 auto 或整数（收到 {raw!r}）") from exc
    if val < 0 or (not allow_zero and val == 0):
        limit = ">= 0" if allow_zero else ">= 1"
        raise ValueError(f"{flag} 必须 {limit}（收到 {val}）")
    return val


async def run_watch_cli_async(
    args: argparse.Namespace,
    *,
    jobs_dir: Path | None = None,
) -> int:
    """执行 `vivado-mcp watch` 并返回精确退出码 0/1/2/3/4/5。"""
    has_job = bool(getattr(args, "job", None) and str(args.job).strip())
    has_rdir = bool(getattr(args, "run_dir", None) and str(args.run_dir).strip())
    has_xpr = bool(getattr(args, "xpr", None) and str(args.xpr).strip())
    if int(has_job) + int(has_rdir) + int(has_xpr) != 1:
        err = {"error": "必须且只能指定 --job、--run-dir 或 --xpr 之一"}
        print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
        return 1

    try:
        run_name = validate_identifier(getattr(args, "run", "impl_1") or "impl_1", "run")
        stall_val = _parse_auto_or_int(
            getattr(args, "stall_seconds", "auto"), flag="--stall-seconds", allow_zero=True
        )
        timeout_val = _parse_auto_or_int(
            getattr(args, "timeout", "auto"), flag="--timeout", allow_zero=False
        )
        poll_val = int(getattr(args, "poll_seconds", 2))
        if poll_val < 0:
            raise ValueError(f"--poll-seconds 不能为负数（收到 {poll_val}）")
        result = await watch_target_async(
            job_id=args.job if has_job else None,
            run_dir=args.run_dir if has_rdir else None,
            xpr_path=args.xpr if has_xpr else None,
            run_name=run_name,
            until=getattr(args, "until", "complete"),
            stall_seconds=stall_val,
            timeout_seconds=timeout_val,
            poll_seconds=poll_val,
            fail_fast=not bool(getattr(args, "no_fail_fast", False)),
            mem_guard=not bool(getattr(args, "no_mem_guard", False)),
            jobs_dir=jobs_dir,
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        if getattr(args, "pretty", False):
            print(f"[ERROR] {exc}", file=sys.stderr)
        else:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    if getattr(args, "pretty", False):
        print(result.format_human())
    else:
        print(
            json.dumps(
                result.to_compact_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return int(result.exit_code)


def run_watch_cli(args: argparse.Namespace, *, jobs_dir: Path | None = None) -> int:
    """`run_watch_cli_async` 的同步入口。"""
    return asyncio.run(run_watch_cli_async(args, jobs_dir=jobs_dir))


def watch_main(argv: Sequence[str] | None = None, *, jobs_dir: Path | None = None) -> int:
    """独立解析并运行 `vivado-mcp watch` 参数。"""
    parser = _WatchArgumentParser(
        prog="vivado-mcp watch",
        description="零 Token 后台只读观察 Vivado 运行或批处理任务（退出码 0/2/3/4/5）。",
    )
    add_watch_arguments(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return run_watch_cli(args, jobs_dir=jobs_dir)
    except KeyboardInterrupt:
        return 1
