"""Vivado 控制台 MCP App 服务端元数据、可见性、路径穿越防护与产物校验测试。"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from mcp.server.mcpserver import Context

from vivado_mcp import batch_jobs
from vivado_mcp.console_http import ConsoleHttpServer, _inject_csp_nonce
from vivado_mcp.server import AppContext, app_lifespan, mcp
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


async def test_open_vivado_console_and_state_payload_contract(monkeypatch, tmp_path):
    ctx, app_ctx = _make_context(tmp_path)
    off_dir = tmp_path / "ooc_out"
    off_dir.mkdir()
    timing_text = (FIXTURES / "sample_report_timing.txt").read_text(encoding="utf-8")
    (off_dir / "timing.rpt").write_text(
        "| Design State : Synthesized\n" + timing_text,
        encoding="utf-8",
    )

    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened_urls.append(u) or True)

    try:
        # 无参 {} 启动（open_browser=False）：静默启动 127.0.0.1 服务且不弹出浏览器
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
            "local_web",
            "url",
            "server_time",
        }
        lw = sc["local_web"]
        assert lw["running"] is True
        assert lw["url"].startswith("http://127.0.0.1:")
        assert lw["host_port"].startswith("127.0.0.1:")
        assert sc["url"] == lw["url"]
        assert lw["url"] in empty_res.content[0].text
        assert opened_urls == []
        assert app_ctx.console_http is not None
        assert len(json.dumps(sc, ensure_ascii=False).encode("utf-8")) < 256 * 1024

        # 传入 inspect_path，复用同一 127.0.0.1 URL
        inspected_res = await mcp.call_tool(
            "open_vivado_console", {"inspect_path": str(off_dir)}, context=ctx
        )
        assert not inspected_res.is_error
        assert len(inspected_res.structured_content["inspected_targets"]) == 1
        assert inspected_res.structured_content["inspected_targets"][0]["timing"]["wns"] == 0.234
        assert inspected_res.structured_content["local_web"]["url"] == lw["url"]
        assert opened_urls == []

        # state 工具不附带大报告正文，且保留 local_web
        state_res = await mcp.call_tool("vivado_console_state", {}, context=ctx)
        assert not state_res.is_error
        assert len(state_res.structured_content["inspected_targets"]) == 1
        assert state_res.structured_content["local_web"] == lw
    finally:
        if app_ctx.console_http is not None:
            await asyncio.to_thread(app_ctx.console_http.close)
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
    assert "var(--font-sans)" not in html
    assert ".nav-card-meta span:last-child" in html
    assert "reportReqId" in html
    assert "preventScroll" in html
    assert 'freshness==="stale"' in html or 'freshness === "stale"' in html
    for theme_id in (
        "macos",
        "macos-light",
        "graphite",
        "geist",
        "tokyo-night",
        "catppuccin",
        "nord",
    ):
        assert theme_id in html
    assert "/api/state" in html and "/api/action" in html
    assert "Local Web" in html and "MCP App" in html
    assert "sb-local-wrap" in html
    assert "本机服务地址 (127.0.0.1)" in html
    assert "open_local_browser" in html and "ensure_local_web" in html

    # CSP 严格校验：脚本外零外链 URL、无外链资源标签、无内联 style 属性
    non_script = re.sub(r"<script>[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    assert not re.search(r"https?://", non_script, flags=re.IGNORECASE)
    assert "style=" not in non_script.lower()
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


def _http_request(
    url: str,
    *,
    path: str | None = None,
    method: str = "GET",
    body=None,
    headers=None,
):
    parsed = urlsplit(url)
    conn = HTTPConnection(parsed.hostname, parsed.port, timeout=4)
    try:
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        conn.request(
            method,
            target if path is None else path,
            body=body,
            headers=headers or {},
        )
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


async def test_console_http_serves_page_state_actions_and_enforces_security_guards(tmp_path):
    app_ctx = AppContext(
        session_manager=SessionManager(vivado_path=""),
        batch_jobs=batch_jobs.BatchJobRegistry(jobs_dir=tmp_path / "jobs"),
    )
    allowed_dir = tmp_path / "ooc_http"
    allowed_dir.mkdir()
    timing_text = (FIXTURES / "sample_report_timing.txt").read_text(encoding="utf-8")
    rpt = allowed_dir / "timing.rpt"
    rpt.write_text("| Design State : Synthesized\n" + timing_text, encoding="utf-8")
    outside = tmp_path / "outside.log"
    outside.write_text("SECRET", encoding="utf-8")

    server = ConsoleHttpServer(app_ctx, loop=asyncio.get_running_loop())
    url = await asyncio.to_thread(server.start)
    parsed = urlsplit(url)
    origin = f"http://{parsed.netloc}"
    try:
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port > 0
        assert parsed.path == "/"
        assert await asyncio.to_thread(server.start) == url

        # 1. GET / 返回含 nonce CSP 的控制台页面，无 CORS 放行
        status, headers, page_bytes = await asyncio.to_thread(_http_request, url)
        assert status == 200
        page = page_bytes.decode("utf-8")
        assert "Vivado 控制台" in page
        nonce = re.search(r'<script nonce="([A-Za-z0-9_-]+)">', page).group(1)
        assert f'<style nonce="{nonce}">' in page
        policy = headers["Content-Security-Policy"]
        assert f"script-src 'nonce-{nonce}'" in policy
        assert f"style-src 'nonce-{nonce}'" in policy
        assert "connect-src 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "unsafe-inline" not in policy
        assert "Access-Control-Allow-Origin" not in headers
        assert headers["Cache-Control"] == "no-store"

        # 2. GET /api/state 返回与 vivado_console_state 同构的状态
        status, _, state_bytes = await asyncio.to_thread(_http_request, url, path="/api/state")
        assert status == 200
        state_data = json.loads(state_bytes)
        assert set(state_data) >= {
            "sessions",
            "run_monitors",
            "batch_jobs",
            "debug_sessions",
            "inspected_targets",
            "vivado_installs",
            "server_time",
        }

        # 3. POST /api/action 支持 inspect_path / read_report / remove_inspected
        post_headers = {"Origin": origin, "Content-Type": "application/json"}
        status, _, insp_bytes = await asyncio.to_thread(
            _http_request,
            url,
            path="/api/action",
            method="POST",
            body=json.dumps({"action": "inspect_path", "path": str(allowed_dir)}),
            headers=post_headers,
        )
        assert status == 200
        insp_data = json.loads(insp_bytes)
        assert insp_data["action"] == "inspect_path"
        assert insp_data["state"]["inspected_targets"][0]["timing"]["wns"] == 0.234

        status, _, rpt_bytes = await asyncio.to_thread(
            _http_request,
            url,
            path="/api/action",
            method="POST",
            body=json.dumps(
                {"action": "read_report", "report_path": str(rpt), "max_lines": 5}
            ),
            headers=post_headers,
        )
        assert status == 200
        assert json.loads(rpt_bytes)["report"]["name"] == "timing.rpt"

        # 越界读取拦截
        status, _, _ = await asyncio.to_thread(
            _http_request,
            url,
            path="/api/action",
            method="POST",
            body=json.dumps({"action": "read_report", "report_path": str(outside)}),
            headers=post_headers,
        )
        assert status == 400

        status, _, rem_bytes = await asyncio.to_thread(
            _http_request,
            url,
            path="/api/action",
            method="POST",
            body=json.dumps({"action": "remove_inspected", "path": str(allowed_dir)}),
            headers=post_headers,
        )
        assert status == 200
        assert json.loads(rem_bytes)["removed"] is True

        # 4. Host / Origin / Sec-Fetch-Site / 写请求缺失 Origin 全部拒绝 403
        for bad_headers in (
            {"Host": "attacker.example"},
            {"Host": "localhost"},
            {"Origin": "https://attacker.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            s_get, _, _ = await asyncio.to_thread(
                _http_request, url, path="/api/state", headers=bad_headers
            )
            assert s_get == 403

        # POST 必须带同源 Origin
        s_no_origin, _, _ = await asyncio.to_thread(
            _http_request,
            url,
            path="/api/action",
            method="POST",
            body=json.dumps({"action": "clear_finished"}),
            headers={"Content-Type": "application/json"},
        )
        assert s_no_origin == 403

        # 5. 非法 JSON / 重复键 / 未知字段 / 未知路由 / 非法方法拦截
        for bad_body in (
            b"{",
            b'{"action":"clear_finished","action":"clear_finished"}',
            b'{"action":"clear_finished","extra":1}',
            b'{"action":"run_tcl"}',
        ):
            s_bad, _, _ = await asyncio.to_thread(
                _http_request,
                url,
                path="/api/action",
                method="POST",
                body=bad_body,
                headers=post_headers,
            )
            assert s_bad == 400

        s_415, _, _ = await asyncio.to_thread(
            _http_request,
            url,
            path="/api/action",
            method="POST",
            body=b"{}",
            headers={"Origin": origin, "Content-Type": "text/plain"},
        )
        assert s_415 == 415

        assert (await asyncio.to_thread(_http_request, url, path="/etc/passwd"))[0] == 404
        fav_status, _, fav_body = await asyncio.to_thread(
            _http_request, url, path="/favicon.ico"
        )
        assert fav_status == 204
        assert fav_body == b""
        assert (
            await asyncio.to_thread(_http_request, url, path="/api/state", method="DELETE")
        )[0] == 405

        sample_html = (
            "<html><head><!-- CONSOLE_STYLE --><style>body{}</style></head>"
            "<body><!-- CONSOLE_SCRIPT --><script>const s='<script>';</script></body></html>"
        )
        injected = _inject_csp_nonce(sample_html, "abc123")
        assert '<style nonce="abc123">body{}</style>' in injected
        assert '<script nonce="abc123">const s=\'<script>\';</script>' in injected

        with pytest.raises(ValueError):
            ConsoleHttpServer(app_ctx, host="0.0.0.0")
    finally:
        await asyncio.to_thread(server.close)
        await app_ctx.batch_jobs.close()


async def test_open_vivado_console_open_browser_and_lifespan_teardown(monkeypatch, tmp_path):
    ctx, app_ctx = _make_context(tmp_path)
    off_dir = tmp_path / "codex_ooc"
    off_dir.mkdir()
    (off_dir / "vivado.log").write_text("Vivado v2024.2\nFinished synth_design\n", encoding="utf-8")

    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened_urls.append(u) or True)

    try:
        # 1. open_browser=False 启动 127.0.0.1 监听，但不触发 webbrowser.open
        silent_res = await mcp.call_tool(
            "open_vivado_console",
            {"open_browser": False},
            context=ctx,
        )
        assert not silent_res.is_error
        silent_sc = silent_res.structured_content
        url = silent_sc.get("url", "")
        assert url.startswith("http://127.0.0.1:")
        assert silent_sc["local_web"]["running"] is True
        assert silent_sc["local_web"]["url"] == url
        assert opened_urls == []

        # 2. open_browser=True 复用同一 URL 并调用 webbrowser.open
        res = await mcp.call_tool(
            "open_vivado_console",
            {"inspect_path": str(off_dir), "open_browser": True},
            context=ctx,
        )
        assert not res.is_error
        sc = res.structured_content
        assert sc.get("url") == url
        assert sc["local_web"]["url"] == url
        assert url in res.content[0].text
        assert opened_urls == [url]
        assert app_ctx.console_http is not None

        status, _, state_bytes = await asyncio.to_thread(_http_request, url, path="/api/state")
        assert status == 200
        state_json = json.loads(state_bytes)
        assert state_json["selected_inspected_path"] == str(off_dir.resolve())
        assert state_json["selection_seq"] == 1
        assert state_json["local_web"]["url"] == url
    finally:
        if app_ctx.console_http is not None:
            await asyncio.to_thread(app_ctx.console_http.close)
        await app_ctx.batch_jobs.close()


async def test_console_actions_ensure_local_web_and_open_local_browser(monkeypatch, tmp_path):
    ctx, app_ctx = _make_context(tmp_path)
    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened_urls.append(u) or True)

    try:
        ensure_res = await mcp.call_tool(
            "vivado_console_action",
            {"action": "ensure_local_web"},
            context=ctx,
        )
        assert not ensure_res.is_error
        ensure_sc = ensure_res.structured_content
        url = ensure_sc["url"]
        assert url.startswith("http://127.0.0.1:")
        assert ensure_sc["local_web"]["running"] is True
        assert opened_urls == []

        open_res = await mcp.call_tool(
            "vivado_console_action",
            {"action": "open_local_browser"},
            context=ctx,
        )
        assert not open_res.is_error
        assert open_res.structured_content["url"] == url
        assert opened_urls == [url]
    finally:
        if app_ctx.console_http is not None:
            await asyncio.to_thread(app_ctx.console_http.close)
        await app_ctx.batch_jobs.close()


async def test_console_state_preserves_monitor_and_debug_loopback_urls(tmp_path):
    from vivado_mcp.debug_service import DebugService

    ctx, app_ctx = _make_context(tmp_path)

    class _FakeSession:
        session_id = "default"
        mode = "gui"
        is_alive = True
        state = "stopped"

    fake_session = _FakeSession()
    mon = app_ctx.run_monitors.get(fake_session, "impl_1", "route_design")
    await mon.close()
    mon_url = mon.open_view()

    dbg = DebugService(fake_session, backend=SimpleNamespace())
    app_ctx.debug_services._services["default"] = dbg
    dbg_url = dbg.open_view()

    try:
        state_res = await mcp.call_tool("vivado_console_state", {}, context=ctx)
        assert not state_res.is_error
        sc = state_res.structured_content
        assert sc["run_monitors"][0]["url"] == mon_url
        assert sc["debug_sessions"][0]["url"] == dbg_url
        # 底层快照本身不注入 url，保持 MonitorHTTP/DebugHTTP 契约不变
        assert "url" not in mon.snapshot()
        assert "url" not in dbg.snapshot()
    finally:
        if mon._http is not None:
            await asyncio.to_thread(mon._http.close)
            mon._http = None
        await dbg.close()
        await app_ctx.batch_jobs.close()


async def test_app_lifespan_closes_console_http_on_exit(monkeypatch, tmp_path):
    monkeypatch.setattr("vivado_mcp.server.find_vivado", lambda: "")
    async with app_lifespan(mcp) as app_ctx:
        srv = ConsoleHttpServer(app_ctx, loop=asyncio.get_running_loop())
        url = await asyncio.to_thread(srv.start)
        app_ctx.console_http = srv
        assert (await asyncio.to_thread(_http_request, url))[0] == 200
    assert app_ctx.console_http is None
    assert srv.url == ""


def test_console_cli_subcommand_help():
    res = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "vivado_mcp", "console", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode == 0
    assert "--inspect" in res.stdout
    assert "--no-browser" in res.stdout
    assert "--port" in res.stdout


def test_browser_console_http_themes_and_responsive_layout(tmp_path):
    """可选真实浏览器验证：7 主题切换、严格 CSP 零报错、Local Web 模式与 460px 窄栏。"""
    playwright = pytest.importorskip("playwright.sync_api")
    executable = shutil.which("chromium") or shutil.which("chromium-browser")
    if executable is None:
        pytest.skip("未安装 Chromium；HTTP 边界与构造测试仍独立运行")

    app_ctx = AppContext(
        session_manager=SessionManager(vivado_path=""),
        batch_jobs=batch_jobs.BatchJobRegistry(jobs_dir=tmp_path / "jobs"),
    )
    off_dir = tmp_path / "pw_ooc"
    off_dir.mkdir()
    timing_text = (FIXTURES / "sample_report_timing.txt").read_text(encoding="utf-8")
    util_text = (FIXTURES / "sample_report_utilization.txt").read_text(encoding="utf-8")
    (off_dir / "timing.rpt").write_text(
        "| Design State : Synthesized\n" + timing_text, encoding="utf-8"
    )
    (off_dir / "utilization.rpt").write_text(util_text, encoding="utf-8")
    (off_dir / "vivado.log").write_text(
        "Vivado v2024.2\nPhase 1.1 Placer Initialization\nERROR: [Synth 8-123] demo err\n",
        encoding="utf-8",
    )

    server = ConsoleHttpServer(app_ctx)
    url = server.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                executable_path=executable, headless=True, args=["--no-sandbox"]
            )
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 840})
                errors: list[str] = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                page.on(
                    "console",
                    lambda msg: errors.append(msg.text) if msg.type == "error" else None,
                )

                page.goto(url)
                playwright.expect(page.locator("#sb-transport")).to_have_text("Local Web")
                assert page.locator("#hero-ctx-btn").count() == 0
                assert page.locator("#hero-ask-btn").count() == 0

                page.locator("#inspect-input").fill(str(off_dir))
                page.locator("#btn-inspect").click()
                playwright.expect(page.locator("#pane-title")).not_to_be_empty()
                playwright.expect(page.locator("#tab-panel-overview")).to_contain_text("0.234")

                # 测试 7 主题菜单切换
                page.locator("#btn-theme").click()
                page.locator('[data-theme-id="catppuccin"]').click()
                playwright.expect(page.locator("html")).to_have_attribute(
                    "data-theme", "catppuccin"
                )
                page.locator("#btn-theme").click()
                page.locator('[data-theme-id="macos-light"]').click()
                playwright.expect(page.locator("html")).to_have_attribute(
                    "data-theme", "macos-light"
                )

                # 切换标签页并读取报告
                page.get_by_role("tab", name="告警").click()
                playwright.expect(
                    page.locator("#tab-panel-diagnostics")
                ).to_contain_text("Synth 8-123")
                page.get_by_role("tab", name="报告").click()
                page.locator(".rpt-chip", has_text="timing.rpt").click()
                playwright.expect(page.locator(".terminal-lines")).to_contain_text("WNS(ns)")

                # 窄屏 460x840 零横向溢出
                page.set_viewport_size({"width": 460, "height": 840})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert errors == []
            finally:
                browser.close()
    finally:
        server.close()
        asyncio.run(app_ctx.batch_jobs.close())
