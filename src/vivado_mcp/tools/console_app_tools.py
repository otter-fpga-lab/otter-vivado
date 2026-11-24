"""Antigravity / 浏览器 Vivado 控制台 MCP App 服务端资源、入口与零 Token 工具。"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import time
import webbrowser
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent

from vivado_mcp.batch_jobs import _decode_utf8_or_fallback, _is_link_or_junction
from vivado_mcp.build_eta import compute_build_eta
from vivado_mcp.config import list_vivado_installations
from vivado_mcp.console_http import (
    ConsoleHttpServer,
    preferred_console_port,
    probe_existing_console_hub,
)
from vivado_mcp.server import AppContext, get_app_context, mcp
from vivado_mcp.shared_registry import detect_client_source, is_pid_alive
from vivado_mcp.system_telemetry import (
    collect_system_telemetry,
    infer_pids_clients,
    kill_eda_process,
)

logger = logging.getLogger(__name__)

CONSOLE_APP_URI = "ui://otter-vivado/console.html"
_APP_HTML_ENV = "VIVADO_CONSOLE_APP_HTML"
_MAX_STRUCTURED_BYTES = 240 * 1024
_MAX_REPORT_PREVIEW_BYTES = 4 * 1024 * 1024
_MAX_REPORT_RESPONSE_BYTES = 240 * 1024
_ALLOWED_REPORT_EXTS = frozenset({".rpt", ".log", ".txt"})


def read_console_html() -> str:
    """读取单文件控制台 HTML；支持 VIVADO_CONSOLE_APP_HTML 环境变量热覆盖。"""
    override = os.environ.get(_APP_HTML_ENV, "").strip()
    if override:
        path = Path(override).expanduser().resolve(strict=True)
        if not path.is_file():
            raise FileNotFoundError(f"{_APP_HTML_ENV} 指向的不是文件: {path}")
        return path.read_text(encoding="utf-8")
    return files("vivado_mcp").joinpath("web/console.html").read_text(encoding="utf-8")


@mcp.resource(
    CONSOLE_APP_URI,
    name="Vivado 控制台",
    mime_type="text/html;profile=mcp-app",
    meta={"ui": {"prefersBorder": False}},
)
def vivado_console_html() -> str:
    """提供自包含单文件 Vivado 控制台 MCP App HTML。"""
    return read_console_html()


def _extract_loopback_url(snap: dict[str, Any]) -> str:
    for key in ("url", "local_url", "view_url"):
        val = snap.get(key)
        if isinstance(val, str) and val.startswith("http://127.0.0.1:"):
            return val
    return ""


def _sanitize_monitor_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    cleaned = copy.deepcopy(snap)
    reports = []
    for rep in cleaned.get("reports") or []:
        if isinstance(rep, dict):
            item = dict(rep)
            item.pop("text", None)
            reports.append(item)
    cleaned["reports"] = reports
    loopback_url = _extract_loopback_url(cleaned)
    if loopback_url:
        cleaned["url"] = loopback_url
    return cleaned


def _sanitize_debug_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    cleaned = copy.deepcopy(snap)
    loopback_url = _extract_loopback_url(cleaned)
    if loopback_url:
        cleaned["url"] = loopback_url
    return cleaned


def _local_web_info(app_ctx: AppContext) -> dict[str, Any]:
    pref = preferred_console_port()
    srv = getattr(app_ctx, "console_http", None)
    url = getattr(srv, "url", "") if srv is not None else ""
    if isinstance(url, str) and url.startswith("http://127.0.0.1:"):
        host_port = getattr(srv, "host_port", "") or urlsplit(url).netloc
        return {
            "running": True,
            "url": url,
            "host_port": host_port,
            "preferred_port": pref,
            "shared_hub": False,
        }
    shared_url = getattr(app_ctx, "console_shared_url", "")
    if isinstance(shared_url, str) and shared_url.startswith("http://127.0.0.1:"):
        return {
            "running": True,
            "url": shared_url,
            "host_port": urlsplit(shared_url).netloc,
            "preferred_port": pref,
            "shared_hub": True,
        }
    return {
        "running": False,
        "url": "",
        "host_port": "",
        "preferred_port": pref,
        "shared_hub": False,
    }


async def ensure_console_http(
    app_ctx: AppContext,
    *,
    open_browser: bool = False,
    job_id: str | None = None,
    inspected_path: str | None = None,
) -> dict[str, Any]:
    """确保轻量级本机回环控制台 HTTP 服务已启动（优先复用/绑定首选 18765 端口）。"""
    loop = asyncio.get_running_loop()
    if app_ctx.console_http is not None and getattr(app_ctx.console_http, "is_running", False):
        app_ctx.console_http.set_loop(loop)
        url = await asyncio.to_thread(app_ctx.console_http.start)
    else:
        pref = preferred_console_port()
        existing = (
            await asyncio.to_thread(probe_existing_console_hub, pref)
            if pref > 0
            else None
        )
        if existing:
            app_ctx.console_shared_url = existing
            url = existing
        else:
            app_ctx.console_shared_url = ""
            if app_ctx.console_http is None:
                app_ctx.console_http = ConsoleHttpServer(app_ctx, loop=loop, port=pref)
            else:
                app_ctx.console_http.set_loop(loop)
            try:
                url = await asyncio.to_thread(app_ctx.console_http.start)
            except OSError:
                if pref == 0:
                    raise
                recheck = await asyncio.to_thread(probe_existing_console_hub, pref)
                if recheck:
                    app_ctx.console_http = None
                    app_ctx.console_shared_url = recheck
                    url = recheck
                else:
                    fallback_srv = ConsoleHttpServer(app_ctx, loop=loop, port=0)
                    app_ctx.console_http = fallback_srv
                    url = await asyncio.to_thread(fallback_srv.start)

    if app_ctx.console_http is not None and (job_id or inspected_path):
        app_ctx.console_http.set_selection(
            job_id=job_id,
            inspected_path=inspected_path,
        )
    if open_browser and url:
        try:
            await asyncio.to_thread(webbrowser.open, url)
        except Exception as exc:
            logger.warning("自动打开浏览器失败: %s", exc)
    return _local_web_info(app_ctx)


def _json_byte_len(obj: Any) -> int:
    return len(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _shrink_payload(
    payload: dict[str, Any], limit_bytes: int = _MAX_STRUCTURED_BYTES
) -> dict[str, Any]:
    """保证 structuredContent 严格低于 256 KiB，不触碰轨迹 1 MiB 上限。"""
    if _json_byte_len(payload) <= limit_bytes:
        return payload

    trimmed = copy.deepcopy(payload)
    for bucket_key in ("batch_jobs", "inspected_targets", "remote_runs"):
        for item in trimmed.get(bucket_key) or []:
            if isinstance(item, dict):
                item["log_tail"] = (item.get("log_tail") or [])[-12:]
                if "phases" in item:
                    item["phases"] = (item.get("phases") or [])[-15:]
                diag = item.get("diagnostics") or {}
                diag.pop("warning_items", None)
                if "all_issues" in diag:
                    diag["all_issues"] = (diag.get("all_issues") or [])[:15]
    if _json_byte_len(trimmed) <= limit_bytes:
        return trimmed

    for bucket_key in ("batch_jobs", "inspected_targets", "remote_runs"):
        items = trimmed.get(bucket_key) or []
        trimmed[bucket_key] = items[:12]
        for item in trimmed[bucket_key]:
            if isinstance(item, dict):
                item["log_tail"] = []
                if "phases" in item:
                    item["phases"] = []
                item.pop("forensics", None)
                item["reports"] = (item.get("reports") or [])[:20]
                diag = item.get("diagnostics") or {}
                if "all_issues" in diag:
                    diag["all_issues"] = (diag.get("all_issues") or [])[:8]
                diag["top_issues"] = (diag.get("top_issues") or [])[:5]
    for mon in trimmed.get("run_monitors") or []:
        if isinstance(mon, dict):
            run = mon.get("run")
            if isinstance(run, dict):
                run["tail"] = (run.get("tail") or [])[-10:]
            for rep in mon.get("reports") or []:
                if isinstance(rep, dict):
                    summary = rep.get("summary")
                    if isinstance(summary, dict) and isinstance(summary.get("resources"), list):
                        summary["resources"] = summary["resources"][:12]
    if _json_byte_len(trimmed) <= limit_bytes:
        return trimmed

    # 极端场景下逐级削减列表与明文明细，确保绝对满足 <= limit_bytes 硬上限
    for bucket_key in ("batch_jobs", "inspected_targets", "remote_runs"):
        for item in trimmed.get(bucket_key) or []:
            if isinstance(item, dict):
                item["reports"] = []
                diag = item.get("diagnostics")
                if isinstance(diag, dict):
                    if "all_issues" in diag:
                        diag["all_issues"] = []
                    diag["top_issues"] = (diag.get("top_issues") or [])[:2]
    for mon in trimmed.get("run_monitors") or []:
        if isinstance(mon, dict):
            run = mon.get("run")
            if isinstance(run, dict):
                run["tail"] = []
            mon["reports"] = (mon.get("reports") or [])[:4]
    if _json_byte_len(trimmed) <= limit_bytes:
        return trimmed

    for bucket_key in ("batch_jobs", "inspected_targets", "remote_runs"):
        for item in trimmed.get(bucket_key) or []:
            if isinstance(item, dict):
                item.pop("eta", None)
    for mon in trimmed.get("run_monitors") or []:
        if isinstance(mon, dict):
            mon.pop("eta", None)
            run = mon.get("run")
            if isinstance(run, dict):
                run.pop("eta", None)
    if _json_byte_len(trimmed) <= limit_bytes:
        return trimmed

    for keep in (8, 4, 2, 1, 0):
        for list_key in (
            "batch_jobs",
            "inspected_targets",
            "remote_runs",
            "run_monitors",
            "debug_sessions",
            "sessions",
            "vivado_installs",
        ):
            val = trimmed.get(list_key)
            if isinstance(val, list) and len(val) > keep:
                trimmed[list_key] = val[:keep]
        sys_obj = trimmed.get("system")
        if isinstance(sys_obj, dict) and isinstance(sys_obj.get("eda_processes"), list):
            if len(sys_obj["eda_processes"]) > keep:
                sys_obj["eda_processes"] = sys_obj["eda_processes"][:keep]
        if _json_byte_len(trimmed) <= limit_bytes:
            return trimmed

    return trimmed


def _build_known_eda_pids(
    sessions: list[dict[str, Any]],
    batch_jobs: list[dict[str, Any]],
    remote_runs: list[dict[str, Any]],
    local_client: str,
) -> tuple[dict[int, dict[str, Any]], bool]:
    known: dict[int, dict[str, Any]] = {}
    has_attached_gui = False
    local_pid = os.getpid()

    for s in sessions:
        if not isinstance(s, dict):
            continue
        mode = str(s.get("mode") or "").lower()
        owner = str(s.get("owner") or "").lower()
        is_gui = (
            mode in {"gui", "attached_gui", "external_gui", "attach", "external"}
            or bool(s.get("attached"))
            or bool(s.get("attached_external"))
            or owner == "external"
        )
        if is_gui:
            has_attached_gui = True
        pid = s.get("pid")
        info = {
            "owner_client": local_client if owner != "external" else "GUI",
            "owner_pid": local_pid,
            "owner_alive": True,
            "label": str(s.get("session_id") or ""),
            "is_attached_gui": is_gui,
        }
        if isinstance(pid, int) and pid > 0:
            known[pid] = info
        elif s.get("is_alive") and not is_gui and local_pid not in known:
            known[local_pid] = info

    for job in batch_jobs:
        if not isinstance(job, dict) or job.get("status") != "running":
            continue
        pid = job.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            raw_opid = job.get("owner_pid")
            opid = (
                raw_opid
                if isinstance(raw_opid, int) and not isinstance(raw_opid, bool) and raw_opid > 0
                else None
            )
            if job.get("is_local_owner"):
                owner_alive = True
            elif opid is not None:
                owner_alive = is_pid_alive(opid)
            else:
                owner_alive = False
            known[pid] = {
                "owner_client": str(
                    job.get("client") or (local_client if job.get("is_local_owner") else "")
                ),
                "owner_pid": opid,
                "owner_alive": owner_alive,
                "label": str(job.get("top") or job.get("job_id") or ""),
                "is_attached_gui": False,
            }

    for rr in remote_runs:
        if not isinstance(rr, dict):
            continue
        mode = str(rr.get("mode") or "").lower()
        is_gui = mode in {"gui", "attached_gui", "external_gui", "attach", "external"}
        alive = bool(rr.get("alive"))
        if alive and is_gui:
            has_attached_gui = True
        label = str(rr.get("project_name") or rr.get("run_name") or rr.get("session_id") or "")
        info = {
            "owner_client": str(rr.get("client") or ""),
            "owner_pid": rr.get("owner_pid") if isinstance(rr.get("owner_pid"), int) else None,
            "owner_alive": alive,
            "label": label,
            "is_attached_gui": is_gui,
        }
        vpid = rr.get("vivado_pid")
        if isinstance(vpid, int) and vpid > 0:
            known[vpid] = info
        opid = rr.get("owner_pid")
        if alive and isinstance(opid, int) and opid > 0 and opid not in known:
            known[opid] = info

    return known, has_attached_gui


_GENERIC_CLIENTS = frozenset({"", "CLI", "外部"})


def _collect_pids_needing_attribution(
    batch_jobs: list[dict[str, Any]],
    remote_runs: list[dict[str, Any]],
) -> list[int]:
    pids: list[int] = []
    for job in batch_jobs:
        if not isinstance(job, dict) or str(job.get("status") or "").lower() != "running":
            continue
        if str(job.get("client") or "").strip() not in _GENERIC_CLIENTS:
            continue
        for key_name in ("pid", "owner_pid"):
            val = job.get(key_name)
            if isinstance(val, int) and val > 0:
                pids.append(val)
    for rr in remote_runs:
        if not isinstance(rr, dict):
            continue
        if not (rr.get("alive") or str(rr.get("status") or "").lower() == "running"):
            continue
        if str(rr.get("client") or "").strip() not in _GENERIC_CLIENTS:
            continue
        for key_name in ("vivado_pid", "owner_pid"):
            val = rr.get(key_name)
            if isinstance(val, int) and val > 0:
                pids.append(val)
    return pids


def _backfill_inferred_clients(
    batch_jobs: list[dict[str, Any]],
    remote_runs: list[dict[str, Any]],
    inferred_clients: dict[int, tuple[str, int | None]],
) -> None:
    if not inferred_clients:
        return
    for job in batch_jobs:
        if not isinstance(job, dict) or str(job.get("status") or "").lower() != "running":
            continue
        if str(job.get("client") or "").strip() not in _GENERIC_CLIENTS:
            continue
        hit = None
        for key_name in ("pid", "owner_pid"):
            val = job.get(key_name)
            if isinstance(val, int) and val in inferred_clients:
                hit = inferred_clients[val]
                break
        if hit:
            job["client"] = hit[0]
            if not job.get("owner_pid") and hit[1]:
                job["owner_pid"] = hit[1]
    for rr in remote_runs:
        if not isinstance(rr, dict):
            continue
        if not (rr.get("alive") or str(rr.get("status") or "").lower() == "running"):
            continue
        if str(rr.get("client") or "").strip() not in _GENERIC_CLIENTS:
            continue
        hit = None
        for key_name in ("owner_pid", "vivado_pid"):
            val = rr.get(key_name)
            if isinstance(val, int) and val in inferred_clients:
                hit = inferred_clients[val]
                break
        if hit:
            rr["client"] = hit[0]
            if not rr.get("owner_pid") and hit[1]:
                rr["owner_pid"] = hit[1]


def _enrich_etas_sync(
    batch_jobs: list[dict[str, Any]],
    remote_runs: list[dict[str, Any]],
    run_monitors: list[dict[str, Any]],
    inspected_targets: list[dict[str, Any]],
) -> None:
    for job in batch_jobs:
        if not isinstance(job, dict):
            continue
        with contextlib.suppress(Exception):
            eta = compute_build_eta(
                status=str(job.get("status") or ""),
                kind=str(job.get("kind") or ""),
                project=str(job.get("project") or ""),
                top=str(job.get("top") or ""),
                part=str(job.get("part") or ""),
                output_dir=job.get("output_dir") or None,
                log_path=job.get("log_path") or None,
                tcl_path=job.get("tcl_path") or None,
                elapsed_sec=job.get("elapsed_sec"),
                finished_at=job.get("finished_at"),
            )
            if eta is not None:
                job["eta"] = eta

    for rr in remote_runs:
        if not isinstance(rr, dict):
            continue
        with contextlib.suppress(Exception):
            eta = compute_build_eta(
                status=str(rr.get("status") or rr.get("state") or ""),
                run_name=str(rr.get("run_name") or ""),
                target_step=str(rr.get("target_step") or ""),
                project=str(rr.get("project_name") or ""),
                top=str(rr.get("top") or ""),
                part=str(rr.get("part") or ""),
                xpr_path=str(rr.get("xpr_path") or ""),
                run_dir=rr.get("run_dir") or None,
                log_path=rr.get("log_path") or None,
                elapsed_sec=rr.get("elapsed_sec"),
            )
            if eta is not None:
                rr["eta"] = eta

    for mon in run_monitors:
        if not isinstance(mon, dict):
            continue
        run = mon.get("run")
        if not isinstance(run, dict) or not run:
            continue
        with contextlib.suppress(Exception):
            eta = compute_build_eta(
                status=str(run.get("state") or mon.get("connection") or ""),
                run_name=str(mon.get("run_name") or ""),
                target_step=str(mon.get("target_step") or ""),
                project=str(run.get("project") or ""),
                top=str(run.get("top") or ""),
                xpr_path=str(run.get("project_file") or ""),
                run_dir=run.get("directory") or None,
                log_path=run.get("log_path") or None,
                elapsed_sec=run.get("elapsed"),
            )
            if eta is not None:
                mon["eta"] = eta

    for insp in inspected_targets:
        if not isinstance(insp, dict):
            continue
        with contextlib.suppress(Exception):
            eta = compute_build_eta(
                status=str(insp.get("status") or ""),
                kind=str(insp.get("kind") or ""),
                project=str(insp.get("project") or ""),
                top=str(insp.get("top") or ""),
                part=str(insp.get("part") or ""),
                xpr_path=str(insp.get("xpr_path") or ""),
                output_dir=insp.get("output_dir") or None,
                log_path=insp.get("log_path") or None,
                runs=insp.get("runs") if isinstance(insp.get("runs"), list) else None,
            )
            if eta is not None:
                insp["eta"] = eta


async def collect_console_state(
    app_ctx: AppContext,
    *,
    inspect_path: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """收集 Vivado 控制台完整聚合状态（MCP App 与 Local Web 共用）。"""
    inspect_error = ""
    inspected_entry: dict[str, Any] | None = None
    if inspect_path and inspect_path.strip():
        try:
            inspected_entry = await asyncio.to_thread(
                app_ctx.batch_jobs.inspect_and_remember, inspect_path.strip()
            )
        except (ValueError, OSError) as exc:
            inspect_error = str(exc)

    async def _recover_shared_hub() -> None:
        if app_ctx.console_http is not None:
            return
        shared_url = getattr(app_ctx, "console_shared_url", "")
        if not isinstance(shared_url, str) or not shared_url.startswith("http://127.0.0.1:"):
            return
        shared_port = urlsplit(shared_url).port
        still_alive = (
            await asyncio.to_thread(
                probe_existing_console_hub, shared_port, timeout=0.1
            )
            if shared_port
            else None
        )
        if not still_alive:
            app_ctx.console_shared_url = ""
            with contextlib.suppress(OSError):
                await ensure_console_http(app_ctx, open_browser=False)

    async def _safe_sessions() -> list[dict[str, Any]]:
        try:
            return await app_ctx.session_manager.list_sessions(probe_external=True)
        except Exception:
            try:
                return await app_ctx.session_manager.list_sessions(probe_external=False)
            except Exception:
                return []

    def _safe_remote_runs() -> list[dict[str, Any]]:
        shared_reg = getattr(app_ctx, "shared_registry", None)
        if shared_reg is None:
            return []
        try:
            return shared_reg.list_remote_runs(exclude_owner_pid=os.getpid())
        except Exception:
            return []

    hub_task = asyncio.create_task(_recover_shared_hub())
    sessions_task = asyncio.create_task(_safe_sessions())
    installs_task = asyncio.to_thread(list_vivado_installations)
    inspected_task = asyncio.to_thread(app_ctx.batch_jobs.list_inspected)
    jobs_task = asyncio.to_thread(app_ctx.batch_jobs.list_jobs, limit=30)
    remote_task = asyncio.to_thread(_safe_remote_runs)

    _, sessions, vivado_installs, inspected_targets, batch_jobs, remote_runs = (
        await asyncio.gather(
            hub_task,
            sessions_task,
            installs_task,
            inspected_task,
            jobs_task,
            remote_task,
        )
    )
    run_monitors = [
        _sanitize_monitor_snapshot(s) for s in app_ctx.run_monitors.snapshots()
    ]
    debug_sessions = [
        _sanitize_debug_snapshot(s) for s in app_ctx.debug_services.snapshots()
    ]

    pids_to_infer = _collect_pids_needing_attribution(batch_jobs, remote_runs)
    if pids_to_infer:
        inferred_clients = await asyncio.to_thread(infer_pids_clients, pids_to_infer)
        _backfill_inferred_clients(batch_jobs, remote_runs, inferred_clients)

    local_client = detect_client_source()
    known_pids, has_attached_gui = _build_known_eda_pids(
        sessions, batch_jobs, remote_runs, local_client
    )
    system, _ = await asyncio.gather(
        asyncio.to_thread(
            collect_system_telemetry, known_pids, has_attached_gui=has_attached_gui
        ),
        asyncio.to_thread(
            _enrich_etas_sync, batch_jobs, remote_runs, run_monitors, inspected_targets
        ),
    )

    state: dict[str, Any] = {
        "client": local_client,
        "sessions": sessions,
        "run_monitors": run_monitors,
        "batch_jobs": batch_jobs,
        "remote_runs": remote_runs,
        "debug_sessions": debug_sessions,
        "inspected_targets": inspected_targets,
        "vivado_installs": vivado_installs,
        "local_web": _local_web_info(app_ctx),
        "system": system,
        "server_time": time.time(),
    }
    return _shrink_payload(state), inspected_entry, inspect_error


def _read_guarded_report(
    app_ctx: AppContext,
    report_path: str,
    max_lines: int = 300,
) -> dict[str, Any]:
    """严格白名单根路径校验后有界读取 .rpt / .log 文件。"""
    if not report_path or not isinstance(report_path, str) or "\x00" in report_path:
        raise ValueError("无效的报告路径")

    raw_path = Path(report_path.strip()).expanduser()
    if _is_link_or_junction(raw_path):
        raise ValueError("安全策略拒绝读取符号链接或联接文件")

    resolved = raw_path.resolve(strict=True)
    if _is_link_or_junction(resolved) or not resolved.is_file():
        raise ValueError("目标不是可读普通文件")

    monitor_snaps = app_ctx.run_monitors.snapshots()
    allowed_roots = app_ctx.batch_jobs.allowed_report_roots(monitor_snaps)
    shared_reg = getattr(app_ctx, "shared_registry", None)
    if shared_reg is not None:
        seen_roots = {os.path.normcase(str(r)) for r in allowed_roots}
        for rr_root in shared_reg.allowed_report_roots():
            key = os.path.normcase(str(rr_root))
            if key not in seen_roots:
                seen_roots.add(key)
                allowed_roots.append(rr_root)
    if not allowed_roots or not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise ValueError("访问被拒绝：路径不在已知构建任务、运行观察器或已检视工程目录内")

    suffix = resolved.suffix.lower()
    known_tcls = app_ctx.batch_jobs.known_tcl_paths()
    if suffix not in _ALLOWED_REPORT_EXTS and os.path.normcase(str(resolved)) not in known_tcls:
        raise ValueError(
            "安全策略仅允许预览 .rpt / .log / .txt 或任务生成的 Tcl 脚本"
            f"（收到 {suffix or '无扩展名'}）"
        )

    # 校验每一层目录都未通过链接逃逸白名单根
    matching_root = next(root for root in allowed_roots if resolved.is_relative_to(root))
    rel = resolved.relative_to(matching_root)
    cur = matching_root
    for part in rel.parts:
        cur = cur / part
        if _is_link_or_junction(cur):
            raise ValueError("安全策略拒绝经由符号链接目录读取文件")

    size = resolved.stat().st_size
    read_tail = suffix == ".log"
    truncated_bytes = size > _MAX_REPORT_PREVIEW_BYTES
    if not truncated_bytes:
        raw = resolved.read_bytes()
    elif read_tail:
        with resolved.open("rb") as handle:
            handle.seek(max(0, size - _MAX_REPORT_PREVIEW_BYTES))
            raw = handle.read(_MAX_REPORT_PREVIEW_BYTES)
    else:
        with resolved.open("rb") as handle:
            raw = handle.read(_MAX_REPORT_PREVIEW_BYTES)

    text = _decode_utf8_or_fallback(
        raw,
        offset_nonzero=bool(truncated_bytes and read_tail),
        final=not truncated_bytes,
    ).lstrip("\ufeff")
    all_lines = text.splitlines()
    if truncated_bytes and read_tail and all_lines:
        all_lines = all_lines[1:]

    clamped = max(1, min(int(max_lines), 1500))
    total_lines = len(all_lines)
    if total_lines > clamped:
        if read_tail:
            selected = all_lines[-clamped:]
            start_line = total_lines - clamped + 1
        else:
            selected = all_lines[:clamped]
            start_line = 1
        truncated = True
    else:
        selected = all_lines
        start_line = 1
        truncated = truncated_bytes

    def _build_report_payload(
        lines_slice: list[str], first_line: int, is_trunc: bool
    ) -> dict[str, Any]:
        return {
            "path": str(resolved),
            "name": resolved.name,
            "kind": "log" if suffix == ".log" else ("tcl" if suffix == ".tcl" else "rpt"),
            "size_bytes": size,
            "total_lines": total_lines,
            "returned_lines": len(lines_slice),
            "start_line": first_line,
            "tail": read_tail,
            "truncated": is_trunc,
            "lines": [
                {"line": first_line + idx, "text": line}
                for idx, line in enumerate(lines_slice)
            ],
        }

    result = _build_report_payload(selected, start_line, truncated)
    if _json_byte_len(result) > _MAX_REPORT_RESPONSE_BYTES:
        truncated = True
        working = list(selected)
        while len(working) > 1 and _json_byte_len(result) > _MAX_REPORT_RESPONSE_BYTES:
            drop = max(1, len(working) // 3)
            if read_tail:
                working = working[drop:]
                start_line += drop
            else:
                working = working[:-drop]
            result = _build_report_payload(working, start_line, True)
        if len(working) == 1 and _json_byte_len(result) > _MAX_REPORT_RESPONSE_BYTES:
            single = working[0]
            while len(single) > 64 and _json_byte_len(result) > _MAX_REPORT_RESPONSE_BYTES:
                single = single[: len(single) // 2]
                result = _build_report_payload(
                    [f"{single} ... [line truncated]"], start_line, True
                )
    return result


async def execute_console_action(
    app_ctx: AppContext,
    *,
    action: str,
    path: str | None = None,
    job_id: str | None = None,
    report_path: str | None = None,
    max_lines: int = 300,
    pid: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """执行控制台交互操作并返回 ``(text_summary, structured_payload)``。"""
    act = (action or "").strip()
    if act == "inspect_path":
        if not path or not path.strip():
            raise ValueError("inspect_path 操作需要传入 path")
        entry = await asyncio.to_thread(
            app_ctx.batch_jobs.inspect_and_remember, path.strip()
        )
        state, _, _ = await collect_console_state(app_ctx)
        state["selected_inspected_path"] = entry["target_path"]
        return "inspected", {"action": act, "inspected": entry, "state": state}

    if act == "remove_inspected":
        if not path or not path.strip():
            raise ValueError("remove_inspected 操作需要传入 path")
        removed = app_ctx.batch_jobs.remove_inspected(path.strip())
        state, _, _ = await collect_console_state(app_ctx)
        return "removed", {"action": act, "removed": removed, "state": state}

    if act == "read_report":
        if not report_path or not report_path.strip():
            raise ValueError("read_report 操作需要传入 report_path")
        preview = await asyncio.to_thread(
            _read_guarded_report, app_ctx, report_path.strip(), max_lines
        )
        return f"read {preview['name']}", {"action": act, "report": preview}

    if act == "cancel_job":
        if not job_id or not job_id.strip():
            raise ValueError("cancel_job 操作需要传入 job_id")
        job = await app_ctx.batch_jobs.cancel_job(job_id.strip())
        state, _, _ = await collect_console_state(app_ctx)
        return f"cancelled {job.job_id}", {
            "action": act,
            "job": job.to_dict(),
            "state": state,
        }

    if act == "clear_finished":
        cleared = app_ctx.batch_jobs.clear_finished()
        state, _, _ = await collect_console_state(app_ctx)
        return f"cleared {cleared}", {
            "action": act,
            "cleared": cleared,
            "state": state,
        }

    if act == "ensure_local_web":
        info = await ensure_console_http(app_ctx, open_browser=False)
        return f"local web {info['url']}", {
            "action": act,
            "url": info["url"],
            "local_web": info,
        }

    if act == "open_local_browser":
        info = await ensure_console_http(app_ctx, open_browser=True)
        return f"opened {info['url']}", {
            "action": act,
            "url": info["url"],
            "local_web": info,
        }

    if act == "kill_process":
        if pid is None or type(pid) is not int or pid < 1:
            raise ValueError("kill_process 操作需要传入正整数 pid")
        res = await asyncio.to_thread(kill_eda_process, pid)
        state, _, _ = await collect_console_state(app_ctx)
        return str(res.get("message") or f"killed {pid}"), {
            "action": act,
            "ok": bool(res.get("ok", True)),
            "pid": res.get("pid", pid),
            "name": res.get("name", ""),
            "message": res.get("message", ""),
            "state": state,
        }

    if act == "cleanup_stale_registry":
        shared_reg = getattr(app_ctx, "shared_registry", None)
        removed = (
            await asyncio.to_thread(shared_reg.cleanup_stale)
            if shared_reg is not None
            else 0
        )
        state, _, _ = await collect_console_state(app_ctx)
        return f"cleaned {removed} stale entries", {
            "action": act,
            "ok": True,
            "removed_entries": removed,
            "state": state,
        }

    raise ValueError(f"不支持的控制台操作: {act!r}")


@mcp.tool(
    name="open_vivado_console",
    title="Vivado 控制台",
    meta={
        "ui": {"resourceUri": CONSOLE_APP_URI},
        "com.google.antigravity/ui": {
            "entrypoints": [
                {"type": "aux_pane", "title": "Vivado 控制台"},
                {"type": "global", "title": "Vivado 控制台"},
            ]
        },
    },
)
async def open_vivado_console(
    inspect_path: str | None = None,
    job_id: str | None = None,
    open_browser: bool = False,
    ctx: Context = None,
) -> CallToolResult:
    """打开跨 Agent Vivado 控制台（Runs/跨会话构建/Batch/主机内存/EDA 进程与 .xpr 检视）。
    Antigravity 默认侧栏（open_browser=False），CLI/浏览器宿主演示传 open_browser=True。"""
    app_ctx = get_app_context(ctx)
    local_web_err = ""
    try:
        await ensure_console_http(app_ctx, open_browser=False)
    except OSError as exc:
        if open_browser:
            raise
        logger.warning("静默启动本机控制台 HTTP 服务失败: %s", exc)
        local_web_err = str(exc)

    state, inspected_entry, inspect_error = await collect_console_state(
        app_ctx, inspect_path=inspect_path
    )
    if local_web_err and not state.get("local_web", {}).get("running"):
        state["local_web"] = {
            "running": False,
            "url": "",
            "host_port": "",
            "preferred_port": preferred_console_port(),
            "shared_hub": False,
            "error": local_web_err,
        }
    if job_id:
        state["selected_job_id"] = job_id
    if inspected_entry is not None:
        state["selected_inspected_path"] = inspected_entry["target_path"]
    if inspect_error:
        state["inspect_error"] = inspect_error

    if app_ctx.console_http is not None:
        app_ctx.console_http.set_selection(
            job_id=job_id,
            inspected_path=inspected_entry["target_path"] if inspected_entry else None,
        )

    local_url = state.get("local_web", {}).get("url", "")
    if local_url:
        state["url"] = local_url
        if open_browser:
            try:
                await asyncio.to_thread(webbrowser.open, local_url)
            except Exception as exc:
                logger.warning("自动打开浏览器失败: %s", exc)

    state = _shrink_payload(state)

    running_jobs = sum(1 for j in state["batch_jobs"] if j.get("status") == "running")
    summary_text = (
        f"已打开 Vivado 控制台：运行观察 {len(state['run_monitors'])} 项，"
        f"Batch/OOC 任务 {len(state['batch_jobs'])} 项（运行中 {running_jobs}），"
        f"离线检视 {len(state['inspected_targets'])} 项，活跃会话 {len(state['sessions'])} 个。"
    )
    if local_url:
        prefix = "本地浏览器地址" if open_browser else "本机服务地址"
        summary_text += f" {prefix}: {local_url}"
    if inspect_error:
        summary_text += f" [检视失败: {inspect_error}]"

    return CallToolResult(
        content=[TextContent(type="text", text=summary_text)],
        structuredContent=state,
    )


@mcp.tool(meta={"ui": {"visibility": ["app"]}})
async def vivado_console_state(
    inspect_path: str | None = None,
    ctx: Context = None,
) -> CallToolResult:
    """App-only：刷新并返回 Vivado 控制台完整聚合状态（对模型隐藏，零上下文开销）。"""
    app_ctx = get_app_context(ctx)
    state, inspected_entry, inspect_error = await collect_console_state(
        app_ctx, inspect_path=inspect_path
    )
    if inspected_entry is not None:
        state["selected_inspected_path"] = inspected_entry["target_path"]
    if inspect_error:
        state["inspect_error"] = inspect_error
    state = _shrink_payload(state)
    return CallToolResult(
        content=[TextContent(type="text", text="ok")],
        structuredContent=state,
        isError=bool(inspect_error),
    )


@mcp.tool(meta={"ui": {"visibility": ["app"]}})
async def vivado_console_action(
    action: str,
    path: str | None = None,
    job_id: str | None = None,
    report_path: str | None = None,
    max_lines: int = 300,
    pid: int | None = None,
    ctx: Context = None,
) -> CallToolResult:
    """App-only：执行控制台交互操作（离线检视、移除、受限报告预览、取消任务、
    清理记录、终止孤儿 EDA 进程、本机服务/浏览器打开）。"""
    app_ctx = get_app_context(ctx)
    try:
        summary, structured = await execute_console_action(
            app_ctx,
            action=action,
            path=path,
            job_id=job_id,
            report_path=report_path,
            max_lines=max_lines,
            pid=pid,
        )
        return CallToolResult(
            content=[TextContent(type="text", text=summary)],
            structuredContent=structured,
        )
    except KeyError:
        return CallToolResult(
            content=[TextContent(type="text", text=f"未找到任务: {job_id}")],
            structuredContent={"error": f"未找到任务: {job_id}"},
            isError=True,
        )
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=str(exc))],
            structuredContent={"error": str(exc)},
            isError=True,
        )
