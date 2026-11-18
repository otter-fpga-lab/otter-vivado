"""共用 ``Vivado_init.tcl`` 路径定位与 ``general.maxThreads`` 只读解析。

供 ``install.py``、``doctor.py`` 与 ``run_feedback.py`` 共用，避免路径发现或 Tcl 参数解析
逻辑分叉。本模块只读检查文件，不创建目录或修改任何文件。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from vivado_mcp.config import find_vivado

_MAX_INIT_FILE_BYTES = 1024 * 1024
_MAX_TCL_DEPTH = 16
_INT_RE = re.compile(r"[+-]?\d+")


def infer_install_init_tcl(vivado_path: str | None) -> Path | None:
    """从给定的 ``vivado_path`` 推出安装目录 ``scripts/Vivado_init.tcl``；无法推出返回 None。"""
    if not isinstance(vivado_path, str) or not vivado_path.strip():
        return None
    parent = Path(vivado_path.strip()).parent
    for _ in range(4):
        if parent.name.lower() == "bin" and parent.parent != parent:
            return parent.parent / "scripts" / "Vivado_init.tcl"
        if parent.parent == parent:
            break
        parent = parent.parent
    return None


def resolve_install_init_tcl(vivado_path: str | None = None) -> Path:
    """定位安装级 ``Vivado_init.tcl``（找不到 Vivado 可执行文件时按 ``find_vivado`` 抛错）。"""
    exe = Path(find_vivado(vivado_path)).resolve()
    inferred = infer_install_init_tcl(str(exe))
    if inferred is not None:
        return inferred
    return exe.parent.parent / "scripts" / "Vivado_init.tcl"


def resolve_user_init_tcl(home: Path | None = None) -> Path:
    """定位用户级 ``Vivado_init.tcl``（不创建目录或文件）。

    Windows 为 ``%APPDATA%\\Xilinx\\Vivado\\Vivado_init.tcl``（指定 ``home`` 时为
    ``<home>/AppData/Roaming/Xilinx/Vivado/Vivado_init.tcl``）；Linux 等为
    ``~/.Xilinx/Vivado/Vivado_init.tcl``。
    """
    if sys.platform == "win32":
        if home is not None:
            base = home / "AppData" / "Roaming"
        else:
            appdata = os.environ.get("APPDATA")
            base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Xilinx" / "Vivado" / "Vivado_init.tcl"
    user_home = home if home is not None else Path.home()
    return user_home / ".Xilinx" / "Vivado" / "Vivado_init.tcl"


def _is_line_continuation(text: str, i: int) -> int:
    """若 ``text[i:]`` 为 Tcl 反斜杠续行，返回应跳过的字符数（2 或 3），否则 0。"""
    if text[i] != "\\":
        return 0
    if i + 1 < len(text) and text[i + 1] == "\n":
        return 2
    if i + 2 < len(text) and text[i + 1 : i + 3] == "\r\n":
        return 3
    return 0


def _read_braced(text: str, i: int, line: int) -> tuple[str, int, int]:
    """读取 ``{...}``，返回 ``(inner, next_i, next_line)``；``text[i]`` 须为 ``{``。"""
    n = len(text)
    i += 1
    start = i
    depth = 1
    while i < n and depth > 0:
        ch = text[i]
        cont = _is_line_continuation(text, i)
        if cont:
            line += 1
            i += cont
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "\n":
            line += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1, line
        i += 1
    return text[start:n], n, line


def _read_quoted(text: str, i: int, line: int) -> tuple[str, int, int]:
    """读取 ``"..."``，返回 ``(inner, next_i, next_line)``；``text[i]`` 须为 ``"``。"""
    n = len(text)
    i += 1
    start = i
    while i < n:
        ch = text[i]
        cont = _is_line_continuation(text, i)
        if cont:
            line += 1
            i += cont
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "\n":
            line += 1
        elif ch == '"':
            return text[start:i], i + 1, line
        i += 1
    return text[start:n], n, line


def _read_bracket(text: str, i: int, line: int) -> tuple[str, int, int]:
    """读取 ``[...]``，返回 ``(raw_with_brackets, next_i, next_line)``；``text[i]`` 须为 ``[``。"""
    n = len(text)
    start = i
    i += 1
    depth = 1
    while i < n and depth > 0:
        ch = text[i]
        cont = _is_line_continuation(text, i)
        if cont:
            line += 1
            i += cont
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "{":
            _, i, line = _read_braced(text, i, line)
            continue
        if ch == '"':
            _, i, line = _read_quoted(text, i, line)
            continue
        if ch == "\n":
            line += 1
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1], i + 1, line
        i += 1
    return text[start:n], n, line


def _read_bare(text: str, i: int, line: int) -> tuple[str, int, int]:
    """读取裸词，遇到空白、``;`` 或 ``\\n`` 结束（保留嵌套 ``[...]``）。"""
    n = len(text)
    start = i
    while i < n:
        ch = text[i]
        if ch in (" ", "\t", "\r", "\n", ";"):
            break
        if _is_line_continuation(text, i):
            break
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "[":
            _, i, line = _read_bracket(text, i, line)
            continue
        i += 1
    return text[start:i], i, line


def _extract_set_param_threads(
    words: list[tuple[str, str, int]],
    cmd_line: int,
) -> dict[str, Any] | None:
    """从一条 Tcl 命令词列表提取 ``set_param ... general.maxThreads ...``。"""
    if not words or words[0][1].strip() != "set_param":
        return None
    args = [value for _, value, _ in words[1:]]
    param_name: str | None = None
    param_val: str | None = None
    positional: list[str] = []

    idx = 0
    while idx < len(args):
        tok = args[idx].strip()
        if tok == "-name" and idx + 1 < len(args):
            param_name = args[idx + 1].strip()
            idx += 2
            continue
        if tok == "-value" and idx + 1 < len(args):
            param_val = args[idx + 1].strip()
            idx += 2
            continue
        if tok in ("-quiet", "-verbose"):
            idx += 1
            continue
        positional.append(tok)
        idx += 1

    if param_name is None and param_val is None and len(positional) >= 2:
        param_name, param_val = positional[0], positional[1]

    if param_name != "general.maxThreads" or param_val is None:
        return None
    int_val = int(param_val) if _INT_RE.fullmatch(param_val) else None
    return {"line": cmd_line, "raw": param_val, "value": int_val}


def _scan_tcl_commands(
    text: str,
    start_line: int = 1,
    *,
    depth: int = 0,
    out: list[dict[str, Any]],
) -> None:
    """按 Tcl 命令边界拆分文本，跳过注释并递归扫描 ``{...}`` 与 ``[...]`` 子句。"""
    if depth > _MAX_TCL_DEPTH:
        return
    n = len(text)
    i = 0
    line = start_line

    while i < n:
        # 跳过命令之间的空白、空分隔符与续行
        while i < n:
            cont = _is_line_continuation(text, i)
            if cont:
                i += cont
                line += 1
            elif text[i] in (" ", "\t", "\r", "\n", ";"):
                if text[i] == "\n":
                    line += 1
                i += 1
            else:
                break
        if i >= n:
            break

        # Tcl 注释仅在命令起始处有效；行尾奇数个反斜杠会延续注释到下一物理行
        if text[i] == "#":
            while i < n:
                cont = _is_line_continuation(text, i)
                if cont:
                    i += cont
                    line += 1
                    continue
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == "\n":
                    i += 1
                    line += 1
                    break
                i += 1
            continue

        cmd_line = line
        words: list[tuple[str, str, int]] = []

        while i < n:
            # 词间水平空白与反斜杠换行续行
            while i < n:
                ch = text[i]
                cont = _is_line_continuation(text, i)
                if cont:
                    i += cont
                    line += 1
                elif ch in (" ", "\t", "\r"):
                    i += 1
                else:
                    break
            if i >= n:
                break
            ch = text[i]
            if ch == ";":
                i += 1
                break
            if ch == "\n":
                line += 1
                i += 1
                break

            word_line = line
            if ch == "{":
                inner, i, line = _read_braced(text, i, line)
                words.append(("brace", inner, word_line))
            elif ch == '"':
                inner, i, line = _read_quoted(text, i, line)
                words.append(("quote", inner, word_line))
            elif ch == "[":
                raw_br, i, line = _read_bracket(text, i, line)
                words.append(("bracket", raw_br, word_line))
            else:
                bare, i, line = _read_bare(text, i, line)
                words.append(("bare", bare, word_line))

        matched = _extract_set_param_threads(words, cmd_line)
        if matched is not None:
            out.append(matched)

        for kind, val, wline in words:
            if kind == "brace":
                _scan_tcl_commands(val, wline, depth=depth + 1, out=out)
            elif kind == "bracket" and len(val) >= 2:
                _scan_tcl_commands(val[1:-1], wline, depth=depth + 1, out=out)


def parse_general_max_threads(content: str) -> list[dict[str, Any]]:
    """提取 Tcl 文本中非注释的 ``set_param general.maxThreads`` 设置列表。

    支持：
    - 位置参数与 ``-name ... -value ...``（含乱序、``-quiet``）
    - 花括号/引号写法（如 ``{general.maxThreads}``、``"general.maxThreads"``、``{ 32 }``）
    - 分号分隔多命令、``catch { ... }`` / ``if { ... } { ... }`` 嵌套块
    - 忽略整行 ``#`` 与 ``;#`` 注释（含行尾 ``\\`` 续行注释）
    """
    matches: list[dict[str, Any]] = []
    _scan_tcl_commands(content or "", 1, depth=0, out=matches)
    return matches


def inspect_init_tcl_file(path: Path) -> dict[str, Any]:
    """只读检查单个 ``Vivado_init.tcl`` 文件中的 ``general.maxThreads`` 设置。"""
    try:
        exists = path.is_file()
    except OSError as exc:
        return {
            "path": path,
            "exists": False,
            "matches": [],
            "max_threads": None,
            "raw_value": None,
            "error": str(exc),
        }
    if not exists:
        return {
            "path": path,
            "exists": False,
            "matches": [],
            "max_threads": None,
            "raw_value": None,
            "error": None,
        }
    try:
        with path.open("rb") as handle:
            content = handle.read(_MAX_INIT_FILE_BYTES).decode("utf-8", errors="replace")
    except OSError as exc:
        return {
            "path": path,
            "exists": True,
            "matches": [],
            "max_threads": None,
            "raw_value": None,
            "error": str(exc),
        }
    matches = parse_general_max_threads(content)
    last = matches[-1] if matches else None
    return {
        "path": path,
        "exists": True,
        "matches": matches,
        "max_threads": last["value"] if last is not None else None,
        "raw_value": last["raw"] if last is not None else None,
        "error": None,
    }


def inspect_user_init_tcl(home: Path | None = None) -> dict[str, Any]:
    """只读检查用户级 ``Vivado_init.tcl`` 及其中 ``general.maxThreads`` 设置。"""
    return inspect_init_tcl_file(resolve_user_init_tcl(home=home))
