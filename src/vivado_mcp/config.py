"""Vivado 路径检测与全局配置。

检测优先级：
1. 显式路径 / VIVADO_PATH 环境变量（无效即报错，不偷偷切换版本）
2. 系统 PATH 中的 vivado / vivado.bat
3. 平台相关的默认安装路径（所有默认目录一起比较版本）
"""

import glob
import os
import re
import shutil
import sys


def _default_install_globs() -> list[str]:
    """根据当前平台返回 Vivado 默认安装搜索路径。"""
    if sys.platform == "win32":
        return [
            "D:/Xilinx/Vivado/*/bin/vivado.bat",
            "C:/Xilinx/Vivado/*/bin/vivado.bat",
        ]
    else:
        # Linux / macOS
        return [
            "/tools/Xilinx/Vivado/*/bin/vivado",
            "/opt/Xilinx/Vivado/*/bin/vivado",
            "/opt/xilinx/Vivado/*/bin/vivado",
            os.path.expanduser("~/Xilinx/Vivado/*/bin/vivado"),
        ]


def normalize_path(path: str) -> str:
    """将 Windows 反斜杠路径转换为正斜杠（Tcl 兼容）。"""
    return path.replace("\\", "/")


def _selected_path(path: str, source: str) -> str:
    """明确的版本选择不能因拼写错误而悄悄退回其他安装。"""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise FileNotFoundError(
            f"{source} 指定的 Vivado 文件不存在：{path}。"
            "不会自动改用其他版本；请修正路径，或清除该选择后重新检测。"
        )
    return normalize_path(os.path.abspath(expanded))


def _default_candidates() -> list[str]:
    """全局排序默认安装，避免旧版 D 盘安装压过新版 C 盘安装。"""
    paths = {
        normalize_path(os.path.abspath(path))
        for pattern in _default_install_globs()
        for path in glob.glob(pattern)
        if os.path.isfile(path)
    }

    def order(path):
        version = get_vivado_version(path)
        numbers = tuple(map(int, version.split("."))) if version != "unknown" else ()
        return numbers, path

    return sorted(paths, key=order, reverse=True)


def find_vivado(vivado_path: str | None = None) -> str:
    """查找 Vivado 可执行文件路径。

    Args:
        vivado_path: 显式指定的路径，优先级最高。

    Returns:
        Vivado 可执行文件的完整路径（正斜杠格式）。

    Raises:
        FileNotFoundError: 未找到任何 Vivado 安装。
    """
    # 1. 显式传入
    if vivado_path:
        return _selected_path(vivado_path, "显式路径")

    # 2. 环境变量 VIVADO_PATH
    env_path = os.environ.get("VIVADO_PATH")
    if env_path:
        return _selected_path(env_path, "VIVADO_PATH")

    # 3. 系统 PATH
    which = shutil.which("vivado") or shutil.which("vivado.bat")
    if which:
        return normalize_path(which)

    # 4. 平台相关的默认安装目录（取版本号最大的）
    matches = _default_candidates()
    if matches:
        return matches[0]

    raise FileNotFoundError(
        "未找到 Vivado 安装。请设置 VIVADO_PATH 环境变量，"
        "或确保 vivado 可执行文件在系统 PATH 中。"
    )


def get_vivado_version(vivado_path: str) -> str:
    """从路径推断版本号，不执行 Vivado，也不构成实际版本验证。"""
    parts = normalize_path(vivado_path).split("/")
    for i, part in enumerate(parts):
        if part.lower() == "vivado" and i + 1 < len(parts):
            candidate = parts[i + 1]
            if re.fullmatch(r"20\d{2}\.\d+(?:\.\d+)?", candidate):
                return candidate
    return "unknown"


def list_vivado_installations() -> dict:
    """只读列出已配置和常见安装路径；不启动 EDA，不扫描全盘。"""
    found: dict[str, dict] = {}

    def add(path: str | None, source: str) -> None:
        if not path:
            return
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            return
        path = normalize_path(os.path.abspath(path))
        key = os.path.normcase(path)
        item = found.setdefault(key, {
            "path": path, "version_from_path": get_vivado_version(path), "sources": [],
        })
        if source not in item["sources"]:
            item["sources"].append(source)

    add(os.environ.get("VIVADO_PATH"), "VIVADO_PATH")
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder:
            add(shutil.which("vivado", path=folder), "PATH")
            add(shutil.which("vivado.bat", path=folder), "PATH")
    for path in _default_candidates():
        add(path, "default_directory")
    try:
        selected, error = find_vivado(), ""
    except FileNotFoundError as exc:
        selected, error = None, str(exc)
    return {
        "selected_path": selected,
        "selection_error": error,
        "installations": list(found.values()),
        "version_evidence": "installation_path_only",
        "note": "仅检查本地文件和路径版本；未启动 Vivado，未核对已有 GUI 或许可证。",
    }
