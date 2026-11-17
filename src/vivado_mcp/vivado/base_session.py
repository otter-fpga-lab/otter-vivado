"""会话抽象基类。

两种实现共享同一接口：
- ``SubprocessSession`` — 启动 ``vivado -mode tcl`` 无头子进程（CI / 批处理友好）
- ``GuiSession`` — Popen ``vivado -mode gui`` 或 attach 已有 GUI，通过 TCP 通信

工具层代码通过 BaseSession 接口操作会话，不感知具体实现。
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from enum import Enum

from vivado_mcp.vivado.tcl_utils import TclResult

logger = logging.getLogger(__name__)

# 启动后一次性核对实际版本与工作目录；每项单独 catch，缺一项不影响另一项。
_RUNTIME_INFO_QUERY = (
    'if {[catch {version -short} __vmcp_rt_v]} { set __vmcp_rt_v \"\" }\n'
    'puts "VMCP_RT_VERSION:$__vmcp_rt_v"\n'
    'if {[catch {pwd} __vmcp_rt_d]} { set __vmcp_rt_d \"\" }\n'
    'puts "VMCP_RT_PWD:$__vmcp_rt_d"'
)
VERSION_RE = re.compile(r"20\d{2}\.\d+(?:\.\d+)?")


class SessionState(str, Enum):
    """会话状态枚举。"""
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    DEAD = "dead"  # D4: socket 断开 / 进程崩溃 / 用户关闭 GUI


class BaseSession(ABC):
    """Vivado 会话抽象基类。

    子类必须实现 ``start``、``execute``、``stop``、``is_alive``。
    """

    def __init__(
        self, vivado_path: str, session_id: str = "default", cwd: str | None = None,
    ):
        self.vivado_path = vivado_path
        self.session_id = session_id
        # spawn 子进程时使用的工作目录（.Xil/ 等落点）；None = 继承宿主（attach 不 spawn）。
        self.cwd = cwd
        self._state: SessionState = SessionState.STOPPED
        self._start_time: float | None = None
        # 由实际进程输出核对的版本 / pwd；未核对时保持 None，不用路径推断冒充。
        self._runtime_version: str | None = None
        self._runtime_pwd: str | None = None

    @property
    def runtime_version(self) -> str | None:
        return self._runtime_version

    @property
    def runtime_pwd(self) -> str | None:
        return self._runtime_pwd

    async def probe_runtime_info(self, timeout: float = 10.0) -> None:
        """启动后一次核对 ``version -short`` 与 ``pwd``；失败只降级为未知。

        走正常 ``execute``（锁 + 在途响应所有权），超时也不会让迟到响应串到后续命令。
        """
        try:
            result = await self.execute(_RUNTIME_INFO_QUERY, timeout=timeout)
        except Exception as exc:
            logger.warning("会话 '%s' 运行版本/工作目录核对失败: %s", self.session_id, exc)
            return
        if result.is_error:
            logger.warning(
                "会话 '%s' 运行版本/工作目录核对返回错误: %s",
                self.session_id, result.output[:200],
            )
            return
        for line in result.output.splitlines():
            text = line.strip()
            if text.startswith("VMCP_RT_VERSION:"):
                version = text[len("VMCP_RT_VERSION:"):].strip()
                if VERSION_RE.fullmatch(version):
                    self._runtime_version = version
            elif text.startswith("VMCP_RT_PWD:"):
                pwd = text[len("VMCP_RT_PWD:"):].strip()
                if pwd:
                    self._runtime_pwd = pwd

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    @property
    @abstractmethod
    def is_alive(self) -> bool:
        """会话是否仍可响应命令。"""

    @property
    @abstractmethod
    def mode(self) -> str:
        """会话类型标识：``"tcl"`` / ``"gui"`` / ``"attach"``。"""

    @abstractmethod
    async def start(self, timeout: float = 120.0) -> str:
        """启动或连接 Vivado，返回启动信息。"""

    @abstractmethod
    async def execute(
        self,
        tcl_command: str,
        timeout: float = 120.0,
    ) -> TclResult:
        """执行一条 Tcl 命令并返回结果。"""

    @abstractmethod
    async def stop(self, timeout: float = 10.0) -> None:
        """关闭会话。"""

    def status_dict(self) -> dict:
        """返回会话状态信息字典。"""
        d = {
            "session_id": self.session_id,
            "mode": self.mode,
            "state": self._state.value,
            "vivado_path": self.vivado_path,
            "is_alive": self.is_alive,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "runtime_version": self._runtime_version or "unknown",
        }
        if self.cwd:
            d["cwd"] = self.cwd
        if self._runtime_pwd:
            d["runtime_pwd"] = self._runtime_pwd
        # 可选字段:GUI session 暴露端口和 external attach 标志,让 list_sessions
        # / 调试时一眼看出命令会落到哪个 Vivado 实例
        port = getattr(self, "connected_port", None)
        if port is not None:
            d["port"] = port
        if getattr(self, "attached_external", False):
            d["attached_external"] = True
        return d
