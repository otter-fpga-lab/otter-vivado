"""工程目录约定查询：get_project_layout（只读，不创建目录）。"""

import asyncio
import json

from mcp.server.mcpserver import Context

from vivado_mcp.project_layout import (
    LayoutError,
    inspect_layout_hygiene,
    query_session_xpr,
    resolve_layout,
)
from vivado_mcp.server import _NO_SESSION, _require_session, mcp


@mcp.tool()
async def get_project_layout(
    project: str = "", session_id: str = "default", ctx: Context = None,
) -> str:
    """只读解析工程目录约定（支持 otter-vivado.toml [layout]）及卫生提示；
    留空取 session_id 当前工程，不创建目录。"""
    try:
        source = "argument"
        if not project.strip():
            session = await _require_session(ctx, session_id)
            if session is None:
                raise LayoutError(
                    _NO_SESSION.format(sid=session_id) + " 或直接传入 project=.xpr 路径。"
                )
            project = await query_session_xpr(session) or ""
            if not project:
                raise LayoutError(
                    f"会话 '{session_id}' 当前没有打开工程；请先打开工程或传入 project=.xpr 路径。"
                )
            source = "session"
        layout = resolve_layout(project)
        hygiene = await asyncio.to_thread(inspect_layout_hygiene, layout)
        return json.dumps(
            {**layout.to_dict(), "source": source, "hygiene": hygiene},
            ensure_ascii=False,
        )
    except (LayoutError, OSError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
