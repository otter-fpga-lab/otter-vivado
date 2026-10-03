"""独立只读观察入口，连接已有 GUI 或显式回放输入，不启动 Vivado。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from vivado_mcp.monitor_http import MonitorHTTP
from vivado_mcp.run_monitor import RunMonitor, parse_snapshot
from vivado_mcp.vivado.gui_session import GuiSession


async def run_monitor_cli(args) -> None:
    """CLI 与 MCP 使用同一观察器和页面；退出只断开 attach 连接。"""
    if args.replay:
        path = Path(args.replay)
        value = {
            "source": "replay", "session_id": "replay", "session_mode": "replay",
            "run_name": args.run, "target_step": args.target, "connection": "connected",
            "observed_at": time.time(), "last_attempt_at": time.time(),
            "error": "合成/回放输入，不代表当前 Vivado 运行；不按时间生成进度",
            "run": parse_snapshot(path.read_text(encoding="utf-8"), args.run, args.target),
            "reports": [], "quality": {"timing": "unknown", "resources": "unknown"},
        }
        if args.json:
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return
        http = MonitorHTTP(lambda: value)
        try:
            print(http.start(), flush=True)
            await asyncio.Event().wait()
        finally:
            await asyncio.to_thread(http.close)
        return

    if not 1 <= args.port <= 65535:
        raise ValueError("--port 必须是已运行 GUI 的实际端口 (1~65535)")
    session = GuiSession("", session_id="monitor", port=args.port, attach_only=True)
    monitor = RunMonitor(session, args.run, args.target)
    try:
        await session.start(timeout=5)
        await monitor.sample()
        if args.json:
            # 一次输出最多等一秒报告；未读完显式保留 loading，不阻塞真实运行。
            await monitor.wait_for_reports(timeout=1.0)
            print(json.dumps(monitor.snapshot(), ensure_ascii=False, indent=2))
            return
        monitor.start()
        print(monitor.open_view(), flush=True)
        await asyncio.Event().wait()
    finally:
        await monitor.close()
        await session.stop()
