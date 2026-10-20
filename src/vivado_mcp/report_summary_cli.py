"""离线日志/报告摘要命令，不加载主机配置或启动 Vivado。"""

import json
import sys
from pathlib import Path

from vivado_mcp.analysis.report_summary import format_summary, summarize_directory


def command_report_summary(directory: str, *, json_output: bool = False) -> int:
    """输出人可读或 JSON 摘要，路径错误返回非零。"""
    try:
        summary = summarize_directory(Path(directory))
    except (OSError, ValueError) as exc:
        print(f"[ERROR] 无法读取报告目录：{exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(summary, ensure_ascii=False, indent=2)
        if json_output
        else format_summary(summary)
    )
    return 0
