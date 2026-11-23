"""独立启动本地 Vivado 控制台 HTTP 页面（零 Vivado 进程开销）。"""

from __future__ import annotations

import asyncio
import webbrowser

from vivado_mcp.console_http import ConsoleHttpServer
from vivado_mcp.server import AppContext
from vivado_mcp.vivado.session_manager import SessionManager


async def run_console_cli(args) -> None:
    """在回环地址启动 Vivado 控制台页面并阻塞至中断退出。"""
    if not 0 <= args.port <= 65535:
        raise ValueError("--port 必须在 0~65535 之间")

    app_ctx = AppContext(session_manager=SessionManager(vivado_path=""))
    inspected_path: str | None = None
    if getattr(args, "inspect", None):
        entry = await asyncio.to_thread(
            app_ctx.batch_jobs.inspect_and_remember, args.inspect
        )
        inspected_path = entry["target_path"]

    server = ConsoleHttpServer(
        app_ctx, loop=asyncio.get_running_loop(), port=args.port
    )
    app_ctx.console_http = server
    try:
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
