"""本机展示入口的安全边界、只读行为和页面数据隔离测试。"""

from __future__ import annotations

import json
import re
import shutil
import time
from http.client import HTTPConnection
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

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
