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


def test_resolve_user_init_tcl_differs_from_install_init_tcl(tmp_path, monkeypatch):
    exe = tmp_path / "Xilinx" / "Vivado" / "2024.2" / "bin" / "vivado.bat"
    exe.parent.mkdir(parents=True)
    exe.write_text("fake", encoding="utf-8")
    install_init = installer._resolve_init_tcl(str(exe))
    assert install_init == exe.parent.parent / "scripts" / "Vivado_init.tcl"

    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert installer._resolve_user_init_tcl() == (
        tmp_path / "Roaming" / "Xilinx" / "Vivado" / "Vivado_init.tcl"
    )
    monkeypatch.setattr(installer.sys, "platform", "linux")
    assert installer._resolve_user_init_tcl(home=tmp_path / "linux_home") == (
        tmp_path / "linux_home" / ".Xilinx" / "Vivado" / "Vivado_init.tcl"
    )


def test_parse_general_max_threads_handles_tcl_variants():
    content = (
        "# set_param general.maxThreads 64\n"
        "puts hello ;# set_param general.maxThreads 99\n"
        "set_param general.maxThreads {16} ;# comment\n"
        "catch {set_param general.maxThreads 32}\n"
        'set_param general.maxThreads "8"\n'
        "set_param general.maxThreads $::env(THREADS)\n"
        'set_param {general.maxThreads} { 24 }; puts "a;b"; '
        'set_param "general.maxThreads" "12"\n'
        'set_param -name general.maxThreads -value { 32 }\n'
        'set_param -quiet -value "6" -name "general.maxThreads"\n'
        'puts "set_param general.maxThreads 77"\n'
    )
    parsed = installer.parse_general_max_threads(content)
    assert parsed == [
        {"line": 3, "raw": "16", "value": 16},
        {"line": 4, "raw": "32", "value": 32},
        {"line": 5, "raw": "8", "value": 8},
        {"line": 6, "raw": "$::env(THREADS)", "value": None},
        {"line": 7, "raw": "24", "value": 24},
        {"line": 7, "raw": "12", "value": 12},
        {"line": 8, "raw": "32", "value": 32},
        {"line": 9, "raw": "6", "value": 6},
    ]


def test_install_warns_without_modifying_high_max_threads_user_init(
    tmp_path, monkeypatch, capsys,
):
    install_init = tmp_path / "install" / "scripts" / "Vivado_init.tcl"
    script = tmp_path / "server.tcl"
    script.write_text("# server", encoding="utf-8")
    roaming = tmp_path / "Roaming"
    user_init = roaming / "Xilinx" / "Vivado" / "Vivado_init.tcl"
    user_init.parent.mkdir(parents=True)
    user_original = "set_param general.maxThreads 32"
    user_init.write_text(user_original, encoding="utf-8")

    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setattr(installer, "_resolve_init_tcl", lambda path: install_init)
    monkeypatch.setattr(installer, "_locate_server_script", lambda: script)

    installer.install(port=9999)
    out = capsys.readouterr().out
    assert "general.maxThreads 32" in out
    assert "install 不会自动修改用户级 Vivado_init.tcl" in out
    assert user_init.read_text(encoding="utf-8") == user_original
    assert not user_init.with_suffix(".tcl.vmcp_backup").exists()
