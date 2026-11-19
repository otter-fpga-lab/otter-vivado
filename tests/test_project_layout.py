"""project_layout 与 get_project_layout：只解析目录约定，不创建目录。"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
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
    assert all(item["severity"] == "warning" for item in hygiene)

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
    assert hygiene[0]["severity"] == "warning"
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
    assert all(item["severity"] == "warning" for item in result["hygiene"])
    assert all("清单" in item["suggestion"] for item in result["hygiene"])
    assert (root / "script").is_dir()
    assert (root / ".vmcp").is_dir()


@pytest.mark.parametrize("gitignore_text", [
    "# comment\n.work/\n",
    "/.work\n",
    "/.work/\n",
    "**/.work\n",
    "**/.work/\n",
])
def test_hygiene_downgrades_gitignored_and_script_convention_work_dir(tmp_path, gitignore_text):
    root, _ = _project(tmp_path)
    (root / ".git").mkdir()
    (root / ".gitignore").write_text(gitignore_text, encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools" / "rtl_common.py").write_text(
        'WORK = REPO / ".work"\n', encoding="utf-8",
    )
    (root / ".work").mkdir()

    hygiene = inspect_layout_hygiene(resolve_layout(root))
    assert len(hygiene) == 1
    item = hygiene[0]
    assert item["kind"] == "convention_work_dir"
    assert item["severity"] == "info"
    assert item["name"] == ".work"
    assert item["location"] == "project_root"
    assert item["path"] == str(root / ".work")
    assert ".gitignore" in item["message"] and "脚本" in item["message"]
    assert "不得用作草稿" in item["message"]
    assert "Debug/" in item["message"]
    assert "Debug/" in item["suggestion"]
    assert "不得用作草稿" in item["suggestion"]


def test_hygiene_supports_nested_project_under_git_root_and_root_scripts(tmp_path):
    repo = tmp_path / "isp_like"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("# generated\n.work/\n", encoding="utf-8")
    (repo / "build.ps1").write_text(
        "$out = Join-Path $PSScriptRoot '.work\\sim'\n", encoding="utf-8",
    )
    project_root = repo / "projects" / "cam"
    project_root.mkdir(parents=True)
    (project_root / "cam.xpr").write_text("<Project/>", encoding="utf-8")
    (project_root / ".work").mkdir()

    hygiene = inspect_layout_hygiene(resolve_layout(project_root))
    assert len(hygiene) == 1
    assert hygiene[0]["kind"] == "convention_work_dir"
    assert hygiene[0]["severity"] == "info"

    # 嵌套子目录 .gitignore 否定规则覆盖仓根忽略规则时，保持 warning
    (project_root / ".gitignore").write_text("!.work/\n", encoding="utf-8")
    hygiene_negated = inspect_layout_hygiene(resolve_layout(project_root))
    assert [(i["kind"], i["severity"]) for i in hygiene_negated] == [
        ("leftover_hidden_dir", "warning"),
    ]

    # 仓根 /.work/ 仅锚定仓根，不忽略子目录内的 .work
    (project_root / ".gitignore").unlink()
    (repo / ".gitignore").write_text("/.work/\n", encoding="utf-8")
    hygiene_anchored = inspect_layout_hygiene(resolve_layout(project_root))
    assert [(i["kind"], i["severity"]) for i in hygiene_anchored] == [
        ("leftover_hidden_dir", "warning"),
    ]


@pytest.mark.parametrize("negation", ["!.work\n", "!.work/\n"])
def test_hygiene_keeps_warning_when_gitignore_negates_work(tmp_path, negation):
    root, _ = _project(tmp_path)
    (root / ".git").mkdir()
    (root / ".gitignore").write_text(f".work/\n{negation}", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "run.tcl").write_text('set w ".work/out"\n', encoding="utf-8")
    (root / ".work").mkdir()

    hygiene = inspect_layout_hygiene(resolve_layout(root))
    assert len(hygiene) == 1
    assert hygiene[0]["kind"] == "leftover_hidden_dir"
    assert hygiene[0]["severity"] == "warning"


def test_hygiene_keeps_warning_when_only_one_condition_met_or_token_mismatched(tmp_path):
    # 1) 仅 gitignore，无脚本约定
    only_git = tmp_path / "only_git"
    (only_git / ".git").mkdir(parents=True)
    (only_git / ".gitignore").write_text(".work/\n", encoding="utf-8")
    (only_git / "cam.xpr").write_text("<Project/>", encoding="utf-8")
    (only_git / ".work").mkdir()
    h1 = inspect_layout_hygiene(resolve_layout(only_git))
    assert [(i["kind"], i["severity"]) for i in h1] == [("leftover_hidden_dir", "warning")]

    # 2) 仅脚本约定，未 gitignore（含无 .git 的工程目录）
    only_script = tmp_path / "only_script"
    only_script.mkdir()
    (only_script / "cam.xpr").write_text("<Project/>", encoding="utf-8")
    (only_script / "Makefile").write_text("WORK := .work\n", encoding="utf-8")
    (only_script / ".work").mkdir()
    h2 = inspect_layout_hygiene(resolve_layout(only_script))
    assert [(i["kind"], i["severity"]) for i in h2] == [("leftover_hidden_dir", "warning")]

    # 3) .workbuddy / .workspace / Debug/scripts/.work 不误判为仓库脚本约定
    false_tok = tmp_path / "false_tok"
    false_tok.mkdir()
    (false_tok / "cam.xpr").write_text("<Project/>", encoding="utf-8")
    (false_tok / ".gitignore").write_text(".work/\n", encoding="utf-8")
    (false_tok / "tools").mkdir()
    (false_tok / "tools" / "env.py").write_text(
        'CACHE = ".workbuddy/bin"\nWS = ".workspace/cfg"\nTAG = "my.work"\n',
        encoding="utf-8",
    )
    (false_tok / "Debug" / "scripts").mkdir(parents=True)
    (false_tok / "Debug" / "scripts" / "agent.py").write_text(
        'BAD = ".work"\n', encoding="utf-8",
    )
    (false_tok / ".work").mkdir()
    h3 = inspect_layout_hygiene(resolve_layout(false_tok))
    assert [(i["kind"], i["severity"]) for i in h3] == [("leftover_hidden_dir", "warning")]


def test_hygiene_never_downgrades_debug_work_dir(tmp_path):
    root, _ = _project(tmp_path)
    (root / ".git").mkdir()
    (root / ".gitignore").write_text(".work/\n", encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools" / "build.py").write_text('WORK = ".work"\n', encoding="utf-8")
    (root / ".work").mkdir()
    (root / "Debug" / ".work").mkdir(parents=True)

    hygiene = inspect_layout_hygiene(resolve_layout(root))
    by_loc = {item["location"]: item for item in hygiene}
    assert by_loc["project_root"]["kind"] == "convention_work_dir"
    assert by_loc["project_root"]["severity"] == "info"
    assert by_loc["debug_dir"]["kind"] == "leftover_hidden_dir"
    assert by_loc["debug_dir"]["severity"] == "warning"


def test_hygiene_supports_rtl_library_studio_bridge_delegation(monkeypatch, tmp_path):
    lib = tmp_path / "otter-rtl-library"
    (lib / ".git").mkdir(parents=True)
    (lib / ".gitignore").write_text("/.work/\n", encoding="utf-8")
    (lib / "tools").mkdir()
    (lib / "tools" / "rtl_common.py").write_text(
        'from studio_bridge import load\nsys.modules[__name__] = load("rtl_common")\n',
        encoding="utf-8",
    )
    (lib / "tools" / "studio_bridge.py").write_text(
        "import rtl_studio\nfrom rtl_studio.context import LibraryContext, initialize\n",
        encoding="utf-8",
    )
    (lib / ".work").mkdir()

    # 1) 同级 studio 含 .work -> 降级为 info
    studio = tmp_path / "otter-rtl-studio"
    (studio / "rtl_studio").mkdir(parents=True)
    (studio / "rtl_studio" / "context.py").write_text(
        'def active_work(): return active_repo() / ".work"\n', encoding="utf-8",
    )
    monkeypatch.delenv("RTL_STUDIO_HOME", raising=False)
    h_with_studio = inspect_layout_hygiene(resolve_layout(lib))
    assert [(i["kind"], i["severity"]) for i in h_with_studio] == [
        ("convention_work_dir", "info"),
    ]

    # 2) RTL_STUDIO_HOME 指向不含 .work 的目录 -> 保持 warning
    empty_studio = tmp_path / "empty-studio"
    (empty_studio / "rtl_studio").mkdir(parents=True)
    (empty_studio / "rtl_studio" / "context.py").write_text(
        "def active_repo(): pass\n", encoding="utf-8",
    )
    monkeypatch.setenv("RTL_STUDIO_HOME", str(empty_studio))
    h_without_work = inspect_layout_hygiene(resolve_layout(lib))
    assert [(i["kind"], i["severity"]) for i in h_without_work] == [
        ("leftover_hidden_dir", "warning"),
    ]


async def test_get_project_layout_tool_reports_convention_work_dir(tmp_path):
    from tests.test_debug_design_tools import call, context

    root, xpr = _project(tmp_path)
    (root / ".git").mkdir()
    (root / ".gitignore").write_text("/.work/\n", encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools" / "run.py").write_text('WORK = Path(".work")\n', encoding="utf-8")
    (root / ".work").mkdir()
    (root / "Debug" / ".work").mkdir(parents=True)

    result = await call("get_project_layout", {"project": str(xpr)}, context())
    by_path = {item["path"]: item for item in result["hygiene"]}
    root_work = by_path[str(root / ".work")]
    debug_work = by_path[str(root / "Debug" / ".work")]
    assert root_work["kind"] == "convention_work_dir"
    assert root_work["severity"] == "info"
    assert debug_work["kind"] == "leftover_hidden_dir"
    assert debug_work["severity"] == "warning"


def test_hygiene_gitignore_ancestor_dir_and_windows_case_insensitivity(tmp_path):
    # 1) 仓根 .gitignore 忽略祖先目录（如 projects/）时，子目录内 .work 同样判定为已忽略
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("projects/\n", encoding="utf-8")
    (repo / "tools").mkdir()
    (repo / "tools" / "build.py").write_text('WORK = ".work"\n', encoding="utf-8")
    proj = repo / "projects" / "cam"
    proj.mkdir(parents=True)
    (proj / "cam.xpr").write_text("<Project/>", encoding="utf-8")
    (proj / ".work").mkdir()

    h_ancestor = inspect_layout_hygiene(resolve_layout(proj))
    assert [(i["kind"], i["severity"]) for i in h_ancestor] == [
        ("convention_work_dir", "info"),
    ]

    # 2) Windows 下 .gitignore 大小写不敏感匹配（.WORK/）
    if os.name == "nt":
        case_proj = tmp_path / "win_case"
        (case_proj / ".git").mkdir(parents=True)
        (case_proj / ".gitignore").write_text(".WORK/\n", encoding="utf-8")
        (case_proj / "tools").mkdir()
        (case_proj / "tools" / "build.py").write_text('WORK = ".work"\n', encoding="utf-8")
        (case_proj / "cam.xpr").write_text("<Project/>", encoding="utf-8")
        (case_proj / ".work").mkdir()

        h_case = inspect_layout_hygiene(resolve_layout(case_proj))
        assert [(i["kind"], i["severity"]) for i in h_case] == [
            ("convention_work_dir", "info"),
        ]


def test_hygiene_studio_delegation_missing_fallback_and_junction_guards(monkeypatch, tmp_path):
    # 1) studio_pkg 不存在时仍兼容回退为 True（独立 otter-rtl-library 检出）
    standalone = tmp_path / "standalone_lib"
    (standalone / ".git").mkdir(parents=True)
    (standalone / ".gitignore").write_text("/.work/\n", encoding="utf-8")
    (standalone / "tools").mkdir()
    (standalone / "tools" / "rtl_common.py").write_text(
        'from studio_bridge import load\nsys.modules[__name__] = load("rtl_common")\n',
        encoding="utf-8",
    )
    (standalone / "tools" / "studio_bridge.py").write_text(
        "import rtl_studio\nfrom rtl_studio.context import LibraryContext\n",
        encoding="utf-8",
    )
    (standalone / ".work").mkdir()
    monkeypatch.setenv("RTL_STUDIO_HOME", str(tmp_path / "missing_studio"))
    h_missing = inspect_layout_hygiene(resolve_layout(standalone))
    assert [(i["kind"], i["severity"]) for i in h_missing] == [
        ("convention_work_dir", "info"),
    ]

    if os.name != "nt":
        return

    def mk_junction(link: Path, target: Path) -> None:
        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, check=False,
        )
        assert res.returncode == 0, res.stdout + res.stderr

    real_studio_pkg = tmp_path / "real_pkg"
    real_studio_pkg.mkdir()
    (real_studio_pkg / "context.py").write_text(
        'WORK = ".work"\n', encoding="utf-8",
    )
    junc_studio = tmp_path / "junc_studio"
    junc_studio.mkdir()
    junc_pkg = junc_studio / "rtl_studio"
    mk_junction(junc_pkg, real_studio_pkg)

    junc_work_proj = tmp_path / "junc_work_proj"
    (junc_work_proj / ".git").mkdir(parents=True)
    (junc_work_proj / ".gitignore").write_text(".work/\n", encoding="utf-8")
    (junc_work_proj / "tools").mkdir()
    (junc_work_proj / "tools" / "build.py").write_text('WORK = ".work"\n', encoding="utf-8")
    real_work = tmp_path / "real_work"
    real_work.mkdir()
    mk_junction(junc_work_proj / ".work", real_work)

    junc_tools_proj = tmp_path / "junc_tools_proj"
    (junc_tools_proj / ".git").mkdir(parents=True)
    (junc_tools_proj / ".gitignore").write_text(".work/\n", encoding="utf-8")
    (junc_tools_proj / ".work").mkdir()
    real_tools = tmp_path / "real_tools"
    real_tools.mkdir()
    (real_tools / "build.py").write_text('WORK = ".work"\n', encoding="utf-8")
    mk_junction(junc_tools_proj / "tools", real_tools)

    try:
        # 2) studio_pkg 为 junction -> 拒绝跟随，保持 warning
        monkeypatch.setenv("RTL_STUDIO_HOME", str(junc_studio))
        h_junc_studio = inspect_layout_hygiene(resolve_layout(standalone))
        assert [(i["kind"], i["severity"]) for i in h_junc_studio] == [
            ("leftover_hidden_dir", "warning"),
        ]

        # 3) .work 为 junction -> 不降级，保持 warning
        h_junc_work = inspect_layout_hygiene(resolve_layout(junc_work_proj))
        assert [(i["kind"], i["severity"]) for i in h_junc_work] == [
            ("leftover_hidden_dir", "warning"),
        ]

        # 4) tools/ 为 junction -> 脚本扫描跳过 junction，保持 warning
        h_junc_tools = inspect_layout_hygiene(resolve_layout(junc_tools_proj))
        assert [(i["kind"], i["severity"]) for i in h_junc_tools] == [
            ("leftover_hidden_dir", "warning"),
        ]
    finally:
        for link in (junc_pkg, junc_work_proj / ".work", junc_tools_proj / "tools"):
            if os.path.lexists(link):
                os.rmdir(link)
