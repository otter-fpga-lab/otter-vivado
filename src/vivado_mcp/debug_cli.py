"""独立板上调试入口：连接已有 GUI，或显式使用本地合成演示。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from vivado_mcp.debug_demo import DEMO_DEVICE, DEMO_TARGET, DemoDebugBackend
from vivado_mcp.debug_service import DebugService, load_panel
from vivado_mcp.vivado.gui_session import GuiSession


async def run_debug_cli(args) -> None:
    """网页和 MCP 使用相同操作协议；退出仅断开 attach 连接。"""
    if bool(args.target) != bool(args.device):
        raise ValueError("--target 和 --device 必须同时指定")
    if not args.demo and not 1 <= args.port <= 65535:
        raise ValueError("--port 必须是已运行 GUI 的实际端口 (1~65535)")
    panel = load_panel(args.panel) if args.panel else None
    session = (
        SimpleNamespace(session_id="demo", is_alive=True, state="ready")
        if args.demo
        else GuiSession("", session_id="debug", port=args.port, attach_only=True)
    )
    service = None
    try:
        if not args.demo:
            await session.start(timeout=5)
        service = DebugService(
            session, backend=DemoDebugBackend() if args.demo else None,
            source="demo" if args.demo else "live",
        )
        if panel:
            service.set_panel(panel)
        service.submit(
            {"action": "inventory", "params": {},
             "expected_revision": service.snapshot()["revision"]},
            source="manual",
        )
        await service.wait_idle()
        # 仅显式 demo 可自动选择固定的合成设备；真实会话从不猜测板卡。
        target = args.target or (DEMO_TARGET if args.demo else None)
        device = args.device or (DEMO_DEVICE if args.demo else None)
        if target and device:
            service.submit(
                {"action": "select", "params": {"target": target, "device": device},
                 "expected_revision": service.snapshot()["revision"]},
                source="manual",
            )
            await service.wait_idle()
        if args.json:
            print(json.dumps(service.snapshot(), ensure_ascii=False, indent=2))
            return
        print(service.open_view(), flush=True)
        if args.demo:
            print("合成演示：不连接 Vivado/板卡，控件只修改本地示例数据。", flush=True)
        else:
            print("已连接现有 Vivado；在页面中核对并选择目标与设备。", flush=True)
        print("按 Ctrl+C 关闭本服务；不会关闭 Vivado 或停止硬件采集。", flush=True)
        await asyncio.Event().wait()
    finally:
        try:
            if service is not None:
                await service.close()
        finally:
            if not args.demo:
                await session.stop()
