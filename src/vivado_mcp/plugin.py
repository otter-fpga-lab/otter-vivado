"""Create a small host plugin bound to this editable source."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from vivado_mcp import __version__
from vivado_mcp.connect import _editable_source


def create_plugin(client: str, output: Path) -> dict:
    if client not in ("codex", "antigravity", "workbuddy"):
        raise ValueError(
            "Only Codex, Antigravity and WorkBuddy native plugin formats are generated"
        )
    output = Path(output).expanduser()
    if not output.is_absolute():
        raise ValueError("Plugin output must be an explicit absolute directory")
    if os.path.lexists(output):
        raise FileExistsError(f"Refusing to replace existing plugin contents: {output}")

    try:
        root = _editable_source()
    except RuntimeError as exc:
        raise RuntimeError(
            "Plugin generation requires this source's editable installation. "
            "Prepare the product private .venv, then run the plugin command with it."
        ) from exc
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if Path(sys.prefix).resolve() != environment.resolve() or not python.is_file():
        raise ValueError("Generate the plugin with this product's private .venv interpreter")
    bootstrap = (root / "packaging/codex/skills/otter-vivado/SKILL.md").read_text(encoding="utf-8")
    manifest = {
        "name": "otter-vivado",
        "version": __version__,
        "description": "Local and remote Vivado tools, using one maintained source checkout.",
        "skills": "./skills/",
        "mcpServers": "./.mcp.json",
        "interface": {
            "displayName": "Otter Vivado",
            "shortDescription": "Vivado 工程、构建、调试与报告",
        },
    }
    mcp = {
        "mcpServers": {
            "otter-vivado": {
                "command": str(python),
                "args": ["-B", "-m", "vivado_mcp"],
            }
        }
    }
    host = {"codex": "Codex", "antigravity": "Antigravity", "workbuddy": "WorkBuddy"}[client]
    readme = f"""# Otter Vivado：{host} 本机插件入口

此目录只有 manifest、MCP 配置和简短引导 Skill。业务源码与指南仍在 `{root}`。
由宿主支持的本地插件入口安装；生成器没有安装插件或修改客户端配置。

MCP 使用源仓私有解释器 `{python}` 的 editable 安装，从宿主缓存启动也读取同一源。
`vivado_guide` 每次读取当前 Skill/参考，普通指南更新无需重新复制插件。
Python 业务更新在正常空闲边界重载 MCP；重载拥有本地 Vivado 的服务会清理其会话，
不要为刷新文字中断活动任务。源位置、解释器或引导协议变化时重新生成明确的新入口。

在同一宿主选择此插件或既有源码 Skill/MCP 接入，避免重复入口。
实际宿主发现与模型采用应单独验证；本文件不表示 EDA、SSH 或板卡已通过。
完整方法由 `vivado_guide` 返回，或从源根 `docs/SOURCE_CONNECTION.md` 阅读。
"""
    if client == "antigravity":
        manifest = {key: manifest[key] for key in ("name", "description")}
    elif client == "workbuddy":
        manifest = {
            key: manifest[key] for key in ("name", "version", "description", "skills", "mcpServers")
        }
    manifest_path = {
        "codex": ".codex-plugin/plugin.json",
        "antigravity": "plugin.json",
        "workbuddy": ".codebuddy-plugin/plugin.json",
    }[client]
    mcp_path = "mcp_config.json" if client == "antigravity" else ".mcp.json"
    files = {
        manifest_path: json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        mcp_path: json.dumps(mcp, ensure_ascii=False, indent=2) + "\n",
        "skills/otter-vivado/SKILL.md": bootstrap,
        "README.md": readme,
    }
    output.mkdir(parents=True, exist_ok=False)
    for relative, content in files.items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")
    return {
        "client": client,
        "plugin_dir": str(output),
        "source": str(root),
        "python": str(python),
        "files": list(files),
        "installed_in_host": False,
    }
