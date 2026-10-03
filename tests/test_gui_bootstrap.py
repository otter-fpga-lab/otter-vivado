"""真实 Tcl 读取 GUI 启动 wrapper；不需要 Vivado，也不启动 GUI。"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from vivado_mcp.vivado import gui_session


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("tclsh"), reason="需要 Tcl 解释器验证启动脚本")
async def test_gui_bootstrap_reads_utf8_source_with_literal_unicode_special_path(
    tmp_path, monkeypatch,
):
    source = tmp_path / "工具 space $literal [not_a_command]" / "server.tcl"
    source.parent.mkdir()
    source.write_text('puts "VMCP_TEST:中文 ${::VMCP_PORT_PREF}"\n', encoding="utf-8")
    monkeypatch.setattr(gui_session, "_locate_server_script", lambda: source)
    captured = {}

    async def capture_launch(*args, **kwargs):
        wrapper = Path(args[args.index("-source") + 1])
        captured["script"] = wrapper.read_bytes()
        # 不真正启动 EDA；此异常仅在源码已生成后阻断 spawn。
        raise OSError("test captured launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)
    session = gui_session.GuiSession("unused-vivado", port=0)
    try:
        with pytest.raises(RuntimeError, match="test captured launch"):
            await session.start()
        wrapper = captured["script"].decode("ascii")
        # tclsh 按平台默认编码读取 wrapper；被 source 的文件固定使用 UTF-8。
        driver = (
            'fconfigure stdout -encoding utf-8\n'
            + wrapper
        )
        result = subprocess.run(
            [shutil.which("tclsh")], input=driver.encode("ascii"), capture_output=True,
            check=True,
        )
        assert result.stderr == b""
        assert result.stdout.decode("utf-8").strip() == (
            f"VMCP_TEST:中文 {session._allocated_port}"
        )
    finally:
        await session.stop()
