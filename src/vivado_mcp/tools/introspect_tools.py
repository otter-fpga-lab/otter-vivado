"""离线 introspection 工具:parse_xpr / parse_bit_header / parse_ltx。

三个纯 Python 离线工具,不启动 Vivado 直接解析工程 / 比特流 / ILA 探针文件。
同 compare_xci 范式:解析逻辑在 analysis/,本文件只做薄壳(参数收集 + 调 parser
+ try/except 兜底 + 返回中文摘要)。无 Vivado 会话依赖。

为什么这三个值得做工具(都满足"Tcl 做不了或做不好"):
  - parse_xpr     —— get_project_info 需 start_session + open_project(中文路径会
                     TclStackFree 崩);离线读 .xpr 秒级摸底,CI 友好。
  - parse_bit_header —— Vivado 无任何 Tcl 命令读离线 .bit;烧前防错板 / 交付对账。
  - parse_ltx     —— get_hw_probes 需板子在手 + 活 hw session;离线读探针清单。
"""

import asyncio
import json

from mcp.server.mcpserver import Context

from vivado_mcp.analysis.bit_header_parser import format_bit as _format_bit
from vivado_mcp.analysis.bit_header_parser import parse_bit as _parse_bit
from vivado_mcp.analysis.ltx_parser import format_ltx as _format_ltx
from vivado_mcp.analysis.ltx_parser import parse_ltx as _parse_ltx
from vivado_mcp.analysis.xpr_parser import format_xpr as _format_xpr
from vivado_mcp.analysis.xpr_parser import parse_xpr as _parse_xpr
from vivado_mcp.server import mcp


@mcp.tool()
async def parse_xpr(file_path: str, ctx: Context = None) -> str:
    """纯 Python 离线解析 .xpr 工程文件（part/顶层/源文件/XDC/runs），无需 Vivado。"""
    try:
        cfg = _parse_xpr(file_path)
    except (OSError, ValueError) as e:
        return f"[ERROR] .xpr 解析失败: {e}"
    return _format_xpr(cfg)


@mcp.tool()
async def parse_bit_header(file_path: str, ctx: Context = None) -> str:
    """离线读取 .bit 头部元信息（设计名/part 至 package 级/构建时间）及文件 SHA256，无需 Vivado。"""
    try:
        header = await asyncio.to_thread(_parse_bit, file_path)
    except (OSError, ValueError) as e:
        return f"[ERROR] .bit 解析失败: {e}"
    return _format_bit(header)


@mcp.tool()
async def parse_ltx(file_path: str, ctx: Context = None) -> str:
    """离线解析 .ltx 调试探针文件（hw_ila/probe 名/位宽/net 映射），无需连板或 Vivado。"""
    try:
        cfg = await asyncio.to_thread(_parse_ltx, file_path)
    except (OSError, ValueError) as e:
        return f"[ERROR] .ltx 解析失败: {e}"
    return _format_ltx(cfg)


@mcp.tool()
async def check_debug_artifacts(
    bit_path: str, ltx_path: str, expected: dict | None = None,
    manifest_path: str | None = None,
) -> str:
    """只读离线核对 bit/ltx 器件、核、探针位宽与可选 manifest 指纹；
    不连接/烧录，pairing 恒为 unverified。"""
    from vivado_mcp.analysis.debug_artifacts import check_debug_artifacts as check

    try:
        result = await asyncio.to_thread(check, bit_path, ltx_path, expected, manifest_path)
    except (OSError, ValueError) as exc:
        result = {"status": "blocked", "pairing": "unverified", "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def read_ila_waveform(
    file_path: str, signal_ids: list[str] | None = None,
    start_tick: str = "0", end_tick: str | None = None,
    offset: int = 0, limit: int = 1000, expected_sha256: str | None = None,
) -> str:
    """离线分页读取数字 VCD（上限 32MiB/4096 信号/4096 位，[] 仅目录），
    保留 x/z 并校验 expected_sha256；不需要 Vivado。"""
    from vivado_mcp.analysis.ila_waveform import read_ila_waveform as read

    try:
        result = await asyncio.to_thread(
            read, file_path, signal_ids, start_tick, end_tick, offset, limit, expected_sha256,
        )
    except (OSError, ValueError) as exc:
        result = {"status": "blocked", "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def analyze_ila_capture(
    file_path: str, spec: dict, start_tick: str = "0", end_tick: str | None = None,
    expected_sha256: str | None = None,
) -> str:
    """只读分析 VCD（≤64 信号/16 规则/200k 事件）：
    支持 allowed_values/state_transitions/stable_while_stalled；需显式时钟与取值时机。"""
    from vivado_mcp.analysis.ila_analysis import analyze_ila_capture as analyze

    try:
        result = await asyncio.to_thread(
            analyze, file_path, spec, start_tick, end_tick, expected_sha256,
        )
    except (OSError, ValueError) as exc:
        result = {"status": "blocked", "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)
