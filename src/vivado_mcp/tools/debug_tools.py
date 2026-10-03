"""人和 AI 共用的硬件调试入口；独立于只读构建观察器。"""

import json
from typing import Literal

from mcp.server.mcpserver import Context

from vivado_mcp.debug_service import load_panel
from vivado_mcp.server import _NO_SESSION, _require_session, mcp


async def _service(ctx, session_id):
    registry = ctx.request_context.lifespan_context.debug_services
    session = _require_session(ctx, session_id)
    if session is None:
        raise ValueError(_NO_SESSION.format(sid=session_id))
    return await registry.get(session)


def _error(exc):
    return json.dumps({"error": str(exc)}, ensure_ascii=False)


@mcp.tool()
async def open_debug_panel(
    session_id: str = "default", panel_path: str | None = None, ctx: Context = None,
) -> str:
    """为现有 Vivado 会话打开本机 ILA/VIO 调试面板，返回 URL 与共享状态。

    不连接 JTAG、不烧录、不自动选设备；先在原生 Hardware Manager 打开目标并加载
    对应 probes。人可独立操作面板；AI 使用 debug_action 调用同一后端。
    panel_path 可指向消费者工程内的 JSON 控件描述（title/controls，最多 64 KiB）。
    未指定时显示通用面板。关闭浏览器不停止 Vivado 或正在等待触发的 ILA。
    """
    try:
        panel = load_panel(panel_path) if panel_path is not None else None
        service = await _service(ctx, session_id)
        if panel is not None:
            service.set_panel(panel)
        return json.dumps({"url": service.open_view(), "snapshot": service.snapshot()},
                          ensure_ascii=False)
    except (ValueError, RuntimeError, OSError) as exc:
        return _error(exc)


@mcp.tool()
async def get_debug_snapshot(session_id: str = "default", ctx: Context = None) -> str:
    """读取与调试面板相同的缓存，不发起硬件查询。

    先检查 connection/observed_at/revision/control，以及 operations 的最终状态。
    初次使用 debug_action(inventory) 枚举，select 明确 target/device；refresh 显式刷新。
    unknown 表示操作结果未完全确认，不能当作失败自动重试；重新核对设备状态。
    ILA status 是硬件原始状态，VIO 读回不证明业务逻辑已经采用参数。
    """
    try:
        registry = ctx.request_context.lifespan_context.debug_services
        session = _require_session(ctx, session_id)
        service = registry.last(session_id) if session is None else await registry.get(session)
        if service is None:
            raise ValueError(_NO_SESSION.format(sid=session_id))
        return json.dumps(service.snapshot(), ensure_ascii=False)
    except (ValueError, RuntimeError) as exc:
        return _error(exc)


@mcp.tool()
async def debug_action(
    action: Literal[
        "inventory", "select", "refresh", "control", "write_vio",
        "configure_ila", "arm_ila", "upload_ila",
    ],
    params: dict,
    expected_revision: int,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """提交一次具名硬件调试操作，立即返回 operation_id；随后查询共享快照。

    params：inventory/refresh={}；select={target,device}（精确发现的名称）；
    control={owner:'ai'|'manual'}；write_vio={core,probe,value}（非负十进制或0x字符串）；
    configure_ila={core,probe,trigger_value,trigger_position?}（原生触发值语法）；
    arm_ila={core,immediate?}；upload_ila={core}（首批仅完整单窗口，上传至Vivado）。
    expected_revision 必须取自最新 get_debug_snapshot。设备选择/硬件操作前显式切换
    control 为 ai，已授权范围内可逐步执行；交回人工后不能继续写。控制权仅覆盖本服务，
    不拦截原生 GUI/run_tcl/其它进程；使用那些入口时请协调操作。
    无任意 Tcl、隐式重连或烧录。拒绝 busy/过期请求，不排队或自动重试设备动作。
    """
    try:
        service = await _service(ctx, session_id)
        result = service.submit({
            "action": action, "params": params, "expected_revision": expected_revision,
        }, source="ai")
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, RuntimeError) as exc:
        return _error(exc)


@mcp.tool()
async def close_debug_panel(session_id: str = "default", ctx: Context = None) -> str:
    """释放调试页面和轮询；等待已接受短操作的回执，不停止 ILA 或关闭 Vivado。"""
    registry = ctx.request_context.lifespan_context.debug_services
    closed = await registry.release(session_id)
    return json.dumps({"status": "closed" if closed else "not_found"})
