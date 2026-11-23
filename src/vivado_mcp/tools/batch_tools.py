"""Token-frugal 托管 Vivado 批处理 Tcl 与独立 OOC 综合工具。"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context

from vivado_mcp.batch_jobs import (
    BatchJobRegistry,
    compact_summary_from_record,
)
from vivado_mcp.server import get_app_context, mcp


def _registry(ctx: Context | None) -> BatchJobRegistry:
    return get_app_context(ctx).batch_jobs


def _json_compact(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _error_json(exc: Exception) -> str:
    return _json_compact({"error": str(exc)})


@mcp.tool()
async def run_batch_tcl(
    tcl_file: str | None = None,
    tcl_content: str | None = None,
    cwd: str | None = None,
    output_dir: str | None = None,
    vivado_path: str | None = None,
    tcl_args: list[str] | None = None,
    timeout_seconds: int = 600,
    wait: bool = True,
    max_cw_items: int = 8,
    notrace: bool = True,
    ctx: Context = None,
) -> str:
    """在服务端托管运行 ``vivado -mode batch`` 并返回紧凑结构化摘要（取代 shell sleep 轮询）。

    ``tcl_file`` 与 ``tcl_content`` 必须且只能提供一个。省略 ``output_dir`` 时自动在
    ``%LOCALAPPDATA%\\otter-vivado\\jobs\\<job_id>\\`` 下分配隔离工作目录，绝不在用户
    工程内建隐藏目录。

    ``wait=True``（限幅 1~1800s）在服务端等待完成并周期上报 progress；若等待超时，
    不会杀死 Vivado，而是返回 ``status="running", wait_timed_out=True, job_id=...``，
    可用 ``get_batch_job`` 继续等待或在 Vivado 控制台查看实时进度。
    """
    try:
        cw_limit = max(0, min(int(max_cw_items), 50))
        wait_limit = max(1, min(int(timeout_seconds), 1800))
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
        if wait and job.status == "running":
            job, timed_out = await registry.wait_job(job.job_id, wait_limit, ctx=ctx)
        record = job.to_dict()
        if timed_out:
            record["wait_timed_out"] = True
        return _json_compact(compact_summary_from_record(record, max_cw_items=cw_limit))
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
    timeout_seconds: int = 600,
    wait: bool = True,
    ctx: Context = None,
) -> str:
    """一键独立 Out-of-Context 综合（无需 .xpr 工程），自动生成 Tcl/时钟约束并解析时序/资源/诊断。

    支持读取 ``.sv`` / ``.v`` / ``.vhd`` / ``.xci`` / ``.xdc``，配置 ``include_dirs``、
    ``defines``、``generics`` 及可选 ``clock_port`` + ``clock_period_ns``，执行
    ``synth_design -top <top> -part <part> -mode out_of_context`` 并写出
    ``timing_summary.rpt`` 与 ``utilization.rpt``。返回紧凑 JSON 摘要（不含长报告全文）。
    """
    try:
        wait_limit = max(1, min(int(timeout_seconds), 1800))
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
        if wait and job.status == "running":
            job, timed_out = await registry.wait_job(job.job_id, wait_limit, ctx=ctx)
        record = job.to_dict()
        if timed_out:
            record["wait_timed_out"] = True
        return _json_compact(compact_summary_from_record(record, max_cw_items=8))
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        return _error_json(exc)


@mcp.tool()
async def get_batch_job(
    job_id: str | None = None,
    wait_seconds: int = 0,
    log_tail_lines: int = 25,
    cancel: bool = False,
    ctx: Context = None,
) -> str:
    """查询、服务端等待或取消托管批处理 / OOC 综合任务。

    - ``job_id=None``：返回活跃及近期任务紧凑列表。
    - ``job_id=<id>``：可选 ``wait_seconds``（0~1800）在服务端阻塞等待或 ``cancel=True`` 终止，
      并返回当前状态、日志尾及已解析时序/资源/诊断摘要。
    """
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
        if cancel:
            job = await registry.cancel_job(jid)
        else:
            wait_limit = max(0, min(int(wait_seconds), 1800))
            if wait_limit > 0:
                job, timed_out = await registry.wait_job(jid, wait_limit, ctx=ctx)
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
