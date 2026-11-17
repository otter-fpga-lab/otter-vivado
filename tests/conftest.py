"""测试 fixtures：mock Vivado 进程、会话管理器等。"""

import pytest

from vivado_mcp.vivado.session_manager import SessionManager


@pytest.fixture
def session_manager() -> SessionManager:
    """创建一个不连接实际 Vivado 的 SessionManager 实例。"""
    return SessionManager(vivado_path="/fake/vivado")


@pytest.fixture(autouse=True)
def _isolate_user_cache(tmp_path_factory, monkeypatch):
    """CW 快照等用户缓存写入临时目录，不污染真实 %LOCALAPPDATA%/XDG cache。"""
    monkeypatch.setenv("OTTER_VIVADO_CACHE_DIR", str(tmp_path_factory.mktemp("otter_cache")))
