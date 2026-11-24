"""独立启动本地 Vivado 控制台 HTTP 页面（零 Vivado 进程开销）。"""

from __future__ import annotations

import asyncio
import sys
import webbrowser

from vivado_mcp.console_http import (
    ConsoleHttpServer,
    preferred_console_port,
    probe_existing_console_hub,
)
from vivado_mcp.server import AppContext
from vivado_mcp.vivado.session_manager import SessionManager


async def run_console_cli(args) -> None:
    """在回环地址启动 Vivado 控制台页面并阻塞至中断退出。"""
    raw_port = getattr(args, "port", None)
    requested_port = preferred_console_port() if raw_port is None else raw_port
    if type(requested_port) is not int or not (0 <= requested_port <= 65535):
        raise ValueError("--port 必须在 0~65535 之间")

    inspect_arg = getattr(args, "inspect", None)
    if requested_port > 0 and not inspect_arg:
        existing_url = await asyncio.to_thread(probe_existing_console_hub, requested_port)
        if existing_url:
            print(f"[INFO] 复用已运行的 otter-vivado 控制台: {existing_url}", file=sys.stderr)
            print(existing_url, flush=True)
            if not getattr(args, "no_browser", False):
                await asyncio.to_thread(webbrowser.open, existing_url)
            return

    app_ctx = AppContext(session_manager=SessionManager(vivado_path=""))
    inspected_path: str | None = None
    if inspect_arg:
        entry = await asyncio.to_thread(
            app_ctx.batch_jobs.inspect_and_remember, inspect_arg
        )
        inspected_path = entry["target_path"]

    bind_port = requested_port
    if requested_port > 0 and inspect_arg:
        existing_url = await asyncio.to_thread(probe_existing_console_hub, requested_port)
        if existing_url:
            print(
                f"[INFO] 端口 {requested_port} 已有共享控制台运行，--inspect 将回退到自动端口。",
                file=sys.stderr,
            )
            bind_port = 0

    server = ConsoleHttpServer(
        app_ctx, loop=asyncio.get_running_loop(), port=bind_port
    )
    app_ctx.console_http = server
    try:
        try:
            url = await asyncio.to_thread(server.start)
        except OSError as exc:
            if bind_port == 0:
                raise
            print(
                f"[WARN] 首选端口 {bind_port} 已被占用 ({exc})，回退到自动分配端口。",
                file=sys.stderr,
            )
            server = ConsoleHttpServer(app_ctx, loop=asyncio.get_running_loop(), port=0)
            app_ctx.console_http = server
            url = await asyncio.to_thread(server.start)
        if inspected_path:
            server.set_selection(inspected_path=inspected_path)
        print(url, flush=True)
        if not getattr(args, "no_browser", False):
            await asyncio.to_thread(webbrowser.open, url)
        await asyncio.Event().wait()
    finally:
        await asyncio.to_thread(server.close)
        await app_ctx.batch_jobs.close()
        app_ctx.shared_registry.close()
