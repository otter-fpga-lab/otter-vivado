"""Antigravity / 浏览器 Vivado 控制台 MCP App 服务端资源、入口与零 Token 工具。"""

from __future__ import annotations

import asyncio
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
from vivado_mcp.config import list_vivado_installations
from vivado_mcp.server import AppContext, get_app_context, mcp

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
    srv = getattr(app_ctx, "console_http", None)
    url = getattr(srv, "url", "") if srv is not None else ""
    if not isinstance(url, str) or not url.startswith("http://127.0.0.1:"):
        return {"running": False, "url": "", "host_port": ""}
    host_port = getattr(srv, "host_port", "") or urlsplit(url).netloc
    return {"running": True, "url": url, "host_port": host_port}


async def ensure_console_http(
    app_ctx: AppContext,
    *,
    open_browser: bool = False,
    job_id: str | None = None,
    inspected_path: str | None = None,
) -> dict[str, Any]:
    """确保轻量级本机回环控制台 HTTP 服务已启动，并仅在需要时打开浏览器。"""
    from vivado_mcp.console_http import ConsoleHttpServer

    loop = asyncio.get_running_loop()
    if app_ctx.console_http is None:
        app_ctx.console_http = ConsoleHttpServer(app_ctx, loop=loop)
    else:
        app_ctx.console_http.set_loop(loop)

    url = await asyncio.to_thread(app_ctx.console_http.start)
    if job_id or inspected_path:
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
    for bucket_key in ("batch_jobs", "inspected_targets"):
        for item in trimmed.get(bucket_key) or []:
            if isinstance(item, dict):
                item["log_tail"] = (item.get("log_tail") or [])[-12:]
                item["phases"] = (item.get("phases") or [])[-15:]
                diag = item.get("diagnostics") or {}
                diag.pop("warning_items", None)
                diag["all_issues"] = (diag.get("all_issues") or [])[:15]
    if _json_byte_len(trimmed) <= limit_bytes:
        return trimmed

    for bucket_key in ("batch_jobs", "inspected_targets"):
        items = trimmed.get(bucket_key) or []
        trimmed[bucket_key] = items[:12]
        for item in trimmed[bucket_key]:
            if isinstance(item, dict):
                item["log_tail"] = []
                item["phases"] = []
                item.pop("forensics", None)
                item["reports"] = (item.get("reports") or [])[:20]
                diag = item.get("diagnostics") or {}
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
    for bucket_key in ("batch_jobs", "inspected_targets"):
        for item in trimmed.get(bucket_key) or []:
            if isinstance(item, dict):
                item["reports"] = []
                diag = item.get("diagnostics")
                if isinstance(diag, dict):
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

    for keep in (8, 4, 2, 1, 0):
        for list_key in (
            "batch_jobs",
            "inspected_targets",
            "run_monitors",
            "debug_sessions",
            "sessions",
            "vivado_installs",
        ):
            val = trimmed.get(list_key)
            if isinstance(val, list) and len(val) > keep:
                trimmed[list_key] = val[:keep]
        if _json_byte_len(trimmed) <= limit_bytes:
            return trimmed

    return trimmed


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

    async def _safe_sessions() -> list[dict[str, Any]]:
        try:
            return await app_ctx.session_manager.list_sessions(probe_external=True)
        except Exception:
            try:
                return await app_ctx.session_manager.list_sessions(probe_external=False)
            except Exception:
                return []

    sessions_task = asyncio.create_task(_safe_sessions())
    installs_task = asyncio.to_thread(list_vivado_installations)
    inspected_task = asyncio.to_thread(app_ctx.batch_jobs.list_inspected)

    sessions, vivado_installs, inspected_targets = await asyncio.gather(
        sessions_task, installs_task, inspected_task
    )
    run_monitors = [
        _sanitize_monitor_snapshot(s) for s in app_ctx.run_monitors.snapshots()
    ]
    batch_jobs = app_ctx.batch_jobs.list_jobs(limit=30)
    debug_sessions = [
        _sanitize_debug_snapshot(s) for s in app_ctx.debug_services.snapshots()
    ]

    state: dict[str, Any] = {
        "sessions": sessions,
        "run_monitors": run_monitors,
        "batch_jobs": batch_jobs,
        "debug_sessions": debug_sessions,
        "inspected_targets": inspected_targets,
        "vivado_installs": vivado_installs,
        "local_web": _local_web_info(app_ctx),
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
    """打开 Vivado 控制台侧边栏/全局界面，或以 open_browser=True 在本机浏览器打开同款界面。

    Antigravity 客户端默认使用内置 MCP App 侧栏/全局视图（open_browser=False），
    同时静默启动本机 127.0.0.1 回环服务并在控制台展示可复制与弹出的地址；
    Codex / Claude Code / Cursor 等无 MCP App 宿主的客户端可传 open_browser=True，
    直接拉起系统浏览器访问同一控制台并零 Token 查看。
    """
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
    ctx: Context = None,
) -> CallToolResult:
    """App-only：执行控制台交互操作（离线检视、移除、受限报告预览、取消任务、清理记录、本机服务/浏览器打开）。"""
    app_ctx = get_app_context(ctx)
    try:
        summary, structured = await execute_console_action(
            app_ctx,
            action=action,
            path=path,
            job_id=job_id,
            report_path=report_path,
            max_lines=max_lines,
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
