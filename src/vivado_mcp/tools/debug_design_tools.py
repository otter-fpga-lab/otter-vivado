"""人和 AI 共用的调试工程准备入口。"""

import json

from mcp.server.mcpserver import Context

from vivado_mcp.debug_design import DebugDesignPreparation
from vivado_mcp.debug_design import plan_debug_design as make_plan
from vivado_mcp.server import _NO_SESSION, _require_session, mcp


def _result(value):
    return json.dumps(value, ensure_ascii=False)


@mcp.tool()
async def plan_debug_design(spec: dict) -> str:
    """离线生成 ILA/VIO IP、网表 ILA 或 MARK_DEBUG 计划，不连接或修改 Vivado。

    spec.kind 为 ila_ip/vio_ip/ila_netlist/mark_debug。IP 计划输出创建 Tcl 和 Verilog
    例化片段；网表计划输出精确 net 名称与位序的 unmanaged Tcl 约束，需指定自由运行时钟。
    完整 schema/示例见 docs/DEBUG_DESIGN.md；不猜测时钟域，不自动修改业务 RTL。
    计划不是构建通过证明；返回 artifacts 可保存到消费者工程，不写入插件安装目录。
    """
    try:
        return _result(make_plan(spec))
    except ValueError as exc:
        return _result({"error": str(exc)})


@mcp.tool()
async def inspect_debug_design(
    spec: dict, session_id: str = "default", ctx: Context = None,
) -> str:
    """只读检查当前工程、目标 run 或可用 IP，返回状态、工程身份与剩余验证步骤。

    ready 仅表示可进入准备阶段。IP CONFIG/part 兼容性仍由创建后的 Vivado 验证；
    网表信号连接在实际 run 加载生成约束时验证。保持当前工程，不打开其它 run/design。
    """
    try:
        session = _require_session(ctx, session_id)
        if session is None:
            raise ValueError(_NO_SESSION.format(sid=session_id))
        return _result(await DebugDesignPreparation(session).inspect(spec))
    except (ValueError, RuntimeError) as exc:
        return _result({"error": str(exc)})


@mcp.tool()
async def prepare_debug_design(
    spec: dict, expected_project: dict, session_id: str = "default", ctx: Context = None,
) -> str:
    """准备已检查的工程；expected_project 必须传 inspect 返回的 name/directory/part。

    IP 路径创建命名 IP、验证配置并生成输出产物；仍需将例化模板接入业务 RTL。
    约束路径在工程 otter_debug 下只创建新 Tcl 文件，加入指定 run 的 CONSTRSET，
    返回 affected_runs；不执行该约束或启动构建。禁止覆盖已有文件/同名 IP。
    created/constraints_added 都不代表 bit/ltx 已构建；partial/结果未知时核对现场，不自动重试。
    不连接、烧录或采集板卡；下一步复用现有构建和报告入口。
    """
    try:
        session = _require_session(ctx, session_id)
        if session is None:
            raise ValueError(_NO_SESSION.format(sid=session_id))
        return _result(await DebugDesignPreparation(session).apply(spec, expected_project))
    except (ValueError, RuntimeError) as exc:
        return _result({"error": str(exc)})


@mcp.tool()
async def export_debug_bundle(
    checkpoint_path: str, output_dir: str, expected_part: str,
    source_revision: str | None = None, timeout_seconds: int = 1800,
    session_id: str = "default", ctx: Context = None,
) -> str:
    """在专用空闲会话中从明确的实现 DCP 导出 bit/ltx/报告和来源清单。

    必须使用没有打开工程/设计的独立会话；不关闭已有 GUI，不自动运行综合/实现。
    checkpoint_path 是已完成实现的 DCP；expected_part 为完整器件名；output_dir
    必须是父目录已存在的新目录。复制检查点后，同一 execute 中打开该副本，生成 bit、
    ltx、timing/utilization/DRC 报告；SHA256 清单保存到消费者目录，不提供固定页面。
    source_revision 仅记录调用方声明；不自动认证检查点源码来源。
    exported 只表示导出和文件核对完成；不代表时序、板卡配对通过。
    partial/unknown 保留文件；超时后 Vivado 可能继续运行，先核对状态，禁止直接重放。
    导出后会话保留检查点。MCP 与 Vivado 必须共享本机文件系统。
    """
    from vivado_mcp.debug_bundle import export_debug_bundle as export

    try:
        session = _require_session(ctx, session_id)
        if session is None:
            raise ValueError(_NO_SESSION.format(sid=session_id))
        return _result(await export(
            session, checkpoint_path, output_dir, expected_part, source_revision, timeout_seconds,
        ))
    except (ValueError, RuntimeError, OSError) as exc:
        return _result({"status": "blocked", "error": str(exc)})
