"""设计流程工具。

run_synthesis / run_implementation / generate_bitstream / wait_run / program_device。
封装 Vivado 长时间运行的操作，提供超时管理和进度反馈。
综合/实现完成后自动执行警告诊断，bitstream 生成前自动安全检查。

**D5 架构**：长任务使用 Python 侧轮询 STATUS/PROGRESS，不再依赖 Tcl `wait_on_run`
阻塞事件循环。这样 subprocess 和 GuiSession 两种实现共用同一套轮询代码，
GUI 模式下 Vivado 界面保持响应。
"""

import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import Context

from vivado_mcp.analysis.run_progress_parser import progress_percent, run_state
from vivado_mcp.analysis.warning_parser import parse_diag_counts, parse_pre_bitstream
from vivado_mcp.config import get_vivado_version
from vivado_mcp.run_feedback import (
    WAIT_UNTIL_CHOICES,
    build_run_progress_report,
    evaluate_wait,
)
from vivado_mcp.server import (
    _NO_SESSION,
    _require_session,
    _safe_execute,
    mcp,
    sync_shared_session,
)
from vivado_mcp.system_telemetry import (
    collect_system_telemetry,
    format_memory_guard_warning,
)
from vivado_mcp.tcl_scripts import (
    CHECK_PRE_BITSTREAM,
    COUNT_WARNINGS,
    LAUNCH_RUN_IF_IDLE,
    POLL_RUN_STATUS,
    QUERY_FILESET_OVERRIDES,
)
from vivado_mcp.vivado.tcl_utils import to_tcl_path, validate_identifier
from vivado_mcp.watch_engine import (
    PROGRESS_HEARTBEAT_SEC,
    LogWatchState,
    _extract_rst_pid,
    _is_link_or_junction,
    estimate_watch_profile,
    extract_compact_signoff,
    format_heartbeat_message,
    format_signoff_line,
    report_mcp_progress,
)

logger = logging.getLogger(__name__)

# 轮询间隔（秒）。综合/实现任务通常以分钟计，2 秒足够快响应完成事件
_POLL_INTERVAL_SEC = 2.0

# --------------------------------------------------------------------------- #
#  内部辅助：综合 / 实现 / bitstream 共享的轮询逻辑(单一来源,勿复制)
# --------------------------------------------------------------------------- #

def _session_is_busy(session) -> bool:
    """活跃会话的在途查询应继续持有响应，不因旁路采样中断构建等待。"""
    state = getattr(session, "state", None)
    return getattr(state, "value", state) == "busy" and session.is_alive


async def _poll_run_until_done(
    session,
    run_name: str,
    timeout_sec: float,
    ctx: Context,
    target_step: str | None = None,
) -> tuple[str, str, str, str]:
    """每 2s 查询状态，直到目标完成、失败、取消或观察超时。

    Tcl 片段在 tcl_scripts.POLL_RUN_STATUS(单一定义);本 helper 是
    _launch_and_wait 和 generate_bitstream 的共用轮询循环。

    Returns:
        ``(outcome, final_status, final_progress, final_elapsed)``,
        outcome ∈ {"done", "timeout"}。只上报实际 PROGRESS，不推算百分比。

    Raises:
        除活跃会话忙碌外，execute 异常原样上抛，由调用方格式化错误消息。
    """
    deadline = time.monotonic() + timeout_sec
    final_status = "UNKNOWN"
    final_progress = ""
    final_elapsed = ""
    last_progress = None

    while time.monotonic() < deadline:
        if _session_is_busy(session):
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            continue
        try:
            poll = await session.execute(
                POLL_RUN_STATUS.format(run_name=run_name), timeout=15.0
            )
        except RuntimeError:
            # 检查状态与获得 session 锁之间，另一条查询可能进入 BUSY。
            # 只重试仍活跃的忙会话；真正的 Tcl/断连错误仍交给调用方处理。
            if _session_is_busy(session):
                await asyncio.sleep(_POLL_INTERVAL_SEC)
                continue
            raise
        if poll.is_error:
            raise RuntimeError(f"Vivado 状态查询失败: {poll.output[:500]}")

        line = next(
            (ln for ln in poll.output.splitlines() if ln.startswith("VMCP_POLL|")),
            None,
        )
        if line is None:
            raise RuntimeError("Vivado 未返回 VMCP_POLL 状态标记；run 未取消，可继续查询。")
        parts = line[len("VMCP_POLL|"):].split("|")
        if len(parts) < 2 or not parts[0].strip():
            raise RuntimeError("Vivado 返回的 VMCP_POLL 状态标记不完整。")
        final_status = parts[0].strip()
        final_progress = parts[1].strip()
        final_elapsed = parts[2].strip() if len(parts) >= 3 else ""

        # 无效或缺失的百分比保持未知，不合成初始 0% 或终态 100%。
        percent = progress_percent(final_progress)
        if percent is not None and percent != last_progress:
            if ctx is not None:
                await ctx.report_progress(progress=percent, total=100)
            last_progress = percent

        state = run_state(final_status, target_step)
        if state in {"completed", "failed", "cancelled"}:
            return ("done", final_status, final_progress, final_elapsed)

        await asyncio.sleep(_POLL_INTERVAL_SEC)
    return ("timeout", final_status, final_progress, final_elapsed)


async def _query_fileset_overrides(session) -> list[str]:
    """PRD B4:查 sources_1 的 generic / verilog_define,格式化成结果行。

    Returns:
        要追加进结果的行列表:成功时两行 ``applied_generic: ...`` /
        ``applied_verilog_define: ...``(空值明示「(无)」;generic 有值时
        行尾追加 vivado-quirks §3 提醒 —— fileset generic 显示有值不保证
        综合实际生效);查询失败时一行 ``[DEGRADED]``(含具体原因,不阻塞主流程)。
    """
    try:
        ov = await session.execute(QUERY_FILESET_OVERRIDES, timeout=15.0)
    except Exception as e:
        logger.warning("查询 fileset 参数覆盖(generic/verilog_define)异常: %s", e)
        return [f"[DEGRADED] generic/verilog_define 查询异常: {e}"]

    if ov.is_error:
        logger.warning(
            "查询 fileset 参数覆盖失败(rc=%d): %s", ov.return_code, ov.output[:200]
        )
        return [
            f"[DEGRADED] generic/verilog_define 查询失败"
            f"(rc={ov.return_code}): {ov.output[:200]}"
        ]

    applied_generic = "(无)"
    applied_vdefine = "(无)"
    for ln in ov.output.splitlines():
        s = ln.strip()
        if s.startswith("VMCP_FS_OVERRIDE:generic="):
            applied_generic = s[len("VMCP_FS_OVERRIDE:generic="):].strip() or "(无)"
        elif s.startswith("VMCP_FS_OVERRIDE:verilog_define="):
            applied_vdefine = (
                s[len("VMCP_FS_OVERRIDE:verilog_define="):].strip() or "(无)"
            )
    generic_line = f"applied_generic: {applied_generic}"
    if applied_generic != "(无)":
        # vivado-quirks §3:fileset 级 generic 即便显示有值(综合日志都打印
        # bound to:)也可能没真生效(类型不匹配/缓存走错分支),行尾明示防误信。
        generic_line += (
            "(注意 2019.1 quirk: fileset generic 显示有值不保证综合实际生效,"
            "关键参数请核对 runme.log 的 'bound to:' 行,见 vivado-quirks §3)"
        )
    return [
        generic_line,
        f"applied_verilog_define: {applied_vdefine}",
    ]


# --------------------------------------------------------------------------- #
#  内部辅助：综合 / 实现共享的 launch-and-wait 逻辑
# --------------------------------------------------------------------------- #

def _resolve_session_vivado_version(session) -> str:
    raw_rt = getattr(session, "runtime_version", None)
    if isinstance(raw_rt, str):
        rt = raw_rt.strip()
        if rt and rt != "unknown":
            return rt
    raw_vp = getattr(session, "vivado_path", None)
    if isinstance(raw_vp, str) and raw_vp.strip():
        return get_vivado_version(raw_vp.strip())
    return ""


async def _collect_memory_guard_warning() -> str:
    with contextlib.suppress(Exception):
        return await asyncio.to_thread(
            lambda: format_memory_guard_warning(collect_system_telemetry())
        )
    return ""


def _sync_from_rp(ctx: Context, session_id: str, rp, *, until: str = "") -> None:
    if rp is None or not getattr(rp, "found", False):
        return
    run_dir = rp.run_dir or (str(Path(rp.log_path).parent) if rp.log_path else "")
    sync_shared_session(
        ctx,
        session_id=session_id,
        vivado_version=rp.vivado_version or None,
        project_name=rp.project_name or None,
        xpr_path=rp.xpr_path or None,
        run_name=rp.run_name or None,
        target_step=until or None,
        run_dir=run_dir or None,
        status=rp.state(),
        state=rp.status,
        progress=progress_percent(rp.progress),
        phase=rp.current_phase(),
        log_path=rp.log_path or None,
        log_tail=[{"line": t.lineno, "text": t.text} for t in rp.tail[-20:]],
    )


async def _launch_and_wait(
    session,
    run_name: str,
    jobs: int,
    timeout_minutes: int,
    label: str,
    ctx: Context,
    wait: bool = True,
    target_step: str | None = None,
    extra_notes: list[str] | None = None,
) -> str:
    """原子启动 run；按 wait 选择立即返回或轮询、open_run、诊断。

    不再调用 Tcl 的 `wait_on_run`（它会阻塞 Vivado event loop，
    GUI 模式下会冻住界面）。wait=True 时改用 Python 每 2 秒查一次
    STATUS/PROGRESS；wait=False 时返回 job_id，由 get_run_progress 查询。
    """
    timeout_sec = timeout_minutes * 60.0

    # ------------------- 0. PRD B4:读取实际生效的参数覆盖 -------------------
    # 在 reset_run 之前查 generic / verilog_define,结果里明示,
    # 防止"以为 set_property generic 生效了实际没设上"的隐性坑。
    override_lines = await _query_fileset_overrides(session)

    # ------------------- 1. 启动 -------------------
    try:
        launch_result = await session.execute(
            LAUNCH_RUN_IF_IDLE.format(run_name=run_name, jobs=jobs), timeout=60.0
        )
        if launch_result.is_error:
            return f"[ERROR] 启动 {label} 失败:\n{launch_result.output}"
    except Exception as e:
        return f"[ERROR] 启动 {label} 失败: {e}"

    launch_line = next(
        (
            line for line in launch_result.output.splitlines()
            if line.startswith("VMCP_RUN_LAUNCH|")
        ),
        None,
    )
    if launch_line is None:
        return f"[ERROR] 启动 {label} 失败: Vivado 未返回 VMCP_RUN_LAUNCH 状态标记"

    launch_parts = launch_line.split("|", 2)
    launch_state = launch_parts[1] if len(launch_parts) >= 2 else ""
    launch_status = launch_parts[2] if len(launch_parts) >= 3 else ""
    if launch_state == "busy":
        return (
            f"[BUSY] {label} run '{run_name}' 已在运行，未执行 reset_run。\n"
            f"状态: {launch_status}\n"
            f"请用 get_run_progress(run_name='{run_name}', "
            f"session_id='{session.session_id}') 查询。"
        )
    if launch_state == "missing":
        return f"[ERROR] 启动 {label} 失败: 找不到 run '{run_name}'"
    if launch_state != "started":
        return f"[ERROR] 启动 {label} 失败: 未知启动状态 {launch_state!r}"

    mem_warn = await _collect_memory_guard_warning()
    extra_lines = list(extra_notes or [])
    if mem_warn:
        extra_lines.append(mem_warn)

    sync_shared_session(
        ctx,
        session_id=session.session_id,
        run_name=run_name,
        target_step=target_step or "",
        status="running",
        state=launch_status or "Running",
    )

    if not wait:
        wait_target = target_step or "complete"
        result_parts = [
            f"{label}已异步启动。",
            f"job_id: {session.session_id}:{run_name}",
            f"状态: {launch_status or '已提交'}",
            (
                f"查询: get_run_progress(run_name='{run_name}', "
                f"session_id='{session.session_id}')"
            ),
            (
                f"等待: wait_run(run_name='{run_name}', until='{wait_target}', "
                f"session_id='{session.session_id}')（服务端轮询，不要 sleep 后反复查询）"
            ),
        ]
        result_parts.extend(override_lines)
        result_parts.extend(extra_lines)
        return "\n".join(result_parts)

    # ------------------- 2. 轮询 -------------------
    try:
        outcome, final_status, final_progress, final_elapsed = (
            await _poll_run_until_done(
                session, run_name, timeout_sec, ctx, target_step=target_step
            )
        )
    except Exception as e:
        return f"[ERROR] 轮询 {label} 状态失败: {e}"
    if outcome == "timeout":
        sync_shared_session(
            ctx,
            session_id=session.session_id,
            run_name=run_name,
            target_step=target_step or "",
            status="running",
            state=final_status,
            progress=progress_percent(final_progress),
        )
        msg = (
            f"[ERROR] 等待{label}超时（{timeout_minutes} 分钟），最后状态: {final_status}。"
            "run 未取消，可继续查询。"
        )
        if extra_lines:
            msg += "\n" + "\n".join(extra_lines)
        return msg

    # ------------------- 3. B4 修复：自动 open_run -------------------
    # 综合/实现完成后自动打开设计，让紧随其后的 report_* / report_io 能工作。
    # catch 保护：run 可能已经打开（无害），或有其他运行时错误
    # 注意:catch 吞异常后外层 return_code=0,所以不能只看 is_error。
    # 必须把 $__open_err 的内容 puts 出来,Python 侧检测 VMCP_OPEN_ERR: 前缀。
    open_note = ""
    final_state = run_state(final_status, target_step)
    sync_shared_session(
        ctx,
        session_id=session.session_id,
        run_name=run_name,
        target_step=target_step or "",
        status=final_state,
        state=final_status,
        progress=progress_percent(final_progress),
    )
    if final_state == "completed":
        try:
            open_result = await session.execute(
                f"if {{[catch {{ open_run {run_name} }} __open_err]}} "
                f'{{ puts "VMCP_OPEN_ERR:$__open_err" }}',
                timeout=120.0,
            )
            # 外层 is_error (Tcl 语法错等) 和内层 VMCP_OPEN_ERR 都要看
            err_line = next(
                (ln for ln in open_result.output.splitlines()
                 if ln.startswith("VMCP_OPEN_ERR:")),
                None,
            )
            if open_result.is_error:
                open_note = f"(open_run 自动打开失败: {open_result.output[:200]})"
            elif err_line:
                inner = err_line[len("VMCP_OPEN_ERR:"):].strip()
                # "already open" 这类无害信息不告警
                if "already" not in inner.lower():
                    open_note = f"(open_run 返回错误: {inner[:200]})"
        except Exception as e:
            open_note = f"(open_run 自动打开异常: {e})"

    # ------------------- 4. 诊断概览 -------------------
    result_parts: list[str] = [
        f"--- {label}结果 ---",
        f"状态: {final_status}",
        f"进度: {final_progress or '(未知)'}",
        f"耗时: {final_elapsed}",
    ]
    result_parts.extend(override_lines)
    result_parts.extend(extra_lines)
    if final_state in {"failed", "cancelled"}:
        state_label = "已取消" if final_state == "cancelled" else "失败"
        result_parts.insert(0, f"[ERROR] {label}{state_label}。")
    if open_note:
        result_parts.append(open_note)

    try:
        diag_result = await session.execute(
            COUNT_WARNINGS.format(run_name=run_name), timeout=30.0
        )
        if diag_result.is_error:
            # 计数命令本身失败:不能把 -1 哨兵打成数字误导 AI,显式降级
            result_parts.append(
                f"\n[DEGRADED] 诊断计数不可用(rc={diag_result.return_code}): "
                f"{diag_result.output[:200]}"
            )
        else:
            errors, cw, w = parse_diag_counts(diag_result.output)
            if errors < 0 or cw < 0:
                # -1 哨兵 = runme.log 缺失或输出无 VMCP_DIAG 标记
                result_parts.append(
                    "\n[DEGRADED] 诊断计数不可用: "
                    "runme.log 缺失或未匹配到 VMCP_DIAG 标记"
                )
            else:
                if cw > 0:
                    result_parts.insert(
                        0,
                        f"!! 发现 {cw} 条 CRITICAL WARNING !! "
                        "建议立即运行 get_critical_warnings 查看分类详情和修复建议。",
                    )
                if errors > 0:
                    result_parts.insert(
                        0,
                        f"!! 发现 {errors} 条 ERROR !! 请检查 runme.log 详情。",
                    )
                result_parts.append(
                    f"\n诊断概览: errors={errors},"
                    f" critical_warnings={cw}, warnings={w}"
                )
    except Exception as e:
        # 诊断失败不阻塞主流程，但要告诉用户原因（1.4 错误处理铁律）
        result_parts.append(f"\n（诊断统计失败: {e}）")

    return "\n".join(result_parts)


# --------------------------------------------------------------------------- #
#  工具定义
# --------------------------------------------------------------------------- #

@mcp.tool()
async def run_synthesis(
    run_name: str = "synth_1",
    jobs: int = 4,
    timeout_minutes: int = 30,
    session_id: str = "default",
    ctx: Context = None,
    wait: bool = True,
) -> str:
    """在已有会话中 ``launch_runs`` 综合（不另开进程，不阻塞 GUI）。
    wait=False 立即返回 job_id 供 wait_run/get_run_progress。"""
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    return await _launch_and_wait(
        session, run_name, jobs, timeout_minutes, "综合", ctx,
        wait=wait, target_step="synth_design",
    )


@mcp.tool()
async def run_implementation(
    run_name: str = "impl_1",
    jobs: int = 4,
    timeout_minutes: int = 60,
    session_id: str = "default",
    ctx: Context = None,
    wait: bool = True,
    skip_route_power_report: bool = False,
) -> str:
    """启动实现（布局布线）；在 ``session_id`` 已有会话内 ``launch_runs``，不阻塞 GUI。

    STATUS 含 ERROR 时只 ``reset_run -prev_step`` 续跑，保留已完成的 place/physopt。
    Windows Vivado 2024.x 若在 route 后自动 ``report_power`` 处静默退出，
    设 ``skip_route_power_report=True``。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    extra_notes: list[str] = []
    if skip_route_power_report:
        try:
            pwr_res = await session.execute(
                f"set __r [get_runs -quiet {run_name}]\n"
                "if {[llength $__r] > 0} {\n"
                f"    set __rcfg [get_report_configs -quiet "
                f"{run_name}_route_report_power_0 -of_objects $__r]\n"
                "    if {[llength $__rcfg] > 0} {\n"
                "        set_property IS_ENABLED false $__rcfg\n"
                '        puts "VMCP_RPWR_DISABLED"\n'
                "    }\n"
                "}",
                timeout=15.0,
            )
            if "VMCP_RPWR_DISABLED" in (getattr(pwr_res, "output", "") or ""):
                extra_notes.append(
                    f"已禁用 {run_name}_route_report_power_0（跳过 route 后自动功耗报告，"
                    "完成后可从 routed/physopt.dcp 单独运行 report_power）。"
                )
            elif not getattr(pwr_res, "is_error", False):
                extra_notes.append(
                    f"未找到 {run_name}_route_report_power_0，无需额外禁用。"
                )
        except Exception as exc:
            extra_notes.append(
                f"[WARN] 禁用 {run_name}_route_report_power_0 失败: {exc}"
            )
    else:
        ver = _resolve_session_vivado_version(session)
        if sys.platform == "win32" and ver.startswith("2024."):
            try:
                chk_res = await session.execute(
                    f"set __r [get_runs -quiet {run_name}]\n"
                    "if {[llength $__r] > 0} {\n"
                    f"    set __rcfg [get_report_configs -quiet "
                    f"{run_name}_route_report_power_0 -of_objects $__r]\n"
                    "    if {[llength $__rcfg] > 0} {\n"
                    "        set __en [get_property IS_ENABLED $__rcfg]\n"
                    "        if {[string length $__en] && $__en} {\n"
                    '            puts "VMCP_RPWR_ENABLED"\n'
                    "        }\n"
                    "    }\n"
                    "}",
                    timeout=10.0,
                )
                if "VMCP_RPWR_ENABLED" in (getattr(chk_res, "output", "") or ""):
                    extra_notes.append(
                        f"[WARN] Windows Vivado {ver} 的 {run_name}_route_report_power_0 "
                        "已启用，route 后自动 report_power 存在静默退出风险；"
                        "建议传 skip_route_power_report=True，需要功耗再单独运行 report_power。"
                    )
            except Exception:
                pass

    return await _launch_and_wait(
        session, run_name, jobs, timeout_minutes, "实现", ctx,
        wait=wait, target_step="route_design", extra_notes=extra_notes,
    )


@mcp.tool()
async def generate_bitstream(
    impl_run: str = "impl_1",
    jobs: int = 4,
    timeout_minutes: int = 30,
    force: bool = False,
    session_id: str = "default",
    ctx: Context = None,
    wait: bool = True,
) -> str:
    """生成比特流（不重置 route）。默认发现 CW 时拦截，confirm 无风险传 force=True；
    wait=False 配合 wait_run(until='write_bitstream')。"""
    try:
        impl_run = validate_identifier(impl_run, "impl_run")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 前置安全检查：force=False 时检测 CRITICAL WARNING。
    # 检查本身失败时不拦截,但必须降级为显式 [DEGRADED](追加进最终返回),
    # 不允许静默放行 —— 否则"未布线/日志不可读"这类信号会被吞掉。
    precheck_warn = ""
    if not force:
        try:
            pre_result = await session.execute(
                CHECK_PRE_BITSTREAM.format(impl_run=impl_run), timeout=30.0
            )
            status, cw_count, samples = parse_pre_bitstream(pre_result.output)

            if pre_result.is_error or cw_count < 0:
                # Tcl 报错(run 不存在/无项目)或输出无 VMCP_PRE_BIT 标记(-1 哨兵)
                reason = (
                    f"rc={pre_result.return_code}, 输出: {pre_result.output[:200]}"
                    if pre_result.is_error
                    else "输出未匹配到 VMCP_PRE_BIT 标记(run 不存在或日志不可读)"
                )
                logger.warning("bitstream 前置安全检查未能执行: %s", reason)
                precheck_warn = (
                    f"[DEGRADED] 前置 CW 安全检查未能执行: {reason}。"
                    "本次生成未经 CRITICAL WARNING 门禁。"
                )
            elif cw_count > 0:
                lines = [
                    f"!! 安全检查未通过: 发现 {cw_count} 条 CRITICAL WARNING !!",
                    f"实现状态: {status}",
                    "",
                    "前 10 条 CRITICAL WARNING 样本:",
                ]
                for s in samples:
                    lines.append(f"  - {s}")
                lines.append("")
                lines.append(
                    "建议: 先运行 get_critical_warnings 查看详情并修复。"
                )
                lines.append(
                    "如确认可忽略，请使用 force=True 跳过安全检查。"
                )
                return "\n".join(lines)
        except Exception as e:
            # 安全检查本身失败不应阻塞——降级为跳过检查。但一定要告诉用户:
            # 否则"未布线"这种致命信号会被静默吞,用户以为一切正常继续跑。
            logger.warning(
                "bitstream 前置安全检查失败,降级跳过: %s: %s",
                type(e).__name__, e,
            )
            precheck_warn = (
                f"[DEGRADED] 前置 CW 安全检查失败: {type(e).__name__}: {e}。"
                "本次生成未经 CRITICAL WARNING 门禁。"
            )

    # D5 架构同步到 bitstream:不再用 Tcl wait_on_run(阻塞 Vivado event loop,
    # GUI 模式下界面冻住)。改 Python 轮询 STATUS/PROGRESS,界面保持响应 +
    # 提供进度反馈。
    timeout_sec = timeout_minutes * 60.0

    # 启动 —— 到 write_bitstream step 为止,不重置 route 结果
    try:
        launch_result = await session.execute(
            f"launch_runs {impl_run} -to_step write_bitstream -jobs {jobs}",
            timeout=60.0,
        )
        if launch_result.is_error:
            return f"[ERROR] 启动比特流生成失败:\n{launch_result.output}"
    except Exception as e:
        return f"[ERROR] 启动比特流生成失败: {e}"

    sync_shared_session(
        ctx,
        session_id=session.session_id,
        run_name=impl_run,
        target_step="write_bitstream",
        status="running",
        state="write_bitstream Running",
    )

    if not wait:
        lines = [
            "比特流生成已异步启动。",
            f"job_id: {session.session_id}:{impl_run}",
            (
                f"等待: wait_run(run_name='{impl_run}', until='write_bitstream', "
                f"session_id='{session.session_id}')（服务端轮询，不要 sleep 后反复查询）"
            ),
            (
                f"查询: get_run_progress(run_name='{impl_run}', "
                f"session_id='{session.session_id}')"
            ),
        ]
        if precheck_warn:
            lines.append(precheck_warn)
        return "\n".join(lines)

    # 轮询(与 _launch_and_wait 共用 _poll_run_until_done,单一来源)
    try:
        outcome, final_status, final_progress, final_elapsed = (
            await _poll_run_until_done(
                session, impl_run, timeout_sec, ctx, target_step="write_bitstream"
            )
        )
    except Exception as e:
        return f"[ERROR] 轮询比特流状态失败: {e}"
    if outcome == "timeout":
        sync_shared_session(
            ctx,
            session_id=session.session_id,
            run_name=impl_run,
            target_step="write_bitstream",
            status="running",
            state=final_status,
            progress=progress_percent(final_progress),
        )
        return (
            f"[ERROR] 等待比特流生成超时({timeout_minutes} 分钟)。"
            f"最后状态: {final_status},进度: {final_progress or '(未知)'}。"
            "run 未取消，可继续查询。"
        )

    if run_state(final_status, "write_bitstream") != "completed":
        sync_shared_session(
            ctx,
            session_id=session.session_id,
            run_name=impl_run,
            target_step="write_bitstream",
            status=run_state(final_status, "write_bitstream"),
            state=final_status,
            progress=progress_percent(final_progress),
        )
        return (
            f"[ERROR] 生成比特流未完成。\n状态: {final_status}\n"
            f"进度: {final_progress or '(未知)'}\n耗时: {final_elapsed}\n"
            f"建议:运行 get_critical_warnings(run_name='{impl_run}') 查看详情。"
        )

    # 查比特流输出目录
    try:
        bit_result = await session.execute(
            f'set d [get_property DIRECTORY [get_runs {impl_run}]]\n'
            f'puts "VMCP_BITDIR:$d"',
            timeout=10.0,
        )
        bit_dir = next(
            (ln[len("VMCP_BITDIR:"):].strip()
             for ln in bit_result.output.splitlines()
             if ln.startswith("VMCP_BITDIR:")),
            "(未能读取)",
        )
    except Exception as e:
        bit_dir = f"(查询失败: {e})"

    sync_shared_session(
        ctx,
        session_id=session.session_id,
        run_name=impl_run,
        target_step="write_bitstream",
        run_dir=bit_dir if bit_dir and not bit_dir.startswith("(") else None,
        status="completed",
        state=final_status,
        progress=progress_percent(final_progress),
    )

    result_text = (
        f"--- 比特流生成结果 ---\n"
        f"状态: {final_status}\n"
        f"进度: {final_progress or '(未知)'}\n"
        f"耗时: {final_elapsed}\n"
        f"比特流目录: {bit_dir}"
    )
    if precheck_warn:
        # 安全门检查失败时的显式降级标记:不拦截,但必须让 AI/用户看见
        result_text += f"\n{precheck_warn}"
    return result_text


_WAIT_MAX_TIMEOUT_SEC = 1800
# 已停在完成/未启动态时再确认一次的间隔：区分"真的停了"和"步骤之间的瞬时状态"。
_WAIT_CONFIRM_SEC = 5.0
_WAIT_OUTCOME_LABEL = {
    "reached": "已到达目标",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "stopped": "已停止，未到达目标",
    "idle": "未在运行",
    "unknown": "状态无法识别",
    "timeout": "等待超时（run 未取消）",
    "stalled": "疑似卡死（run 未取消）",
    "early_error": "提前捕获 ERROR（run 未取消）",
    "memory_critical": "主机内存告急（run 未取消）",
}

_QUERY_WAIT_META_TCL = """\
set __r [get_runs -quiet {run_name}]
if {{$__r ne ""}} {{
    puts "VMCP_WAIT_META:dir=[get_property DIRECTORY $__r]"
    catch {{puts "VMCP_WAIT_META:part=[get_property PART [current_project]]"}}
    catch {{
        set __p [current_project]
        set __pd [get_property DIRECTORY $__p]
        set __pn [get_property NAME $__p]
        set __xp [file join $__pd "$__pn.xpr"]
        if {{[file isfile $__xp]}} {{ puts "VMCP_WAIT_META:xpr=$__xp" }}
    }}
}}
"""


async def _query_wait_meta(session, run_name: str) -> tuple[Path | None, Path | None, str]:
    """等待开始时只读查询一次 run_dir / xpr / part（缓存供后续磁盘观测）。"""
    if _session_is_busy(session):
        return None, None, ""
    try:
        res = await session.execute(_QUERY_WAIT_META_TCL.format(run_name=run_name), timeout=10.0)
    except Exception:
        return None, None, ""
    if getattr(res, "is_error", False):
        return None, None, ""
    run_dir: Path | None = None
    xpr_path: Path | None = None
    part = ""
    for line in (getattr(res, "output", "") or "").splitlines():
        s = line.strip()
        if s.startswith("VMCP_WAIT_META:dir="):
            raw_dir = s[len("VMCP_WAIT_META:dir="):].strip()
            if raw_dir:
                cand = Path(raw_dir)
                if cand.is_dir():
                    run_dir = cand
        elif s.startswith("VMCP_WAIT_META:xpr="):
            raw_xpr = s[len("VMCP_WAIT_META:xpr="):].strip()
            if raw_xpr and Path(raw_xpr).is_file():
                xpr_path = Path(raw_xpr)
        elif s.startswith("VMCP_WAIT_META:part="):
            part = s[len("VMCP_WAIT_META:part="):].strip()
    return run_dir, xpr_path, part


@mcp.tool()
async def wait_run(
    run_name: str = "impl_1",
    until: str = "complete",
    timeout_seconds: int | None = None,
    poll_seconds: int = 15,
    session_id: str = "default",
    compact: bool = True,
    stall_seconds: int | None = None,
    fail_fast: bool = True,
    mem_guard: bool = True,
    ctx: Context = None,
) -> str:
    """只读轮询 run 至目标/完成/失败/超时，内置 10s 心跳与卡死/早错/内存早醒（超时不取消）。

    ``until`` 支持 ``complete`` 或 ``synth_design``..``write_bitstream``；
    ``timeout_seconds``/``stall_seconds`` 省略时按工程规模自适应（1~1800 / 0 禁用卡死）。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"
    until = (until or "").strip().lower()
    if until not in WAIT_UNTIL_CHOICES:
        return f"[ERROR] until 必须是 {', '.join(WAIT_UNTIL_CHOICES)} 之一（收到 {until!r}）。"
    if timeout_seconds is not None and not 1 <= timeout_seconds <= _WAIT_MAX_TIMEOUT_SEC:
        return (
            f"[ERROR] timeout_seconds 必须在 1~{_WAIT_MAX_TIMEOUT_SEC} 之间"
            f"（收到 {timeout_seconds}）；更长的构建请分次调用 wait_run。"
        )
    if stall_seconds is not None and stall_seconds < 0:
        return f"[ERROR] stall_seconds 不能为负数（收到 {stall_seconds}）。"
    if not 2 <= poll_seconds <= 300:
        return f"[ERROR] poll_seconds 必须在 2~300 之间（收到 {poll_seconds}）。"

    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    run_dir, xpr_path, part = await _query_wait_meta(session, run_name)
    profile = estimate_watch_profile(
        run_dir=run_dir,
        xpr_path=xpr_path,
        part=part,
        run_name=run_name,
        until=until,
    )
    eff_timeout = (
        min(profile.timeout_seconds, _WAIT_MAX_TIMEOUT_SEC)
        if timeout_seconds is None
        else int(timeout_seconds)
    )
    eff_stall = profile.stall_seconds if stall_seconds is None else int(stall_seconds)

    started = time.monotonic()
    deadline = started + eff_timeout
    polls = 0
    status = "UNKNOWN"
    outcome = "timeout"
    pending_settle: str | None = None
    last_percent = None
    last_heartbeat_at = started
    last_now = started
    tracker = LogWatchState(
        started_monotonic=started,
        stall_seconds=eff_stall,
        fail_fast=fail_fast,
        mem_guard=mem_guard,
    )
    log_file = (run_dir / "runme.log") if run_dir is not None else None
    vivado_pid = _extract_rst_pid(run_dir / ".vivado.begin.rst") if run_dir else None
    if vivado_pid is None:
        raw_pid = getattr(session, "pid", None)
        if isinstance(raw_pid, int) and raw_pid > 0:
            vivado_pid = raw_pid

    while True:
        percent = None
        if _session_is_busy(session):
            verdict = "busy"
        else:
            try:
                poll = await session.execute(
                    POLL_RUN_STATUS.format(run_name=run_name), timeout=15.0
                )
            except RuntimeError as e:
                # 状态检查与拿锁之间被其他命令抢先：只重试仍活跃的忙会话。
                if not _session_is_busy(session):
                    return f"[ERROR] 查询 run 状态失败: {e}。run 未取消，可稍后再次 wait_run。"
                verdict = "busy"
            except Exception as e:
                return f"[ERROR] 查询 run 状态失败: {e}。run 未取消，可稍后再次 wait_run。"
            else:
                if poll.is_error:
                    return (
                        f"[ERROR] Vivado 状态查询失败: {poll.output[:500]}\n"
                        "请确认工程已打开且 run 名正确（run_tcl 'get_runs' 可列出）。"
                    )
                line = next(
                    (ln for ln in poll.output.splitlines() if ln.startswith("VMCP_POLL|")),
                    None,
                )
                parts = line[len("VMCP_POLL|"):].split("|") if line else []
                if len(parts) < 2 or not parts[0].strip():
                    return (
                        "[ERROR] Vivado 未返回完整的 VMCP_POLL 状态标记；"
                        "run 未取消，可继续查询。"
                    )
                polls += 1
                status = parts[0].strip()
                percent = progress_percent(parts[1].strip())
                verdict = evaluate_wait(status, until)

        if verdict in {"reached", "failed", "cancelled"}:
            if ctx is not None and percent is not None and percent != last_percent:
                await report_mcp_progress(
                    ctx,
                    progress=percent,
                    total=100,
                    message=format_heartbeat_message(
                        status=status,
                        phase=tracker.latest_phase,
                        elapsed_sec=max(0.0, time.monotonic() - started),
                        ram_pct=tracker.ram_pct,
                    ),
                )
                last_percent = percent
            outcome = verdict
            break
        if verdict in {"settled", "idle", "unknown"}:
            if pending_settle == status:
                if verdict == "settled":
                    reached_end = until == "complete" and run_state(status) == "completed"
                    outcome = "completed" if reached_end else "stopped"
                else:
                    outcome = verdict
                break
            pending_settle = status
        elif verdict != "busy":
            pending_settle = None

        now = time.monotonic()
        last_now = now

        if log_file is not None:
            begin_mtime: float | None = None
            if run_dir is not None:
                begin_rst = run_dir / ".vivado.begin.rst"
                fresh_pid = _extract_rst_pid(begin_rst)
                if fresh_pid is not None:
                    vivado_pid = fresh_pid
                if not _is_link_or_junction(begin_rst):
                    with contextlib.suppress(OSError):
                        begin_mtime = begin_rst.stat().st_mtime
            tracker.scan_log(log_file, now, begin_mtime=begin_mtime)
            if fail_fast and tracker.fatal_errors:
                settle_sec = min(2.0, max(0.05, deadline - now))
                await asyncio.sleep(settle_sec)
                tracker.scan_log(log_file, time.monotonic(), begin_mtime=begin_mtime)
                outcome = "early_error"
                break

        mem_crit, ram_pct, _ = tracker.check_memory()
        if mem_crit:
            outcome = "memory_critical"
            break

        if run_dir is not None and verdict == "running":
            is_stalled = await asyncio.to_thread(tracker.evaluate_stall, now, vivado_pid)
            if is_stalled:
                outcome = "stalled"
                break

        elapsed_now = max(0.0, now - started)
        percent_changed = percent is not None and percent != last_percent
        heartbeat_due = (now - last_heartbeat_at) >= PROGRESS_HEARTBEAT_SEC
        if ctx is not None and (percent_changed or heartbeat_due):
            msg = format_heartbeat_message(
                status=status,
                phase=tracker.latest_phase,
                elapsed_sec=elapsed_now,
                ram_pct=ram_pct,
                cpu_active_grace=tracker.cpu_active_grace,
            )
            await report_mcp_progress(
                ctx,
                progress=percent if percent is not None else round(elapsed_now, 1),
                total=100 if percent is not None else float(eff_timeout),
                message=msg,
            )
            last_heartbeat_at = now
            if percent is not None:
                last_percent = percent

        remaining = deadline - now
        if remaining <= 0:
            outcome = "timeout"
            break
        interval = _WAIT_CONFIRM_SEC if pending_settle is not None else poll_seconds
        await asyncio.sleep(min(interval, remaining))

    waited = int(time.monotonic() - started)
    label = _WAIT_OUTCOME_LABEL[outcome]
    if outcome == "reached" and until != "complete":
        label = f"已到达 {until}"
    header = [
        f"=== wait_run: {run_name} until={until} → {label} ===",
        f"等待 {waited} 秒，查询 {polls} 次（间隔 {poll_seconds} 秒）；最后状态: {status}",
    ]
    if outcome == "timeout":
        header.append(
            f"run 仍在执行且未被取消（阈值 {profile.tier}: stall={eff_stall}s, "
            f"timeout={eff_timeout}s; {profile.basis}）。"
            f"可再次 wait_run(run_name='{run_name}', until='{until}', "
            f"session_id='{session.session_id}')；不要 sleep 后反复查询。"
        )
    elif outcome == "stalled":
        silent_sec = int(max(0.0, last_now - tracker.last_activity_at))
        cpu_note = (
            f"CPU {tracker.last_cpu_pct:.1f}%"
            if tracker.last_cpu_pct is not None
            else "无活跃 CPU 信号"
        )
        header.append(
            f"run 未取消！日志连续 {silent_sec} 秒未增长且无新阶段（{cpu_note}；"
            f"阈值 {eff_stall}s, {profile.tier}）。"
        )
    elif outcome == "early_error":
        header.append(
            f"run 未取消！提前捕获 {len(tracker.fatal_errors)} 条 ERROR"
            f"（{profile.tier}）："
        )
        for err_ln in tracker.fatal_errors[:5]:
            header.append(f"  {err_ln[:200]}")
    elif outcome == "memory_critical":
        header.append(
            f"run 未取消！主机内存危急：{tracker.ram_pct:.1f}% "
            f"（可用 {tracker.ram_avail_gb:.1f} GB）；建议排查孤儿 vivado.exe 或降低并行。"
        )
    elif outcome == "stopped":
        header.append(
            f"run 已不在运行，但停在 {status}，未到达 {until}；"
            "确认是否用 -to_step 提前结束，或需要继续 launch。"
        )

    use_compact = bool(compact) and outcome not in {"failed", "early_error"}
    rp, report = await build_run_progress_report(
        session,
        run_name,
        tail_lines=5 if use_compact else 30,
        compact=use_compact,
    )
    if outcome in {"completed", "reached"}:
        summary_dir = run_dir or (Path(rp.run_dir) if rp and getattr(rp, "run_dir", "") else None)
        if summary_dir is not None and summary_dir.is_dir():
            signoff = await asyncio.to_thread(
                extract_compact_signoff, summary_dir, run_name=run_name
            )
            signoff_line = format_signoff_line(signoff)
            if signoff_line:
                header.append(signoff_line)

    _sync_from_rp(ctx, session.session_id, rp, until=until)
    return "\n".join(header) + "\n\n" + report


@mcp.tool()
async def program_device(
    bitstream_path: str,
    target: str = "*",
    hw_server_url: str = "localhost:3121",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """将 ``.bit`` 比特流烧录到 FPGA（掉电不保存，SPI flash/QSPI 须用
    write_cfgmem 写 .mcs/.bin）；封装 hw_manager 连接与预检。"""
    # 路径预检:避免半路 program_hw_devices 才报 "file not found",
    # 此时 hw_server / hw_target 已连上,留下脏状态。
    import os
    if not os.path.isfile(bitstream_path):
        return (
            f"[ERROR] 比特流文件不存在: {bitstream_path}\n"
            "提示:先 generate_bitstream 或确认路径(常见位置:"
            "<proj>.runs/impl_1/<top>.bit)"
        )
    if not bitstream_path.lower().endswith(".bit"):
        return (
            f"[ERROR] 文件扩展名不是 .bit: {bitstream_path}\n"
            "program_device 只接受 .bit 文件(.bin/.mcs 用 write_cfgmem 烧 flash,"
            "配方见本工具 docstring)"
        )

    # 轻量参数校验:两者都会裸拼进 Tcl,空串/含 Tcl 特殊字符直接报清晰错误,
    # 而不是半路炸出难懂的 Tcl 解析错(此时 hw_server 可能已连上,留脏状态)。
    _TCL_UNSAFE = set(' \t\n;$[]{}"\\')
    if not target or any(c in _TCL_UNSAFE for c in target):
        # 文案与实现对齐:实现是黑名单(只拒会破坏 Tcl 裸拼词法的字符)
        return (
            f"[ERROR] target 为空或含非法字符: {target!r}。"
            "不允许空白/;/$/[]/{}/引号/反斜杠"
            "(如 '*' 或 'localhost:3121/xilinx_tcf/...')"
        )
    if not hw_server_url or any(c in _TCL_UNSAFE or c == "*" for c in hw_server_url):
        return (
            f"[ERROR] hw_server_url 为空或含非法字符: {hw_server_url!r}。"
            "应形如 'localhost:3121' 或 '<主机名/IP>:<端口>'"
        )

    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    bit_tcl = to_tcl_path(bitstream_path)

    tcl = (
        f'open_hw_manager\n'
        f'connect_hw_server -url {hw_server_url}\n'
        f'open_hw_target [lindex [get_hw_targets {target}] 0]\n'
        f'set dev [lindex [get_hw_devices] 0]\n'
        f'current_hw_device $dev\n'
        f'set_property PROGRAM.FILE {bit_tcl} $dev\n'
        f'program_hw_devices $dev\n'
        f'puts "编程完成: $dev"'
    )

    return await _safe_execute(session, tcl, 60.0, "编程设备失败")
