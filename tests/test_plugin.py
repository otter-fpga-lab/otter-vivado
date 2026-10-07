"""Generated wrappers remain small, source-bound and never overwrite user directories."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vivado_mcp import plugin


def test_plugin_contains_only_entry_files_and_binds_private_source(tmp_path):
    output = tmp_path / "entry"
    result = plugin.create_plugin("codex", output)
    files = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
    assert files == set(result["files"])
    assert len(files) == 4
    manifest = json.loads((output / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    server = json.loads((output / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "otter-vivado"
    ]
    assert Path(server["command"]).is_file()
    assert Path(result["source"]) / ".venv" == Path(sys.prefix)
    assert server["args"] == ["-B", "-m", "vivado_mcp"]
    assert "env" not in server
    assert "cwd" not in server
    assert not any(path.endswith(".py") or "/references/" in path for path in files)
    assert result["installed_in_host"] is False

    # Cache copying does not require another business installation.
    cache = tmp_path / "cache"
    shutil.copytree(output, cache)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    process = subprocess.run(
        [
            server["command"],
            "-c",
            "from vivado_mcp.guide import read_guide; "
            "import json; print(json.dumps(read_guide('remote')))",
        ],
        cwd=cache,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["source_root"] == result["source"]


def test_conflicting_output_is_preserved(tmp_path):
    output = tmp_path / "owned"
    output.mkdir()
    original = output / "README.md"
    original.write_bytes(b"user content\r\n")
    with pytest.raises(FileExistsError):
        plugin.create_plugin("codex", output)
    assert original.read_bytes() == b"user content\r\n"
    assert list(output.iterdir()) == [original]


def test_relative_output_and_unimplemented_clients_are_rejected():
    with pytest.raises(ValueError, match="absolute"):
        plugin.create_plugin("codex", Path("relative-entry"))
    with pytest.raises(ValueError, match="Only the Codex"):
        plugin.create_plugin("workbuddy", Path("relative-entry"))


def test_missing_editable_binding_creates_no_output(tmp_path, monkeypatch):
    def missing_source():
        raise RuntimeError("not editable")

    monkeypatch.setattr(plugin, "_editable_source", missing_source)
    output = tmp_path / "entry"
    with pytest.raises(RuntimeError, match="editable installation"):
        plugin.create_plugin("codex", output)
    assert not output.exists()


def test_cli_generation_uses_the_same_product_entry(tmp_path):
    output = tmp_path / "cli-entry"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vivado_mcp",
            "plugin",
            "--client",
            "codex",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["installed_in_host"] is False
    assert (output / ".mcp.json").is_file()
