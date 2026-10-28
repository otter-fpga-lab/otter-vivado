"""Strip IDE/Electron variables before spawning Vivado.

Cursor、VS Code、CodeBuddy 等宿主会给 MCP 进程注入 ``ELECTRON_RUN_AS_NODE=1``
和 ``VSCODE_IPC_HOOK``。Vivado GUI 若原样继承，Tools → Custom Editor 再去
启动 Code.exe / Cursor.exe 时会按 Node 子进程起来，窗口出不来。
attach 用户自己开的 GUI 不受影响；spawn 路径必须显式传净化后的 env。
"""

from __future__ import annotations

import os
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


def sanitize_vivado_child_env(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy ``source`` (default ``os.environ``) without IDE/Electron leaks.

    Does not mutate the input mapping. Callers must pass the returned dict as
    ``env=`` to subprocess; omitting ``env`` would still inherit the dirty
    parent environment.
    """
    env = dict(os.environ if source is None else source)
    for key in list(env):
        if _is_ide_leak(key):
            del env[key]
    return env
