"""通用 Tcl 执行工具：run_tcl / safe_tcl。

**设计哲学**：能用 run_tcl 做的事就不包装成专用工具——
一个工具的存在应该是因为它提供"Tcl 做不了或做不好"的本地价值（如结构化解析、
跨命令协议、本地知识库）。绝大多数 Vivado 操作就是一行 Tcl，让 AI 自己拼。

- ``run_tcl`` — 执行任意 Tcl 命令（AI 自己负责引号 / 路径 / 标识符安全性）
- ``safe_tcl`` — 带参数模板的版本，自动用 Tcl ``list`` 规则转义参数，
  Windows 路径含空格 / 中文 / ``$`` / ``[]`` / ``{}`` / 反斜杠也能安全执行
"""

import re
from pathlib import Path

from mcp.server.mcpserver import Context

from vivado_mcp.server import (
    _NO_SESSION,
    _require_session,
    _safe_execute,
    mcp,
    sync_shared_session,
)
from vivado_mcp.vivado.tcl_utils import tcl_quote

_RE_OPEN_PROJ = re.compile(
    r"(?i)\bopen_project\b[^\n;]*?(?:\"([^\"]+\.xpr)\"|\{([^\}]+\.xpr)\}|([^\s;\"'{}]+\.xpr))"
)
_RE_CREATE_PROJ = re.compile(
    r"(?i)\bcreate_project\b(?:\s+-\S+)*\s+"
    r"(?:\"([^\"]+)\"|\{([^\}]+)\}|([^\s;\"'{}]+))\s+"
    r"(?:\"([^\"]+)\"|\{([^\}]+)\}|([^\s;\"'{}]+))"
)


def _sync_tcl_project_hint(
    ctx: Context | None,
    session_id: str,
    command: str,
    result_text: str,
) -> None:
    """在 open_project/create_project 成功执行后无感同步共享注册表（零额外 Tcl 查询）。"""
    if result_text.startswith("[ERROR]") or not command:
        return
    m_open = _RE_OPEN_PROJ.search(command)
    if m_open:
        raw_xpr = next((g for g in m_open.groups() if g), "").strip()
        if raw_xpr:
            p = Path(raw_xpr).expanduser()
            resolved = str(p.resolve()) if p.exists() else raw_xpr
            sync_shared_session(
                ctx,
                session_id=session_id,
                project_name=p.stem,
                xpr_path=resolved,
                status="idle",
            )
            return
    m_create = _RE_CREATE_PROJ.search(command)
    if m_create:
        groups = m_create.groups()
        proj_name = next((g for g in groups[:3] if g), "").strip()
        proj_dir = next((g for g in groups[3:] if g), "").strip()
        if proj_name and not proj_name.startswith("-"):
            xpr_cand = Path(proj_dir).expanduser() / f"{proj_name}.xpr" if proj_dir else None
            xpr_str = (
                str(xpr_cand.resolve())
                if xpr_cand is not None and xpr_cand.exists()
                else (str(xpr_cand) if xpr_cand is not None else "")
            )
            sync_shared_session(
                ctx,
                session_id=session_id,
                project_name=proj_name,
                xpr_path=xpr_str or None,
                status="idle",
            )


@mcp.tool()
async def run_tcl(
    command: str,
    session_id: str = "default",
    timeout: int = 120,
    ctx: Context = None,
) -> str:
    """执行任意 Vivado Tcl 命令（支持多行）。含空格/中文/特殊字符路径请用 ``safe_tcl``。

    注意：``timeout``（默认 120s）超时仅终止本次等待，命令仍在 Vivado 内继续执行；
    长构建请用 ``run_synthesis`` / ``run_implementation`` / ``generate_bitstream``
    或 ``run_batch_tcl``；
    波形缩放/模拟显示请用 ``set_wave_zoom`` / ``set_wave_analog``
    （勿手写 ``RADIX DEC`` / ``ANALOG``）。
    """
    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    out = await _safe_execute(session, command, float(timeout), "命令执行失败")
    _sync_tcl_project_hint(ctx, session.session_id, command, out)
    return out


@mcp.tool()
async def safe_tcl(
    template: str,
    args: list[str] = None,
    session_id: str = "default",
    timeout: int = 120,
    ctx: Context = None,
) -> str:
    """用 Tcl ``list`` 规则转义 ``args`` 并格式化 ``template``（``{0}``/``{1}``），
    防止路径/特殊字符注入与解析错误。"""
    if args is None:
        args = []
    quoted = [tcl_quote(str(a)) for a in args]
    try:
        cmd = template.format(*quoted)
    except (IndexError, KeyError) as e:
        return (
            f"[ERROR] safe_tcl 模板占位符不匹配: {e}。"
            f"template={template!r}, args={args!r}"
        )

    session = await _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    out = await _safe_execute(session, cmd, float(timeout), "safe_tcl 执行失败")
    _sync_tcl_project_hint(ctx, session.session_id, cmd, out)
    return out
