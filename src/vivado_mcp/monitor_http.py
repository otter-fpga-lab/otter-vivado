"""仅向本机浏览器提供已缓存快照的只读 HTTP 展示层。"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files


class _LocalServer(ThreadingHTTPServer):
    """慢速浏览器连接不得阻止展示服务关闭。"""

    daemon_threads = True
    block_on_close = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address

    def handle_error(self, request, client_address):
        # 不把含 token 的请求、工程信息或回调异常写到 stderr。
        pass


class MonitorHTTP:
    """提供随机端口和随机路径的只读页面，不拥有或停止 Vivado 会话。

    ``snapshot`` 必须只读取后台缓存；HTTP 请求不应向 EDA 发起查询。
    关闭页面或本服务只关闭展示层，不调用任何会话结束动作。
    """

    def __init__(self, snapshot: Callable[[], dict]):
        self._snapshot = snapshot
        self._server: _LocalServer | None = None
        self._thread: threading.Thread | None = None
        self._url = ""
        self._lock = threading.Lock()

    def start(self) -> str:
        """启动本机展示服务；重复调用返回同一 URL。"""
        with self._lock:
            if self._server is not None:
                return self._url
            token = secrets.token_urlsafe(32)
            nonce = secrets.token_urlsafe(24)
            page = (
                files("vivado_mcp")
                .joinpath("web/monitor.html")
                .read_text(encoding="utf-8")
                .replace("__CSP_NONCE__", nonce)
                .encode("utf-8")
            )
            snapshot = self._snapshot
            base_path = f"/{token}/"
            csp = (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'none'; object-src 'none'"
            )

            class Handler(BaseHTTPRequestHandler):
                """只接受两个精确路径，绝不将 URL 映射到本地文件。"""

                def log_message(self, format, *args):
                    pass

                def version_string(self):
                    return "OtterVivadoMonitor"

                def _respond(self, status, body, content_type="text/plain; charset=utf-8"):
                    try:
                        self.send_response(status)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Security-Policy", csp)
                        self.send_header("Referrer-Policy", "no-referrer")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("X-Frame-Options", "DENY")
                        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        pass
                    self.close_connection = True

                def send_error(self, code, message=None, explain=None):
                    # 标准库错误页不应回显请求内容或内部错误。
                    self._respond(code, b"Request rejected")

                def _allowed_source(self):
                    host = f"127.0.0.1:{self.server.server_address[1]}"
                    hosts = self.headers.get_all("Host", [])
                    origins = self.headers.get_all("Origin", [])
                    return (
                        hosts == [host]
                        and (not origins or origins == [f"http://{host}"])
                        and self.headers.get("Sec-Fetch-Site") != "cross-site"
                    )

                def do_GET(self):
                    if not self._allowed_source():
                        self._respond(403, b"Forbidden")
                        return
                    if self.path == base_path:
                        self._respond(200, page, "text/html; charset=utf-8")
                    elif self.path == f"{base_path}snapshot.json":
                        try:
                            body = json.dumps(
                                snapshot(), ensure_ascii=False, allow_nan=False
                            ).encode("utf-8")
                        except Exception:
                            self._respond(
                                503,
                                b'{"error":"Snapshot unavailable"}',
                                "application/json; charset=utf-8",
                            )
                            return
                        self._respond(200, body, "application/json; charset=utf-8")
                    else:
                        self._respond(404, b"Not found")

                def _read_only(self):
                    self._respond(405, b"Read-only monitor")

                do_POST = _read_only
                do_PUT = _read_only
                do_DELETE = _read_only
                do_PATCH = _read_only
                do_OPTIONS = _read_only
                do_HEAD = _read_only
                do_CONNECT = _read_only
                do_TRACE = _read_only

            server = _LocalServer(("127.0.0.1", 0), Handler)
            self._url = f"http://127.0.0.1:{server.server_address[1]}{base_path}"
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="vivado-monitor-http",
                daemon=True,
            )
            self._server = server
            self._thread.start()
            return self._url

    def close(self) -> None:
        """关闭 HTTP 服务，不触碰快照提供者或 EDA 运行。"""
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
