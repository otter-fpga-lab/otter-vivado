"""config.py 单元测试。

测试路径检测逻辑（使用环境变量 mock，不依赖实际 Vivado 安装）。
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

from vivado_mcp.config import (
    _default_install_globs,
    find_vivado,
    get_vivado_version,
    list_vivado_installations,
    normalize_path,
)


class TestNormalizePath:
    """路径标准化测试。"""

    def test_backslash_to_forward(self):
        assert normalize_path("C:\\Users\\test") == "C:/Users/test"

    def test_forward_slash_unchanged(self):
        assert normalize_path("/opt/vivado") == "/opt/vivado"

    def test_empty_string(self):
        assert normalize_path("") == ""


class TestDefaultInstallGlobs:
    """跨平台默认安装路径测试。"""

    def test_windows_globs(self):
        """Windows 平台返回 .bat 路径。"""
        with patch.object(sys, "platform", "win32"):
            globs = _default_install_globs()
            assert any("vivado.bat" in g for g in globs)
            assert any("D:/" in g for g in globs)

    def test_linux_globs(self):
        """Linux 平台返回无后缀路径。"""
        with patch.object(sys, "platform", "linux"):
            globs = _default_install_globs()
            assert any("/tools/Xilinx" in g for g in globs)
            assert any("/opt/Xilinx" in g for g in globs)
            assert all(".bat" not in g for g in globs)


class TestFindVivado:
    """Vivado 路径查找测试。"""

    def test_explicit_path(self, tmp_path):
        """显式传入的路径优先使用。"""
        fake = tmp_path / "vivado.bat"
        fake.write_text("fake")
        result = find_vivado(str(fake))
        assert "vivado.bat" in result

    def test_env_var(self, tmp_path):
        """VIVADO_PATH 环境变量被识别。"""
        fake = tmp_path / "vivado"
        fake.write_text("fake")
        with patch.dict(os.environ, {"VIVADO_PATH": str(fake)}):
            result = find_vivado()
            assert "vivado" in result

    def test_not_found_raises(self):
        """全部搜索失败时抛出 FileNotFoundError。"""
        with patch.dict(os.environ, {}, clear=True):
            with patch("vivado_mcp.config.shutil.which", return_value=None):
                with patch("vivado_mcp.config.glob.glob", return_value=[]):
                    with pytest.raises(FileNotFoundError, match="未找到"):
                        find_vivado()


class TestGetVivadoVersion:
    """版本号提取测试。"""

    def test_extract_version(self):
        assert get_vivado_version("D:/Xilinx/Vivado/2019.1/bin/vivado.bat") == "2019.1"

    def test_linux_path(self):
        assert get_vivado_version("/opt/Xilinx/Vivado/2024.1/bin/vivado") == "2024.1"

    def test_unknown_format(self):
        assert get_vivado_version("/usr/local/bin/vivado") == "unknown"

    @pytest.mark.parametrize("version", ["2018.3", "2020.2", "2022.2", "2024.2"])
    def test_windows_native_paths_on_any_host(self, version):
        assert get_vivado_version(f"D:\\Xilinx\\Vivado\\{version}\\bin\\vivado.bat") == version


@pytest.mark.parametrize("choice", ["explicit", "environment"])
def test_bad_explicit_selection_never_falls_back_to_another_install(tmp_path, monkeypatch, choice):
    fallback = tmp_path / "vivado.bat"
    fallback.touch()
    monkeypatch.setattr("vivado_mcp.config.shutil.which", lambda *a, **kw: str(fallback))
    missing = str(tmp_path / "Vivado" / "2018.3" / "bin" / "vivado.bat")
    monkeypatch.setenv("VIVADO_PATH", missing if choice == "environment" else str(fallback))
    with pytest.raises(FileNotFoundError, match="不会自动改用其他版本"):
        find_vivado(missing if choice == "explicit" else None)


def test_default_selection_compares_all_directories_and_numeric_versions(tmp_path, monkeypatch):
    from vivado_mcp import config

    monkeypatch.delenv("VIVADO_PATH", raising=False)
    monkeypatch.setattr(config.shutil, "which", lambda *a, **kw: None)
    roots = [tmp_path / "D" / "Vivado", tmp_path / "C" / "Vivado"]
    paths = []
    for root, version in [(roots[0], "2018.3"), (roots[1], "2024.2"), (roots[1], "2024.10")]:
        exe = root / version / "bin" / "vivado.bat"
        exe.parent.mkdir(parents=True)
        exe.touch()
        paths.append(exe)
    monkeypatch.setattr(config, "_default_install_globs", lambda: [
        str(root / "*" / "bin" / "vivado.bat") for root in roots
    ])
    assert find_vivado() == paths[-1].as_posix()


def test_versions_lists_evidence_without_starting_tools_and_retains_selection_error(
    tmp_path, monkeypatch, capsys,
):
    from vivado_mcp import config
    from vivado_mcp.__main__ import main

    exe = tmp_path / "Vivado" / "2024.2" / "bin" / "vivado.bat"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setenv("VIVADO_PATH", str(tmp_path / "missing-2018.3"))
    monkeypatch.setenv("PATH", str(exe.parent))
    monkeypatch.setattr(config.shutil, "which", lambda *a, **kw: str(exe))
    monkeypatch.setattr(config, "_default_install_globs", lambda: [str(exe)])
    report = list_vivado_installations()
    assert report["selected_path"] is None
    assert "VIVADO_PATH" in report["selection_error"]
    item, = report["installations"]
    assert item["version_from_path"] == "2024.2"
    assert item["sources"] == ["PATH", "default_directory"]
    assert report["version_evidence"] == "installation_path_only"
    monkeypatch.setattr(sys, "argv", ["vivado-mcp", "versions", "--json"])
    main()
    assert json.loads(capsys.readouterr().out) == report


def test_versions_expands_home_environment_path_before_listing(tmp_path, monkeypatch):
    from vivado_mcp import config

    exe = tmp_path / "Vivado/2024.2/bin/vivado.bat"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setattr(
        config.os.path, "expanduser",
        lambda path: str(tmp_path / path[2:]) if path.startswith("~/") else path,
    )
    monkeypatch.setenv("VIVADO_PATH", "~/Vivado/2024.2/bin/vivado.bat")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(config, "_default_install_globs", lambda: [])
    report = list_vivado_installations()
    item, = report["installations"]
    assert item["path"] == report["selected_path"] == exe.as_posix()
    assert item["sources"] == ["VIVADO_PATH"]
