"""Strip IDE/Electron variables before spawning Vivado.

Cursor、VS Code、CodeBuddy 等宿主会给 MCP 进程注入 ``ELECTRON_RUN_AS_NODE=1``
和 ``VSCODE_IPC_HOOK``。Vivado GUI 若原样继承，Tools → Custom Editor 再去
启动 Code.exe / Cursor.exe 时会按 Node 子进程起来，窗口出不来。
attach 用户自己开的 GUI 不受影响；spawn 路径必须显式传净化后的 env。

部分宿主（实测 Codex 沙箱化 MCP）反而给 MCP 进程**极简环境**：子进程连
``SystemRoot``/``PATH``/``COMSPEC`` 都没有。``create_subprocess_exec`` 直接跑
``vivado.bat`` 时，cmd 链初始化即静默退出（stdout 立刻 EOF），tcl 模式表现为
"Vivado 进程意外退出"。因此这里以注册表登录环境（HKLM Session Manager +
HKCU）为底座、静态系统目录变量兜底，补齐缺失的基本变量。
"""

from __future__ import annotations

import ctypes
import os
import re
from collections.abc import Mapping

_IDE_ENV_PREFIXES = (
    "ELECTRON_",
    "VSCODE_",
    "CURSOR_",
    "CODEBUDDY_",
)


def _is_ide_leak(key: str) -> bool:
    upper = key.upper()
    return any(upper.startswith(prefix) for prefix in _IDE_ENV_PREFIXES)


def _windows_directories() -> tuple[str, str]:
    """返回 (Windows 目录, System32 目录)，失败时回退标准路径。"""
    buf = ctypes.create_unicode_buffer(260)
    try:
        ok = ctypes.windll.kernel32.GetWindowsDirectoryW(buf, 260)
        windows_dir = buf.value if ok else r"C:\Windows"
        ok = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)
        system_dir = buf.value if ok else rf"{windows_dir}\System32"
    except AttributeError:  # 非 Windows（测试 / 文档构建）
        windows_dir, system_dir = r"C:\Windows", r"C:\Windows\System32"
    return windows_dir, system_dir


def _registry_login_env() -> dict[str, str]:
    """读取 Windows 登录默认环境（HKLM Session Manager + HKCU）。

    极简宿主 env 连 ``SystemRoot`` 都没有时，静态兜底的几个变量不足以让
    vivado.bat 链跑通（实测 loader 阶段 rc=1 静默退出）；注册表才是登录
    进程拿到的真实环境。REG_EXPAND_SZ 针对合并后的 env 展开，空值跳过。
    """
    if os.name != "nt":
        return {}
    import winreg

    env: dict[str, str] = {}
    for root, sub in (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ):
        try:
            with winreg.OpenKey(root, sub) as key:
                index = 0
                while True:
                    try:
                        name, value, vtype = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if vtype == winreg.REG_EXPAND_SZ and isinstance(value, str):
                        # 针对已合并的 env 展开，两轮足够（无嵌套引用的实际场景）
                        for _ in range(2):
                            value = re.sub(
                                r"%([^%]+)%",
                                lambda m: env.get(m.group(1), m.group(0)),
                                value,
                            )
                    if isinstance(value, str) and value:
                        if name.upper() == "PATH" and env.get(name):
                            # Windows 语义：用户 PATH 拼在系统 PATH 后，不是覆盖
                            env[name] = f"{env[name]};{value}"
                            continue
                        env[name] = value
        except OSError:
            continue
    return env


def _essential_fallbacks() -> dict[str, str]:
    """构造缺失的 Windows 基本变量默认值。"""
    windows_dir, system_dir = _windows_directories()
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if not temp:
        local = os.environ.get("LOCALAPPDATA")
        temp = rf"{local}\Temp" if local else rf"{windows_dir}\Temp"
    return {
        "SystemRoot": windows_dir,
        "windir": windows_dir,
        "COMSPEC": rf"{system_dir}\cmd.exe",
        # 覆盖 AutoRun(chcp) 与 bat 链内部对 system32 的基本需求
        "PATH": rf"{system_dir};{windows_dir};{windows_dir}\System32\Wbem",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC",
        "TEMP": temp,
        "TMP": temp,
    }


def _ensure_windows_essentials(env: dict[str, str]) -> None:
    """为缺失的 Windows 基本变量填默认值（已有值一律不覆盖）。

    Windows 环境变量名大小写不敏感，按大写去重后判断缺失。
    """
    upper_keys = {key.upper() for key in env}
    if "SYSTEMROOT" not in upper_keys:
        # 极简宿主：以注册表登录环境为底座，静态兜底只补仍缺的
        for key, value in _registry_login_env().items():
            if key.upper() not in upper_keys and key not in env:
                env[key] = value
                upper_keys.add(key.upper())
    for key, value in _essential_fallbacks().items():
        if key.upper() not in upper_keys:
            env[key] = value


def sanitize_vivado_child_env(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy ``source`` (default ``os.environ``) without IDE/Electron leaks,
    then fill missing Windows essentials.

    Does not mutate the input mapping. Callers must pass the returned dict as
    ``env=`` to subprocess; omitting ``env`` would still inherit the dirty
    parent environment.
    """
    env = dict(os.environ if source is None else source)
    for key in list(env):
        if _is_ide_leak(key):
            del env[key]
    _ensure_windows_essentials(env)
    return env
