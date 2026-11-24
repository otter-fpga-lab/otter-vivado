"""跨智能体 SharedRegistry 与客户端来源检测单元测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from mcp.server.mcpserver import Context

from vivado_mcp.server import AppContext, app_lifespan, mcp, sync_shared_session
from vivado_mcp.shared_registry import (
    SharedRegistry,
    detect_client_source,
    is_pid_alive,
)
from vivado_mcp.tools.tcl_tools import _sync_tcl_project_hint
from vivado_mcp.vivado.session_manager import SessionManager

FIXTURES = Path(__file__).parent / "fixtures"

_CLIENT_ENV_KEYS = (
    "CODEX_THREAD_ID",
    "CODEX_SANDBOX",
    "CODEX_CI",
    "CODEX_SHELL",
    "CODEX_MANAGED_BY_NPM",
    "CODEX_MANAGED_BY_BUN",
    "CODEX_HOME",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CURSOR_TRACE_ID",
    "CURSOR_AGENT",
    "CURSOR_EXTENSION_HOST_ROLE",
    "ANTIGRAVITY_AGENT",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_LS_ADDRESS",
    "ANTIGRAVITY_TRAJECTORY_ID",
    "ANTIGRAVITY_AGENTAPI_EXE",
)


def _clear_client_env(monkeypatch) -> None:
    for key in list(os.environ):
        upper = key.upper()
        if (
            key in _CLIENT_ENV_KEYS
            or upper.startswith(("CODEX_", "CLAUDE_", "CURSOR_", "ANTIGRAVITY_"))
        ):
            monkeypatch.delenv(key, raising=False)


def test_detect_client_source_env_precedence_and_fallback(monkeypatch):
    try:
        _clear_client_env(monkeypatch)
        monkeypatch.setattr(
            "vivado_mcp.shared_registry._ancestor_process_names", lambda: ()
        )
        detect_client_source.cache_clear()
        assert detect_client_source() == "CLI"

        monkeypatch.setenv("CODEX_THREAD_ID", "th-1")
        detect_client_source.cache_clear()
        assert detect_client_source() == "Codex"

        _clear_client_env(monkeypatch)
        monkeypatch.setenv("ANTIGRAVITY_AGENT", "1")
        detect_client_source.cache_clear()
        assert detect_client_source() == "Antigravity"

        _clear_client_env(monkeypatch)
        monkeypatch.setenv("CLAUDECODE", "1")
        detect_client_source.cache_clear()
        assert detect_client_source() == "Claude"

        _clear_client_env(monkeypatch)
        monkeypatch.setenv("CURSOR_TRACE_ID", "cur-1")
        detect_client_source.cache_clear()
        assert detect_client_source() == "Cursor"

        # 祖先链优先匹配 AI 客户端（Code.exe -> codex.exe -> python 归属 Codex，而非 VS Code）
        _clear_client_env(monkeypatch)
        monkeypatch.setattr(
            "vivado_mcp.shared_registry._ancestor_process_names",
            lambda: ["python.exe", "python.exe", "codex.exe", "code.exe"],
        )
        detect_client_source.cache_clear()
        assert detect_client_source() == "Codex"

        monkeypatch.setattr(
            "vivado_mcp.shared_registry._ancestor_process_names",
            lambda: ["python.exe", "language_server_windows_x64.exe", "code.exe"],
        )
        detect_client_source.cache_clear()
        assert detect_client_source() == "Antigravity"

        monkeypatch.setattr(
            "vivado_mcp.shared_registry._ancestor_process_names",
            lambda: ["pwsh.exe", "code.exe"],
        )
        detect_client_source.cache_clear()
        assert detect_client_source() == "VS Code"
    finally:
        detect_client_source.cache_clear()


def test_is_pid_alive_checks_self_and_dead_pid():
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(0) is False
    assert is_pid_alive(-42) is False
    # 启动一个立即退出的轻量进程再验证其退出后返回 False
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=10)
    assert is_pid_alive(proc.pid) is False


def test_shared_registry_persists_in_cache_and_enriches_remote_runs(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("OTTER_VIVADO_CACHE_DIR", str(cache_dir))

    proj_dir = tmp_path / "user_proj"
    impl_dir = proj_dir / "demo.runs" / "impl_1"
    impl_dir.mkdir(parents=True)
    xpr_file = proj_dir / "demo.xpr"
    xpr_file.write_text("<Project/>", encoding="utf-8")

    timing_text = (FIXTURES / "sample_report_timing.txt").read_text(encoding="utf-8")
    util_text = (FIXTURES / "sample_report_utilization.txt").read_text(encoding="utf-8")
    (impl_dir / "route_timing_summary.rpt").write_text(
        "Design State : Routed\n" + timing_text, encoding="utf-8"
    )
    (impl_dir / "route_utilization.rpt").write_text(util_text, encoding="utf-8")
    (impl_dir / ".vivado.begin.rst").write_text("<ProcessHandle/>", encoding="utf-8")
    (impl_dir / "runme.log").write_text(
        "Vivado v2024.2\n"
        "Phase 1.1 Placer Initialization\n"
        "Phase 4.1 Global Routing\n"
        "INFO: [Timing 38-35] Done setting XDC timing constraints.\n",
        encoding="utf-8",
    )

    reg = SharedRegistry()
    # 写入自身记录（默认 exclude_owner_pid 排除）
    reg.upsert_session(
        "default",
        mode="gui",
        port=9999,
        project_name="local_demo",
        status="idle",
    )
    # 绝不在用户工程内落盘任何隐藏目录或文件
    assert not any(proj_dir.glob(".*"))
    assert reg.sessions_dir == cache_dir / "registry" / "sessions"
    assert (reg.sessions_dir / f"{os.getpid()}_default.json").is_file()

    # 写入一个模拟仍在运行的远程 Codex 会话（假定远端 PID 存活，且尚未主动更新状态）
    live_remote_pid = 888001
    dead_completed_pid = 888002
    dead_failed_pid = 888003
    monkeypatch.setattr(
        "vivado_mcp.shared_registry.is_pid_alive",
        lambda pid: pid in {os.getpid(), live_remote_pid},
    )

    remote_live_payload = {
        "entry_id": f"{live_remote_pid}_codex_main",
        "owner_pid": live_remote_pid,
        "client": "Codex",
        "session_id": "codex_main",
        "mode": "tcl",
        "port": None,
        "vivado_pid": 777001,
        "project_name": "demo",
        "xpr_path": str(xpr_file),
        "run_name": "impl_1",
        "target_step": "route_design",
        "run_dir": str(impl_dir),
        "status": "running",
        "state": "route_design Running",
        "progress": 55.0,
        "updated_at": 1e12,
    }
    (reg.sessions_dir / f"{live_remote_pid}_codex_main.json").write_text(
        json.dumps(remote_live_payload), encoding="utf-8"
    )

    # 写两个已死亡的远端记录，分别带 .vivado.end.rst 和 .vivado.error.rst
    done_dir = proj_dir / "done.runs" / "impl_1"
    done_dir.mkdir(parents=True)
    (done_dir / ".vivado.begin.rst").write_text("b", encoding="utf-8")
    (done_dir / ".vivado.end.rst").write_text("e", encoding="utf-8")
    (reg.sessions_dir / f"{dead_completed_pid}_s1.json").write_text(
        json.dumps(
            {
                "entry_id": f"{dead_completed_pid}_s1",
                "owner_pid": dead_completed_pid,
                "client": "Claude",
                "session_id": "s1",
                "run_dir": str(done_dir),
                "status": "running",
                "updated_at": 1e12 - 10,
            }
        ),
        encoding="utf-8",
    )

    err_dir = proj_dir / "err.runs" / "impl_1"
    err_dir.mkdir(parents=True)
    (err_dir / ".vivado.begin.rst").write_text("b", encoding="utf-8")
    (err_dir / ".vivado.error.rst").write_text("err", encoding="utf-8")
    (reg.sessions_dir / f"{dead_failed_pid}_s2.json").write_text(
        json.dumps(
            {
                "entry_id": f"{dead_failed_pid}_s2",
                "owner_pid": dead_failed_pid,
                "client": "Cursor",
                "session_id": "s2",
                "run_dir": str(err_dir),
                "status": "running",
                "updated_at": 1e12 - 20,
            }
        ),
        encoding="utf-8",
    )

    remote = reg.list_remote_runs()
    assert len(remote) == 3
    by_id = {item["entry_id"]: item for item in remote}

    expected_keys = {
        "entry_id",
        "owner_pid",
        "client",
        "alive",
        "session_id",
        "mode",
        "port",
        "vivado_pid",
        "vivado_version",
        "project_name",
        "xpr_path",
        "run_name",
        "target_step",
        "run_dir",
        "status",
        "state",
        "progress",
        "phase",
        "elapsed_sec",
        "updated_at",
        "timing",
        "utilization",
        "diagnostics",
        "reports",
        "log_path",
        "log_tail",
        "monitor_url",
        "debug_url",
    }
    codex_item = by_id[f"{live_remote_pid}_codex_main"]
    assert set(codex_item) == expected_keys
    assert codex_item["alive"] is True
    assert codex_item["client"] == "Codex"
    assert codex_item["status"] == "running"
    assert codex_item["timing"]["wns"] == 0.234
    assert codex_item["utilization"]["lut"]["used"] is not None
    assert "Global Routing" in codex_item["phase"]
    assert any(r["name"] == "route_timing_summary.rpt" for r in codex_item["reports"])
    assert len(codex_item["log_tail"]) >= 3

    # 死亡进程记录由 running 降级为 completed / failed
    assert by_id[f"{dead_completed_pid}_s1"]["alive"] is False
    assert by_id[f"{dead_completed_pid}_s1"]["status"] == "completed"
    assert by_id[f"{dead_failed_pid}_s2"]["alive"] is False
    assert by_id[f"{dead_failed_pid}_s2"]["status"] == "failed"

    # allowed_report_roots 允许读取跨 Agent 报告目录
    roots = reg.allowed_report_roots()
    assert impl_dir.resolve() in roots
    assert proj_dir.resolve() in roots

    # cleanup_stale 清除死亡条目但保留存活条目
    removed = reg.cleanup_stale()
    assert removed == 2
    remaining = reg.list_remote_runs()
    assert [r["entry_id"] for r in remaining] == [f"{live_remote_pid}_codex_main"]

    # close() 后再清理无构建上下文的占位记录
    reg.close()
    assert not (reg.sessions_dir / f"{os.getpid()}_default.json").exists()


def test_tcl_project_hint_and_app_lifespan_cleanup(monkeypatch, tmp_path):
    reg = SharedRegistry(registry_dir=tmp_path / "registry")
    app_ctx = AppContext(
        session_manager=SessionManager(vivado_path=""),
        shared_registry=reg,
    )
    req = SimpleNamespace(lifespan_context=app_ctx)
    ctx = Context(request_context=req, mcp_server=mcp)

    xpr = tmp_path / "proj" / "top_proj.xpr"
    xpr.parent.mkdir()
    xpr.write_text("<Project/>", encoding="utf-8")

    sync_shared_session(ctx, session_id="s_tcl", mode="tcl", status="idle")
    _sync_tcl_project_hint(
        ctx,
        "s_tcl",
        f'open_project "{xpr}"',
        "INFO: open_project done",
    )
    entries = reg.list_remote_runs(exclude_owner_pid=None)
    assert len(entries) == 1
    assert entries[0]["project_name"] == "top_proj"
    assert Path(entries[0]["xpr_path"]) == xpr.resolve()

    # 出错时不误记录
    _sync_tcl_project_hint(ctx, "s_tcl", "open_project /bad/x.xpr", "[ERROR] failed")
    assert Path(reg.list_remote_runs(exclude_owner_pid=None)[0]["xpr_path"]) == xpr.resolve()

    # stop_session / mark_session_disconnected(status=...) 正确持久化 status="ended"
    sync_shared_session(ctx, session_id="s_tcl", disconnected=True, status="ended")
    after_disc = reg.list_remote_runs(exclude_owner_pid=None)
    assert len(after_disc) == 1
    assert after_disc[0]["alive"] is False
    assert after_disc[0]["status"] == "ended"


def test_allowed_roots_hardening_atomic_retry_cleanup_and_dir_cache(monkeypatch, tmp_path):
    from vivado_mcp import batch_jobs

    # 1. CODEX_HOME 单独存在时不误判为 Codex
    _clear_client_env(monkeypatch)
    monkeypatch.setattr("vivado_mcp.shared_registry._ancestor_process_names", lambda: ())
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    detect_client_source.cache_clear()
    try:
        assert detect_client_source() == "CLI"
    finally:
        detect_client_source.cache_clear()

    # 2. 原子写遇到 Windows 短暂共享冲突 PermissionError 时自动重试并清理 orphan .tmp
    attempts = 0
    real_replace = os.replace

    def flaky_replace(src, dst):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("transient sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    reg = SharedRegistry(registry_dir=tmp_path / "reg")
    reg.upsert_session("retry_s", mode="tcl", status="idle")
    assert attempts == 3
    assert list(reg.sessions_dir.glob("*.tmp")) == []
    monkeypatch.setattr(os, "replace", real_replace)

    # 3. allowed_report_roots 拒绝相对路径与中间 junction/symlink
    real_dir = tmp_path / "real_runs" / "impl_1"
    real_dir.mkdir(parents=True)
    nested_junction = tmp_path / "junction_parent" / "impl_1"
    nested_junction.mkdir(parents=True)

    monkeypatch.setattr(
        batch_jobs,
        "_is_link_or_junction",
        lambda p: p.name == "junction_parent",
    )

    (reg.sessions_dir / "888111_rel.json").write_text(
        json.dumps(
            {
                "entry_id": "888111_rel",
                "owner_pid": 888111,
                "log_path": "runme.log",
                "xpr_path": "rel.xpr",
                "run_dir": str(nested_junction),
                "updated_at": 1e12,
            }
        ),
        encoding="utf-8",
    )
    reg.sessions_dir.joinpath("888112_ok.json").write_text(
        json.dumps(
            {
                "entry_id": "888112_ok",
                "owner_pid": 888112,
                "run_dir": str(real_dir),
                "updated_at": 1e12,
            }
        ),
        encoding="utf-8",
    )
    roots = reg.allowed_report_roots()
    assert real_dir.resolve() in roots
    assert Path.cwd().resolve() not in roots
    assert nested_junction.resolve() not in roots

    # BatchJobRegistry.allowed_report_roots 同样拒绝相对路径
    breg = batch_jobs.BatchJobRegistry(jobs_dir=tmp_path / "jobs")
    breg._ensure_loaded()
    breg._jobs["rel_job"] = batch_jobs.BatchJob(
        job_id="rel_job",
        kind="batch_tcl",
        status="completed",
        started_at=1.0,
        output_dir="",
        log_path="vivado.log",
    )
    assert Path.cwd().resolve() not in breg.allowed_report_roots()

    # 4. cleanup_stale 不误删短暂 OSError 锁定文件，但清理畸形 JSON
    corrupt_f = reg.sessions_dir / "888113_bad.json"
    corrupt_f.write_text("{bad json", encoding="utf-8")
    locked_f = reg.sessions_dir / "888114_locked.json"
    locked_f.write_text(
        json.dumps({"entry_id": "888114_locked", "owner_pid": 888114, "status": "ended"}),
        encoding="utf-8",
    )
    orig_read_text = Path.read_text

    def guarded_read(self, *args, **kwargs):
        if self.name == "888114_locked.json":
            raise PermissionError("locked by writer")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    reg.cleanup_stale()
    assert not corrupt_f.exists()
    assert locked_f.exists()
    monkeypatch.setattr(Path, "read_text", orig_read_text)

    # 5. list_remote_runs 目录指纹未变时不重复 parse_directory_reports
    (reg.sessions_dir / "888112_ok.json").write_text(
        json.dumps(
            {
                "entry_id": "888112_ok",
                "owner_pid": 888112,
                "status": "running",
                "run_dir": str(real_dir),
            }
        ),
        encoding="utf-8",
    )
    (real_dir / "runme.log").write_text("Phase 1.1\n", encoding="utf-8")
    parse_calls = 0
    orig_parse = batch_jobs.parse_directory_reports

    def counting_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return orig_parse(*args, **kwargs)

    monkeypatch.setattr(batch_jobs, "parse_directory_reports", counting_parse)
    monkeypatch.setattr("vivado_mcp.shared_registry.is_pid_alive", lambda pid: pid == 888112)
    reg.list_remote_runs()
    reg.list_remote_runs()
    assert parse_calls == 1


async def test_app_lifespan_closes_shared_registry(monkeypatch):
    monkeypatch.setattr("vivado_mcp.server.find_vivado", lambda: "")
    async with app_lifespan(mcp) as app_ctx:
        app_ctx.shared_registry.upsert_session(
            "run_sess",
            mode="gui",
            run_name="impl_1",
            status="running",
            project_name="live_proj",
        )
        entries = app_ctx.shared_registry.list_remote_runs(exclude_owner_pid=None)
        assert entries[0]["status"] == "running"
    after = app_ctx.shared_registry.list_remote_runs(exclude_owner_pid=None)
    assert len(after) == 1
    assert after[0]["alive"] is False
    assert after[0]["status"] == "ended"
