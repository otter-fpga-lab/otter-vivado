"""Vivado 控制台 MCP App 服务端元数据、可见性、路径穿越防护与产物校验测试。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from mcp.server.mcpserver import Context

from vivado_mcp import batch_jobs
from vivado_mcp.server import AppContext, mcp
from vivado_mcp.tools.console_app_tools import (
    _MAX_STRUCTURED_BYTES,
    CONSOLE_APP_URI,
    _shrink_payload,
)
from vivado_mcp.vivado.session_manager import SessionManager

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "src" / "vivado_mcp" / "web" / "console.html"
FIXTURES = Path(__file__).parent / "fixtures"


def _make_context(tmp_path: Path) -> tuple[Context, AppContext]:
    app_ctx = AppContext(
        session_manager=SessionManager(vivado_path=""),
        batch_jobs=batch_jobs.BatchJobRegistry(jobs_dir=tmp_path / "jobs"),
    )
    req = SimpleNamespace(lifespan_context=app_ctx)
    return Context(request_context=req, mcp_server=mcp), app_ctx


async def test_console_tool_metadata_entrypoints_visibility_and_resource(monkeypatch, tmp_path):
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "open_vivado_console" in tools
    assert "vivado_console_state" in tools
    assert "vivado_console_action" in tools

    open_tool = tools["open_vivado_console"]
    assert open_tool.meta["ui"]["resourceUri"] == CONSOLE_APP_URI
    entrypoints = open_tool.meta["com.google.antigravity/ui"]["entrypoints"]
    assert {"type": "aux_pane", "title": "Vivado 控制台"} in entrypoints
    assert {"type": "global", "title": "Vivado 控制台"} in entrypoints
    # 点击侧栏时宿主以 {} 启动，不允许必填参数
    assert open_tool.input_schema.get("required", []) == []

    # App-only 工具必须对模型隐藏
    assert tools["vivado_console_state"].meta == {"ui": {"visibility": ["app"]}}
    assert tools["vivado_console_action"].meta == {"ui": {"visibility": ["app"]}}

    resources = {str(res.uri): res for res in await mcp.list_resources()}
    assert CONSOLE_APP_URI in resources
    res_meta = resources[CONSOLE_APP_URI]
    assert res_meta.mime_type == "text/html;profile=mcp-app"
    assert res_meta.meta == {"ui": {"prefersBorder": False}}

    contents = list(await mcp.read_resource(CONSOLE_APP_URI))
    assert contents[0].mime_type == "text/html;profile=mcp-app"
    assert "Vivado 控制台" in contents[0].content

    # 环境变量热覆盖路径生效
    override_html = tmp_path / "override.html"
    override_html.write_text("<html><body>override-console</body></html>", encoding="utf-8")
    monkeypatch.setenv("VIVADO_CONSOLE_APP_HTML", str(override_html))
    overridden = list(await mcp.read_resource(CONSOLE_APP_URI))
    assert "override-console" in overridden[0].content


async def test_open_vivado_console_and_state_payload_contract(tmp_path):
    ctx, app_ctx = _make_context(tmp_path)
    off_dir = tmp_path / "ooc_out"
    off_dir.mkdir()
    timing_text = (FIXTURES / "sample_report_timing.txt").read_text(encoding="utf-8")
    (off_dir / "timing.rpt").write_text(
        "| Design State : Synthesized\n" + timing_text,
        encoding="utf-8",
    )

    # 无参 {} 启动
    empty_res = await mcp.call_tool("open_vivado_console", {}, context=ctx)
    assert not empty_res.is_error
    sc = empty_res.structured_content
    assert set(sc) >= {
        "sessions",
        "run_monitors",
        "batch_jobs",
        "debug_sessions",
        "inspected_targets",
        "vivado_installs",
        "server_time",
    }
    assert len(json.dumps(sc, ensure_ascii=False).encode("utf-8")) < 256 * 1024

    # 传入 inspect_path
    inspected_res = await mcp.call_tool(
        "open_vivado_console", {"inspect_path": str(off_dir)}, context=ctx
    )
    assert not inspected_res.is_error
    assert len(inspected_res.structured_content["inspected_targets"]) == 1
    assert inspected_res.structured_content["inspected_targets"][0]["timing"]["wns"] == 0.234

    # state 工具不附带大报告正文
    state_res = await mcp.call_tool("vivado_console_state", {}, context=ctx)
    assert not state_res.is_error
    assert len(state_res.structured_content["inspected_targets"]) == 1
    await app_ctx.batch_jobs.close()


async def test_console_action_read_report_and_path_traversal_guards(tmp_path):
    ctx, app_ctx = _make_context(tmp_path)
    allowed_dir = tmp_path / "allowed_ooc"
    allowed_dir.mkdir()
    rpt = allowed_dir / "timing_summary.rpt"
    rpt.write_text("\n".join(f"line {i}" for i in range(1, 21)), encoding="utf-8")
    log = allowed_dir / "vivado.log"
    log.write_text("\n".join(f"log line {i}" for i in range(1, 50)), encoding="utf-8")
    disallowed_ext = allowed_dir / "secret.json"
    disallowed_ext.write_text('{"token": "abc"}', encoding="utf-8")

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_log = outside_dir / "secret.log"
    outside_log.write_text("TOP SECRET", encoding="utf-8")

    # 1. 注册已检视目录
    insp_res = await mcp.call_tool(
        "vivado_console_action",
        {"action": "inspect_path", "path": str(allowed_dir)},
        context=ctx,
    )
    assert not insp_res.is_error

    # 2. 白名单内读取 .rpt（从头读）和 .log（从尾读）
    rpt_res = await mcp.call_tool(
        "vivado_console_action",
        {"action": "read_report", "report_path": str(rpt), "max_lines": 5},
        context=ctx,
    )
    assert not rpt_res.is_error
    rpt_data = rpt_res.structured_content["report"]
    assert rpt_data["total_lines"] == 20
    assert rpt_data["start_line"] == 1
    assert rpt_data["truncated"] is True
    assert rpt_data["lines"][0]["text"] == "line 1"

    log_res = await mcp.call_tool(
        "vivado_console_action",
        {"action": "read_report", "report_path": str(log), "max_lines": 5},
        context=ctx,
    )
    assert not log_res.is_error
    log_data = log_res.structured_content["report"]
    assert log_data["tail"] is True
    assert log_data["lines"][-1]["text"] == "log line 49"

    # 3. 路径穿越 .. 拦截
    traversal = allowed_dir / ".." / "outside" / "secret.log"
    res_trav = await mcp.call_tool(
        "vivado_console_action",
        {"action": "read_report", "report_path": str(traversal)},
        context=ctx,
    )
    assert res_trav.is_error

    # 4. 越界绝对路径拦截
    res_out = await mcp.call_tool(
        "vivado_console_action",
        {"action": "read_report", "report_path": str(outside_log)},
        context=ctx,
    )
    assert res_out.is_error

    # 5. 非报告扩展名拦截
    res_ext = await mcp.call_tool(
        "vivado_console_action",
        {"action": "read_report", "report_path": str(disallowed_ext)},
        context=ctx,
    )
    assert res_ext.is_error

    # 6. 符号链接逃逸拦截
    sym = allowed_dir / "escape.log"
    try:
        sym.symlink_to(outside_log)
    except OSError:
        sym = None
    if sym is not None:
        res_sym = await mcp.call_tool(
            "vivado_console_action",
            {"action": "read_report", "report_path": str(sym)},
            context=ctx,
        )
        assert res_sym.is_error

    # 7. remove_inspected
    rem_res = await mcp.call_tool(
        "vivado_console_action",
        {"action": "remove_inspected", "path": str(allowed_dir)},
        context=ctx,
    )
    assert not rem_res.is_error
    assert rem_res.structured_content["removed"] is True
    assert rem_res.structured_content["state"]["inspected_targets"] == []
    await app_ctx.batch_jobs.close()


def test_built_console_html_is_self_contained_csp_safe_and_under_budget():
    assert BUNDLE_PATH.is_file(), f"缺少构建产物: {BUNDLE_PATH}"
    raw = BUNDLE_PATH.read_bytes()
    assert 0 < len(raw) < 500 * 1024, f"console.html 超出 500 KB 上限: {len(raw)} bytes"

    html = raw.decode("utf-8")
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "<style>" in html and "<script>" in html
    assert "ui/initialize" in html and "ui/notifications/initialized" in html
    assert "Vivado 控制台" in html
    assert "<form" not in html.lower()
    assert "--app-font-sans" in html
    assert ".nav-card-meta span:last-child" in html
    assert "reportReqId" in html
    assert "preventScroll" in html
    assert 'freshness==="stale"' in html or 'freshness === "stale"' in html

    # CSP 严格校验：脚本外零外链 URL、无外链资源标签
    non_script = re.sub(r"<script>[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    assert not re.search(r"https?://", non_script, flags=re.IGNORECASE)
    assert not re.search(
        r"<(?:script|link|img|iframe|source|video|audio)\b[^>]*(?:src|href)\s*=",
        non_script,
        flags=re.IGNORECASE,
    )

    node = shutil.which("node")
    if node:
        match = re.search(r"<script>([\s\S]*?)</script>", html, flags=re.IGNORECASE)
        assert match is not None
        check = subprocess.run(
            [
                node,
                "-e",
                "const vm=require('vm'),fs=require('fs');"
                "const h=fs.readFileSync(process.argv[1],'utf8');"
                "const m=h.match(/<script>([\\s\\S]*?)<\\/script>/i);"
                "new vm.Script(m[1],{filename:'console.html'});",
                str(BUNDLE_PATH),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert check.returncode == 0, check.stderr


def test_clear_finished_keeps_running_jobs_only(tmp_path):
    reg = batch_jobs.BatchJobRegistry(jobs_dir=tmp_path / "jobs")
    j_done = batch_jobs.BatchJob(
        job_id="done_1", kind="ooc_synth", status="completed", started_at=1.0
    )
    j_run = batch_jobs.BatchJob(
        job_id="run_1", kind="batch_tcl", status="running", started_at=2.0
    )
    reg._ensure_loaded()
    reg._jobs = {"done_1": j_done, "run_1": j_run}
    reg._processes["run_1"] = SimpleNamespace(returncode=None)  # type: ignore[assignment]
    reg._persist_job(j_done)
    reg._persist_job(j_run)
    cleared = reg.clear_finished()
    assert cleared == 1
    assert set(reg._jobs) == {"run_1"}
    assert not (reg.jobs_dir / "done_1.json").exists()
    assert (reg.jobs_dir / "run_1.json").exists()


async def test_console_strips_monitor_report_text_but_allows_guarded_read(tmp_path):
    ctx, app_ctx = _make_context(tmp_path)
    run_dir = tmp_path / "proj.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    rpt = run_dir / "impl_timing.rpt"
    rpt.write_text("Design Timing Summary\nWNS(ns) 0.500\n", encoding="utf-8")

    class _FakeSession:
        session_id = "default"
        mode = "gui"
        is_alive = True
        state = "stopped"

    mon = app_ctx.run_monitors.get(_FakeSession(), "impl_1", "route_design")
    # 停掉后台循环并注入带 raw text 的报告快照
    await mon.close()
    mon._value["connection"] = "connected"
    mon._value["run"] = {"directory": str(run_dir), "state": "completed"}
    mon._value["reports"] = [
        {
            "name": "impl_timing.rpt",
            "path": str(rpt),
            "stage": "post-route",
            "freshness": "current",
            "text": "BIG RAW REPORT BODY THAT SHOULD BE STRIPPED",
        }
    ]

    state_res = await mcp.call_tool("vivado_console_state", {}, context=ctx)
    assert not state_res.is_error
    monitors = state_res.structured_content["run_monitors"]
    assert len(monitors) == 1
    assert "text" not in monitors[0]["reports"][0]

    # 点击报告芯片后可经受限动作按需读取
    read_res = await mcp.call_tool(
        "vivado_console_action",
        {"action": "read_report", "report_path": str(rpt)},
        context=ctx,
    )
    assert not read_res.is_error
    assert read_res.structured_content["report"]["lines"][1]["text"] == "WNS(ns) 0.500"
    await app_ctx.batch_jobs.close()


def test_shrink_payload_enforces_hard_byte_budget():
    huge_issue = {"severity": "ERROR", "id": "X-1", "message": "M" * 4000, "files": ["a.log"]}
    state = {
        "sessions": [{"session_id": f"s{i}", "note": "N" * 2000} for i in range(20)],
        "run_monitors": [
            {
                "run": {"name": f"r{i}", "tail": [{"text": "T" * 1000} for _ in range(30)]},
                "reports": [
                    {
                        "name": f"rep{j}.rpt",
                        "summary": {"resources": [{"name": "LUT"}] * 40},
                    }
                    for j in range(10)
                ],
            }
            for i in range(10)
        ],
        "batch_jobs": [
            {
                "job_id": f"j{i}",
                "log_tail": [{"text": "L" * 500} for _ in range(40)],
                "phases": [{"text": "P" * 200} for _ in range(40)],
                "reports": [{"name": f"f{j}.rpt"} for j in range(50)],
                "diagnostics": {
                    "top_issues": [dict(huge_issue) for _ in range(20)],
                    "all_issues": [dict(huge_issue) for _ in range(50)],
                },
            }
            for i in range(20)
        ],
        "inspected_targets": [],
        "debug_sessions": [],
        "vivado_installs": [],
        "server_time": 1234.5,
    }
    shrunk = _shrink_payload(state, limit_bytes=_MAX_STRUCTURED_BYTES)
    encoded = json.dumps(shrunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= _MAX_STRUCTURED_BYTES


async def test_read_report_enforces_response_byte_budget_and_utf8_tail(tmp_path):
    ctx, app_ctx = _make_context(tmp_path)
    rpt_dir = tmp_path / "big_logs"
    rpt_dir.mkdir()
    big_log = rpt_dir / "huge.log"
    # 写入大于 350 KiB 的 UTF-8 中文日志行，触发响应字节预算裁剪
    line = ("中文诊断信息：" + "A" * 500 + "\n").encode("utf-8")
    big_log.write_bytes(line * 800)

    insp = await mcp.call_tool(
        "vivado_console_action",
        {"action": "inspect_path", "path": str(rpt_dir)},
        context=ctx,
    )
    assert not insp.is_error

    res = await mcp.call_tool(
        "vivado_console_action",
        {"action": "read_report", "report_path": str(big_log), "max_lines": 1500},
        context=ctx,
    )
    assert not res.is_error
    report = res.structured_content["report"]
    assert report["truncated"] is True
    assert report["tail"] is True
    encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= 256 * 1024
    assert "中文诊断信息" in report["lines"][-1]["text"]
    assert "\ufffd" not in report["lines"][-1]["text"]
    await app_ctx.batch_jobs.close()
