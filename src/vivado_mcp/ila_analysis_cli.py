"""离线采样分析 CLI；有限读取消费者规则，不连接设备。"""

import json
from pathlib import Path

from vivado_mcp.analysis.ila_analysis import analyze_ila_capture


def _object(pairs):
    """拒绝重复 JSON 字段，避免分析语义被静默覆盖。"""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"分析描述有重复字段：{key}")
        value[key] = item
    return value


def run_ila_analysis_cli(args) -> int:
    """0=观察/窗口内一致，1=输入阻断，2=证据不足，3=发现声明规则违规。"""
    try:
        with Path(args.spec).open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("分析描述不能超过 64 KiB")
        spec = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_object)
        result = analyze_ila_capture(
            args.file, spec, args.start_tick, args.end_tick, args.expected_sha256,
        )
    except (OSError, ValueError) as exc:
        result = {'status': 'blocked', 'error': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {'consistent': 0, 'observed': 0, 'blocked': 1, 'inconclusive': 2,
            'violated': 3}[result['status']]
