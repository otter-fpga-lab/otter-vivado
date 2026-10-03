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
    """离线解析 Vivado 工程文件(.xpr),无需启动 Vivado。

    秒级摸底陌生工程 / CI 门禁:不启 Vivado(避开 120s GUI 冷启 + 中文路径
    TclStackFree 崩),纯 Python 读 .xpr 拿 part / 顶层 / 源文件(按 fileset 分组,
    含 .v/.mem/.xci IP)/ XDC 约束 / synth+impl runs 及 Strategy。
    对照 get_project_info(需先 start_session + open_project),本工具完全离线。

    Args:
        file_path: .xpr 工程文件的绝对路径。
    """
    try:
        cfg = _parse_xpr(file_path)
    except (OSError, ValueError) as e:
        return f"[ERROR] .xpr 解析失败: {e}"
    return _format_xpr(cfg)


@mcp.tool()
async def parse_bit_header(file_path: str, ctx: Context = None) -> str:
    """离线解析 .bit 比特流文件头部,无需启动 Vivado。

    解析文件头并分块读取整文件计算 SHA256（不解释配置载荷）:
    提取设计名 / 目标 part(原始 + 规整)/ 构建日期时间 /
    文件 SHA256。用于烧录前防错板(part 比对)、交付/返修对账(确认孤立 .bit 是不是
    声称的那版)。Vivado 无任何 Tcl 命令读离线 .bit。
    注意:.bit 里 part 去 'xc' 前缀 + 去速度等级(如 7k325tffg900);规整字段补回
    'xc' 但速度等级无法还原,与 .xpr 的 part 比对时只能比到 package 级。

    Args:
        file_path: .bit 文件的绝对路径。
    """
    try:
        header = await asyncio.to_thread(_parse_bit, file_path)
    except (OSError, ValueError) as e:
        return f"[ERROR] .bit 解析失败: {e}"
    return _format_bit(header)


@mcp.tool()
async def parse_ltx(file_path: str, ctx: Context = None) -> str:
    """离线解析 ILA 调试探针文件(.ltx),无需连板 / 启动 Vivado。

    连板 ILA 抓波前先离线拿清单:每个 hw_ila 挂哪些 probe、probe 名、位宽、映射的
    net。辅助在写 set_property TRIGGER_COMPARE_VALUE eq<位宽>'h.. [get_hw_probes
    <probe>] 之前确认正确的 probe 名和宽度。get_hw_probes 需板子在手 + 活 hw
    session,本工具完全离线。Vivado 2019.1 的 .ltx 是 JSON 格式。

    Args:
        file_path: .ltx 文件的绝对路径。
    """
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
    """离线核对 bit/ltx：载荷长度、器件/封装、ILA/VIO 核、UUID 与探针要求。

    expected 可含 part、cores；核使用实际层级 name/type/probes，可选 uuid。
    probe 使用 name/width，可选 direction/port_index。预期是需要存在的子集，
    不从 IP 模块名猜实例名。完整 schema 见 docs/DEBUG_ARTIFACTS.md。
    无 expected 时仅摸底，返回 incomplete。consistent 仅表示离线检查一致；
    pairing 始终 unverified：没有解析 bit 内部 UUID，也没有实际硬件验证。
    manifest_path 可指向 export_debug_bundle 的清单，核对全包指纹和导出记录；
    record_matches 也不代表不可伪造的配对认证。只读本地文件，不构建、不连接设备、不烧录。
    """
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
    """离线读取数字 VCD，为消费者波形页面/分析提供 JSON，不需要 Vivado 会话。

    最多读取 32 MiB；4096 个声明/4096 位宽；不支持实数或字符串 VCD。
    signal_ids 为文件内的标识符代码，[] 只取目录；null 选全部。
    时间为非负十进制字符串 tick，值为保留 x/z 的全位宽二进制字符串。
    start/end 均包含边界；initial_values_before_start 为开始前保持值，null 表示未知。
    offset/limit 分页保留同刻变化，下一页使用 next_offset 与同一过滤条件，必须传回
    expected_sha256 防止文件替换。只返回有限页，不抽点；truncated 不能当完整波形。
    timescale 是文件标注，不证明物理采样周期；触发位置和采集完整性不能由 VCD 推断。
    """
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
    """只读分析数字 VCD：统计信号变化，并检查消费者声明的采样规则。

    spec={signals:[VCD 标识符], sample_clock?:{id,edge:'rising'|'falling',
    values:'before_tick'|'after_tick'}, reset?:{id,active:'0'|'1'}, checks?:[...]}。
    检查需明确时钟及取值时机；全部引用必须在 signals 中。支持 allowed_values
    {name,kind,signal,values:[全位宽二进制]}、state_transitions
    {name,kind,signal,allowed_pairs:[[前态,后态]]} 和 stable_while_stalled
    {name,kind,valid,ready,data:[标识符]}。不猜状态编码或协议，详细语义见 ILA_ANALYSIS.md。
    返回 violated/consistent/inconclusive/observed、有限违规原值和 tick、源文件与描述指纹。
    consistent 仅是观察窗口内声明规则一致；未知值、时钟歧义、无有效检查不能算通过。
    最多 64 信号、16 规则、窗口 200000 事件；不截断后给成功。不连接设备或执行触发建议。
    """
    from vivado_mcp.analysis.ila_analysis import analyze_ila_capture as analyze

    try:
        result = await asyncio.to_thread(
            analyze, file_path, spec, start_tick, end_tick, expected_sha256,
        )
    except (OSError, ValueError) as exc:
        result = {"status": "blocked", "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)
