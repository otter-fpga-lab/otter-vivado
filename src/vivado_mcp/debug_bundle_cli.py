"""专用已有空 GUI 会话的调试交付导出入口，不重启或关闭 Vivado。"""

import json

from vivado_mcp.debug_bundle import export_debug_bundle
from vivado_mcp.vivado.gui_session import GuiSession


async def run_debug_export_cli(args) -> int:
    if not 1 <= args.port <= 65535:
        raise ValueError("--port 必须是专用已有 GUI 的实际协议端口")
    session = GuiSession("", session_id="debug-export", port=args.port, attach_only=True)
    try:
        await session.start(timeout=5)
        result = await export_debug_bundle(
            session, args.checkpoint, args.output_dir, args.part,
            args.source_revision, args.timeout,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "exported" else 1
    finally:
        await session.stop()
