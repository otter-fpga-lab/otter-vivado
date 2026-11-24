"""Token-frugal 托管 Vivado 批处理 Tcl 与独立 OOC 综合工具。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from vivado_mcp.batch_jobs import (
    BatchJobRegistry,
    compact_summary_from_record,
    summarize_build_target,
)
from vivado_mcp.server import get_app_context, mcp
from vivado_mcp.system_telemetry import (
    collect_system_telemetry,
    format_memory_guard_warning,
)


def _registry(ctx: Context | None) -> BatchJobRegistry:
    return get_app_context(ctx).batch_jobs


def _json_compact(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _error_json(exc: Exception) -> str:
    return _json_compact({"error": str(exc)})


async def _collect_memory_warning() -> str:
    with contextlib.suppress(Exception):
        return await asyncio.to_thread(
            lambda: format_memory_guard_warning(collect_system_telemetry())
        )
    return ""


def _apply_watch_info(
    summary: dict[str, Any],
    watch_info: dict[str, Any] | None,
    *,
    always_include_profile: bool = False,
) -> dict[str, Any]:
    if not watch_info:
        return summary
    ev = watch_info.get("event")
    is_alert = ev in {"stalled", "early_error", "memory_critical"}
    is_wait_state = bool(summary.get("wait_timed_out")) or is_alert or always_include_profile
    if is_wait_state and watch_info.get("watch_profile"):
        summary["watch_profile"] = watch_info["watch_profile"]
    if is_alert:
        summary["watch_event"] = ev
        if watch_info.get("alert_reason"):
            summary["watch_reason"] = watch_info["alert_reason"]
        if watch_info.get("error_lines"):
            summary["early_error_lines"] = watch_info["error_lines"]
        if watch_info.get("cpu_pct") is not None:
            summary["cpu_pct"] = watch_info["cpu_pct"]
    return summary


@mcp.tool()
async def run_batch_tcl(
    tcl_file: str | None = None,
    tcl_content: str | None = None,
    cwd: str | None = None,
    output_dir: str | None = None,
    vivado_path: str | None = None,
    tcl_args: list[str] | None = None,
    timeout_seconds: int | None = None,
    wait: bool = True,
    max_cw_items: int = 8,
    notrace: bool = True,
    stall_seconds: int | None = None,
    fail_fast: bool = True,
    mem_guard: bool = True,
    ctx: Context = None,
) -> str:
    """在服务端托管运行 ``vivado -mode batch`` 并返回紧凑摘要（超时/早醒不终止 Vivado）。"""
    try:
        cw_limit = max(0, min(int(max_cw_items), 50))
        wait_limit = (
            None
            if timeout_seconds is None
            else max(1, min(int(timeout_seconds), 1800))
        )
        registry = _registry(ctx)
        job = await registry.start_batch_tcl(
            tcl_file=tcl_file,
            tcl_content=tcl_content,
            cwd=cwd,
            output_dir=output_dir,
            vivado_path=vivado_path,
            tcl_args=tcl_args,
            notrace=notrace,
        )
        timed_out = False
        watch_info: dict[str, Any] | None = None
        if wait and job.status == "running":
            job, timed_out, watch_info = await registry.wait_job_ex(
                job.job_id,
                wait_limit,
                ctx=ctx,
                stall_seconds=stall_seconds,
                fail_fast=fail_fast,
                mem_guard=mem_guard,
            )
        record = job.to_dict()
        if timed_out:
            record["wait_timed_out"] = True
        summary = compact_summary_from_record(record, max_cw_items=cw_limit)
        _apply_watch_info(summary, watch_info)
        mem_warn = await _collect_memory_warning()
        if mem_warn:
            summary["memory_warning"] = mem_warn
        return _json_compact(summary)
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        return _error_json(exc)


@mcp.tool()
async def run_ooc_synth(
    top: str,
    part: str,
    files: list[str],
    include_dirs: list[str] | None = None,
    defines: dict[str, Any] | list[str] | None = None,
    generics: dict[str, Any] | list[str] | None = None,
    clock_port: str | None = None,
    clock_period_ns: float | None = None,
    synth_args: list[str] | None = None,
    output_dir: str | None = None,
    vivado_path: str | None = None,
    timeout_seconds: int | None = None,
    wait: bool = True,
    stall_seconds: int | None = None,
    fail_fast: bool = True,
    mem_guard: bool = True,
    ctx: Context = None,
) -> str:
    """独立 Out-of-Context 综合（无需 .xpr），内置自适应心跳与早醒，返回紧凑 JSON 摘要。"""
    try:
        wait_limit = (
            None
            if timeout_seconds is None
            else max(1, min(int(timeout_seconds), 1800))
        )
        registry = _registry(ctx)
        job = await registry.start_ooc_synth(
            top=top,
            part=part,
            files=files,
            include_dirs=include_dirs,
            defines=defines,
            generics=generics,
            clock_port=clock_port,
            clock_period_ns=clock_period_ns,
            synth_args=synth_args,
            output_dir=output_dir,
            vivado_path=vivado_path,
        )
        timed_out = False
        watch_info: dict[str, Any] | None = None
        if wait and job.status == "running":
            job, timed_out, watch_info = await registry.wait_job_ex(
                job.job_id,
                wait_limit,
                ctx=ctx,
                stall_seconds=stall_seconds,
                fail_fast=fail_fast,
                mem_guard=mem_guard,
            )
        record = job.to_dict()
        if timed_out:
            record["wait_timed_out"] = True
        summary = compact_summary_from_record(record, max_cw_items=8)
        _apply_watch_info(summary, watch_info)
        mem_warn = await _collect_memory_warning()
        if mem_warn:
            summary["memory_warning"] = mem_warn
        return _json_compact(summary)
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        return _error_json(exc)


@mcp.tool()
async def get_batch_job(
    job_id: str | None = None,
    wait_seconds: int = 0,
    log_tail_lines: int = 25,
    cancel: bool = False,
    stall_seconds: int | None = None,
    fail_fast: bool = True,
    mem_guard: bool = True,
    ctx: Context = None,
) -> str:
    """查询、等待（wait_seconds 0~1800，含 10s 心跳与卡死/早错/内存早醒）或取消批处理/OOC 任务。"""
    try:
        registry = _registry(ctx)
        tail_limit = max(0, min(int(log_tail_lines), 200))
        if job_id is None or not str(job_id).strip():
            items = []
            for raw in registry.list_jobs(limit=20):
                timing = raw.get("timing") or {}
                diag = raw.get("diagnostics") or {}
                items.append(
                    {
                        "job_id": raw.get("job_id", ""),
                        "kind": raw.get("kind", ""),
                        "status": raw.get("status", ""),
                        "top": raw.get("top", ""),
                        "part": raw.get("part", ""),
                        "exit_code": raw.get("exit_code"),
                        "elapsed_sec": raw.get("elapsed_sec"),
                        "current_phase": raw.get("current_phase", ""),
                        "wns": timing.get("wns"),
                        "errors": diag.get("errors", 0),
                        "critical_warnings": diag.get("critical_warnings", 0),
                    }
                )
            return _json_compact({"count": len(items), "jobs": items})

        jid = str(job_id).strip()
        timed_out = False
        wait_limit = 0
        watch_info: dict[str, Any] | None = None
        if cancel:
            job = await registry.cancel_job(jid)
        else:
            wait_limit = max(0, min(int(wait_seconds), 1800))
            if wait_limit > 0:
                job, timed_out, watch_info = await registry.wait_job_ex(
                    jid,
                    wait_limit,
                    ctx=ctx,
                    stall_seconds=stall_seconds,
                    fail_fast=fail_fast,
                    mem_guard=mem_guard,
                )
            else:
                found = registry.get_job(jid, log_tail_lines=tail_limit)
                if found is None:
                    raise KeyError(jid)
                job = found

        if tail_limit > 0:
            job = registry.get_job(jid, log_tail_lines=tail_limit) or job
        record = job.to_dict()
        if timed_out:
            record["wait_timed_out"] = True
        summary = compact_summary_from_record(
            record,
            max_cw_items=8,
            include_log_tail=tail_limit > 0,
        )
        _apply_watch_info(summary, watch_info, always_include_profile=wait_limit > 0)
        if tail_limit == 0:
            summary.pop("log_tail", None)
        elif "log_tail" not in summary:
            summary["log_tail"] = []
        if job.phases:
            summary["recent_phases"] = [p.get("text", "") for p in job.phases[-6:]]
        return _json_compact(summary)
    except KeyError:
        return _json_compact({"error": f"未找到批处理任务: {job_id}"})
    except (ValueError, RuntimeError, OSError) as exc:
        return _error_json(exc)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def get_build_summary(
    target: str | None = None,
    run_name: str = "impl_1",
    ctx: Context = None,
) -> str:
    """跨 Agent 构建总览（target=None，<1KB JSON）或单目标签核指标提取
    （.xpr/.runs/OOC，<700B JSON）。"""
    try:
        if target is not None and str(target).strip():
            summary = await asyncio.to_thread(
                summarize_build_target,
                str(target).strip(),
                run_name=run_name,
            )
            return _json_compact(summary)

        app_ctx = get_app_context(ctx)
        registry = app_ctx.batch_jobs
        shared_reg = getattr(app_ctx, "shared_registry", None)

        from vivado_mcp.shared_registry import detect_client_source
        from vivado_mcp.tools.console_app_tools import _build_known_eda_pids

        async def _safe_local_sessions() -> list[dict[str, Any]]:
            try:
                return await app_ctx.session_manager.list_sessions(probe_external=False)
            except Exception:
                return []

        sessions_task = asyncio.create_task(_safe_local_sessions())
        jobs_task = asyncio.to_thread(registry.list_jobs, limit=12)
        remote_task = (
            asyncio.to_thread(shared_reg.list_remote_runs, exclude_owner_pid=None)
            if shared_reg is not None
            else asyncio.sleep(0, result=[])
        )
        sessions, jobs, remote_runs = await asyncio.gather(
            sessions_task, jobs_task, remote_task
        )
        known_pids, has_attached_gui = _build_known_eda_pids(
            sessions, jobs, remote_runs, detect_client_source()
        )
        telemetry = await asyncio.to_thread(
            collect_system_telemetry,
            known_pids,
            has_attached_gui=has_attached_gui,
        )

        compact_runs = [
            {
                "client": r.get("client") or "",
                "owner": r.get("owner_pid"),
                "alive": bool(r.get("alive")),
                "sid": r.get("session_id") or "",
                "proj": r.get("project_name") or "",
                "run": r.get("run_name") or "",
                "status": r.get("status") or "",
                "prog": r.get("progress"),
                "wns": (r.get("timing") or {}).get("wns"),
            }
            for r in (remote_runs or [])[:8]
        ]
        compact_jobs = [
            {
                "id": j.get("job_id") or "",
                "client": j.get("client") or "",
                "owner": j.get("owner_pid") or 0,
                "local": bool(j.get("is_local_owner")),
                "status": j.get("status") or "",
                "top": j.get("top") or "",
                "wns": (j.get("timing") or {}).get("wns"),
                "err": (j.get("diagnostics") or {}).get("errors"),
            }
            for j in (jobs or [])[:8]
        ]
        overview: dict[str, Any] = {
            "ram": {
                "used_gb": telemetry.get("ram_used_gb"),
                "avail_gb": telemetry.get("ram_avail_gb"),
                "pct": telemetry.get("ram_pct"),
            },
            "vivado": telemetry.get("vivado_count", 0),
            "orphans": telemetry.get("orphan_count", 0),
            "runs": compact_runs,
            "jobs": compact_jobs,
        }

        def _bytes(obj: dict[str, Any]) -> int:
            return len(_json_compact(obj).encode("utf-8"))

        while _bytes(overview) >= 1000 and (overview["runs"] or overview["jobs"]):
            if len(overview["jobs"]) >= len(overview["runs"]) and overview["jobs"]:
                overview["jobs"].pop()
            elif overview["runs"]:
                overview["runs"].pop()
        return _json_compact(overview)
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        return _error_json(exc)
