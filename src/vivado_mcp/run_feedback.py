"""构建反馈：run 进度报告、子进程静默退出诊断、TCL.PRE/POST 钩子告警与等待判定。

``get_run_progress`` 与 ``wait_run`` 共用 :func:`build_run_progress_report`，
保证两者返回同一份结构化状态与摘要。本模块只读查询，不启动、重置或修改 run。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from vivado_mcp.analysis.run_progress_parser import (
    RunProgress,
    format_run_progress,
    parse_run_progress,
    run_state,
)
from vivado_mcp.analysis.warning_parser import (
    SilentExitForensics,
    VivadoInitThreadSetting,
    WindowsCrashEvent,
    detect_silent_run_child_exit,
    format_nonstandard_section,
    is_high_thread_route_heap_risk,
    is_route_power_silent_exit,
    match_silent_run_child_exit,
    parse_diag_counts,
    parse_silent_exit_tcl_output,
    parse_tail_runme_output,
    scan_nonstandard_errors,
)
from vivado_mcp.tcl_scripts import (
    COUNT_WARNINGS,
    QUERY_RUN_PROGRESS,
    QUERY_SILENT_EXIT_FORENSICS,
    TAIL_RUNME_LOG,
)
from vivado_mcp.vivado.tcl_utils import validate_identifier
from vivado_mcp.vivado_init import (
    infer_install_init_tcl,
    inspect_init_tcl_file,
    parse_general_max_threads,
    resolve_user_init_tcl,
)

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
#  失败诊断：子进程静默退出取证 / 非标错误
# --------------------------------------------------------------------------- #

_EVENT_LOG_TIMEOUT_SEC = 5.0
_EVENT_LOG_WINDOW_MIN = 10

_RE_EVENT_EX_CODE = re.compile(
    r"(?:Exception\s+code|异常代码)\s*[:：]\s*(?:0x)?([0-9a-fA-F]{8})\b",
    re.IGNORECASE,
)
_RE_EVENT_P7 = re.compile(r"\bP7\s*[:：]\s*(?:0x)?([0-9a-fA-F]{8})\b", re.IGNORECASE)
_RE_EVENT_HEX_CODE = re.compile(r"\b(?:0x)?([cC]000[0-9a-fA-F]{4}|8000[0-9a-fA-F]{4})\b")
_RE_EVENT_WER_NAME = re.compile(
    r"(?:Event\s+Name|事件名称)\s*[:：]\s*([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_RE_EVENT_WER_KW = re.compile(
    r"\b(RADAR_PRE_LEAK(?:_64|_32)?|APPCRASH|BEX64)\b",
    re.IGNORECASE,
)
_RE_EVENT_MODULE = re.compile(
    r"(?:Faulting\s+module\s+name|错误模块名称)\s*[:：]\s*([^\s,，;；]+)",
    re.IGNORECASE,
)


@dataclass
class FailureInsight:
    """STATUS 失败时从日志得到的真实情况。"""

    kind: str  # "silent_exit" / "nonstandard"
    section: str
    step: str = ""
    high_thread_risk: bool = False
    route_power_risk: bool = False


@dataclass
class SilentExitDiagnosis:
    """``get_run_progress`` / ``wait_run`` / ``get_critical_warnings`` 共用的静默退出结果。"""

    matched: bool
    step: str = ""
    section: str = ""
    high_thread_risk: bool = False
    route_power_risk: bool = False
    forensics: SilentExitForensics | None = None


def parse_vivado_init_max_threads(content: str) -> tuple[str, int]:
    """从 Vivado_init.tcl 文本提取最后一个非注释 ``general.maxThreads`` 值及行号。"""
    matches = parse_general_max_threads(content)
    if not matches:
        return "", 0
    last = matches[-1]
    return str(last["raw"]), int(last["line"])


def user_vivado_init_tcl_path() -> Path:
    """返回当前平台用户级 ``Vivado_init.tcl`` 路径（委托共用 helper）。"""
    return resolve_user_init_tcl()


def inspect_vivado_init_files(
    vivado_path: str | None = None,
    *,
    candidates: list[Path] | None = None,
) -> list[VivadoInitThreadSetting]:
    """只读检查候选 ``Vivado_init.tcl`` 文件中的 ``general.maxThreads`` 设置。"""
    if candidates is None:
        raw_candidates = [resolve_user_init_tcl()]
        install_init = infer_install_init_tcl(vivado_path)
        if install_init is not None:
            raw_candidates.append(install_init)
    else:
        raw_candidates = list(candidates)

    seen: set[str] = set()
    results: list[VivadoInitThreadSetting] = []
    for path in raw_candidates:
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        info = inspect_init_tcl_file(path)
        last = info["matches"][-1] if info["matches"] else None
        results.append(
            VivadoInitThreadSetting(
                path=str(path),
                exists=bool(info["exists"]),
                max_threads=str(last["raw"]) if last is not None else "",
                line_number=int(last["line"]) if last is not None else 0,
                error=str(info["error"] or ""),
            )
        )
    return results


def _parse_windows_event_record(item: dict) -> WindowsCrashEvent:
    try:
        event_id = int(item.get("Id") or 0)
    except (TypeError, ValueError):
        event_id = 0
    time_created = str(item.get("Time") or item.get("TimeCreated") or "").strip()
    provider = str(item.get("Provider") or item.get("ProviderName") or "").strip()
    message = str(item.get("Message") or "")

    ex_match = (
        _RE_EVENT_EX_CODE.search(message)
        or _RE_EVENT_P7.search(message)
        or _RE_EVENT_HEX_CODE.search(message)
    )
    exception_code = f"0x{ex_match.group(1).upper()}" if ex_match else ""

    wer_match = _RE_EVENT_WER_NAME.search(message) or _RE_EVENT_WER_KW.search(message)
    wer_event_name = wer_match.group(1) if wer_match else ""

    mod_match = _RE_EVENT_MODULE.search(message)
    faulting_module = mod_match.group(1).rstrip("，。,.") if mod_match else ""

    first_line = next((ln.strip() for ln in message.splitlines() if ln.strip()), "")
    summary = first_line[:160]
    return WindowsCrashEvent(
        event_id=event_id,
        time_created=time_created,
        provider=provider,
        exception_code=exception_code,
        wer_event_name=wer_event_name,
        faulting_module=faulting_module,
        summary=summary,
    )


def query_windows_crash_events(
    log_mtime: int = 0,
    *,
    timeout_sec: float = _EVENT_LOG_TIMEOUT_SEC,
    window_minutes: int = _EVENT_LOG_WINDOW_MIN,
) -> tuple[str, str, str, list[WindowsCrashEvent]]:
    """仅在 Windows 上限时查询 Application 日志中 vivado.exe 的崩溃与 WER 事件。

    返回 ``(status, note, window_label, events)``；失败或超时标记 ``unavailable``。
    """
    if sys.platform != "win32":
        return "skipped", "非 Windows 平台，跳过系统事件日志查询", "", []

    if log_mtime and log_mtime > 0:
        delta = max(60, int(window_minutes) * 60)
        start_epoch = max(0, int(log_mtime) - delta)
        end_epoch = int(log_mtime) + delta
        window_label = f"runme.log mtime ±{window_minutes} 分钟"
    else:
        now = int(time.time())
        start_epoch = max(0, now - 1800)
        end_epoch = now + 60
        window_label = "最近 30 分钟"

    ps_script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        f"$s=[DateTimeOffset]::FromUnixTimeSeconds({start_epoch}).LocalDateTime;"
        f"$e=[DateTimeOffset]::FromUnixTimeSeconds({end_epoch}).LocalDateTime;"
        "$err=@();"
        "$raw=Get-WinEvent -FilterHashtable "
        "@{LogName='Application';Id=1000,1001;StartTime=$s;EndTime=$e} "
        "-MaxEvents 200 -ErrorAction SilentlyContinue -ErrorVariable err;"
        "$realErr=@($err | Where-Object { "
        "$_.FullyQualifiedErrorId -notmatch '^NoMatchingEventsFound' });"
        "if ($realErr.Count -gt 0) { Write-Error ($realErr[0].ToString()); exit 1 };"
        "$ev=@($raw | Where-Object { $_.Message -match 'vivado\\.exe' } "
        "| Select-Object -First 10 "
        "| ForEach-Object { [PSCustomObject]@{Id=$_.Id;Time=$_.TimeCreated.ToString('o');"
        "Provider=$_.ProviderName;Message=$_.Message} });"
        "ConvertTo-Json -InputObject $ev -Compress"
    )
    effective_timeout = min(max(0.5, float(timeout_sec)), _EVENT_LOG_TIMEOUT_SEC)
    kwargs: dict[str, object] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return "unavailable", f"超时(>{effective_timeout:g}s)", window_label, []
    except (OSError, ValueError) as exc:
        return "unavailable", str(exc), window_label, []

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
        return "unavailable", err[:200], window_label, []

    raw_out = (proc.stdout or "").lstrip("\ufeff").strip()
    if not raw_out or raw_out == "null":
        return "ok", "", window_label, []

    try:
        parsed = json.loads(raw_out)
    except json.JSONDecodeError:
        return "unavailable", "事件日志输出 JSON 解析失败", window_label, []

    if isinstance(parsed, dict):
        items = [parsed]
    elif isinstance(parsed, list):
        items = [entry for entry in parsed if isinstance(entry, dict)]
    else:
        return "unavailable", "事件日志输出格式异常", window_label, []

    events = [_parse_windows_event_record(item) for item in items]
    return "ok", "", window_label, events


async def collect_silent_exit_forensics(
    session,
    *,
    run_name: str = "",
    log_mtime: int = 0,
    vivado_version: str = "",
) -> SilentExitForensics:
    """收集子进程静默退出的多维取证数据；单项失败不抛出异常。"""
    safe_run = ""
    if run_name:
        try:
            safe_run = validate_identifier(run_name, "run_name")
        except ValueError:
            safe_run = ""

    max_threads: int | None = None
    max_threads_raw = ""
    max_threads_note = ""
    tcl_version = ""
    tcl_mtime = 0

    try:
        res = await session.execute(
            QUERY_SILENT_EXIT_FORENSICS.format(run_name=safe_run),
            timeout=10.0,
        )
        if getattr(res, "is_error", False):
            max_threads_note = f"Tcl 查询失败 rc={getattr(res, 'return_code', '?')}"
        else:
            output = getattr(res, "output", "") or ""
            (
                max_threads,
                max_threads_raw,
                err_msg,
                tcl_version,
                tcl_mtime,
            ) = parse_silent_exit_tcl_output(output)
            if err_msg and max_threads is None and not max_threads_raw:
                max_threads_note = err_msg[:120]
            elif max_threads is None and not max_threads_raw:
                max_threads_note = "Tcl 输出未返回 general.maxThreads"
    except Exception as exc:
        logger.warning("静默退出取证查询 maxThreads 失败: %s", exc)
        max_threads_note = str(exc)[:120] or type(exc).__name__

    raw_rt_ver = getattr(session, "runtime_version", None)
    rt_ver = (
        raw_rt_ver.strip()
        if isinstance(raw_rt_ver, str) and raw_rt_ver.strip() not in ("", "unknown")
        else ""
    )
    resolved_version = tcl_version or (vivado_version or "").strip() or rt_ver

    raw_vivado_path = getattr(session, "vivado_path", None)
    vivado_path = raw_vivado_path if isinstance(raw_vivado_path, str) else None
    init_files = inspect_vivado_init_files(vivado_path)

    effective_mtime = log_mtime if log_mtime and log_mtime > 0 else tcl_mtime
    ev_status, ev_note, ev_window, events = await asyncio.to_thread(
        query_windows_crash_events, effective_mtime,
    )

    return SilentExitForensics(
        vivado_version=resolved_version,
        effective_max_threads=max_threads,
        effective_max_threads_raw=max_threads_raw,
        max_threads_note=max_threads_note,
        init_files=init_files,
        event_log_status=ev_status,
        event_log_note=ev_note,
        event_log_window=ev_window,
        events=events,
    )


async def diagnose_silent_run_child_exit(
    session,
    status: str,
    body: str,
    *,
    run_name: str = "",
    counts_note: str = "messageDb 0 ERROR / 0 CW",
    log_mtime: int = 0,
    vivado_version: str = "",
) -> SilentExitDiagnosis:
    """``get_run_progress`` / ``wait_run`` / ``get_critical_warnings`` 共用的静默退出诊断与取证。"""
    step = match_silent_run_child_exit(status, body)
    if not step:
        return SilentExitDiagnosis(matched=False)

    forensics = await collect_silent_exit_forensics(
        session,
        run_name=run_name,
        log_mtime=log_mtime,
        vivado_version=vivado_version,
    )
    section = detect_silent_run_child_exit(
        status,
        body,
        counts_note=counts_note,
        forensics=forensics,
    )
    risk = is_high_thread_route_heap_risk(status, step, forensics)
    power_risk = is_route_power_silent_exit(body)
    return SilentExitDiagnosis(
        matched=True,
        step=step.lower(),
        section=section,
        high_thread_risk=risk,
        route_power_risk=power_risk,
        forensics=forensics,
    )


def _last_completed_step(body: str) -> str:
    step = ""
    for found in _COMPLETED_STEP_RE.finditer(body):
        step = found.group(1).lower()
    return step


async def diagnose_failed_run(session, rp: RunProgress) -> FailureInsight | None:
    """STATUS 失败且 runme.log 无 ERROR 行时，复用静默退出 / 非标错误检测。

    只在失败时多发只读查询；runme.log 有 ERROR 行时交给 get_critical_warnings。
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
    silent = await diagnose_silent_run_child_exit(
        session,
        status or rp.status,
        body,
        run_name=rp.run_name,
        counts_note=f"runme.log ERROR=0 / CRITICAL WARNING={cw_text}",
        log_mtime=rp.log_mtime,
    )
    if not silent.matched:
        return None
    return FailureInsight(
        kind="silent_exit",
        section=silent.section,
        step=_last_completed_step(body) or silent.step,
        high_thread_risk=silent.high_thread_risk,
        route_power_risk=silent.route_power_risk,
    )


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
        if insight.route_power_risk and not run.startswith("synth"):
            step1 = (
                "  1. 命中 Vivado 2024.2 route 后自动 report_power 静默退出"
                "（*_routed.dcp 未写出）；"
                f"建议传 skip_route_power_report=True 续跑："
                f"run_implementation(run_name='{run}', session_id='{session_id}', "
                "skip_route_power_report=True, wait=False)，"
                "需要功耗可从 physopt.dcp 单独运行 report_power。"
            )
        elif insight.high_thread_risk:
            step1 = (
                "  1. 按上面「高线程 route 堆损坏风险提示」处理"
                "（工程内 scripts/ 挂 PRE=1/POST 恢复钩子"
                "或调低全局 maxThreads；亦排查 ROUTE 后 report_* / 双开 Vivado）。"
            )
        else:
            step1 = (
                "  1. 按上面原因排查：ROUTE 后 report_power / report_bus_skew / "
                "report_timing_summary -report_unconstrained，或同一 .xpr 开了第二份 Vivado。"
            )
        return [
            "推荐动作（日志显示该步已成功，这不是 RTL/时序 ERROR）：",
            step1,
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
    session,
    run_name: str,
    tail_lines: int = 30,
    *,
    compact: bool = False,
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
        return rp, format_run_progress(rp, compact=compact)

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
        rp,
        state_note=state_note,
        advice=advice,
        extra_sections=sections,
        compact=compact,
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
