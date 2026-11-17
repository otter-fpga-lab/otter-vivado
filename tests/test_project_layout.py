"""project_layout 与 get_project_layout：只解析目录约定，不创建目录。"""

from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vivado_mcp.project_layout import (
    LayoutError,
    _scan_dir_size,
    consume_reserved_debug_subdir,
    default_output_dir,
    default_vivado_cwd,
    discard_reserved_debug_subdir,
    find_project_from_path,
    inspect_layout_hygiene,
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
        assert any(
            ".work" in rule and ".vmcp" in rule and ".otter" in rule
            for rule in layout.rules
        )
        assert any(
            "临时脚本不要挂到 run 的 TCL.PRE/POST" in rule
            and "严禁挂指向工程外或 %TEMP% 的钩子" in rule
            and "PITFALLS C3" in rule
            for rule in layout.rules
        )
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
    assert result["hygiene"] == []

    session = _session(f"VMCP_LAYOUT_XPR:{xpr.as_posix()}")
    result = await call("get_project_layout", {"session_id": "board"}, context(session))
    assert result["source"] == "session"
    assert result["xpr"] == str(xpr)
    assert result["hygiene"] == []

    empty = _session("VMCP_LAYOUT_XPR:NONE")
    result = await call("get_project_layout", {"session_id": "board"}, context(empty))
    assert "没有打开工程" in result["error"]
    result = await call("get_project_layout", {"session_id": "nobody"}, context())
    assert "不存在" in result["error"]
    assert not (root / "Debug").exists()


def test_hygiene_detects_leftovers_singular_script_and_oversized_debug(tmp_path):
    trash = tmp_path / "99_垃圾堆"
    trash.mkdir()
    root, _ = _project(tmp_path)
    (root / ".vmcp").mkdir()
    (root / ".work").mkdir()
    (root / ".otter").mkdir()
    debug_dir = root / "Debug"
    (debug_dir / ".work").mkdir(parents=True)
    (debug_dir / ".otter").mkdir()
    big_sub = debug_dir / "20261117_heap"
    big_sub.mkdir()
    (big_sub / "trace.bin").write_bytes(b"x" * 2048)
    singular = root / "script"
    singular.mkdir()
    (singular / "old.tcl").write_text("# old\n", encoding="utf-8")

    layout = resolve_layout(root)
    hygiene = inspect_layout_hygiene(layout, debug_size_limit_bytes=1024)

    kinds = [item["kind"] for item in hygiene]
    assert kinds.count("leftover_hidden_dir") == 5
    assert "singular_script_dir" in kinds
    assert "oversized_debug_dir" in kinds

    paths = {item["path"] for item in hygiene}
    assert str(root / ".vmcp") in paths
    assert str(root / ".work") in paths
    assert str(root / ".otter") in paths
    assert str(debug_dir / ".work") in paths
    assert str(debug_dir / ".otter") in paths
    assert str(singular) in paths
    assert str(debug_dir) in paths

    for item in hygiene:
        assert str(trash) in item["suggestion"]
        assert "清单" in item["suggestion"]
        assert "不自动移动" in item["suggestion"]

    oversized = next(item for item in hygiene if item["kind"] == "oversized_debug_dir")
    assert oversized["total_bytes"] == 2048
    assert oversized["threshold_bytes"] == 1024
    assert oversized["truncated"] is False

    # 只读：不得移动或删除任何残留目录
    assert (root / ".vmcp").is_dir()
    assert (root / ".work").is_dir()
    assert (debug_dir / ".work").is_dir()
    assert singular.is_dir()
    assert list(trash.iterdir()) == []


def test_hygiene_respects_configured_script_dir_and_no_trash(tmp_path):
    root, _ = _project(tmp_path)
    (root / "otter-vivado.toml").write_text(
        '[layout]\nscripts_dir = "script"\ndebug_dir = "."\n',
        encoding="utf-8",
    )
    (root / "script").mkdir()
    (root / ".vmcp").mkdir()

    layout = resolve_layout(root)
    hygiene = inspect_layout_hygiene(layout)

    assert len(hygiene) == 1
    assert hygiene[0]["kind"] == "leftover_hidden_dir"
    assert "未找到 99_垃圾堆" in hygiene[0]["suggestion"]
    assert "清单" in hygiene[0]["suggestion"]


def test_hygiene_scan_bounds_max_files_and_timeout(monkeypatch, tmp_path):
    root, _ = _project(tmp_path)
    debug_dir = root / "Debug"
    debug_dir.mkdir()
    for i in range(6):
        (debug_dir / f"f{i}.bin").write_bytes(b"a" * 300)

    exact = _scan_dir_size(debug_dir, max_files=6, timeout_seconds=5.0)
    assert exact.scanned_files == 6
    assert exact.total_bytes == 1800
    assert exact.truncated is False

    capped = _scan_dir_size(debug_dir, max_files=4, timeout_seconds=5.0)
    assert capped.scanned_files == 4
    assert capped.total_bytes == 1200
    assert capped.truncated is True
    assert capped.timed_out is False

    layout = resolve_layout(root)
    hygiene = inspect_layout_hygiene(
        layout, debug_size_limit_bytes=1000, max_files=4, timeout_seconds=5.0,
    )
    assert len(hygiene) == 1
    assert hygiene[0]["kind"] == "oversized_debug_dir"
    assert hygiene[0]["truncated"] is True
    assert hygiene[0]["scanned_files"] == 4

    # 截断但累计未超阈值时，仍给出截断提示（总大小未知）
    truncated_under = inspect_layout_hygiene(
        layout, debug_size_limit_bytes=100_000, max_files=4, timeout_seconds=5.0,
    )
    assert len(truncated_under) == 1
    assert truncated_under[0]["kind"] == "debug_dir_scan_truncated"
    assert truncated_under[0]["truncated"] is True
    assert truncated_under[0]["timed_out"] is False
    assert truncated_under[0]["scanned_files"] == 4
    assert "扫描文件数上限" in truncated_under[0]["message"]
    assert "总大小未知" in truncated_under[0]["message"]

    ticks = iter([100.0, 100.0, 105.0, 200.0, 200.0, 205.0])
    monkeypatch.setattr("vivado_mcp.project_layout.time.monotonic", lambda: next(ticks, 205.0))
    timed_out = _scan_dir_size(debug_dir, max_files=100, timeout_seconds=1.0)
    assert timed_out.truncated is True
    assert timed_out.timed_out is True

    timed_out_hygiene = inspect_layout_hygiene(
        layout, debug_size_limit_bytes=100_000, max_files=100, timeout_seconds=1.0,
    )
    assert len(timed_out_hygiene) == 1
    assert timed_out_hygiene[0]["kind"] == "debug_dir_scan_truncated"
    assert timed_out_hygiene[0]["timed_out"] is True
    assert "扫描超时上限" in timed_out_hygiene[0]["message"]


def test_reserve_debug_subdir_can_skip_creating_parent(tmp_path):
    root, _ = _project(tmp_path)
    layout = resolve_layout(root)
    path1 = reserve_debug_subdir(layout, "bundle", now=NOW, create_parent=False)
    path2 = reserve_debug_subdir(layout, "bundle", now=NOW, create_parent=False)
    assert path1 == root / "Debug" / "20261117_093005_bundle"
    assert path2 == root / "Debug" / "20261117_093005_bundle_2"
    assert not (root / "Debug").exists()
    assert consume_reserved_debug_subdir(path1) is True
    assert consume_reserved_debug_subdir(path1) is False
    discard_reserved_debug_subdir(path2)
    assert consume_reserved_debug_subdir(path2) is False


async def test_get_project_layout_tool_includes_hygiene(tmp_path):
    from tests.test_debug_design_tools import call, context

    trash = tmp_path / "99_垃圾堆"
    trash.mkdir()
    root, xpr = _project(tmp_path)
    (root / "script").mkdir()
    (root / ".vmcp").mkdir()

    result = await call("get_project_layout", {"project": str(xpr)}, context())
    kinds = [item["kind"] for item in result["hygiene"]]
    assert kinds == ["leftover_hidden_dir", "singular_script_dir"]
    assert all("清单" in item["suggestion"] for item in result["hygiene"])
    assert (root / "script").is_dir()
    assert (root / ".vmcp").is_dir()
