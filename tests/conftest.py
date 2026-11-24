"""测试 fixtures：mock Vivado 进程、会话管理器等。"""

import socket

import pytest

from vivado_mcp.vivado.session_manager import SessionManager


def _pick_unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def session_manager(tmp_path, monkeypatch) -> SessionManager:
    """创建一个不连接实际 Vivado 的 SessionManager 实例。

    默认 Vivado 运行目录指向临时目录，避免测试在真实 %APPDATA% 下创建目录。
    """
    monkeypatch.setattr(
        "vivado_mcp.vivado.session_manager.default_vivado_cwd",
        lambda: tmp_path / "vivado-default-cwd",
    )
    return SessionManager(vivado_path="/fake/vivado")


@pytest.fixture(autouse=True)
def _isolate_user_cache(tmp_path_factory, monkeypatch):
    """CW 快照等用户缓存写入临时目录，不污染真实 %LOCALAPPDATA%/XDG cache。"""
    monkeypatch.setenv("OTTER_VIVADO_CACHE_DIR", str(tmp_path_factory.mktemp("otter_cache")))
    monkeypatch.setenv("OTTER_VIVADO_CONSOLE_PORT", str(_pick_unused_loopback_port()))
