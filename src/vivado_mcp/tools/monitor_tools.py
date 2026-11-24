"""只读运行面板与同源结构化状态入口。"""

import json

from mcp.server.mcpserver import Context

from vivado_mcp.server import _NO_SESSION, _require_session, mcp, sync_shared_session


async def _monitor(ctx, session_id, run_name, target_step):
    registry = ctx.request_context.lifespan_context.run_monitors
    session = await _require_session(ctx, session_id)
    if session is None:
        last = registry.last(session_id, run_name, target_step)
        if last is not None:
            return last
        raise ValueError(_NO_SESSION.format(sid=session_id))
    return registry.get(session, run_name, target_step)


@mcp.tool()
async def open_run_monitor(
    run_name: str = "impl_1",
    target_step: str = "route_design",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """为已存在会话打开本机只读面板，返回 URL；不会启动构建或新 Vivado。

    每 5 秒由程序采样，人和 get_run_snapshot 读同一缓存。target_step 选
    synth_design / route_design / write_bitstream。关闭浏览器不影响运行。
    URL 仅能在运行 MCP 的本机打开；首次等待采样是正常状态。
    """
    try:
        monitor = await _monitor(ctx, session_id, run_name, target_step)
        url = monitor.open_view()
        snap = monitor.snapshot()
        run_info = snap.get("run") or {}
        sync_shared_session(
            ctx,
            session_id=session_id,
            run_name=run_name,
            target_step=target_step,
            run_dir=run_info.get("directory") or None,
            log_path=run_info.get("log_path") or None,
            monitor_url=url or None,
            status=run_info.get("state") or None,
            state=run_info.get("status") or None,
            phase=run_info.get("phase") or None,
        )
        return json.dumps({
            "url": url, "session_id": session_id, "run_name": run_name,
            "target_step": target_step, "connection": snap["connection"],
        }, ensure_ascii=False)
    except (ValueError, OSError) as exc:
        return f"[ERROR] {exc}"


@mcp.tool()
async def get_run_snapshot(
    run_name: str = "impl_1",
    target_step: str = "route_design",
    session_id: str = "default",
    ctx: Context = None,
    include_report_text: bool = False,
) -> str:
    """读取运行面板 JSON 缓存（不阻塞主通道）；
    核对 connection/observed_at 与 reports_status 新鲜度。"""
    try:
        snapshot = (await _monitor(ctx, session_id, run_name, target_step)).snapshot()
        if not include_report_text:
            for report in snapshot["reports"]:
                report.pop("text", None)
        return json.dumps(snapshot, ensure_ascii=False)
    except ValueError as exc:
        return f"[ERROR] {exc}"


@mcp.tool()
async def close_run_monitor(
    run_name: str = "impl_1",
    target_step: str = "route_design",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """关闭匹配面板并释放后台采样（不停止 Vivado、run 或 GUI）。"""
    try:
        registry = ctx.request_context.lifespan_context.run_monitors
        count = await registry.release(session_id, run_name, target_step)
        return json.dumps({
            "status": "closed" if count else "not_found", "closed": count,
            "session_id": session_id, "run_name": run_name, "target_step": target_step,
        }, ensure_ascii=False)
    except (ValueError, OSError) as exc:
        return f"[ERROR] {exc}"
