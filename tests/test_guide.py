"""Guide access is bounded and the real stdio session reads fresh source text."""

import json
import os
import shutil
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from vivado_mcp import guide, plugin


def isolated_guides(root):
    (root / "skills/otter-vivado/references").mkdir(parents=True)
    (root / "docs").mkdir()
    overview = root / "skills/otter-vivado/SKILL.md"
    overview.write_text("source overview one\n", encoding="utf-8")
    (root / "docs/REMOTE_BUILD.md").write_text("remote guide\n", encoding="utf-8")
    (root / "docs/REMOTE_TCL.md").write_text("Tcl guide\n", encoding="utf-8")
    (root / "skills/otter-vivado/references/workflows.md").write_text(
        "workflow reference\n", encoding="utf-8"
    )
    return overview


@pytest.mark.parametrize(
    "reference",
    [
        "../config/remote-hosts.local.json",
        "/etc/passwd",
        "C:\\private.txt",
        "unknown.md",
        "workflows.md/../private",
    ],
)
def test_arbitrary_reference_paths_are_rejected(reference):
    with pytest.raises(ValueError):
        guide.read_guide("reference", reference)


def test_source_file_redirect_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "source"
    document = isolated_guides(root)
    secret = tmp_path / "private.txt"
    secret.write_text("secret fixture", encoding="utf-8")
    document.unlink()
    try:
        document.symlink_to(secret)
    except (PermissionError, OSError) as exc:
        pytest.skip(f"File symlink is unavailable: {exc}")
    monkeypatch.setattr(guide, "SOURCE_ROOT", root)
    with pytest.raises(ValueError, match="redirected"):
        guide.read_guide()


def test_guide_fresh_read_and_explicit_routes(tmp_path, monkeypatch):
    root = tmp_path / "source"
    overview = isolated_guides(root)
    monkeypatch.setattr(guide, "SOURCE_ROOT", root)
    first = guide.read_guide()
    overview.write_text("source overview two\n", encoding="utf-8")
    second = guide.read_guide()
    assert first["sha256"] != second["sha256"]
    assert second["content"] == "source overview two\n"
    assert guide.read_guide("remote")["content"] == "remote guide\n"
    assert guide.read_guide("reference", "workflows.md")["content"] == "workflow reference\n"
    with pytest.raises(ValueError):
        guide.read_guide("remote", "workflows.md")


def result_json(result):
    assert not result.is_error
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_generated_cache_starts_source_mcp_without_eda(tmp_path):
    output = tmp_path / "entry"
    generated = plugin.create_plugin("codex", output)
    cache = tmp_path / "cache"
    shutil.copytree(output, cache)
    server = json.loads((cache / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "otter-vivado"
    ]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    parameters = StdioServerParameters(
        command=server["command"],
        args=server["args"],
        cwd=str(cache),
        env=env,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools["vivado_guide"].annotations.read_only_hint is True
            assert tools["vivado_guide"].annotations.open_world_hint is False
            result = result_json(await session.call_tool("vivado_guide", {"section": "overview"}))
            assert result["source_root"] == generated["source"]
            assert result["path"] == "skills/otter-vivado/SKILL.md"
            sessions = await session.call_tool("list_sessions", {})
            assert not sessions.is_error


@pytest.mark.asyncio
async def test_same_stdio_session_reads_updated_isolated_guides(tmp_path):
    root = tmp_path / "source"
    overview = isolated_guides(root)
    cache = tmp_path / "cache"
    cache.mkdir()
    script = (
        "import sys; from pathlib import Path; from vivado_mcp import guide; "
        "guide.SOURCE_ROOT=Path(sys.argv[1]); from vivado_mcp.server import mcp; "
        "mcp.run(transport='stdio')"
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-c", script, str(root)],
        cwd=str(cache),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            first = result_json(await session.call_tool("vivado_guide", {}))
            overview.write_text("source overview updated\n", encoding="utf-8")
            second = result_json(await session.call_tool("vivado_guide", {}))
            assert first["sha256"] != second["sha256"]
            assert second["content"] == "source overview updated\n"
            reference = result_json(
                await session.call_tool(
                    "vivado_guide", {"section": "reference", "reference": "workflows.md"}
                )
            )
            assert reference["content"] == "workflow reference\n"
            rejected = result_json(
                await session.call_tool(
                    "vivado_guide",
                    {"section": "reference", "reference": "../config/remote-hosts.local.json"},
                )
            )
            assert "error" in rejected
