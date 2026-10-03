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
    capabilities.stop_ila=false 表示插件不支持独立停止；stop_ila_details 说明核对范围。
    实验暂停/中止、等待超时与关闭页面均不停止 ILA；原生处理后显式 refresh 再核对。
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
        "configure_ila", "arm_ila", "upload_ila", "export_ila",
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
    export_ila={core,output_dir}：完整单窗口上传并导出到消费者新目录，返回 VCD/清单。
    不覆盖文件；失败/unknown 保留现场，不自动重试；不启动新采集。
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
    """中止本地实验后续步骤并释放页面/轮询；保留短操作回执，不停止 ILA/Vivado。"""
    registry = ctx.request_context.lifespan_context.debug_services
    closed = await registry.release(session_id)
    return json.dumps({"status": "closed" if closed else "not_found"})


@mcp.tool()
async def create_debug_experiment(
    spec: dict, output_dir: str, expected_revision: int,
    session_id: str = "default", ctx: Context = None,
) -> str:
    """在已选择设备且 control=ai 的共享服务上创建本地实验，尚不执行步骤。

    spec={title,steps:[{id,kind,...}]}；首步必须 ready，需消费者本地人工入口确认。
    ready={label}；countdown={label,duration_ms}；cue={label,tone:'none'|'beep'}；
    debug={action,params} 仅 configure_ila/write_vio/arm_ila/export_ila，导出目录自动生成；
    wait_capture={core,timeout_ms}。时长1~600000ms、最多64步；详细约定见 DEBUG_EXPERIMENT.md。
    output_dir 必须是新目录，保存计划/每轮事件/导出数据；expected_revision 来自调试快照。
    倒计时是主机时间，声音由消费者播放，不保证 FPGA 精确触发。无固定实验页面。
    """
    from vivado_mcp.debug_experiment import DebugExperiment

    try:
        service = await _service(ctx, session_id)
        experiment = DebugExperiment(service, spec, output_dir, expected_revision, owner="ai")
        return json.dumps(experiment.snapshot(), ensure_ascii=False)
    except (ValueError, RuntimeError, OSError) as exc:
        return _error(exc)


@mcp.tool()
async def get_debug_experiment(session_id: str = "default", ctx: Context = None) -> str:
    """只读当前实验缓存，不查询设备；事件带 seq，消费者据此去重播放提示。

    remaining_ms 仅为本地倒计时；paused/aborted 不代表 FPGA 停止，硬件短操作不能取消。
    快照只保留最近100个事件；完整逐事件 JSON 位于消费者目录，不支持进程崩溃后自动续跑。
    """
    try:
        registry = ctx.request_context.lifespan_context.debug_services
        session = _require_session(ctx, session_id)
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
    """控制本地实验：start/pause/resume/abort={}；mark={label,frame_id?}。

    expected_revision 是实验快照版本，区别于调试版本。redo={expected_debug_revision}：
    上轮终止后显式 refresh、ILA 明确 IDLE 才可创建新轮；unknown 不自动重做。
    暂停/中止等待已接受硬件短操作回执，不停止 FPGA、不回滚 VIO；恢复不重放完成步骤。
    计时和后续步骤在本地运行，不依赖聊天往返；人工就绪确认只能来自消费者本地适配入口。
    """
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
    """离线解析工程控件描述，预览精确位值/有序预设，解读可选 hardware 快照。

    version=1；spec 含 title/target/device/controls/presets?；详见 DEBUG_CONTROLS.md。
    number 显式声明补码、定点、scale/offset、单位与 min/max/step；所有工程数值用
    十进制字符串。enum 用选项 id，对应显式 0x 原始值。writes=[{control_id,value}]。
    返回 profile_sha256；不连接设备、不执行预设，快照匹配不代表实时硬件验证。
    """
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
    """按已预览的工程控件声明写一次 VIO，复用共享控制权、版本与操作回执。

    value 为十进制工程值字符串或 enum option id；不舍入、不饱和、不隐式生成脉冲。
    写前再次检查实时 target/device/核 UUID/探针位宽和方向；返回 operation_id 后查询
    get_debug_snapshot，核对终态及 result.semantic。读回不证明业务逻辑采纳或物理效果。
    预设逐项等待成功回执与最新 revision；非原子、无回滚，unknown 禁止自动重试。
    """
    try:
        service = await _service(ctx, session_id)
        result = service.submit_control(spec, control_id, value, expected_profile_sha256,
                                        expected_revision, source="ai")
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, RuntimeError) as exc:
        return _error(exc)
