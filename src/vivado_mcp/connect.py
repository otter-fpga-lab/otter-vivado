"""将本仓 Skill 与 editable MCP 一次接入客户端，不触碰 Vivado 安装或会话。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from vivado_mcp import doctor

CLIENTS = ("cursor", "claude-code", "codex", "antigravity")


def _editable_source() -> Path:
    """核对当前解释器确实从同一份 editable 源码加载本包。"""
    source = Path(__file__).resolve().parents[2]
    hint = (
        "请在产品源码目录中运行 "
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


def _client_paths(client: str, config_path: Path | None) -> tuple[Path, Path, str]:
    """使用客户端发现目录；已有 Antigravity 旧配置显式保留其路径。"""
    home = Path.home()
    note = ""
    if client == "codex":
        config = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "config.toml"
        skills = home / ".agents" / "skills"
    elif client == "claude-code":
        config, skills = home / ".claude.json", home / ".claude" / "skills"
    elif client == "cursor":
        config, skills = home / ".cursor" / "mcp.json", home / ".agents" / "skills"
        # 当前 Cursor 也读取 .agents/skills，与 Codex 共用一个链接。
        # 已有 Cursor 原生入口则接续，不再新建第二个入口。
        native = home / ".cursor" / "skills"
        if os.path.lexists(native / "otter-vivado"):
            skills = native
    elif client == "antigravity":
        current = home / ".gemini" / "config" / "mcp_config.json"
        legacy = home / ".gemini" / "antigravity" / "mcp_config.json"
        if config_path is None and current.exists() and legacy.exists():
            raise ValueError(
                "Antigravity 新旧 MCP 配置同时存在；请单独使用 --client antigravity "
                "--config <实际使用的文件> 选择，工具不会猜测或合并它们。"
            )
        config = config_path or (legacy if legacy.exists() and not current.exists() else current)
        old_layout = config.absolute() == legacy.absolute()
        skills = (legacy.parent if old_layout else current.parent) / "skills"
        if old_layout:
            note = "接续已有旧路径；当前 Antigravity 是否读取它需按实际版本确认"
    else:
        raise ValueError(f"client 必须是 {', '.join(CLIENTS)}")
    return config_path or config, skills, note


def _reject_codex_native_plugin(data: dict) -> None:
    if doctor._codex_native_plugin(data) is not None:
        raise ValueError(
            "Codex 已启用 Otter Vivado 原生插件，拒绝再创建普通 Skill/MCP 入口。"
            "继续使用插件；接入其他客户端时显式从 --client 列表去掉 codex/all。"
        )


def _config_update(client: str, path: Path) -> tuple[bytes | None, str | None, str]:
    """只生成缺失入口，保留其他服务器和配置；不输出文件里的凭据。"""
    if os.path.lexists(path) and not path.is_file():
        raise FileExistsError(f"客户端配置路径被非文件占用：{path}")
    # 原子替换文件会拆掉现有 symlink；实际写入其目标，保留用户链接。
    if path.is_symlink() and not path.exists():
        raise FileNotFoundError(f"配置文件是断开的符号链接：{path}")
    original = path.read_bytes() if path.exists() else None
    content = original.decode("utf-8-sig") if original is not None else ""
    try:
        if client == "codex":
            if doctor.tomllib is None:
                raise RuntimeError("当前 Python 缺少 TOML 解析器")
            data = doctor.tomllib.loads(content)
            key = "mcp_servers"
        else:
            data = json.loads(content) if content.strip() else {}
            key = "mcpServers"
    except ValueError as exc:
        raise ValueError(f"{path} 配置无法验证：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get(key, {}), dict):
        raise ValueError(f"{path} 配置及 {key} 必须是对象/表，拒绝覆盖")
    if client == "codex":
        _reject_codex_native_plugin(data)
    servers = data.get(key, {})
    candidates = [
        (name, entry)
        for name, entry in servers.items()
        if name in {"vivado", "vivado-mcp", "otter-vivado"}
        or (
            isinstance(entry, dict)
            and entry.get("args")
            in (
                ["-m", "vivado_mcp"],
                ["-m", "vivado_mcp", "serve"],
            )
        )
        or doctor._valid_server_entry(entry)
    ]
    mcp_state = "source_entry"
    for name, entry in candidates:
        command = entry.get("command") if isinstance(entry, dict) else None
        if (
            not isinstance(command, str)
            or os.path.normcase(command) != os.path.normcase(sys.executable)
            or entry.get("type", "stdio") != "stdio"
            or entry.get("args") not in (["-m", "vivado_mcp"], ["-m", "vivado_mcp", "serve"])
        ):
            raise ValueError(
                f"{path} 中 {name!r} 已指向其他启动入口，拒绝覆盖。"
                f"本源码解释器为 {sys.executable!r}；可先用 --skills-only 仅接入 Skill。"
            )
        env = entry.get("env", {})
        if entry.get("cwd") or (
            isinstance(env, dict) and (env.get("PYTHONPATH") or env.get("PYTHONHOME"))
        ):
            mcp_state = "search_path_override"
    if any(
        entry.get("disabled") is True or entry.get("enabled") is False for _, entry in candidates
    ):
        mcp_state = "disabled"
    if candidates:
        return original, None, mcp_state
    if client == "codex":
        prefix = content.rstrip()
        text = (prefix + "\n\n" if prefix else "") + "\n".join(
            [
                "[mcp_servers.vivado]",
                f"command = {doctor._toml_string(sys.executable)}",
                'args = ["-m", "vivado_mcp"]',
                "",
            ]
        )
        doctor.tomllib.loads(text)
    else:
        data.setdefault(key, {})["vivado"] = {
            "command": sys.executable,
            "args": ["-m", "vivado_mcp"],
        }
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return original, text, mcp_state


@dataclass
class _Connection:
    client: str
    source: Path
    config: Path
    config_target: Path
    target: Path
    note: str
    original: bytes | None
    replacement: str | None
    linked: bool
    skills_only: bool
    mcp_state: str

    def result(self, checking: bool = False) -> dict:
        state = "ready" if self.linked and self.replacement is None else "needs_connection"
        if self.mcp_state in {"disabled", "search_path_override"}:
            state = "needs_attention"
        return {
            "status": state,
            "client": self.client,
            "source": str(self.source),
            "python": sys.executable,
            "config": str(self.config),
            "config_changed": False,
            "config_needed": not self.skills_only and self.replacement is not None,
            "mcp": self.mcp_state,
            "skill": str(self.target),
            "skill_source": str(self.source / "skills/otter-vivado"),
            "skill_changed": False,
            "skill_needed": not self.linked,
            "note": self.note,
            "check_only": checking,
        }


def _plan(client, source, skills_dir=None, config_path=None, skills_only=False):
    config_path = Path(config_path).expanduser().absolute() if config_path is not None else None
    if skills_only and skills_dir is not None and config_path is None and client == "antigravity":
        # 目标 Skill 目录已明确；不因完全不使用的 MCP 配置歧义阻止链接。
        config_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    config, default_skills, note = _client_paths(client, config_path)
    if client == "codex" and skills_only and config.is_file():
        if doctor.tomllib is None:
            raise RuntimeError("当前 Python 缺少 TOML 解析器")
        _reject_codex_native_plugin(doctor.tomllib.loads(config.read_text(encoding="utf-8-sig")))
    target = Path(skills_dir).expanduser() if skills_dir is not None else default_skills
    target = target.absolute() / "otter-vivado"
    skill_source = source / "skills" / "otter-vivado"
    linked = _same_skill_link(target, skill_source)
    if os.path.lexists(target) and not linked:
        raise FileExistsError(f"Skill 目标已被其他内容占用，拒绝覆盖：{target}")
    original, replacement, mcp_state = (
        (None, None, "not_checked") if skills_only else _config_update(client, config)
    )
    if mcp_state == "disabled":
        note += "；已有 MCP 条目被用户停用，保持停用，不表示已可调用"
    elif mcp_state == "search_path_override":
        note += "；已有 cwd/PYTHONPATH/PYTHONHOME 覆盖，实际导入源须确认，设置保持不动"
    return _Connection(
        client,
        source,
        config,
        config.resolve(),
        target,
        note.lstrip("；"),
        original,
        replacement,
        linked,
        skills_only,
        mcp_state,
    )


def _create_junction(target: Path, source: Path) -> None:
    """Windows 目录 junction 无需 symlink 特权，严格引用两个字面路径。"""
    for path in (target, source):
        if any(char in str(path) for char in '"%!\r\n'):
            raise OSError("该路径不能安全传给 mklink；请使用开发者模式符号链接")
    # 直接使用 Windows 命令行字符串，避免 list2cmdline 将内层引号变成 \"。
    command = f'cmd.exe /d /s /c "mklink /J "{target}" "{source}""'
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(f"无法创建目录 junction：{target} -> {source}（rc={result.returncode}）")


def _create_link(target: Path, source: Path, mode: str) -> bool:
    """仅创建当前入口；同源共享入口幂等，绝不复制 Skill。"""
    if _same_skill_link(target, source):
        return False
    if os.path.lexists(target):
        raise FileExistsError(f"Skill 目标已在接入期间被占用，拒绝覆盖：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "junction":
        _create_junction(target, source)
    else:
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError as exc:
            if sys.platform == "win32" and mode == "auto" and getattr(exc, "winerror", 0) == 1314:
                _create_junction(target, source)
            else:
                raise OSError(
                    f"无法创建 Skill 链接 {target} -> {source}：{exc}。"
                    "Windows 可启用开发者模式或使用 --link-mode junction；不会复制内容。"
                ) from exc
    if not _same_skill_link(target, source):
        raise OSError(f"Skill 链接创建后未指向预期源码：{target}")
    return True


def _apply(plan: _Connection, mode: str) -> dict:
    if not plan.skills_only and plan.config.resolve() != plan.config_target:
        raise OSError(f"配置链接在接入期间发生变化，拒绝覆盖：{plan.config}")
    skill_source = plan.source / "skills/otter-vivado"
    created = _create_link(plan.target, skill_source, mode)
    try:
        if plan.replacement is not None:
            # 用户将配置文件链接到别处时，只更新真实目标，保留目录入口本身。
            doctor._atomic_write_with_backup(
                plan.config_target,
                plan.replacement,
                expected_bytes=plan.original,
            )
    except Exception:
        if created and _same_skill_link(plan.target, skill_source):
            if plan.target.is_symlink():
                plan.target.unlink()
            else:
                plan.target.rmdir()  # Windows junction：只移除目录入口，不递归删除源目录
        raise
    status = (
        "needs_attention" if plan.mcp_state in {"disabled", "search_path_override"} else "connected"
    )
    return {
        **plan.result(),
        "status": status,
        "skill_changed": created,
        "config_changed": plan.replacement is not None,
        "skill_needed": False,
        "config_needed": False,
    }


def connect_clients(
    clients=("codex",),
    *,
    check: bool = False,
    skills_dir: Path | None = None,
    config_path: Path | None = None,
    skills_only: bool = False,
    link_mode: str = "auto",
) -> dict:
    """先预检所选全部客户端，再接入；--check 从不创建目录或改客户端。"""
    if isinstance(clients, str):
        clients = (clients,)
    if any(client not in (*CLIENTS, "all") for client in clients):
        raise ValueError(f"client 必须是 {', '.join(CLIENTS)} 或 all")
    selected = tuple(dict.fromkeys(CLIENTS if "all" in clients else clients))
    if not selected or any(client not in CLIENTS for client in selected):
        raise ValueError(f"client 必须是 {', '.join(CLIENTS)} 或 all")
    if len(selected) > 1 and (skills_dir is not None or config_path is not None):
        raise ValueError("--skills-dir/--config 只适用于单个客户端")
    if link_mode not in {"auto", "symlink", "junction"}:
        raise ValueError("link_mode 必须是 auto/symlink/junction")
    if link_mode == "junction" and sys.platform != "win32":
        raise ValueError("junction 仅适用于 Windows；此系统请用 auto 或 symlink")
    source = _editable_source()
    plans = [_plan(client, source, skills_dir, config_path, skills_only) for client in selected]
    results = []
    written: dict[Path, bytes] = {}
    for plan in plans:
        try:
            if not check and plan.replacement is not None and plan.config_target in written:
                actual = plan.config_target.read_bytes()
                if actual != written[plan.config_target] or actual != plan.replacement.encode(
                    "utf-8"
                ):
                    raise OSError(f"共享 MCP 配置在接入期间发生变化，拒绝覆盖：{plan.config}")
                plan.original, plan.replacement = actual, None
            results.append(plan.result(checking=True) if check else _apply(plan, link_mode))
            if not check and plan.replacement is not None:
                written[plan.config_target] = plan.replacement.encode("utf-8")
        except OSError as exc:
            if results:
                completed = ", ".join(result["client"] for result in results)
                raise OSError(
                    f"已完成 {completed}，{plan.client} 接入失败：{exc}。"
                    "已完成入口保留；修复后重跑幂等接续，不会复制 Skill。"
                ) from exc
            raise
    status = (
        ("ready" if all(r["status"] == "ready" for r in results) else "needs_connection")
        if check
        else "connected"
    )
    if any(r["status"] == "needs_attention" for r in results):
        status = "needs_attention"
    return {
        "status": status,
        "source": str(source),
        "clients": results,
        "check_only": check,
        "next": [
            "Skill 与 references 经目录链接原地读取；普通改源或 git pull 无需同步/复制。",
            "源码 MCP 已加载模块在正常空闲重连边界生效；不会自动重启活动 Vivado。",
        ],
    }


def connect_source(client="codex", skills_dir=None, **options) -> dict:
    """单客户端 API 与批量入口共用同一实现。"""
    result = connect_clients((client,), skills_dir=skills_dir, **options)
    return {**result["clients"][0], "next": result["next"]}
