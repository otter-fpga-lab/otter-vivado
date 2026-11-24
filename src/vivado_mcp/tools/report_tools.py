"""报告工具：get_io_report / get_timing_report。

两个工具都把 Vivado 原始表格文本解析为结构化数据（JSON / 格式化文本），
便于 LLM 精确提取数值。通用的报告命令（utilization / power / drc 等）
请直接用 ``run_tcl("report_xxx -return_string")``，无需包装。
"""

import json
import logging
from pathlib import Path

from mcp.server.mcpserver import Context

from vivado_mcp.analysis.io_parser import parse_report_io
from vivado_mcp.analysis.ip_status_parser import format_ip_status_report, parse_ip_status
from vivado_mcp.analysis.project_parser import format_project_info, parse_project_info
from vivado_mcp.analysis.run_progress_parser import progress_percent
from vivado_mcp.analysis.suggestion_engine import format_suggestion, suggest_next
from vivado_mcp.analysis.timing_parser import (
    format_timing_report,
    parse_timing_summary,
    parse_violating_paths,
)
from vivado_mcp.analysis.util_parser import format_utilization_report, parse_utilization
from vivado_mcp.analysis.warning_parser import parse_diag_counts, parse_pre_bitstream
from vivado_mcp.run_feedback import build_run_progress_report
from vivado_mcp.server import _NO_SESSION, _require_session, mcp, sync_shared_session
from vivado_mcp.tcl_scripts import (
    CHECK_PRE_BITSTREAM,
    COUNT_WARNINGS,
    QUERY_PROJECT_INFO,
    REPORT_VIOLATING_PATHS,
)
from vivado_mcp.vivado.tcl_utils import validate_identifier

logger = logging.getLogger(__name__)


@mcp.tool()
async def get_io_report(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取结构化 IO 引脚报告（JSON）。

    执行 report_io 并解析为结构化数据，包含：
    - 每个端口的引脚、站点、方向、IO 标准、Bank
    - GT / GPIO 类型自动判定
    - 汇总统计（总数、GT 数、GPIO 数、未分配数）

    Args:
        session_id: 目标会话 ID。
    """
    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 直接获取完整输出（不经 _safe_execute，避免 .summary 截断）
    try:
        result = await session.execute(
            "report_io -return_string", timeout=60.0
        )
        # B2 修复：命令失败时不要把错误文本送进 parser（否则解析出 0 个端口等假结果）
        if result.is_error:
            return (
                f"[ERROR] 获取 IO 报告失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_io 需要打开综合或实现后的设计。"
                "请先运行 run_synthesis 或 run_implementation。"
            )
        io_report = parse_report_io(result.output)
        return json.dumps(io_report.to_dict(), ensure_ascii=False, indent=2)
    except Exception as e:
        return f"[ERROR] 获取 IO 报告失败: {e}"


@mcp.tool()
async def get_timing_report(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取结构化时序报告。

    执行 report_timing_summary 并解析为结构化摘要 + 关键路径详情。
    返回人类可读的中文时序分析报告，包含 PASS/FAIL 状态判定。

    Args:
        session_id: 目标会话 ID。
    """
    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 报告对应当前打开的设计；阶段从同一报告头取得，不混用固定 run 的状态。
    try:
        result = await session.execute(
            "report_timing_summary -return_string", timeout=120.0
        )
        if result.is_error:
            return (
                f"[ERROR] 获取时序报告失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_timing_summary 需要打开综合或实现后的设计。"
                "请先运行 run_synthesis 或 run_implementation。"
            )
        timing_report = parse_timing_summary(result.output)

        # 时序违例 → 二次查询违例路径详情 + 模式嗅探 + 修复建议
        # 健康工程跳过不跑,省 10-30s。异常降级:不阻断主报告。
        # parse_status 非 ok(NA/格式不识别)时摘要不可信,跳过这次昂贵查询
        if timing_report.summary.parse_status == "ok" and not timing_report.summary.timing_met:
            try:
                paths_result = await session.execute(
                    REPORT_VIOLATING_PATHS, timeout=60.0
                )
                if paths_result.is_error:
                    err = (
                        f"rc={paths_result.return_code}: "
                        f"{paths_result.output[:200]}"
                    )
                    timing_report.violating_paths_error = err
                    logger.warning("违例路径查询返回错误: %s", err)
                else:
                    timing_report.violating_paths = parse_violating_paths(
                        paths_result.output
                    )
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                timing_report.violating_paths_error = err_msg
                logger.warning("违例路径查询异常: %s", err_msg)

        return format_timing_report(timing_report)
    except Exception as e:
        return f"[ERROR] 获取时序报告失败: {e}"


@mcp.tool()
async def check_bitstream_readiness(
    impl_run: str = "impl_1",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """烧板/生成比特流前门禁：只读检查实现完成态、时序收敛与 CW 计数，输出 READY / WARN / BLOCK。"""
    try:
        impl_run = validate_identifier(impl_run, "impl_run")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 1. 查询实现状态 + CW 计数 + 样本
    try:
        pre_result = await session.execute(
            CHECK_PRE_BITSTREAM.format(impl_run=impl_run), timeout=30.0
        )
        status, cw_count, samples = parse_pre_bitstream(pre_result.output)
    except Exception as e:
        return f"[ERROR] 查询实现状态失败: {e}"

    # 2. 查询时序摘要(尽力而为,失败不致命 —— 但一定要打出原因)
    timing_met = None
    timing_line = ""
    timing_err: str = ""
    timing_stage = "unknown"
    # True = 时序子查询失败(rc 错误/格式不识别/查询异常),判定本身不可信;
    # 与 NA(数据本来就不存在,WARN 语义成立)分流,前者 verdict 附 [DEGRADED]
    timing_degraded = False
    try:
        timing_raw = await session.execute(
            "report_timing_summary -return_string", timeout=60.0
        )
        if not timing_raw.is_error:
            tr = parse_timing_summary(timing_raw.output)
            timing_stage = tr.source_stage
            if tr.summary.parse_status == "ok":
                timing_line = (
                    f"  WNS = {tr.summary.wns:+.3f} ns  WHS = {tr.summary.whs:+.3f} ns  "
                    f"失败端点 = {tr.summary.failing_endpoints}/{tr.summary.total_endpoints}"
                )
                if timing_stage == "post-route":
                    timing_met = tr.summary.timing_met
                else:
                    timing_err = f"当前报告阶段为 {timing_stage}，未确认布线后时序"
                    timing_degraded = True
            else:
                # NA / 格式不识别:timing_met 保持 None 走"未能读取"提示,
                # 绝不能把全零占位摘要当 PASS/FAIL 参与判定
                if tr.summary.parse_status == "no_timing_data":
                    timing_err = "时序摘要为 NA(设计无时序约束或无可分析端点)"
                else:
                    timing_err = "时序报告格式不识别,未能解析 Design Timing Summary"
                    timing_degraded = True
                logger.warning(
                    "check_bitstream_readiness 时序摘要降级(parse_status=%s): %s",
                    tr.summary.parse_status, timing_err,
                )
        else:
            timing_err = (
                f"report_timing_summary rc={timing_raw.return_code}: "
                f"{timing_raw.output[:200]}"
            )
            timing_degraded = True
    except Exception as e:
        timing_err = f"{type(e).__name__}: {e}"
        timing_degraded = True
        logger.warning("check_bitstream_readiness 时序查询失败: %s", timing_err)

    # 3. 判定总体结论
    is_routed = "route_design Complete" in status or "write_bitstream" in status
    has_impl_error = "ERROR" in status.upper()

    blockers: list[str] = []
    warnings_list: list[str] = []

    if has_impl_error:
        blockers.append(f"{impl_run} 执行错误: {status}")
    elif not is_routed:
        blockers.append(f"{impl_run} 未完成布线(当前状态: {status or '未启动'})")

    if timing_met is False:
        blockers.append("时序违例(WNS/WHS 为负)")
    elif timing_met is None and is_routed:
        # 把具体失败原因显示出来,而不是笼统的"不可用"
        detail = f": {timing_err}" if timing_err else ""
        warnings_list.append(f"未能读取时序摘要{detail}")

    if cw_count > 0:
        if cw_count >= 5:
            blockers.append(f"CRITICAL WARNING 数量过多: {cw_count} 条")
        else:
            warnings_list.append(f"存在 {cw_count} 条 CRITICAL WARNING,建议排查")
    elif cw_count < 0:
        warnings_list.append("CRITICAL WARNING 计数不可用")

    # 4. 构造报告
    if blockers:
        verdict = "BLOCK (阻塞,不建议生成比特流)"
    elif warnings_list:
        verdict = "WARN (存在待核对的检查项)"
    else:
        verdict = "READY (本次检查通过)"

    # 时序子查询失败 ≠ 数据不存在:判定不可信,verdict 行附 [DEGRADED] 标记
    # (BLOCK 由实现状态等可信信号触发,结论已足够强,不叠加标记)
    if timing_degraded and not blockers:
        verdict += " [DEGRADED]"

    out: list[str] = [f"=== 烧板前检查: {verdict} ==="]
    out.append(f"实现状态: {status or 'UNKNOWN'}")
    out.append(f"CRITICAL WARNING: {cw_count if cw_count >= 0 else '无法读取'}")
    out.append(f"当前打开设计的报告阶段: {timing_stage}")
    if timing_line:
        out.append("时序摘要:")
        out.append(timing_line)

    if blockers:
        out.append("")
        out.append("阻塞问题:")
        for b in blockers:
            out.append(f"  [X] {b}")
    if warnings_list:
        out.append("")
        out.append("风险提示:")
        for w in warnings_list:
            out.append(f"  [!] {w}")

    if samples and cw_count > 0:
        out.append("")
        out.append(f"CRITICAL WARNING 样本(前 {min(len(samples), 5)} 条):")
        for s in samples[:5]:
            out.append(f"  - {s}")

    if blockers:
        out.append("")
        out.append("建议: 运行 get_critical_warnings 查看详情,修复后再烧板。")

    return "\n".join(out)


@mcp.tool()
async def get_utilization_report(
    detail: bool = False,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取当前打开设计的资源占用摘要（LUT/FF/BRAM/DSP/IO），
    高亮 >70%/90% 风险；detail=True 附 RAMB36/18 明细。"""
    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(
            "report_utilization -return_string", timeout=60.0
        )
        if result.is_error:
            return (
                f"[ERROR] 获取资源占用失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_utilization 需要打开综合或实现后的设计。"
            )
        report = parse_utilization(result.output)
        return format_utilization_report(report, detail=detail)
    except Exception as e:
        return f"[ERROR] 获取资源占用失败: {e}"


@mcp.tool()
async def get_project_info(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取当前 Vivado 项目的综合信息(项目名 / part / 顶层 / 文件列表 / IP / run 状态)。

    一次查询完成"摸底":AI 接手陌生项目时的起点。包含:
    - 项目名称、目录、Part 型号、顶层模块
    - 所有源文件(按类型分组)
    - XDC 约束文件列表
    - IP 实例列表(含 VLNV)
    - synth_1 / impl_1 的当前状态

    Args:
        session_id: 目标会话 ID。
    """
    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(QUERY_PROJECT_INFO, timeout=30.0)
        if result.is_error:
            return (
                f"[ERROR] 获取项目信息失败（rc={result.return_code}）：\n"
                f"{result.output}"
            )
        info = parse_project_info(result.output)
        if info.name or info.directory:
            xpr_cand = (
                Path(info.directory) / f"{info.name}.xpr"
                if info.directory and info.name
                else None
            )
            sync_shared_session(
                ctx,
                session_id=session_id,
                project_name=info.name or None,
                xpr_path=(
                    str(xpr_cand)
                    if xpr_cand is not None and xpr_cand.is_file()
                    else None
                ),
            )
        return format_project_info(info)
    except Exception as e:
        return f"[ERROR] 获取项目信息失败: {e}"


@mcp.tool()
async def get_run_progress(
    run_name: str = "impl_1",
    tail_lines: int = 30,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """查询指定 run 的状态、百分比、Phase 进度、日志尾部及静默退出/钩子告警。
    等待阶段完成请用 ``wait_run``。"""
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    if tail_lines < 0 or tail_lines > 500:
        return "[ERROR] tail_lines 必须在 0~500 之间"

    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    rp, text = await build_run_progress_report(session, run_name, tail_lines)
    if rp is not None and rp.found:
        run_dir = rp.run_dir or (str(Path(rp.log_path).parent) if rp.log_path else "")
        sync_shared_session(
            ctx,
            session_id=session.session_id,
            vivado_version=rp.vivado_version or None,
            project_name=rp.project_name or None,
            xpr_path=rp.xpr_path or None,
            run_name=rp.run_name or run_name,
            run_dir=run_dir or None,
            status=rp.state(),
            state=rp.status,
            progress=progress_percent(rp.progress),
            phase=rp.current_phase(),
            log_path=rp.log_path or None,
            log_tail=[{"line": t.lineno, "text": t.text} for t in rp.tail[-20:]],
        )
    return text


@mcp.tool()
async def get_next_suggestion(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """根据当前项目工程与 run 状态推断下一步建议。"""
    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(QUERY_PROJECT_INFO, timeout=30.0)
        if result.is_error:
            return (
                f"[ERROR] 获取项目信息失败（rc={result.return_code}）：\n"
                f"{result.output}"
            )
        info = parse_project_info(result.output)
        sug = suggest_next(info)
        return format_suggestion(info, sug)
    except Exception as e:
        return f"[ERROR] 生成下一步建议失败: {e}"


@mcp.tool()
async def get_ip_status(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """检查项目中所有 IP 的版本状态(哪个需要升级、哪个已锁定)。

    老项目打开后 Vivado 常提示"N 个 IP 需要升级"。这个工具一次性列出:
    - 需要升级的 IP(Vivado 更新了更好的版本)
    - 已锁定的 IP(IS_LOCKED 属性为 TRUE,改动需先解锁)
    - 已最新的 IP

    附带升级建议(单个升级 / 全部升级 / 升级后验证)。

    Args:
        session_id: 目标会话 ID。
    """
    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(
            "report_ip_status -return_string", timeout=60.0
        )
        if result.is_error:
            return (
                f"[ERROR] 获取 IP 状态失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_ip_status 需要项目已打开且含 IP 实例。"
            )
        rep = parse_ip_status(result.output)
        return format_ip_status_report(rep)
    except Exception as e:
        return f"[ERROR] 获取 IP 状态失败: {e}"


@mcp.tool()
async def get_pre_commit_summary(
    impl_run: str = "impl_1",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """生成含项目/时序/资源/ERROR-CW 与 READY/WARN/BLOCK 门禁的 Markdown 提交摘要。"""
    try:
        impl_run = validate_identifier(impl_run, "impl_run")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 采样失败列表:任何一项失败都降级 verdict,commit 摘要不能撒谎
    # 空列表 = 全部采样成功,有元素 = 至少一项降级
    sample_failures: list[str] = []

    # 1. 项目信息
    info = None
    try:
        proj = await session.execute(QUERY_PROJECT_INFO, timeout=30.0)
        if not proj.is_error:
            info = parse_project_info(proj.output)
        else:
            sample_failures.append(f"项目信息 rc={proj.return_code}")
    except Exception as e:
        sample_failures.append(f"项目信息: {type(e).__name__}")
        logger.warning("pre_commit 查项目信息失败: %s", e)

    # 2. 时序
    timing = None
    try:
        tr = await session.execute(
            "report_timing_summary -return_string", timeout=60.0
        )
        if not tr.is_error:
            timing = parse_timing_summary(tr.output)
            if timing.summary.parse_status != "ok":
                # NA / 格式不识别:全零占位摘要不能进 commit 摘要(会撒谎),
                # 计入采样失败 → verdict 降级 DEGRADED
                reason = (
                    "NA,设计无时序约束"
                    if timing.summary.parse_status == "no_timing_data"
                    else "格式不识别"
                )
                sample_failures.append(f"时序解析降级({reason})")
                logger.warning("pre_commit 时序摘要解析降级: %s", reason)
                timing = None
            elif timing.source_stage != "post-route":
                # 保留有来源的局部数值，但不能用其他 run 完成把它升级为 READY。
                sample_failures.append(f"时序报告阶段未确认布线后({timing.source_stage})")
        else:
            sample_failures.append(f"时序 rc={tr.return_code}")
    except Exception as e:
        sample_failures.append(f"时序: {type(e).__name__}")
        logger.warning("pre_commit 查时序失败: %s", e)

    # 3. 资源
    util = None
    try:
        ur = await session.execute(
            "report_utilization -return_string", timeout=60.0
        )
        if not ur.is_error:
            util = parse_utilization(ur.output)
            if util.parse_error:
                # 格式不识别:空 resources 不能静默跳过资源段(会假 READY),
                # 比照时序分支计入采样失败 → verdict 降级 DEGRADED
                sample_failures.append("资源解析降级(格式不识别)")
                logger.warning("pre_commit 资源报告解析降级: %s", util.parse_error)
                util = None
        else:
            sample_failures.append(f"资源 rc={ur.return_code}")
    except Exception as e:
        sample_failures.append(f"资源: {type(e).__name__}")
        logger.warning("pre_commit 查资源失败: %s", e)

    # 4. CW 计数
    errs, cws, warns = -1, -1, -1
    try:
        cr = await session.execute(
            COUNT_WARNINGS.format(run_name=impl_run), timeout=15.0
        )
        if not cr.is_error:
            errs, cws, warns = parse_diag_counts(cr.output)
        else:
            sample_failures.append(f"CW 计数 rc={cr.return_code}")
    except Exception as e:
        sample_failures.append(f"CW 计数: {type(e).__name__}")
        logger.warning("pre_commit 查 CW 计数失败: %s", e)

    # 门禁判定
    verdict = "READY"
    problems: list[str] = []
    if timing is not None:
        if timing.summary.timing_met is False:
            problems.append("时序违例")
    if errs > 0:
        problems.append(f"{errs} 条 ERROR")
    if cws > 5:
        problems.append(f"{cws} 条 CW")

    if problems:
        verdict = "BLOCK" if (timing and timing.summary.timing_met is False) or errs > 0 else "WARN"

    # 关键:采样失败不能默认 READY —— 降级为 DEGRADED,避免误导 commit 摘要
    # 例如项目没打开,之前会输出"[READY]"但其实全是空洞,现在会明确告警。
    if sample_failures and verdict == "READY":
        verdict = "DEGRADED"

    # 格式化为 markdown
    out: list[str] = [f"## 工程摘要 [{verdict}]"]
    if info:
        out.append(f"- 项目: `{info.project_name}` / part `{info.part}` / top `{info.top}`")
        out.append(
            f"- 综合: {info.synth_status or '(未运行)'}"
            f" | 实现: {info.impl_status or '(未运行)'}"
        )
    if timing:
        s = timing.summary
        met = "✅" if s.timing_met else "❌"
        out.append(
            f"- 时序 {met}: WNS `{s.wns:+.3f} ns`, WHS `{s.whs:+.3f} ns`, "
            f"失败端点 `{s.failing_endpoints}/{s.total_endpoints}`"
        )
        out.append(f"- 当前设计报告阶段: `{timing.source_stage}`；{timing.source_detail}")
    if util and util.resources:
        # 挑最关心的 5 种
        core = {r.name: r for r in util.resources}
        nice = []
        for names in (("Slice LUTs", "CLB LUTs"), ("Slice Registers", "CLB Registers"),
                      ("Block RAM Tile",), ("DSPs",), ("Bonded IOB",)):
            row = next((core[name] for name in names if name in core), None)
            if row is not None:
                nice.append(f"`{row.name}` {row.used}/{row.available} ({row.percent:.1f}%)")
        if nice:
            out.append("- 资源: " + "; ".join(nice))
    if errs >= 0 or cws >= 0:
        out.append(f"- 诊断: ERROR `{errs}`, CRITICAL WARNING `{cws}`, WARNING `{warns}`")
    if problems:
        out.append(f"- ⚠️ 阻塞/风险: {' / '.join(problems)}")
    if sample_failures:
        out.append(
            f"- ⚠️ 采样不完整({len(sample_failures)} 项失败): "
            + ", ".join(sample_failures)
            + " —— 摘要可能不准,勿直接贴进 commit"
        )

    out.append("")
    out.append("_Generated by vivado-mcp get_pre_commit_summary._")
    return "\n".join(out)
