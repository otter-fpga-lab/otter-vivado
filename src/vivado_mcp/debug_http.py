"""本机硬件调试页面：缓存读取与受约束的异步操作入口。"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

_MAX_BODY = 16 * 1024
_ACTIONS = {
    "inventory": (set(), set()),
    "select": ({"target", "device"}, set()),
    "refresh": (set(), set()),
    "control": ({"owner"}, set()),
    "write_vio": ({"core", "probe", "value"}, set()),
    "configure_ila": ({"core", "probe", "trigger_value"}, {"trigger_position"}),
    "arm_ila": ({"core"}, {"immediate"}),
    "upload_ila": ({"core"}, set()),
}


def _object(pairs):
    """拒绝重复字段，避免不同层对同一请求产生不同解释。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _validate_request(value):
    """HTTP 层只允许固定动作结构；具体硬件能力由调试服务验证。"""
    if not isinstance(value, dict) or set(value) != {"action", "params", "expected_revision"}:
        raise ValueError("Invalid envelope")
    action, params, revision = value["action"], value["params"], value["expected_revision"]
    if not isinstance(action, str) or action not in _ACTIONS or not isinstance(params, dict):
        raise ValueError("Invalid action")
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid revision")
    required, optional = _ACTIONS[action]
    if not required <= params.keys() or not params.keys() <= required | optional:
        raise ValueError("Invalid parameters")
    for key, item in params.items():
        if key == "immediate":
            if type(item) is not bool:
                raise ValueError("Invalid immediate flag")
        elif key == "trigger_position":
            if type(item) is not int or item < 0:
                raise ValueError("Invalid trigger position")
        elif not isinstance(item, str) or not item:
            raise ValueError("Expected nonempty string")
    if action == "control" and params["owner"] not in {"manual", "ai"}:
        raise ValueError("Invalid owner")
    return value


class _DebugServer(ThreadingHTTPServer):
    """本机慢连接不阻止退出，错误日志不包含随机路径或硬件信息。"""

    daemon_threads = True
    block_on_close = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address

    def handle_error(self, request, client_address):
        pass


class DebugHTTP:
    """只绑定回环地址，绝不解释 Tcl 或将路径映射到文件。

    ``snapshot`` 只读内存缓存；``submit`` 验证并启动短操作，立即返回操作 ID；不排队重放。
    两个回调都不得等待 Vivado；HTTP 服务关闭不结束硬件会话。
    """

    def __init__(self, snapshot: Callable[[], dict], submit: Callable[[dict], dict]):
        self._snapshot = snapshot
        self._submit = submit
        self._server: _DebugServer | None = None
        self._thread: threading.Thread | None = None
        self._url = ""
        self._lock = threading.Lock()

    def start(self) -> str:
        """启动随机端口与随机路径；重复启动返回当前 URL。"""
        with self._lock:
            if self._server is not None:
                return self._url
            path_token = secrets.token_urlsafe(32)
            session_token = secrets.token_urlsafe(32)
            nonce = secrets.token_urlsafe(24)
            base_path = f"/{path_token}/"
            page = (
                files("vivado_mcp")
                .joinpath("web/debug.html")
                .read_text(encoding="utf-8")
                .replace("__CSP_NONCE__", nonce)
                .replace("__SESSION_TOKEN__", session_token)
                .encode("utf-8")
            )
            snapshot, submit = self._snapshot, self._submit
            csp = (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'none'; object-src 'none'"
            )

            class Handler(BaseHTTPRequestHandler):
                """固定三个路由；写请求必须具备同源 Origin 和专用 token。"""

                def log_message(self, format, *args):
                    pass

                def version_string(self):
                    return "OtterVivadoDebug"

                def _respond(self, status, body, content_type="text/plain; charset=utf-8"):
                    try:
                        self.send_response(status)
                        for key, value in {
                            "Content-Type": content_type,
                            "Content-Length": str(len(body)),
                            "Cache-Control": "no-store",
                            "Content-Security-Policy": csp,
                            "Referrer-Policy": "no-referrer",
                            "X-Content-Type-Options": "nosniff",
                            "X-Frame-Options": "DENY",
                            "Cross-Origin-Resource-Policy": "same-origin",
                            "Connection": "close",
                        }.items():
                            self.send_header(key, value)
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        pass
                    self.close_connection = True

                def _json(self, status, value):
                    body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    self._respond(status, body, "application/json; charset=utf-8")

                def send_error(self, code, message=None, explain=None):
                    self._respond(code, b"Request rejected")

                def _allowed_source(self, *, write=False):
                    host = f"127.0.0.1:{self.server.server_address[1]}"
                    origins = self.headers.get_all("Origin", [])
                    return (
                        self.headers.get_all("Host", []) == [host]
                        and (origins == [f"http://{host}"] or (not write and not origins))
                        and self.headers.get_all("Sec-Fetch-Site", [])
                        in ([], ["same-origin"], ["none"])
                        and (
                            not write
                            or self.headers.get_all("X-Otter-Debug", []) == [session_token]
                        )
                    )

                def do_GET(self):
                    if not self._allowed_source():
                        self._respond(403, b"Forbidden")
                    elif self.path == base_path:
                        self._respond(200, page, "text/html; charset=utf-8")
                    elif self.path == f"{base_path}snapshot.json":
                        try:
                            self._json(200, snapshot())
                        except Exception:
                            self._json(503, {"error": "暂时无法读取调试状态。"})
                    else:
                        self._respond(404, b"Not found")

                def do_POST(self):
                    if not self._allowed_source(write=True):
                        self._respond(403, b"Forbidden")
                        return
                    if self.path != f"{base_path}actions":
                        self._respond(404, b"Not found")
                        return
                    lengths = self.headers.get_all("Content-Length", [])
                    types = self.headers.get_all("Content-Type", [])
                    if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1:
                        self._json(400, {"error": "请求长度无效。"})
                        return
                    if len(types) != 1 or types[0].lower() not in {
                        "application/json",
                        "application/json; charset=utf-8",
                    }:
                        self._json(415, {"error": "仅接受 UTF-8 JSON 请求。"})
                        return
                    try:
                        if not lengths[0].isascii() or not lengths[0].isdecimal():
                            raise ValueError("Invalid length")
                        length = int(lengths[0])
                        if length > _MAX_BODY:
                            self._json(413, {"error": "请求超过 16 KiB。"})
                            return
                        raw = self.rfile.read(length)
                        if len(raw) != length:
                            raise ValueError("Incomplete body")
                        request = _validate_request(
                            json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
                        )
                    except (ValueError, UnicodeError, RecursionError, TimeoutError):
                        self._json(400, {"error": "请求格式或参数无效。"})
                        return
                    try:
                        result = submit(request)
                        self._json(202, result)
                    except ValueError:
                        self._json(400, {"error": "参数无效，请检查设备选择和输入。"})
                    except RuntimeError:
                        self._json(409, {"error": "状态已变化或设备忙碌，请检查最新状态后重试。"})
                    except Exception:
                        self._json(503, {"error": "调试操作暂时不可用。"})

                def _unsupported(self):
                    self._respond(405, b"Method not allowed")

                do_PUT = _unsupported
                do_DELETE = _unsupported
                do_PATCH = _unsupported
                do_OPTIONS = _unsupported
                do_HEAD = _unsupported
                do_CONNECT = _unsupported
                do_TRACE = _unsupported

            self._server = _DebugServer(("127.0.0.1", 0), Handler)
            self._url = f"http://127.0.0.1:{self._server.server_address[1]}{base_path}"
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="vivado-debug-http",
                daemon=True,
            )
            self._thread.start()
            return self._url

    def close(self) -> None:
        """关闭页面服务；不触碰硬件或调试服务任务。"""
        with self._lock:
            if self._server is None:
                return
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=2)
            self._server = None
            self._thread = None
            self._url = ""
