"""本机展示入口的安全边界、只读行为和页面数据隔离测试。"""

from __future__ import annotations

import json
import re
import shutil
import time
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from vivado_mcp.analysis.timing_parser import parse_timing_summary
from vivado_mcp.analysis.util_parser import parse_utilization
from vivado_mcp.monitor_http import MonitorHTTP


def _request(url, *, path=None, method="GET", headers=None):
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection.request(method, target if path is None else path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.fixture
def monitor():
    snapshot = {"source": "live", "run": {"status": "route_design Running", "progress": "50%"}}
    callback = Mock(return_value=snapshot)
    server = MonitorHTTP(callback)
    url = server.start()
    try:
        yield server, url, callback, snapshot
    finally:
        server.close()


def test_page_is_loopback_tokenized_utf8_and_does_not_query_cache(monitor):
    server, url, callback, _ = monitor
    parsed = urlsplit(url)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port > 0
    assert re.fullmatch(r"/[A-Za-z0-9_-]{43}/", parsed.path)
    assert server.start() == url
    status, headers, body = _request(url)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "运行观察" in body.decode("utf-8")
    assert "route_design Running" not in body.decode("utf-8")
    callback.assert_not_called()


def test_snapshot_only_calls_cached_snapshot_provider(monitor):
    _, url, callback, snapshot = monitor
    status, headers, body = _request(url + "snapshot.json")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body) == snapshot
    callback.assert_called_once_with()


@pytest.mark.parametrize("path", ["/", "/snapshot.json", "/etc/passwd", "/favicon.ico"])
def test_unscoped_paths_are_not_served(monitor, path):
    _, url, callback, _ = monitor
    status, headers, _ = _request(url, path=path)
    assert status == 404
    assert headers["Cache-Control"] == "no-store"
    callback.assert_not_called()


@pytest.mark.parametrize(
    "suffix", ["../pyproject.toml", "%2e%2e/README.md", "snapshot.json?path=/etc/passwd", "run.tcl"]
)
def test_token_does_not_authorize_arbitrary_paths_or_queries(monitor, suffix):
    _, url, callback, _ = monitor
    status, _, _ = _request(url + suffix)
    assert status == 404
    callback.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.example"},
        {"Host": "localhost"},
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_foreign_host_or_origin_is_rejected_before_cache_access(monitor, headers):
    _, url, callback, _ = monitor
    status, response_headers, _ = _request(url + "snapshot.json", headers=headers)
    assert status == 403
    assert "Access-Control-Allow-Origin" not in response_headers
    assert response_headers["Cache-Control"] == "no-store"
    callback.assert_not_called()


def test_matching_origin_is_allowed(monitor):
    _, url, callback, _ = monitor
    parsed = urlsplit(url)
    status, _, _ = _request(url + "snapshot.json", headers={"Origin": f"http://{parsed.netloc}"})
    assert status == 200
    callback.assert_called_once_with()


@pytest.mark.parametrize("field", ["Host", "Origin"])
def test_duplicate_security_headers_are_rejected(monitor, field):
    _, url, callback, _ = monitor
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        connection.putrequest("GET", parsed.path + "snapshot.json", skip_host=True)
        connection.putheader("Host", parsed.netloc)
        if field == "Host":
            connection.putheader("Host", parsed.netloc)
        else:
            connection.putheader("Origin", f"http://{parsed.netloc}")
            connection.putheader("Origin", f"http://{parsed.netloc}")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()
    callback.assert_not_called()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"])
def test_no_write_or_execution_methods(monitor, method):
    _, url, callback, _ = monitor
    status, headers, _ = _request(url + "snapshot.json", method=method)
    assert status == 405
    assert headers["Cache-Control"] == "no-store"
    callback.assert_not_called()


def test_callback_errors_are_redacted_and_not_logged(monitor, capsys):
    _, url, callback, _ = monitor
    callback.side_effect = RuntimeError("PRIVATE_PROJECT_AND_TOKEN")
    status, headers, body = _request(url + "snapshot.json")
    assert status == 503
    assert json.loads(body) == {"error": "Snapshot unavailable"}
    assert "PRIVATE" not in body.decode()
    assert headers["Cache-Control"] == "no-store"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_unsafe_snapshot_values_are_not_emitted_as_invalid_json(monitor):
    _, url, callback, _ = monitor
    callback.return_value = {"progress": float("nan")}
    status, _, _ = _request(url + "snapshot.json")
    assert status == 503


def test_page_uses_csp_and_text_sinks_for_untrusted_report_data(monitor):
    _, url, callback, snapshot = monitor
    malicious = '</script><img src=x onerror="window.pwned=1">'
    snapshot["run"]["status"] = malicious
    snapshot["reports"] = [{"name": malicious, "text": malicious}]
    _, headers, page_bytes = _request(url)
    page = page_bytes.decode("utf-8")
    nonce = re.search(r'<script nonce="([A-Za-z0-9_-]+)">', page).group(1)
    policy = headers["Content-Security-Policy"]
    assert f"script-src 'nonce-{nonce}'" in policy
    assert f"style-src 'nonce-{nonce}'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "default-src 'none'" in policy
    assert "unsafe-inline" not in policy
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "Access-Control-Allow-Origin" not in headers
    assert malicious not in page
    for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert unsafe_sink not in page
    assert "textContent" in page
    callback.assert_not_called()
    _, _, body = _request(url + "snapshot.json")
    assert json.loads(body)["reports"][0]["text"] == malicious


def test_closing_display_does_not_touch_snapshot_or_end_backend(monitor):
    server, url, callback, _ = monitor
    server.close()
    server.close()
    callback.assert_not_called()
    next_url = server.start()
    assert urlsplit(url).path != urlsplit(next_url).path
    status, _, _ = _request(next_url)
    assert status == 200
    callback.assert_not_called()


def test_browser_renders_untrusted_text_and_preserves_last_snapshot(monitor, tmp_path):
    """可选真实浏览器验证；没有 Playwright/Chromium 时明确跳过。"""
    playwright = pytest.importorskip("playwright.sync_api")
    executable = shutil.which("chromium") or shutil.which("chromium-browser")
    if executable is None:
        pytest.skip("未安装 Chromium；HTTP 边界测试仍独立运行")
    server, url, _, snapshot = monitor
    malicious = '<img src=x onerror="window.pwned=1"> 中文报告'
    snapshot.update(
        {
            "source": "replay",
            "connection": "connected",
            "session_id": "demo",
            "session_mode": "gui",
            "run_name": "impl_1",
            "target_step": "write_bitstream",
            "observed_at": time.time(),
            "last_attempt_at": time.time(),
            "quality": {"timing": "unknown", "resources": "unknown"},
            "reports": [
                {
                    "name": "timing_routed.rpt",
                    "path": "/demo/timing_routed.rpt",
                    "freshness": "unverified",
                    "stage": "post-route",
                    "text": malicious,
                    "reason": "尚未证明目标与约束匹配",
                    "mtime": time.time(),
                    "size": 400,
                }
            ],
        }
    )
    snapshot["run"].update(
        {
            "state": "running",
            "progress_percent": 50,
            "project": "示例工程 telemetry",
            "version": "Vivado 示例版本",
            "elapsed": "00:08:23",
            "log_offset": 120000,
            "current_phase": "Phase 4.1 Global Iteration",
            "log_mtime": time.time() - 3,
            "tail": [{"lineno": 1, "text": malicious}],
            "phases": [{"lineno": 1, "text": "Starting route_design"}],
            "diagnostics": {
                "errors": 0,
                "warnings": 2,
                "critical_warnings": 1,
                "scope": "仅当前日志尾部；0 不代表全程无警告/错误",
            },
        }
    )
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1150})
            page_errors, console_errors = [], []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.goto(url)
            playwright.expect(page.locator("#state")).to_have_text("运行中")
            assert page.locator("#progress").is_visible()
            assert page.locator("#progress").get_attribute("value") == "50"
            assert page.locator("#source").inner_text() == "回放数据 · 非现场"
            assert page.locator("#report-text").inner_text() == malicious
            assert page.locator("#report-text img").count() == 0
            assert page.evaluate("window.pwned") is None
            assert "相对行号" in page.locator("#log-window").inner_text()
            page.screenshot(path=str(tmp_path / "monitor-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(tmp_path / "monitor-mobile.png"), full_page=True)
            snapshot["run"].update(
                {
                    "progress_percent": None,
                    "progress": "",
                    "state": "completed",
                    "status": "write_bitstream Complete!",
                }
            )
            snapshot["connection"] = "busy"
            playwright.expect(page.locator("#connection")).to_have_text("会话忙碌", timeout=6000)
            assert not page.locator("#progress").is_visible()
            assert page.locator("#timing").inner_text() == "未知"
            assert page.locator("#resources").inner_text() == "未知"
            assert page_errors == []
            assert console_errors == []
            server.close()
            playwright.expect(page.locator("#connection")).to_have_text(
                "展示连接失联", timeout=12000
            )
            assert page.locator("#status").inner_text() == "STATUS: write_bitstream Complete!"
        finally:
            browser.close()


def test_browser_report_metrics_and_windows_gui_project_evidence(monitor, tmp_path):
    """复用真实解析器和仓库报告样本，验证数值、未知降级与 GUI 证据界限。"""
    playwright = pytest.importorskip("playwright.sync_api")
    executable = shutil.which("chromium") or shutil.which("chromium-browser")
    if executable is None:
        pytest.skip("未安装 Chromium；HTTP 边界测试仍独立运行")
    _, url, _, snapshot = monitor
    fixtures = Path(__file__).parent / "fixtures"
    timing_text = (fixtures / "sample_report_timing.txt").read_text(encoding="utf-8")
    util_text = (fixtures / "sample_report_utilization.txt").read_text(encoding="utf-8")
    project_file = r"C:\工程\telemetry\telemetry.xpr"
    timing_report = {
        "name": "timing_routed.rpt",
        "stage": "post-route",
        "freshness": "stale",
        "reason": "样本报告早于运行开始标记，不参与全局结论",
        "text": timing_text,
        "summary": {"kind": "timing", **parse_timing_summary(timing_text).to_dict()},
    }
    util_report = {
        "name": "utilization_routed.rpt",
        "stage": "post-route",
        "freshness": "unverified",
        "reason": "报告来源尚未核实",
        "text": util_text,
        "summary": {"kind": "utilization", **parse_utilization(util_text).to_dict()},
    }
    snapshot.update(
        {
            "connection": "connected",
            "session_mode": "gui",
            "reports": [timing_report, util_report],
            "quality": {"timing": "unknown", "resources": "unknown"},
        }
    )
    snapshot["run"].update({"project_file": project_file, "project_mode": "unknown"})
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1150})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.goto(url)
            playwright.expect(page.locator("#report-wns")).to_have_text("0.234")
            assert page.locator("#report-tns").inner_text() == "0.000"
            assert page.locator("#report-whs").inner_text() == "0.045"
            assert page.locator("#report-ths").inner_text() == "0.000"
            assert "可能过期" in page.locator("#report-context").inner_text()
            assert page.locator("#timing").inner_text() == "未知"
            assert page.locator("#gui-project-state").inner_text() == "已发现原生工程文件"
            assert page.locator("#gui-project-file").inner_text() == project_file
            hint = page.locator("#gui-project-hint").inner_text()
            assert "File > Project > Open" in hint
            assert "同一 Vivado GUI 会话" in hint
            assert "不要让第二个实例同时写入" in hint
            assert "尚未验证" in hint
            assert page.locator('a[href^="file:"]').count() == 0
            page.screenshot(path=str(tmp_path / "monitor-timing.png"), full_page=True)
            page.get_by_role("button", name="utilization_routed.rpt", exact=True).click()
            first_resource = page.locator("#resource-list tr").first
            assert first_resource.locator("td").all_text_contents() == [
                "Slice LUTs",
                "1,440",
                "20,800",
                "6.92%",
            ]
            assert first_resource.locator("progress").get_attribute("value") == "6.92"
            assert page.locator("#resources").inner_text() == "未知"
            assert not page.locator("#timing-metrics").is_visible()
            page.screenshot(path=str(tmp_path / "monitor-resources.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(tmp_path / "monitor-resources-mobile.png"), full_page=True)

            # 解析器的非 ok 摘要含全零占位，展示必须保留未知。
            timing_report["summary"] = {"kind": "timing", **parse_timing_summary("").to_dict()}
            snapshot["run"]["project_mode"] = "in_memory"
            page.reload()
            playwright.expect(page.locator("#timing-parse-note")).to_contain_text("未识别")
            for field in ("wns", "tns", "whs", "ths"):
                assert page.locator(f"#report-{field}").inner_text() == "未知"
            assert page.locator("#gui-project-state").inner_text() == "内存工程 · 无持久 .xpr 交付"
            assert project_file not in page.locator("#gui-project-file").inner_text()
            assert "File > Project > Open" not in page.locator("#gui-project-hint").inner_text()

            # 某字段未知时不以 Used/Available 重新计算报告百分比。
            util_report["summary"]["resources"][0]["percent"] = None
            page.get_by_role("button", name="utilization_routed.rpt", exact=True).click()
            page.reload()
            page.get_by_role("button", name="utilization_routed.rpt", exact=True).click()
            first_resource = page.locator("#resource-list tr").first
            assert first_resource.locator("td").last.inner_text() == "未知"
            assert first_resource.locator("progress").count() == 0

            snapshot["source"] = "replay"
            snapshot["run"]["project_mode"] = "unknown"
            page.reload()
            playwright.expect(page.locator("#gui-project-state")).to_contain_text("回放样本")
            assert "样本路径" in page.locator("#gui-project-file").inner_text()
            assert "File > Project > Open" not in page.locator("#gui-project-hint").inner_text()
            snapshot["source"] = "live"
            snapshot["run"]["project_file"] = ""
            page.reload()
            playwright.expect(page.locator("#gui-project-file")).to_have_text(
                "未确认 .xpr 工程路径"
            )
            assert "File > Project > Open" not in page.locator("#gui-project-hint").inner_text()
            assert errors == []
        finally:
            browser.close()


def test_browser_daily_workflow_keeps_focus_and_supports_themes_copy_and_search(monitor, tmp_path):
    """后台采样不打断键盘/阅读；主题和复制只影响本机展示。"""
    playwright = pytest.importorskip("playwright.sync_api")
    executable = shutil.which("chromium") or shutil.which("chromium-browser")
    if executable is None:
        pytest.skip("未安装 Chromium；HTTP 边界测试仍独立运行")
    _, url, _, snapshot = monitor
    fixtures = Path(__file__).parent / "fixtures"
    timing_text = (fixtures / "sample_report_timing.txt").read_text(encoding="utf-8")
    util_text = (fixtures / "sample_report_utilization.txt").read_text(encoding="utf-8")
    project_file = r"C:\工程\telemetry\telemetry.xpr"
    util_path = r"C:\工程\telemetry\telemetry.runs\impl_1\utilization_routed.rpt"
    timing_report = {
        "name": "timing_routed.rpt",
        "path": r"C:\工程\timing_routed.rpt",
        "stage": "post-route",
        "stage_source": "filename_hint",
        "freshness": "unverified",
        "text": timing_text,
        "reason": "浏览器验证样本，尚未验证目标/约束匹配",
        "summary": {"kind": "timing", **parse_timing_summary(timing_text).to_dict()},
    }
    util_report = {
        "name": "utilization_routed.rpt",
        "path": util_path,
        "stage": "post-route",
        "freshness": "stale",
        "text": util_text,
        "reason": "旧报告样本；不代表本次运行签核",
        "summary": {"kind": "utilization", **parse_utilization(util_text).to_dict()},
    }
    snapshot.update(
        {
            "connection": "connected",
            "session_id": "ui-fixture",
            "session_mode": "gui",
            "run_name": "impl_1",
            "target_step": "route_design",
            "observed_at": time.time(),
            "reports": [timing_report, util_report],
            "reports_status": "ready",
            "reports_observed_at": time.time() - 15,
            "reports_source": {"project": "浏览器测试样本", "run_name": "impl_1"},
            "quality": {"timing": "unknown", "resources": "unknown"},
        }
    )
    snapshot["run"].update(
        {
            "project_file": project_file,
            "project_mode": "unknown",
            "state": "running",
            "progress_percent": 50,
            "project": "浏览器测试样本",
            "version": "Vivado v2024.2（样本）",
            "elapsed": "00:08:23",
            "current_phase": "Phase 4.1 Global Iteration",
            "log_mtime": time.time() - 3,
            "tail": [{"lineno": 1, "text": "INFO: 此处为浏览器验证样本，不是现场 EDA 运行。"}],
            "diagnostics": {
                "errors": 0,
                "warnings": 2,
                "critical_warnings": 0,
                "scope": "仅当前日志尾部；0 不代表全程无警告/错误",
            },
        }
    )
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"]
        )
        try:
            context = browser.new_context(
                viewport={"width": 1440, "height": 1150},
                color_scheme="light",
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.new_page()
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.on("request", lambda request: requests.append((request.method, request.url)))
            page.goto(url)
            playwright.expect(page.locator("#state")).to_have_text("运行中")
            playwright.expect(page.locator("html")).to_have_attribute("data-theme", "light")
            assert page.locator("#reports-sampling-status").inner_text() == "报告读取完成"
            assert "不代表报告新鲜" in page.locator("#reports-sampling-error").inner_text()
            assert "文件名线索" in page.locator("#report-context").inner_text()
            page.screenshot(path=str(tmp_path / "monitor-light.png"), full_page=True)

            theme = page.get_by_role("combobox", name="界面外观")
            theme.select_option("dark")
            playwright.expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            page.reload()
            playwright.expect(theme).to_have_value("dark")
            playwright.expect(page.locator("#state")).to_have_text("运行中")
            page.screenshot(path=str(tmp_path / "monitor-dark.png"), full_page=True)
            theme.select_option("system")
            playwright.expect(page.locator("html")).to_have_attribute("data-theme", "light")
            page.emulate_media(color_scheme="dark")
            playwright.expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(tmp_path / "monitor-dark-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1150})

            page.get_by_role("button", name="复制工程路径").click()
            playwright.expect(page.locator("#project-copy-status")).to_have_text("路径已复制。")
            assert page.evaluate("navigator.clipboard.readText()") == project_file
            selected = page.get_by_role("button", name="utilization_routed.rpt", exact=True)
            selected.focus()
            page.keyboard.press("Enter")
            playwright.expect(selected).to_have_attribute("aria-pressed", "true")
            selected.evaluate("node => { window.originalReportButton = node; }")
            page.locator("#report-text").evaluate("node => { node.scrollTop = 80; }")
            snapshot["run"]["elapsed"] = "00:08:24"
            util_report["mtime"] = time.time()
            snapshot["reports_status"] = "loading"
            playwright.expect(page.locator("#elapsed")).to_have_text("00:08:24", timeout=6000)
            playwright.expect(selected).to_be_focused()
            assert selected.evaluate("node => node === window.originalReportButton")
            assert page.locator("#report-text").evaluate("node => node.scrollTop") == 80
            assert "保留上次结果" in page.locator("#reports-sampling-status").inner_text()
            assert page.locator("#report-title").inner_text() == "utilization_routed.rpt"

            search = page.get_by_role("searchbox", name="查找报告")
            search.fill("timing")
            assert (
                not page.locator("#report-list tr")
                .filter(has_text="utilization_routed")
                .is_visible()
            )
            assert "阅读内容已保留" in page.locator("#report-filter-note").inner_text()
            snapshot["run"]["elapsed"] = "00:08:25"
            snapshot["reports_status"] = "stale"
            playwright.expect(page.locator("#elapsed")).to_have_text("00:08:25", timeout=6000)
            playwright.expect(search).to_be_focused()
            assert page.locator("#report-count").inner_text() == "1 / 2 份"
            assert "不代表当前运行" in page.locator("#reports-sampling-status").inner_text()
            page.keyboard.press("Escape")
            playwright.expect(search).to_have_value("")
            playwright.expect(selected).to_have_attribute("aria-pressed", "true")
            page.get_by_role("button", name="复制报告路径").click()
            playwright.expect(page.locator("#report-copy-status")).to_have_text("路径已复制。")
            assert page.evaluate("navigator.clipboard.readText()") == util_path

            # 模拟浏览器拒绝剪贴板权限，提供可手动复制的准确路径。
            page.evaluate("""Object.defineProperty(navigator, 'clipboard', {
                value: {writeText: () => Promise.reject(new Error('denied'))}, configurable: true
            })""")
            page.get_by_role("button", name="复制报告路径").click()
            playwright.expect(page.locator("#report-copy-status")).to_contain_text("请按 Ctrl+C")
            assert page.evaluate("window.getSelection().toString()") == util_path
            assert page.locator("#timing").inner_text() == "未知"
            assert page.locator("#resources").inner_text() == "未知"
            assert errors == []
            assert requests
            for method, request_url in requests:
                assert method == "GET"
                assert request_url in (url, url + "snapshot.json")
        finally:
            browser.close()


def test_browser_theme_survives_new_monitor_ports_and_storage_failures(monitor):
    """真实浏览器验证跨随机端口继承、旧偏好迁移及存储禁用回退。"""
    playwright = pytest.importorskip("playwright.sync_api")
    executable = shutil.which("chromium") or shutil.which("chromium-browser")
    if executable is None:
        pytest.skip("未安装 Chromium；HTTP 边界测试仍独立运行")
    _, first_url, _, snapshot = monitor
    second = MonitorHTTP(lambda: snapshot)
    second_url = second.start()
    assert urlsplit(first_url).port != urlsplit(second_url).port
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                executable_path=executable, headless=True, args=["--no-sandbox"]
            )
            try:
                context = browser.new_context(color_scheme="light")
                first = context.new_page()
                first.goto(first_url)
                # 迁移已有 origin 下的旧版偏好，另一个端口也应继承。
                first.evaluate("localStorage.setItem('otter-vivado-theme', 'dark')")
                first.reload()
                playwright.expect(first.locator("html")).to_have_attribute("data-theme", "dark")
                other = context.new_page()
                other.goto(second_url)
                playwright.expect(other.get_by_role("combobox", name="界面外观")).to_have_value(
                    "dark"
                )
                other.reload()
                playwright.expect(other.locator("html")).to_have_attribute("data-theme", "dark")
                theme_cookies = [
                    item
                    for item in context.cookies()
                    if item["name"] == "otter_vivado_monitor_theme"
                ]
                assert len(theme_cookies) == 1
                cookie = theme_cookies[0]
                assert cookie["domain"] == "127.0.0.1"
                assert cookie["path"] == "/"
                assert cookie["sameSite"] == "Strict"
                assert cookie["value"] == "dark"

                other.get_by_role("combobox", name="界面外观").select_option("system")
                playwright.expect(other.locator("html")).to_have_attribute("data-theme", "light")
                # 不做跨页面实时同步；重开/刷新才读取新的共享偏好。
                playwright.expect(first.locator("html")).to_have_attribute("data-theme", "dark")
                first.reload()
                playwright.expect(first.get_by_role("combobox", name="界面外观")).to_have_value(
                    "system"
                )
                playwright.expect(first.locator("html")).to_have_attribute("data-theme", "light")

                blocked = browser.new_context(color_scheme="light")
                blocked.add_init_script("""Object.defineProperty(document, 'cookie', {
                    get() { throw new Error('cookie access denied'); },
                    set() { throw new Error('cookie access denied'); }
                });""")
                fallback = blocked.new_page()
                errors = []
                fallback.on("pageerror", lambda error: errors.append(str(error)))
                fallback.goto(first_url)
                fallback.get_by_role("combobox", name="界面外观").select_option("dark")
                fallback.reload()
                playwright.expect(fallback.locator("html")).to_have_attribute("data-theme", "dark")
                assert errors == []
            finally:
                browser.close()
    finally:
        second.close()
