"""project_layout 与 get_project_layout：只解析目录约定，不创建目录。"""

from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vivado_mcp.project_layout import (
    LayoutError,
    default_output_dir,
    default_vivado_cwd,
    find_project_from_path,
    new_debug_subdir,
    project_cache_key,
    query_session_xpr,
    reserve_debug_subdir,
    resolve_layout,
    user_cache_root,
)
from vivado_mcp.vivado.tcl_utils import TclResult

NOW = datetime(2026, 11, 17, 9, 30, 5)


def _project(tmp_path, name="demo"):
    root = tmp_path / "work" / name
    root.mkdir(parents=True)
    xpr = root / f"{name}.xpr"
    xpr.write_text("<Project/>", encoding="utf-8")
    return root, xpr


def _session(output: str, *, is_error: bool = False):
    session = SimpleNamespace()
    session.execute = AsyncMock(return_value=TclResult(
        output=output, return_code=1 if is_error else 0, is_error=is_error,
    ))
    return session


def test_defaults_from_xpr_and_directory_do_not_create(tmp_path):
    root, xpr = _project(tmp_path)
    before = sorted(p.name for p in root.iterdir())
    for value in (str(xpr), str(root)):
        layout = resolve_layout(value)
        assert layout.project_root == root
        assert layout.xpr == xpr
        assert layout.debug_dir == root / "Debug"
        assert layout.notes_file == root / "Debug" / "NOTES.md"
        assert layout.scripts_dir == root / "Debug" / "scripts"
        assert layout.download_dir == root / "Download"
        assert layout.vivado_cwd == default_vivado_cwd()
        assert layout.config_path is None
        assert any(".work" in rule and ".vmcp" in rule for rule in layout.rules)
        assert any("TCL.PRE" in rule for rule in layout.rules)
    assert sorted(p.name for p in root.iterdir()) == before
    data = layout.to_dict()
    assert set(data) == {
        "project_root", "xpr", "debug_dir", "notes_file", "scripts_dir", "download_dir",
        "vivado_cwd", "trash_dir", "config_path", "rules",
    }


def test_directory_without_xpr_and_errors(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_layout(empty).xpr is None
    multi = tmp_path / "multi"
    multi.mkdir()
    (multi / "a.xpr").write_text("", encoding="utf-8")
    (multi / "b.xpr").write_text("", encoding="utf-8")
    with pytest.raises(LayoutError, match="多个 .xpr"):
        resolve_layout(multi)
    with pytest.raises(LayoutError, match="不存在"):
        resolve_layout(tmp_path / "missing.xpr")
    with pytest.raises(LayoutError, match="不存在"):
        resolve_layout(tmp_path / "missing_dir")
    with pytest.raises(LayoutError):
        resolve_layout("")


def test_trash_search_upward_never_creates(tmp_path):
    root, _ = _project(tmp_path)
    assert resolve_layout(root).trash_dir is None
    assert not any(p.name == "99_垃圾堆" for p in tmp_path.rglob("*"))
    trash = tmp_path / "99_垃圾堆"
    trash.mkdir()
    layout = resolve_layout(root)
    assert layout.trash_dir == trash
    assert any(str(trash) in rule or "99_垃圾堆" in rule for rule in layout.rules)


def test_config_overrides_relative_and_absolute(tmp_path):
    root, _ = _project(tmp_path)
    cwd = tmp_path / "vivado_run"
    (root / "otter-vivado.toml").write_text(
        "[layout]\n"
        'debug_dir = "过程"\n'
        'notes_file = "过程/记录.md"\n'
        'scripts_dir = "tools/agent"\n'
        'download_dir = "release"\n'
        f"vivado_cwd = '{cwd}'\n"
        'trash_dir = "../trash"\n',
        encoding="utf-8",
    )
    layout = resolve_layout(root)
    assert layout.config_path == root / "otter-vivado.toml"
    assert layout.debug_dir == root / "过程"
    assert layout.notes_file == root / "过程" / "记录.md"
    assert layout.scripts_dir == root / "tools" / "agent"
    assert layout.download_dir == root / "release"
    assert layout.vivado_cwd == cwd
    assert layout.trash_dir == root.parent / "trash"
    assert not (root / "过程").exists()


@pytest.mark.parametrize("text, message", [
    ("[layout\n", "不是合法 TOML"),
    ("[layout]\ndebug = 'x'\n", "未知键"),
    ("[layout]\ndebug_dir = 3\n", "非空路径字符串"),
    ("[layout]\ndebug_dir = ' '\n", "非空路径字符串"),
    ("[release]\nx = 'y'\n", "未知表"),
    ("layout = 'Debug'\n", "必须是 \\[layout\\] 表"),
])
def test_invalid_config_is_reported_not_ignored(tmp_path, text, message):
    root, _ = _project(tmp_path)
    (root / "otter-vivado.toml").write_text(text, encoding="utf-8")
    with pytest.raises(LayoutError, match=message):
        resolve_layout(root)


def test_default_vivado_cwd_matches_platform(monkeypatch, tmp_path):
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        assert default_vivado_cwd() == tmp_path / "Roaming" / "Xilinx" / "Vivado"
    else:
        assert default_vivado_cwd().parts[-2:] == (".Xilinx", "Vivado")


def test_new_debug_subdir_suffix_and_sanitize(tmp_path):
    root, _ = _project(tmp_path)
    layout = resolve_layout(root)
    first = new_debug_subdir(layout, "ila", now=NOW)
    second = new_debug_subdir(layout.to_dict(), "ila", now=NOW)
    assert first == root / "Debug" / "20261117_093005_ila"
    assert second == root / "Debug" / "20261117_093005_ila_2"
    assert first.is_dir() and second.is_dir()
    odd = new_debug_subdir(layout, "../a b/捕获", now=NOW)
    assert odd.parent == root / "Debug"
    assert odd.name == "20261117_093005_a_b_捕获"
    assert new_debug_subdir(layout, "", now=NOW).name.endswith("_debug")


def test_reserve_debug_subdir_creates_only_parent(tmp_path):
    root, _ = _project(tmp_path)
    layout = resolve_layout(root)
    path = reserve_debug_subdir(layout, "bundle", now=NOW)
    assert path == root / "Debug" / "20261117_093005_bundle"
    assert path.parent.is_dir() and not path.exists()
    path.mkdir()
    assert reserve_debug_subdir(layout, "bundle", now=NOW).name.endswith("_bundle_2")


def test_find_project_from_checkpoint(tmp_path):
    root, xpr = _project(tmp_path)
    dcp = root / "demo.runs" / "impl_1" / "top_routed.dcp"
    dcp.parent.mkdir(parents=True)
    dcp.write_bytes(b"dcp")
    assert find_project_from_path(dcp) == xpr
    loose = tmp_path / "loose" / "a" / "b" / "c" / "d" / "e" / "x.dcp"
    loose.parent.mkdir(parents=True)
    loose.write_bytes(b"dcp")
    assert find_project_from_path(loose) is None


def test_cache_root_and_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OTTER_VIVADO_CACHE_DIR", str(tmp_path / "cache"))
    assert user_cache_root() == tmp_path / "cache"
    monkeypatch.delenv("OTTER_VIVADO_CACHE_DIR")
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert user_cache_root() == tmp_path / "Local" / "otter-vivado"
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert user_cache_root() == tmp_path / "xdg" / "otter-vivado"
    key = project_cache_key(tmp_path / "My Proj")
    assert key.endswith("_My_Proj") and len(key.split("_", 1)[0]) == 12
    assert key == project_cache_key(str(tmp_path / "My Proj") + os.sep)


async def test_query_session_xpr_and_default_output_dir(tmp_path):
    root, xpr = _project(tmp_path)
    session = _session(f"VMCP_LAYOUT_XPR:{xpr.as_posix()}")
    assert await query_session_xpr(session) == xpr.as_posix()
    assert await query_session_xpr(_session("VMCP_LAYOUT_XPR:NONE")) is None
    assert await query_session_xpr(_session("boom", is_error=True)) is None
    raising = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("down")))
    assert await query_session_xpr(raising) is None

    path = await default_output_dir(session, "ila")
    assert os.path.dirname(path) == str(root / "Debug") and not os.path.exists(path)
    with pytest.raises(LayoutError, match="显式传入 output_dir"):
        await default_output_dir(_session("VMCP_LAYOUT_XPR:NONE"), "ila")
    with pytest.raises(LayoutError, match="显式传入 output_dir"):
        await default_output_dir(None, "bundle")


async def test_default_output_dir_prefers_hint_path(tmp_path):
    root, _ = _project(tmp_path)
    dcp = root / "demo.runs" / "impl_1" / "top.dcp"
    dcp.parent.mkdir(parents=True)
    dcp.write_bytes(b"dcp")
    session = _session("VMCP_LAYOUT_XPR:NONE")
    path = await default_output_dir(session, "bundle", hint_path=str(dcp))
    assert os.path.dirname(path) == str(root / "Debug")
    session.execute.assert_not_called()


async def test_get_project_layout_tool(tmp_path):
    from tests.test_debug_design_tools import call, context
    from vivado_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools["get_project_layout"].input_schema
    assert set(schema["properties"]) == {"project", "session_id"}
    assert not schema.get("required")

    root, xpr = _project(tmp_path)
    result = await call("get_project_layout", {"project": str(xpr)}, context())
    assert result["source"] == "argument"
    assert result["debug_dir"] == str(root / "Debug")
    assert result["config_path"] is None and result["rules"]

    session = _session(f"VMCP_LAYOUT_XPR:{xpr.as_posix()}")
    result = await call("get_project_layout", {"session_id": "board"}, context(session))
    assert result["source"] == "session"
    assert result["xpr"] == str(xpr)

    empty = _session("VMCP_LAYOUT_XPR:NONE")
    result = await call("get_project_layout", {"session_id": "board"}, context(empty))
    assert "没有打开工程" in result["error"]
    result = await call("get_project_layout", {"session_id": "nobody"}, context())
    assert "不存在" in result["error"]
    assert not (root / "Debug").exists()
