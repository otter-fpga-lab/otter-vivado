"""安装/卸载只编辑注入块，保留用户 init 的编码、BOM 和原有 CRLF 字节。"""

import shutil
import subprocess

import pytest

from vivado_mcp import install as installer


@pytest.mark.parametrize("original", [
    "# 中文用户配置\r\nset user_setting 1\r\n\r\n".encode("gbk"),
    "# 中文用户配置\r\nset user_setting 1\r\n\r\n".encode("utf-8-sig"),
    b"# ANSI caf\xe9 and ellipsis \x85\r\nset user_setting 1\r\n",
])
def test_install_uninstall_preserves_existing_bytes_and_later_user_changes(
    original, tmp_path, monkeypatch,
):
    init = tmp_path / "Vivado_init.tcl"
    script = tmp_path / "工具 $literal [name]" / "server.tcl"
    script.parent.mkdir()
    script.write_text("# source", encoding="utf-8")
    init.write_bytes(original)
    monkeypatch.setattr(installer, "_resolve_init_tcl", lambda path: init)
    monkeypatch.setattr(installer, "_locate_server_script", lambda: script)

    installer.install(port=9876)
    once = init.read_bytes()
    assert once.startswith(original)
    assert once[len(original):].decode("ascii") == installer._build_injection_block(script, 9876)
    assert init.with_suffix(".tcl.vmcp_backup").read_bytes() == original
    installer.install(port=9876)
    assert init.read_bytes() == once

    later = b"# user added after installation\r\nset another_setting 2\r\n"
    init.write_bytes(once + later)
    installer.uninstall()
    assert init.read_bytes() == original + later
    installer.uninstall()
    assert init.read_bytes() == original + later


@pytest.mark.skipif(not shutil.which("tclsh"), reason="需要 Tcl 解释器验证安装注入")
def test_install_injection_sources_utf8_file_through_literal_unicode_path(tmp_path):
    source = tmp_path / "工具 $literal [not_a_command]" / "server.tcl"
    source.parent.mkdir()
    source.write_text('puts "VMCP_TEST:中文 ${::VMCP_PORT_PREF}"\n', encoding="utf-8")
    block = installer._build_injection_block(source, 9876)
    driver = 'fconfigure stdout -encoding utf-8\n' + block
    result = subprocess.run(
        [shutil.which("tclsh")], input=driver.encode("ascii"), capture_output=True, check=True,
    )
    assert result.stderr == b""
    assert result.stdout.decode("utf-8").strip() == "VMCP_TEST:中文 9876"
