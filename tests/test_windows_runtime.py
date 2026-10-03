"""仅 Windows 上跑真实 junction、ANSI 文件及 .bat/管道/TCP 启动。

Vivado 由明确的协议 stub 替代，这些测试不构成 EDA/GUI/厂商版本验证。
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from vivado_mcp import connect
from vivado_mcp.run_monitor import read_reports
from vivado_mcp.vivado.gui_session import GuiSession
from vivado_mcp.vivado.session import SubprocessSession

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows OS")


def test_real_junction_is_live_and_removal_preserves_source(tmp_path):
    source = tmp_path / "源码 space & [references]"
    source.mkdir()
    skill = source / "SKILL.md"
    skill.write_text("first", encoding="utf-8")
    target = tmp_path / "脚手架 space & [skills]"
    try:
        assert connect._create_link(target, source, "junction")
        assert connect._same_skill_link(target, source)
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "first"
        skill.write_text("changed once 中文", encoding="utf-8")
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "changed once 中文"
        assert not connect._create_link(target, source, "junction")
    finally:
        if target.exists():
            target.rmdir()
    assert skill.read_text(encoding="utf-8") == "changed once 中文"


def test_real_auto_fallback_uses_junction_without_copying(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "client" / "skill"
    source.mkdir()
    (source / "SKILL.md").write_text("same source", encoding="utf-8")

    def no_symlink_privilege(*args, **kwargs):
        exc = OSError("simulated missing symlink privilege")
        exc.winerror = 1314
        raise exc

    monkeypatch.setattr(Path, "symlink_to", no_symlink_privilege)
    try:
        assert connect._create_link(target, source, "auto")
        assert connect._same_skill_link(target, source)
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "same source"
    finally:
        if target.exists():
            target.rmdir()


def test_report_uses_native_windows_ansi_codepage(tmp_path):
    # 当前 runner 的系统代码页；中文 CP936 留给实际中文 Windows 验证。
    text = "Design : demo\nReport path : café\n"
    data = text.encode("mbcs")
    (tmp_path / "demo_timing_summary_routed.rpt").write_bytes(data)
    report, = read_reports({"directory": str(tmp_path), "top": "demo"})
    assert report["text"] == text


_FAKE_VIVADO = r'''
import json
import re
import socket
import sys
from pathlib import Path

mode = sys.argv[sys.argv.index("-mode") + 1]
if mode == "tcl":
    print("VMCP_TEST:Windows batch startup", flush=True)
    for line in sys.stdin:
        if line.strip() == "exit":
            break
        payload = re.search(r"binary format H\* ([0-9a-f]+)", line)
        if payload:
            command = bytes.fromhex(payload[1]).decode("utf-8")
            print("VMCP_TEST:" + command, flush=True)
        sentinel = re.search(r"<<<(VMCP_[0-9a-f]+)_RC=\$__rc>>>", line)
        if sentinel:
            print("<<<" + sentinel[1] + "_RC=0>>>", flush=True)
else:
    script = Path(sys.argv[sys.argv.index("-source") + 1]).read_text(encoding="utf-8")
    port = int(re.search(r"VMCP_PORT_PREF (\d+)", script)[1])
    def read_n(stream, size):
        data = b""
        while len(data) < size:
            block = stream.recv(size - len(data))
            if not block:
                raise EOFError()
            data += block
        return data
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen()
        stream, _ = listener.accept()
        with stream:
            while True:
                size = int.from_bytes(read_n(stream, 4), "big")
                command = read_n(stream, size).decode("utf-8")
                if command == "exit":
                    break
                output = command.removeprefix("puts ")
                payload = json.dumps({"rc": 0, "output": output}).encode("utf-8")
                stream.sendall(len(payload).to_bytes(4, "big") + payload)
'''


@pytest.fixture
def fake_windows_vivado(tmp_path, monkeypatch):
    root = tmp_path / "EDA tools 中文 & [release]"
    root.mkdir()
    script = root / "fake_vivado.py"
    script.write_text(_FAKE_VIVADO, encoding="utf-8")
    batch = root / "vivado.bat"
    batch.write_bytes(b'@echo off\r\n"%VMCP_TEST_PYTHON%" "%~dp0fake_vivado.py" %*\r\n')
    monkeypatch.setenv("VMCP_TEST_PYTHON", sys.executable)
    monkeypatch.chdir(root)
    return batch


@pytest.mark.asyncio
async def test_windows_batch_tcl_session_can_start_execute_and_stop(fake_windows_vivado):
    session = SubprocessSession(str(fake_windows_vivado), "windows-batch-tcl")
    try:
        banner = await session.start(timeout=15)
        assert "Windows batch startup" in banner
        result = await session.execute("puts {Windows transport}", timeout=5)
        assert not result.is_error
        assert "Windows transport" in result.output
    finally:
        await session.stop(timeout=5)
    assert not session.is_alive


@pytest.mark.asyncio
async def test_windows_batch_gui_session_can_start_execute_and_stop(
    fake_windows_vivado, monkeypatch,
):
    # 只测真实 batch spawn + 单条 TCP 通道，不模拟 Vivado 工程查询。
    monkeypatch.setattr(GuiSession, "_current_project_hint", AsyncMock(return_value=""))
    session = GuiSession(str(fake_windows_vivado), session_id="windows-batch-gui", port=0)
    try:
        await session.start(timeout=15)
        result = await session.execute("puts Windows GUI transport 中文", timeout=5)
        assert not result.is_error
        assert result.output == "Windows GUI transport 中文"
    finally:
        await session.stop(timeout=5)
    assert not session.is_alive
