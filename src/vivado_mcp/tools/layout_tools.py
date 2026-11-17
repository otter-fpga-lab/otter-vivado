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
    """只读解析工程目录约定：过程文件、笔记、脚本、交付、Vivado 运行目录与回收站位置。

    project 为 .xpr 路径或工程目录；留空时取 session_id 会话当前打开的工程。
    返回 project_root/xpr/debug_dir/notes_file/scripts_dir/download_dir/vivado_cwd/
    trash_dir/config_path/rules 与 hygiene（只读提示残留 .vmcp/.work、误建 script/、
    超限 debug_dir，附移入 trash_dir 并写清单建议）。工程根下 otter-vivado.toml 的
    [layout] 可覆盖；配置非法时报错，不静默回退。trash_dir 为 null 表示未找到 99_垃圾堆，
    不要自动创建。本工具不创建目录；不要在工程内创建 .work/.vmcp 等隐藏目录。
    """
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
