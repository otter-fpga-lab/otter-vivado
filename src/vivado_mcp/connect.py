"""将本仓 Skill 与 editable MCP 一次接入客户端，不触碰 Vivado 安装或会话。"""

from __future__ import annotations

import json
import os
import sys
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from vivado_mcp import doctor


def _editable_source() -> Path:
    """核对当前解释器确实从同一份 editable 源码加载本包。"""
    source = Path(__file__).resolve().parents[2]
    hint = (
        '请在产品源码目录中运行 '
        f'"{sys.executable}" -m pip install -e .，再用同一解释器运行 connect。'
    )
    try:
        data = json.loads(metadata.distribution("vivado-mcp").read_text("direct_url.json") or "{}")
        url = urlsplit(data.get("url", ""))
        path = url2pathname((f"//{url.netloc}" if url.netloc else "") + url.path)
        editable = data.get("dir_info", {}).get("editable") is True
        if not editable or url.scheme != "file" or Path(path).resolve() != source:
            raise ValueError("当前包不是本仓的 editable 安装")
    except (metadata.PackageNotFoundError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"无法确认唯一源码接入：{exc}。{hint}") from exc
    if not (source / "pyproject.toml").is_file():
        raise RuntimeError(f"源码目录缺少 pyproject.toml。{hint}")
    if not (source / "skills" / "otter-vivado" / "SKILL.md").is_file():
        raise RuntimeError(f"源码目录缺少 skills/otter-vivado/SKILL.md。{hint}")
    return source


def _same_skill_link(target: Path, source: Path) -> bool:
    """识别同源目录符号链接；Windows 同时接受用户已创建的 junction。"""
    is_link = target.is_symlink()
    if sys.platform == "win32" and target.exists():
        # Python 3.10 尚无 Path.is_junction；realpath 会解析目录 junction。
        is_link = is_link or os.path.realpath(target) != os.path.abspath(target)
    return is_link and target.resolve() == source.resolve()


def _check_client_source(client: str, path: Path) -> bool:
    """预检配置，返回是否已接入当前解释器，拒绝覆盖别的安装或非法配置。"""
    if path.exists() and not path.is_file():
        raise FileExistsError(f"客户端配置路径被非文件占用：{path}")
    check = (
        doctor._check_codex_config(path)
        if client == "codex"
        else doctor._check_claude_config(path)
    )
    if check.fixable:
        return False
    if check.status != "ok" and "entry" not in check.evidence:
        raise ValueError(check.message)
    content = path.read_text(encoding="utf-8")
    if client == "codex":
        data = doctor.tomllib.loads(content)
        servers = data["mcp_servers"]
    else:
        data = json.loads(content)
        servers = data["mcpServers"]
    name, entry = doctor._find_server_entry(servers)
    # 不 resolve 解释器：两个 venv 的 python 可能链接到同一个系统可执行文件。
    if not isinstance(entry, dict):
        raise ValueError(check.message)
    command = entry.get("command")
    same_python = isinstance(command, str) and (
        os.path.normcase(command) == os.path.normcase(sys.executable)
    )
    # doctor 兼容旧入口名称；源码接入也接受当前解释器自身的 python3.x 名称。
    if not same_python or entry.get("type", "stdio") != "stdio" or entry.get("args") not in (
        ["-m", "vivado_mcp"],
        ["-m", "vivado_mcp", "serve"],
    ):
        raise ValueError(
            f"{path} 中 {name!r} 已指向其他启动入口，拒绝覆盖。"
            f"请核对后将 command 设为 {sys.executable!r}、args 设为 "
            "['-m', 'vivado_mcp']，再运行 connect。"
        )
    return True


def connect_source(client: str = "codex", skills_dir: Path | None = None) -> dict[str, object]:
    """显式接入本仓源码，保留其他配置；同源重复运行不写入。

    配置失败、路径占用或非 editable 安装会抛出明确异常。只调用 doctor 的
    客户端配置函数，不运行 doctor --fix，因此不会注入 init Tcl 或探测 Vivado。
    """
    if client not in {"codex", "claude-code"}:
        raise ValueError("client 必须是 codex 或 claude-code")
    source = _editable_source()
    skill_source = source / "skills" / "otter-vivado"
    home = Path.home()
    if client == "codex":
        config = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "config.toml"
        default_skills = home / ".agents" / "skills"
        fixer = doctor._fix_codex_config
    else:
        config = home / ".claude.json"
        default_skills = home / ".claude" / "skills"
        fixer = doctor._fix_claude_config
    target = (Path(skills_dir).expanduser() if skills_dir is not None else default_skills)
    target = target.absolute() / "otter-vivado"
    linked = _same_skill_link(target, skill_source)
    if os.path.lexists(target) and not linked:
        raise FileExistsError(f"Skill 目标已被其他内容占用，拒绝覆盖：{target}")
    configured = _check_client_source(client, config)
    created_link = False
    if not linked:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.symlink_to(skill_source, target_is_directory=True)
            created_link = True
        except OSError as exc:
            raise OSError(
                f"无法创建 Skill 目录链接 {target} -> {skill_source}：{exc}。"
                "Windows 可启用开发者模式获得符号链接权限，或在 cmd 中自行执行 "
                f'mklink /J "{target}" "{skill_source}" 后重试；不会复制 Skill 内容。'
            ) from exc
    try:
        if not configured:
            fixer(config, None)
        if not _check_client_source(client, config):
            raise RuntimeError("客户端配置写入后未找到本源码入口")
    except Exception:
        if created_link and _same_skill_link(target, skill_source):
            target.unlink()
        raise
    return {
        "status": "connected",
        "client": client,
        "source": str(source),
        "python": sys.executable,
        "config": str(config),
        "config_changed": not configured,
        "skill": str(target),
        "skill_source": str(skill_source),
        "skill_changed": created_link,
        "next": [
            "在客户端下一次加载配置/发现 Skill 时使用 otter-vivado；按其提示重新连接 MCP。",
            "先使用 start_session 连接或启动明确选择的 Vivado 工程，再查询运行状态。",
            "源码与 Skill 原地维护，无需同步；已加载的 MCP/模型上下文不承诺热更新。",
            "已有长任务先正常完成，再在空闲边界重新加载 MCP；本接入不会终止 Vivado。",
        ],
    }
