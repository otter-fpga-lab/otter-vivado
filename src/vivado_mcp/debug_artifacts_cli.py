"""离线调试产物核对 CLI，不连接 Vivado 或设备。"""

import json
from pathlib import Path

from vivado_mcp.analysis.debug_artifacts import check_debug_artifacts


def run_debug_artifacts_cli(args) -> int:
    """输出 JSON；0=离线一致，1=阻断/输入错误，2=检查证据不足。"""
    expected = None
    if args.expected:
        with Path(args.expected).open("rb") as source:
            raw = source.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("预期产物描述不能超过 64 KiB")
        expected = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(expected, dict):
            raise ValueError("预期产物描述必须是 JSON 对象")
    result = check_debug_artifacts(args.bit, args.ltx, expected, getattr(args, "manifest", None))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"consistent": 0, "blocked": 1, "incomplete": 2}[result["status"]]
