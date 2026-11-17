"""构建反馈：run 进度报告、子进程静默退出诊断、TCL.PRE/POST 钩子告警与等待判定。

``get_run_progress`` 与 ``wait_run`` 共用 :func:`build_run_progress_report`，
保证两者返回同一份结构化状态与摘要。本模块只读查询，不启动、重置或修改 run。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field

from vivado_mcp.analysis.run_progress_parser import (
    RunProgress,
    format_run_progress,
    parse_run_progress,
    run_state,
)
from vivado_mcp.analysis.warning_parser import (
    detect_silent_run_child_exit,
    format_nonstandard_section,
    parse_diag_counts,
    parse_tail_runme_output,
    scan_nonstandard_errors,
)
from vivado_mcp.tcl_scripts import COUNT_WARNINGS, QUERY_RUN_PROGRESS, TAIL_RUNME_LOG

logger = logging.getLogger(__name__)

# 失败诊断时 tail runme.log 的行数（与 get_critical_warnings 的非标扫描同源）。
_FAILURE_TAIL_LINES = 200

# Vivado 工程模式 run 的步骤顺序（synth run 只有 synth_design）。
RUN_STEPS: tuple[str, ...] = (
    "synth_design",
    "opt_design",
    "power_opt_design",
    "place_design",
    "post_place_power_opt_design",
    "phys_opt_design",
    "route_design",
    "post_route_phys_opt_design",
    "write_bitstream",
)
WAIT_UNTIL_CHOICES: tuple[str, ...] = ("complete", *RUN_STEPS)
# 长名在前；\b 保证 power_opt_design 不会命中 post_place_power_opt_design 的后半段。
_STEP_RE = re.compile(
    r"\b(" + "|".join(sorted(RUN_STEPS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_COMPLETED_STEP_RE = re.compile(
    r"\b(" + "|".join(sorted(RUN_STEPS, key=len, reverse=True)) + r")\s+completed successfully",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
#  TCL.PRE / TCL.POST 钩子
# --------------------------------------------------------------------------- #

QUERY_RUN_HOOKS = """\
if {{[catch {{
set __r [get_runs -quiet {run_name}]
if {{$__r eq ""}} {{
    puts "VMCP_HOOK_ERROR:run '{run_name}' not found"
}} else {{
    catch {{puts "VMCP_HOOK_PROJDIR:[file normalize [get_property DIRECTORY [current_project]]]"}}
    foreach __k {{TEMP TMP}} {{
        if {{[info exists ::env($__k)]}} {{
            puts "VMCP_HOOK_TEMP:[file normalize $::env($__k)]"
        }}
    }}
    foreach __p [list_property $__r] {{
        if {{[regexp -nocase {{^STEPS\\.[A-Z0-9_]+\\.TCL\\.(PRE|POST)$}} $__p]}} {{
            set __v [get_property $__p $__r]
            if {{$__v ne ""}} {{
                set __n [file normalize $__v]
                puts "VMCP_HOOK:$__p|[file exists $__n]|$__n"
            }}
        }}
    }}
    puts "VMCP_HOOK_DONE"
}}
}} __hook_err]}} {{
    puts "VMCP_HOOK_ERROR:$__hook_err"
}}
"""


@dataclass
class HookIssue:
    """一条可疑的 run 钩子属性。"""

    prop: str
    path: str
    reasons: list[str] = field(default_factory=list)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.replace("/", os.sep)))


def _is_under(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _local_temp_dirs() -> list[str]:
    dirs = [tempfile.gettempdir()]
    for key in ("TEMP", "TMP"):
        value = os.environ.get(key)
        if value:
            dirs.append(value)
    return dirs


def classify_hooks(raw: str, extra_temp_dirs: list[str] | None = None) -> list[HookIssue]:
    """解析 QUERY_RUN_HOOKS 输出，返回指向工程外、%TEMP% 或不存在文件的钩子。"""
    project_dir = ""
    temp_dirs = list(extra_temp_dirs or [])
    hooks: list[tuple[str, bool, str]] = []
    for line in raw.splitlines():
        text = line.strip()
        if text.startswith("VMCP_HOOK_PROJDIR:"):
            project_dir = text[len("VMCP_HOOK_PROJDIR:"):].strip()
        elif text.startswith("VMCP_HOOK_TEMP:"):
            value = text[len("VMCP_HOOK_TEMP:"):].strip()
            if value:
                temp_dirs.append(value)
        elif text.startswith("VMCP_HOOK:"):
            parts = text[len("VMCP_HOOK:"):].split("|", 2)
            if len(parts) == 3 and parts[2].strip():
                hooks.append((parts[0].strip(), parts[1].strip() == "1", parts[2].strip()))

    issues: list[HookIssue] = []
    for prop, exists, path in hooks:
        reasons: list[str] = []
        if not exists:
            reasons.append("文件不存在")
        in_temp = any(_is_under(path, temp) for temp in temp_dirs if temp)
        if in_temp:
            reasons.append("位于 %TEMP% 临时目录")
        elif project_dir and not _is_under(path, project_dir):
            reasons.append(f"位于工程目录外（工程: {project_dir}）")
        if reasons:
            issues.append(HookIssue(prop=prop, path=path, reasons=reasons))
    return issues


def format_hook_warnings(run_name: str, issues: list[HookIssue]) -> str:
    """钩子告警段：只给建议，不自动修改工程。"""
    if not issues:
        return ""
    lines = [
        f"[!] 钩子告警：{run_name} 挂有指向工程外、临时目录或缺失文件的 Tcl 钩子。",
        "这些属性保存在 .xpr，会影响此后每次运行；临时脚本被清理后 run 会直接失败。",
    ]
    for issue in issues:
        lines.append(f"  {issue.prop} = {issue.path}（{'；'.join(issue.reasons)}）")
    lines.append("清除建议（确认不再需要后手动执行；本工具不会自动修改）：")
    for issue in issues:
        lines.append(f"  set_property {issue.prop} {{}} [get_runs {run_name}]")
    lines.append("需要长期保留的钩子请放进工程目录并纳入版本管理。")
    return "\n".join(lines)


async def query_hook_warnings(session, run_name: str) -> str:
    """读取 run 的 STEPS.*.TCL.PRE/POST 并返回告警段；查询失败降级为一行说明。"""
    try:
        result = await session.execute(QUERY_RUN_HOOKS.format(run_name=run_name), timeout=15.0)
    except Exception as exc:
        logger.warning("读取 run 钩子失败(run=%s): %s", run_name, exc)
        return f"[DEGRADED] 未能检查 {run_name} 的 TCL.PRE/POST 钩子: {exc}"
    output = getattr(result, "output", "") or ""
    if getattr(result, "is_error", False) or "VMCP_HOOK_ERROR:" in output:
        detail = next(
            (ln.split(":", 1)[1].strip() for ln in output.splitlines()
             if ln.startswith("VMCP_HOOK_ERROR:")),
            output[:200],
        )
        return f"[DEGRADED] 未能检查 {run_name} 的 TCL.PRE/POST 钩子: {detail}"
    if "VMCP_HOOK_DONE" not in output:
        return ""
    return format_hook_warnings(run_name, classify_hooks(output, _local_temp_dirs()))


# --------------------------------------------------------------------------- #
#  失败诊断：子进程静默退出 / 非标错误
# --------------------------------------------------------------------------- #


@dataclass
class FailureInsight:
    """STATUS 失败时从日志得到的真实情况。"""

    kind: str  # "silent_exit" / "nonstandard"
    section: str
    step: str = ""


def _last_completed_step(body: str) -> str:
    step = ""
    for found in _COMPLETED_STEP_RE.finditer(body):
        step = found.group(1).lower()
    return step


async def diagnose_failed_run(session, rp: RunProgress) -> FailureInsight | None:
    """STATUS 失败且 runme.log 无 ERROR 行时，复用静默退出 / 非标错误检测。

    只在失败时多发两条只读查询；runme.log 有 ERROR 行时交给 get_critical_warnings。
    """
    if not rp.found or rp.state() != "failed":
        return None
    try:
        counts = await session.execute(COUNT_WARNINGS.format(run_name=rp.run_name), timeout=30.0)
    except Exception as exc:
        logger.warning("失败诊断读取计数失败(run=%s): %s", rp.run_name, exc)
        return None
    if counts.is_error:
        return None
    errors, cw, _ = parse_diag_counts(counts.output)
    if errors != 0:
        return None
    try:
        tail = await session.execute(
            TAIL_RUNME_LOG.format(run_name=rp.run_name, tail_n=_FAILURE_TAIL_LINES),
            timeout=20.0,
        )
    except Exception as exc:
        logger.warning("失败诊断读取日志尾失败(run=%s): %s", rp.run_name, exc)
        return None
    status, start_line, body = parse_tail_runme_output(tail.output)
    if not body:
        return None
    nonstandard = format_nonstandard_section(scan_nonstandard_errors(body, start_line=start_line))
    if nonstandard:
        return FailureInsight(kind="nonstandard", section=nonstandard)
    cw_text = f"{cw}" if cw >= 0 else "未知"
    section = detect_silent_run_child_exit(
        status or rp.status, body,
        counts_note=f"runme.log ERROR=0 / CRITICAL WARNING={cw_text}",
    )
    if not section:
        return None
    return FailureInsight(kind="silent_exit", section=section, step=_last_completed_step(body))


def failure_advice(rp: RunProgress, insight: FailureInsight, session_id: str) -> list[str]:
    """内联推荐动作；续跑入口复用 c3c1fa1 的 ERROR → reset_run -prev_step 逻辑。"""
    run = rp.run_name
    if insight.kind == "silent_exit":
        rerun = (
            f"run_synthesis(run_name='{run}', session_id='{session_id}', wait=False)"
            if run.startswith("synth") else
            f"run_implementation(run_name='{run}', session_id='{session_id}', wait=False)"
        )
        until = "synth_design" if run.startswith("synth") else "route_design"
        return [
            "推荐动作（日志显示该步已成功，这不是 RTL/时序 ERROR）：",
            "  1. 按上面原因排查：ROUTE 后 report_power / report_bus_skew / "
            "report_timing_summary -report_unconstrained，或同一 .xpr 开了第二份 Vivado。",
            f"  2. {rerun}",
            "     STATUS 含 ERROR 时只 reset_run -prev_step 续跑，保留已完成的 place/phys_opt。",
            f"  3. wait_run(run_name='{run}', until='{until}', session_id='{session_id}') "
            "等待结果，不要 sleep 轮询。",
        ]
    return [
        "推荐动作：按上面非标错误的修复方向处理后重跑；"
        f"完整分类见 get_critical_warnings(run_name='{run}')。",
    ]


# --------------------------------------------------------------------------- #
#  统一的进度报告
# --------------------------------------------------------------------------- #


async def build_run_progress_report(
    session, run_name: str, tail_lines: int = 30,
) -> tuple[RunProgress | None, str]:
    """查询 run 进度并附加失败诊断与钩子告警，返回 (快照, 文本)。

    查询失败时快照为 None，文本为 ``[ERROR] ...``。
    """
    try:
        result = await session.execute(
            QUERY_RUN_PROGRESS.format(run_name=run_name, tail_n=tail_lines),
            timeout=30.0,
        )
    except Exception as exc:
        return None, f"[ERROR] 查询 run 进度失败: {exc}"
    if result.is_error:
        return None, (
            f"[ERROR] 查询 run 进度失败（rc={result.return_code}）：\n{result.output}"
        )
    rp = parse_run_progress(result.output, run_name=run_name)
    if rp.error or not rp.found:
        return rp, format_run_progress(rp)

    state_note = ""
    advice: list[str] | None = None
    sections: list[str] = []
    insight = await diagnose_failed_run(session, rp)
    if insight is not None:
        sections.append(insight.section)
        advice = failure_advice(rp, insight, session.session_id)
        if insight.kind == "silent_exit":
            step = insight.step or "上一步"
            state_note = (
                f"子进程静默退出：日志显示 {step} completed successfully，"
                f"之后子 Vivado 未正常退出，STATUS 记为 {rp.status}"
            )
        else:
            state_note = "日志末尾发现非标错误"
    hook_section = await query_hook_warnings(session, run_name)
    if hook_section:
        sections.append(hook_section)
    return rp, format_run_progress(
        rp, state_note=state_note, advice=advice, extra_sections=sections,
    )


# --------------------------------------------------------------------------- #
#  wait_run 判定
# --------------------------------------------------------------------------- #


def status_step(status: str) -> str | None:
    """从 STATUS 文本取步骤名（如 ``route_design Complete!`` → route_design）。"""
    match = _STEP_RE.search(status or "")
    return match.group(1).lower() if match else None


def evaluate_wait(status: str, until: str) -> str:
    """单次 STATUS 相对等待目标的判定。

    返回值：
    - ``reached``：目标已到达（或已越过目标步骤）。
    - ``failed`` / ``cancelled``：在目标之前或目标处失败 / 取消。
    - ``running``：仍在排队或运行。
    - ``settled``：run 已停在完成态但未到目标（或可能马上继续下一步），需再确认一次。
    - ``idle`` / ``unknown``：未启动或无法识别，同样需要再确认。
    """
    state = run_state(status, None)
    step = status_step(status)
    if until != "complete":
        target = RUN_STEPS.index(until)
        index = RUN_STEPS.index(step) if step in RUN_STEPS else -1
        if index > target:
            return "reached"
        if index == target and state in {"completed", "stage_complete"}:
            return "reached"
    if state in {"failed", "cancelled"}:
        return state
    if state == "running":
        return "running"
    if until == "complete" and state == "completed" and step in {"synth_design", "write_bitstream"}:
        return "reached"
    if state in {"completed", "stage_complete"}:
        return "settled"
    return state  # idle / unknown
