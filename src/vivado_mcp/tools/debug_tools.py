"""人和 AI 共用的硬件调试入口；独立于只读构建观察器。"""

import json
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import Context

from vivado_mcp.debug_service import load_panel
from vivado_mcp.project_layout import default_output_dir, discard_reserved_debug_subdir
from vivado_mcp.server import _NO_SESSION, _require_session, mcp, sync_shared_session


async def _service(ctx, session_id):
    registry = ctx.request_context.lifespan_context.debug_services
    session = await _require_session(ctx, session_id)
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
        url = service.open_view()
        sync_shared_session(ctx, session_id=session_id, debug_url=url or None)
        return json.dumps({"url": url, "snapshot": service.snapshot()},
                          ensure_ascii=False)
    except (ValueError, RuntimeError, OSError) as exc:
        return _error(exc)


@mcp.tool()
async def get_debug_snapshot(session_id: str = "default", ctx: Context = None) -> str:
    """只读返回调试面板缓存（connection/observed_at/revision/control/operations），不查硬件。

    unknown 结果不代表失败，禁止自动重试；capabilities.stop_ila=false，
    stop_ila_details 说明核对范围，关闭页面不停止 ILA。
    """
    try:
        registry = ctx.request_context.lifespan_context.debug_services
        session = await _require_session(ctx, session_id)
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
        "configure_ila", "arm_ila", "upload_ila", "export_ila",
    ],
    params: dict,
    expected_revision: int,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """提交具名硬件调试动作，返回 operation_id（随后查 get_debug_snapshot）。

    params：inventory/refresh={}；select={target,device}；control={owner:'ai'|'manual'}；
    write_vio={core,probe,value}；configure_ila={core,probe,trigger_value,trigger_position?}；
    arm_ila={core,immediate?}；upload_ila={core}；
    export_ila={core,output_dir?}（省略落工程 Debug/）。
    expected_revision 取自最新快照，写前需 control=ai；拒绝过期/busy，不覆盖或自动重试。
    """
    reserved_dir: str | None = None
    try:
        service = await _service(ctx, session_id)
        output_dir = None
        if action == "export_ila" and isinstance(params, dict):
            if params.get("output_dir") in (None, ""):
                output_dir = await default_output_dir(
                    service.session, "ila", create_parent=False,
                )
                reserved_dir = output_dir
                params = {**params, "output_dir": output_dir}
            elif isinstance(params["output_dir"], str):
                output_dir = str(Path(params["output_dir"]).expanduser().absolute())
        result = service.submit({
            "action": action, "params": params, "expected_revision": expected_revision,
        }, source="ai")
        reserved_dir = None
        if output_dir is not None:
            result = {**result, "output_dir": output_dir}
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, RuntimeError) as exc:
        if reserved_dir is not None:
            discard_reserved_debug_subdir(reserved_dir)
        return _error(exc)


@mcp.tool()
async def close_debug_panel(session_id: str = "default", ctx: Context = None) -> str:
    """中止本地实验后续步骤并释放页面/轮询；保留短操作回执，不停止 ILA/Vivado。"""
    registry = ctx.request_context.lifespan_context.debug_services
    closed = await registry.release(session_id)
    return json.dumps({"status": "closed" if closed else "not_found"})


@mcp.tool()
async def create_debug_experiment(
    spec: dict, expected_revision: int, output_dir: str | None = None,
    session_id: str = "default", ctx: Context = None,
) -> str:
    """在 control=ai 调试会话创建本地实验（steps: ready/countdown/cue/debug/wait_capture，
    ≤64 步，1~600000ms）；output_dir 省略落工程 Debug/。"""
    from vivado_mcp.debug_experiment import DebugExperiment

    reserved_dir: str | None = None
    try:
        service = await _service(ctx, session_id)
        create_parents = False
        if not output_dir:
            output_dir = await default_output_dir(
                service.session, "experiment", create_parent=False,
            )
            reserved_dir = output_dir
            create_parents = True
        experiment = DebugExperiment(
            service, spec, output_dir, expected_revision,
            owner="ai", create_parents=create_parents,
        )
        reserved_dir = None
        return json.dumps({**experiment.snapshot(), "experiment_dir": str(experiment.root)},
                          ensure_ascii=False)
    except (ValueError, RuntimeError, OSError) as exc:
        if reserved_dir is not None:
            discard_reserved_debug_subdir(reserved_dir)
        return _error(exc)


@mcp.tool()
async def get_debug_experiment(session_id: str = "default", ctx: Context = None) -> str:
    """只读返回当前本地实验缓存（含去重 seq、最多保留最近 100 事件），不查设备。"""
    try:
        registry = ctx.request_context.lifespan_context.debug_services
        session = await _require_session(ctx, session_id)
        service = registry.last(session_id) if session is None else await registry.get(session)
        if service is None or service.experiment is None:
            raise ValueError("该会话没有实验；已有记录可从消费者目录读取")
        return json.dumps(service.experiment.snapshot(), ensure_ascii=False)
    except (ValueError, RuntimeError) as exc:
        return _error(exc)


@mcp.tool()
async def debug_experiment_action(
    experiment_id: str,
    action: Literal["start", "pause", "resume", "abort", "redo", "mark"],
    params: dict, expected_revision: int,
    session_id: str = "default", ctx: Context = None,
) -> str:
    """控制本地实验（start/pause/resume/abort={}；mark={label,frame_id?}；
    redo={expected_debug_revision}）；不停止 FPGA/不自动重做 unknown。"""
    try:
        service = await _service(ctx, session_id)
        experiment = service.experiment
        if experiment is None or experiment.id != experiment_id:
            raise ValueError("实验身份不匹配，请读取当前实验")
        return json.dumps(experiment.command(action, params, expected_revision, source="ai"),
                          ensure_ascii=False)
    except (ValueError, RuntimeError, OSError) as exc:
        return _error(exc)


@mcp.tool()
async def resolve_debug_controls(
    spec: dict, writes: list[dict] | None = None, preset: str | None = None,
    hardware: dict | None = None,
) -> str:
    """离线解析工程控件声明（version=1），预览十进制/enum 位值、有序预设与
    profile_sha256，不连板。"""
    from vivado_mcp.debug_controls import resolve_controls

    try:
        return json.dumps(resolve_controls(spec, writes, preset, hardware), ensure_ascii=False)
    except (ValueError, TypeError, AttributeError) as exc:
        return _error(exc)


@mcp.tool()
async def write_debug_control(
    spec: dict, control_id: str, value: str, expected_profile_sha256: str,
    expected_revision: int, session_id: str = "default", ctx: Context = None,
) -> str:
    """按控件声明写 VIO（十进制串或 enum id，校验 expected_profile_sha256 /
    expected_revision 与 control=ai）；返回 operation_id，unknown 禁止重试。"""
    try:
        service = await _service(ctx, session_id)
        result = service.submit_control(spec, control_id, value, expected_profile_sha256,
                                        expected_revision, source="ai")
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, RuntimeError) as exc:
        return _error(exc)
