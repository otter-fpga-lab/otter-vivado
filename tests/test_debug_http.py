"""本机调试入口的写入边界、异步协议与真实浏览器交互测试。"""

from __future__ import annotations

import json
import re
import shutil
import time
from http.client import HTTPConnection
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from vivado_mcp.debug_http import DebugHTTP


def _request(url, *, method="GET", body=None, headers=None, path=None):
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=4)
    try:
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection.request(
            method, target if path is None else path, body=body, headers=headers or {}
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _snapshot():
    return {
        "source": "demo",
        "session_id": "example-session",
        "connection": "connected",
        "observed_at": time.time(),
        "revision": 7,
        "control": "manual",
        "busy": False,
        "error": None,
        "inventory": {
            "targets": [
                {
                    "name": "demo://isp",
                    "is_open": True,
                    "devices": [{"name": "demo-fpga", "part": "example-part"}],
                }
            ]
        },
        "selection": {"target": "demo://isp", "device": "demo-fpga"},
        "hardware": {
            "target": "demo://isp",
            "device": "demo-fpga",
            "part": "example-part",
            "vios": [
                {
                    "name": "demo_vio",
                    "uuid": "vio-example",
                    "probes": [
                        {"name": "gain", "width": 12, "direction": "out", "value": "00a"},
                        {"name": "bypass", "width": 1, "direction": "out", "value": "0"},
                        {
                            "name": "wide",
                            "width": 64,
                            "direction": "out",
                            "value": "ffffffffffffffff",
                        },
                        {"name": "unknown", "width": None, "direction": "out", "value": None},
                        {"name": "frame_count", "width": 32, "direction": "in", "value": "0001"},
                    ],
                }
            ],
            "ilas": [
                {
                    "name": "demo_ila",
                    "uuid": "ila-example",
                    "status": "IDLE",
                    "window_count": 1,
                    "trigger_mode": "BASIC_ONLY",
                    "depth": 4096,
                    "trigger_position": 1024,
                    "capture_complete": False,
                    "sample_count": 0,
                    "probes": [{"name": "frame_valid", "width": 1, "trigger_value": "eq1'b1"}],
                }
            ],
        },
        "operations": [],
        "panel": {
            "title": "ISP 调试台",
            "controls": [
                {
                    "kind": "slider",
                    "label": "图像增益",
                    "core": "demo_vio",
                    "probe": "gain",
                    "min": 0,
                    "max": 4095,
                    "step": 1,
                },
                {
                    "kind": "toggle",
                    "label": "算法旁路",
                    "core": "demo_vio",
                    "probe": "bypass",
                },
            ],
        },
    }


@pytest.fixture
def debug():
    state = _snapshot()
    snapshot = Mock(side_effect=lambda: state)
    submit = Mock(return_value={"operation_id": "op-1", "status": "running"})
    server = DebugHTTP(snapshot, submit)
    url = server.start()
    _, _, page = _request(url)
    token = re.search(r'const SESSION_TOKEN = "([A-Za-z0-9_-]+)";', page.decode()).group(1)
    headers = {
        "Origin": f"http://{urlsplit(url).netloc}",
        "Content-Type": "application/json",
        "X-Otter-Debug": token,
    }
    try:
        yield server, url, snapshot, submit, state, headers
    finally:
        server.close()


def _post(debug, payload=None, *, headers=None, body=None, suffix="actions"):
    _, url, _, _, _, defaults = debug
    if payload is None:
        payload = {"action": "inventory", "params": {}, "expected_revision": 7}
    return _request(
        url + suffix,
        method="POST",
        body=json.dumps(payload) if body is None else body,
        headers=defaults if headers is None else headers,
    )


def test_loopback_routes_and_cached_snapshot(debug):
    server, url, snapshot, submit, state, _ = debug
    parsed = urlsplit(url)
    assert parsed.hostname == "127.0.0.1"
    assert re.fullmatch(r"/[A-Za-z0-9_-]{43}/", parsed.path)
    assert server.start() == url
    snapshot.assert_not_called()
    status, headers, body = _request(url + "snapshot.json")
    assert status == 200
    assert json.loads(body) == state
    assert headers["Cache-Control"] == "no-store"
    snapshot.assert_called_once_with()
    submit.assert_not_called()


@pytest.mark.parametrize("path", ["/", "/actions", "/etc/passwd", "/favicon.ico"])
def test_unknown_paths_are_not_files(debug, path):
    _, url, snapshot, submit, _, _ = debug
    assert _request(url, path=path)[0] == 404
    snapshot.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize("suffix", ["../README.md", "run.tcl", "snapshot.json?path=secret"])
def test_query_strings_and_arbitrary_paths_are_rejected(debug, suffix):
    assert _request(debug[1] + suffix)[0] == 404
    assert _post(debug, suffix=suffix)[0] == 404
    debug[2].assert_not_called()
    debug[3].assert_not_called()


@pytest.mark.parametrize(
    "replacement",
    [
        {"Host": "evil.example"},
        {"Host": "localhost"},
        {"Origin": "null"},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_get_and_post_reject_foreign_sources(debug, replacement):
    headers = {**debug[5], **replacement}
    assert _request(debug[1] + "snapshot.json", headers=headers)[0] == 403
    assert _post(debug, headers=headers)[0] == 403
    debug[2].assert_not_called()
    debug[3].assert_not_called()


@pytest.mark.parametrize("field", ["Origin", "X-Otter-Debug"])
def test_post_requires_origin_and_session_token(debug, field):
    headers = dict(debug[5])
    del headers[field]
    status, response_headers, _ = _post(debug, headers=headers)
    assert status == 403
    assert "Access-Control-Allow-Origin" not in response_headers
    debug[3].assert_not_called()


@pytest.mark.parametrize("field", ["Host", "Origin", "X-Otter-Debug", "Sec-Fetch-Site"])
def test_duplicate_security_headers_rejected(debug, field):
    parsed = urlsplit(debug[1])
    headers = {"Host": parsed.netloc, **debug[5], "Sec-Fetch-Site": "same-origin"}
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        connection.putrequest("POST", parsed.path + "actions", skip_host=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader(field, headers[field])
        connection.putheader("Content-Length", "0")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()
    debug[3].assert_not_called()


def test_submit_is_async_and_preserves_wide_value_strings(debug):
    payload = {
        "action": "write_vio",
        "params": {"core": "demo_vio", "probe": "wide", "value": "18446744073709551615"},
        "expected_revision": 7,
    }
    status, _, body = _post(debug, payload)
    assert status == 202
    assert json.loads(body) == {"operation_id": "op-1", "status": "running"}
    debug[3].assert_called_once_with(payload)
    debug[2].assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"action": "run_tcl", "params": {"script": "exit"}, "expected_revision": 7},
        {"action": "inventory", "params": {"path": "secret"}, "expected_revision": 7},
        {"action": "inventory", "params": {}, "expected_revision": True},
        {"action": "inventory", "params": {}, "expected_revision": -1},
        {"action": "inventory", "params": {}},
        {"action": "inventory", "params": {}, "expected_revision": 7, "extra": True},
        {
            "action": "write_vio",
            "params": {"core": "vio", "probe": "p", "value": 9007199254740993},
            "expected_revision": 7,
        },
        {
            "action": "arm_ila",
            "params": {"core": "ila", "immediate": "false"},
            "expected_revision": 7,
        },
        {"action": "control", "params": {"owner": "everyone"}, "expected_revision": 7},
        {"action": "stop_ila", "params": {"core": "ila"}, "expected_revision": 7},
    ],
)
def test_actions_have_a_closed_typed_envelope(debug, payload):
    assert _post(debug, payload)[0] == 400
    debug[3].assert_not_called()


@pytest.mark.parametrize(
    "body", [b"{", b"\xff", b'{"action":"inventory","action":"refresh"}', b"[" * 2000]
)
def test_invalid_json_and_duplicate_keys_rejected(debug, body):
    assert _post(debug, body=body)[0] == 400
    debug[3].assert_not_called()


def test_content_type_and_body_limit(debug):
    assert _post(debug, headers={**debug[5], "Content-Type": "text/plain"})[0] == 415
    assert _post(debug, body=b" " * (16 * 1024 + 1))[0] == 413
    assert _post(debug, headers={**debug[5], "Transfer-Encoding": "chunked"})[0] == 400
    assert _post(debug, headers={**debug[5], "Content-Length": "-1"})[0] == 400
    debug[3].assert_not_called()


def test_duplicate_content_lengths_rejected(debug):
    parsed = urlsplit(debug[1])
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        connection.putrequest("POST", parsed.path + "actions")
        for name, value in debug[5].items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", "0")
        connection.putheader("Content-Length", "0")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close()
    debug[3].assert_not_called()


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE"])
def test_other_methods_cannot_submit(debug, method):
    assert _request(debug[1] + "actions", method=method)[0] == 405
    debug[3].assert_not_called()


@pytest.mark.parametrize("error,status", [(ValueError, 400), (RuntimeError, 409), (OSError, 503)])
def test_callback_errors_are_redacted(debug, capsys, error, status):
    debug[3].side_effect = error("PRIVATE_TOKEN_AND_STACK")
    response_status, _, body = _post(debug)
    assert response_status == status
    assert "PRIVATE" not in body.decode()
    assert not capsys.readouterr().err


def test_stale_revision_rejected_by_callback_without_replaying(debug):
    calls = []

    def submit(request):
        calls.append(request)
        if request["expected_revision"] != debug[4]["revision"]:
            raise RuntimeError("Revision changed")
        return {"operation_id": "op-new", "status": "running"}

    debug[3].side_effect = submit
    debug[4]["revision"] = 8
    assert _post(debug)[0] == 409
    assert len(calls) == 1
    assert _post(debug, {"action": "inventory", "params": {}, "expected_revision": 8})[0] == 202


def test_snapshot_failure_keeps_private_information_out(debug, capsys):
    debug[2].side_effect = RuntimeError("PRIVATE_HARDWARE_TOKEN")
    status, _, body = _request(debug[1] + "snapshot.json")
    assert status == 503
    assert "PRIVATE" not in body.decode()
    assert not capsys.readouterr().err


def test_nonce_csp_and_text_only_sinks(debug):
    _, headers, raw = _request(debug[1])
    page = raw.decode()
    nonce = re.search(r'<script nonce="([A-Za-z0-9_-]+)">', page).group(1)
    policy = headers["Content-Security-Policy"]
    assert f"script-src 'nonce-{nonce}'" in policy
    assert f"style-src 'nonce-{nonce}'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "Access-Control-Allow-Origin" not in headers
    assert "__SESSION_TOKEN__" not in page
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert unsafe not in page


def test_close_never_ends_hardware_and_restart_rotates_tokens(debug):
    server, url, snapshot, submit, _, _ = debug
    server.close()
    server.close()
    new_url = server.start()
    assert urlsplit(new_url).path != urlsplit(url).path
    assert _request(new_url)[0] == 200
    snapshot.assert_not_called()
    submit.assert_not_called()


@pytest.fixture
def browser():
    """有 Chromium 时执行真实网页交互；HTTP 边界不依赖浏览器。"""
    playwright = pytest.importorskip("playwright.sync_api")
    executable = shutil.which("chromium") or shutil.which("chromium-browser")
    if not executable:
        pytest.skip("未安装 Chromium")
    with playwright.sync_playwright() as runtime:
        instance = runtime.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"]
        )
        try:
            yield instance, playwright.expect
        finally:
            instance.close()


def test_browser_manual_controls_preserve_inputs_and_value_precision(debug, browser, tmp_path):
    instance, expect = browser
    page = instance.new_page(viewport={"width": 1400, "height": 1150})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(debug[1])
        expect(page.locator("#source")).to_have_text("演示数据 · 非真实设备")
        expect(page.locator("#panel-title")).to_have_text("ISP 调试台")
        assert page.locator("#target").input_value() == ""
        assert page.locator("#device").input_value() == ""
        expect(page.locator("#select-device")).to_be_disabled()
        expect(page.get_by_label("unknown 写入值", exact=True)).to_be_disabled()
        assert page.get_by_label("wide 滑杆", exact=True).count() == 0
        assert page.get_by_label("frame_count 写入值", exact=True).count() == 0
        expect(page.get_by_role("button", name="上传至 Vivado", exact=True)).to_be_disabled()
        debug[3].assert_not_called()
        field = page.get_by_label("gain 写入值", exact=True)
        field.fill("123")
        field.focus()
        debug[4]["hardware"]["vios"][0]["probes"][0]["value"] = "0ff"
        page.wait_for_timeout(1200)
        assert field.input_value() == "123"
        expect(field).to_be_focused()
        expect(page.locator("#vios .readback").first).to_have_text("0x0ff")
        debug[3].assert_not_called()
        field = page.get_by_label("wide 写入值", exact=True)
        field.fill("18446744073709551615")
        field.locator("..").get_by_role("button", name="写入", exact=True).click()
        expect(page.locator("#feedback")).to_contain_text("已提交")
        request = debug[3].call_args.args[0]
        assert request["params"]["value"] == "18446744073709551615"
        assert request["expected_revision"] == 7
        slider = page.get_by_label("gain 滑杆", exact=True)
        count = debug[3].call_count
        slider.evaluate("node => {node.value = '250'; node.dispatchEvent(new Event('input'));}")
        page.wait_for_timeout(200)
        assert debug[3].call_count == count
        slider.dispatch_event("change")
        expect(page.locator("#feedback")).to_contain_text("已提交")
        expect(page.locator("#inventory")).to_be_enabled()
        assert debug[3].call_args.args[0]["params"]["value"] == "250"
        assert debug[3].call_count == count + 1
        debug[4]["hardware"]["ilas"][0]["capture_complete"] = True
        page.wait_for_timeout(1200)
        expect(page.get_by_role("button", name="上传至 Vivado", exact=True)).to_be_enabled()
        page.screenshot(path=str(tmp_path / "debug-desktop.png"), full_page=True)
        page.locator("#theme").select_option("dark")
        assert page.locator("html").get_attribute("data-theme") == "dark"
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(tmp_path / "debug-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
    finally:
        page.close()


def test_browser_actions_ownership_and_untrusted_data(debug, browser):
    instance, expect = browser
    page = instance.new_page()
    try:
        state = debug[4]
        malicious = '<img src=x onerror="window.pwned=1">'
        state["panel"]["title"] = malicious
        state["hardware"]["ilas"][0]["status"] = malicious
        state["control"] = "ai"
        page.goto(debug[1])
        expect(page.locator("#owner")).to_have_text("AI 正在操作")
        expect(page.get_by_label("gain 写入值", exact=True)).to_be_disabled()
        expect(page.locator("#control")).to_have_text("我来操作")
        expect(page.locator("#panel-title")).to_have_text(malicious)
        assert page.locator("img").count() == 0
        assert page.evaluate("window.pwned") is None
        state["hardware"]["ilas"][0]["status"] = "IDLE"

        def submit(request):
            if request["action"] == "control":
                state["control"] = request["params"]["owner"]
            state["revision"] += 1
            return {"operation_id": f"op-{state['revision']}", "status": "running"}

        debug[3].side_effect = submit
        page.locator("#control").click()
        expect(page.locator("#owner")).to_have_text("你正在操作")
        expect(page.get_by_label("gain 写入值", exact=True)).to_be_enabled()
        page.locator("#target").select_option("demo://isp")
        assert page.locator("#device").input_value() == ""
        page.locator("#device").select_option("demo-fpga")
        page.locator("#select-device").click()
        expect(page.locator("#inventory")).to_be_enabled()
        assert debug[3].call_args.args[0]["params"] == {
            "target": "demo://isp",
            "device": "demo-fpga",
        }
        page.get_by_label("demo_ila 触发条件", exact=True).fill("eq1'b0")
        page.get_by_label("demo_ila 触发位置", exact=True).fill("256")
        page.get_by_role("button", name="应用触发条件", exact=True).click()
        expect(page.locator("#inventory")).to_be_enabled()
        assert debug[3].call_args.args[0]["params"] == {
            "core": "demo_ila",
            "probe": "frame_valid",
            "trigger_value": "eq1'b0",
            "trigger_position": 256,
        }
        state["busy"] = True
        page.wait_for_timeout(1200)
        expect(page.get_by_label("gain 写入值", exact=True)).to_be_disabled()
        expect(page.locator("#control")).to_be_disabled()
        state["busy"] = False
        page.wait_for_timeout(1200)
        expect(page.get_by_label("gain 写入值", exact=True)).to_be_enabled()
        debug[2].side_effect = RuntimeError("offline")
        page.wait_for_timeout(1200)
        expect(page.locator("#connection")).to_have_text("本机服务连接失败")
        expect(page.get_by_label("gain 写入值", exact=True)).to_be_disabled()
        assert page.get_by_label("gain 写入值", exact=True).count() == 1
    finally:
        page.close()


def test_arm_can_omit_immediate_like_the_shared_service(debug):
    payload = {"action": "arm_ila", "params": {"core": "ila"}, "expected_revision": 7}
    assert _post(debug, payload)[0] == 202
    debug[3].assert_called_once_with(payload)


def test_browser_ila_capabilities_and_gui_staged_value(debug, browser):
    instance, expect = browser
    page = instance.new_page()
    try:
        state = debug[4]
        probe = state["hardware"]["vios"][0]["probes"][0]
        probe["staged_value"] = "0FF"
        ila = state["hardware"]["ilas"][0]
        ila.update(status="WAITING_FOR_TRIGGER", window_count=1, trigger_mode="BASIC_ONLY")
        page.goto(debug[1])
        expect(page.locator("#vios .probe").first).to_contain_text("GUI 暂存值 0x0FF")
        expect(page.locator("#vios .probe").first).to_contain_text("与硬件读回不同")
        expect(page.locator("#vios .readback").first).to_have_text("0x00a")
        expect(page.get_by_role("button", name="应用触发条件", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="立即采样", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="等待触发", exact=True)).to_be_disabled()
        expect(page.locator("#ilas")).to_contain_text("请在原生 GUI 查看或调整")
        ila.update(status="IDLE", window_count=None, trigger_mode=None)
        page.wait_for_timeout(1200)
        expect(page.get_by_role("button", name="应用触发条件", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="立即采样", exact=True)).to_be_disabled()
        ila.update(window_count=1, trigger_mode="ADVANCED_ONLY")
        page.wait_for_timeout(1200)
        expect(page.get_by_role("button", name="应用触发条件", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="立即采样", exact=True)).to_be_enabled()
        ila.update(trigger_mode="BASIC_ONLY")
        probe["staged_value"] = "000a"
        page.wait_for_timeout(1200)
        expect(page.get_by_role("button", name="应用触发条件", exact=True)).to_be_enabled()
        expect(page.locator("#vios .probe").first).not_to_contain_text("GUI 暂存值")
        debug[3].assert_not_called()
    finally:
        page.close()


def test_reference_http_does_not_accept_export_paths(debug):
    status, _, _ = _post(debug, payload={
        'action': 'export_ila', 'params': {'core': 'ila', 'output_dir': '/some/path'},
        'expected_revision': 7,
    })
    assert status == 400
    debug[3].assert_not_called()
