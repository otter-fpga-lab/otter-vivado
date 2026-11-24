"""向本机浏览器（Codex / Claude Code / CLI 等非 MCP App 宿主）提供 Vivado 控制台 HTTP 服务。"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import secrets
import socket
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vivado_mcp.server import AppContext

DEFAULT_CONSOLE_PORT = 18765
_CONSOLE_APP_ID = "otter-vivado-console"
_MAX_BODY = 64 * 1024
_ALLOWED_ACTIONS = frozenset(
    {
        "inspect_path",
        "remove_inspected",
        "read_report",
        "cancel_job",
        "clear_finished",
        "ensure_local_web",
        "kill_process",
        "cleanup_stale_registry",
    }
)
_ALLOWED_KEYS = frozenset(
    {"action", "path", "job_id", "report_path", "max_lines", "pid"}
)


def preferred_console_port() -> int:
    """返回首选控制台端口（默认 18765，可通过 OTTER_VIVADO_CONSOLE_PORT 覆盖）。"""
    raw = os.environ.get("OTTER_VIVADO_CONSOLE_PORT", "").strip()
    if raw:
        try:
            port = int(raw)
            if 0 <= port <= 65535:
                return port
        except ValueError:
            pass
    return DEFAULT_CONSOLE_PORT


def probe_existing_console_hub(port: int, *, timeout: float = 0.25) -> str | None:
    """快速探测 127.0.0.1:<port>/api/health 是否已有 otter-vivado 控制台在运行。"""
    if type(port) is not int or port <= 0 or port > 65535:
        return None
    conn: http.client.HTTPConnection | None = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/api/health", headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        body = resp.read(2048)
        data = json.loads(body.decode("utf-8"))
        if isinstance(data, dict) and data.get("app") == _CONSOLE_APP_ID:
            return f"http://127.0.0.1:{port}/"
    except (OSError, ValueError, UnicodeError, TimeoutError, http.client.HTTPException):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
    return None


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 键，避免解析歧义。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


_STYLE_MARKER_RE = re.compile(r"(<!--\s*CONSOLE_STYLE\s*-->\s*)<style>")
_SCRIPT_MARKER_RE = re.compile(r"(<!--\s*CONSOLE_SCRIPT\s*-->\s*)<script>")


def _inject_csp_nonce(html: str, nonce: str) -> str:
    """为内联 <style> 与最外层 <script> 注入当前会话 CSP nonce。"""
    if _STYLE_MARKER_RE.search(html):
        out = _STYLE_MARKER_RE.sub(
            lambda m: f'{m.group(1)}<style nonce="{nonce}">', html, count=1
        )
    else:
        out = html.replace("<style>", f'<style nonce="{nonce}">', 1)

    if _SCRIPT_MARKER_RE.search(out):
        out = _SCRIPT_MARKER_RE.sub(
            lambda m: f'{m.group(1)}<script nonce="{nonce}">', out, count=1
        )
    else:
        # 兜底：从 </style> 之后找第一个 <script>（外层脚本包装标签），避免被 JS 字面量干扰
        style_end = out.find("</style>")
        search_from = style_end + len("</style>") if style_end != -1 else 0
        idx = out.find("<script>", search_from)
        if idx != -1:
            out = f'{out[:idx]}<script nonce="{nonce}">{out[idx + len("<script>"):]}'
    return out


def _validate_action_payload(payload: Any) -> dict[str, Any]:
    """严格校验 /api/action 请求体结构与字段类型。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    extra = set(payload) - _ALLOWED_KEYS
    if extra:
        raise ValueError(f"包含不支持的参数字段: {', '.join(sorted(extra))}")
    action = payload.get("action")
    if not isinstance(action, str) or action.strip() not in _ALLOWED_ACTIONS:
        raise ValueError("无效的控制台操作")
    clean: dict[str, Any] = {"action": action.strip()}
    for str_key in ("path", "job_id", "report_path"):
        val = payload.get(str_key)
        if val is not None:
            if not isinstance(val, str):
                raise ValueError(f"{str_key} 必须是字符串")
            clean[str_key] = val
    max_lines = payload.get("max_lines")
    if max_lines is not None:
        if type(max_lines) is not int or max_lines < 1:
            raise ValueError("max_lines 必须是正整数")
        clean["max_lines"] = max_lines
    pid = payload.get("pid")
    if pid is not None:
        if type(pid) is not int or pid < 1:
            raise ValueError("pid 必须是正整数")
        clean["pid"] = pid
    return clean


class _ConsoleServer(ThreadingHTTPServer):
    """慢速浏览器连接不得阻止服务退出，且不向 stderr 泄露敏感信息。"""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def handle_error(self, request, client_address):
        pass


class ConsoleHttpServer:
    """仅绑定 127.0.0.1 的单页控制台本地 HTTP 服务。

    复用与 MCP App 相同的 ``console.html`` 与底层 ``AppContext`` 状态/动作逻辑，
    严格校验 ``Host`` / ``Origin`` / ``Sec-Fetch-Site``，无 CORS 放行。
    """

    def __init__(
        self,
        app_ctx: AppContext,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        if host != "127.0.0.1":
            raise ValueError("ConsoleHttpServer 仅允许绑定回环地址 127.0.0.1")
        if type(port) is not int or not (0 <= port <= 65535):
            raise ValueError("port 必须在 0~65535 之间")
        self._app_ctx = app_ctx
        self._loop = loop
        self._host = host
        self._port = port
        self._server: _ConsoleServer | None = None
        self._thread: threading.Thread | None = None
        self._url = ""
        self._lock = threading.Lock()
        self._selection_seq = 0
        self._selected_job_id: str | None = None
        self._selected_inspected_path: str | None = None

    @property
    def url(self) -> str:
        with self._lock:
            return self._url

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._server is not None and bool(self._url)

    @property
    def host_port(self) -> str:
        with self._lock:
            if not self._url:
                return ""
            return self._url.removeprefix("http://").rstrip("/")

    def set_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        with self._lock:
            self._loop = loop

    def set_selection(
        self,
        *,
        job_id: str | None = None,
        inspected_path: str | None = None,
    ) -> None:
        clean_job = job_id.strip() if isinstance(job_id, str) and job_id.strip() else None
        clean_insp = (
            inspected_path.strip()
            if isinstance(inspected_path, str) and inspected_path.strip()
            else None
        )
        if not clean_job and not clean_insp:
            return
        with self._lock:
            self._selection_seq += 1
            self._selected_job_id = clean_job
            self._selected_inspected_path = clean_insp

    def _run_coro(self, coro_fn: Callable[[], Any]) -> Any:
        with self._lock:
            loop = self._loop
        if loop is not None and loop.is_running():
            coro = coro_fn()
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
            except Exception:
                coro.close()
                raise
            return future.result(timeout=20)
        return asyncio.run(coro_fn())

    def start(self) -> str:
        """启动本地控制台 HTTP 服务；重复调用幂等返回同一 URL。"""
        from vivado_mcp.tools.console_app_tools import (
            collect_console_state,
            execute_console_action,
            read_console_html,
        )

        with self._lock:
            if self._server is not None:
                return self._url

            nonce = secrets.token_urlsafe(24)
            page_bytes = _inject_csp_nonce(read_console_html(), nonce).encode("utf-8")
            csp = (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'none'; object-src 'none'"
            )
            parent = self
            app_ctx = self._app_ctx

            class Handler(BaseHTTPRequestHandler):
                """仅开放 `/`、`/api/state` 与 `/api/action` 三个精确路由。"""

                def log_message(self, format, *args):
                    pass

                def version_string(self):
                    return "OtterVivadoConsole"

                def _respond(
                    self,
                    status: int,
                    body: bytes,
                    content_type: str = "text/plain; charset=utf-8",
                ) -> None:
                    try:
                        self.send_response(status)
                        headers_map = {
                            "Cache-Control": "no-store",
                            "Content-Security-Policy": csp,
                            "Referrer-Policy": "no-referrer",
                            "X-Content-Type-Options": "nosniff",
                            "X-Frame-Options": "DENY",
                            "Cross-Origin-Resource-Policy": "same-origin",
                            "Connection": "close",
                        }
                        if status != 204:
                            headers_map["Content-Type"] = content_type
                            headers_map["Content-Length"] = str(len(body))
                        for key, value in headers_map.items():
                            self.send_header(key, value)
                        self.end_headers()
                        if status != 204 and self.command != "HEAD":
                            self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        pass
                    self.close_connection = True

                def _json(self, status: int, value: Any) -> None:
                    body = json.dumps(
                        value, ensure_ascii=False, allow_nan=False
                    ).encode("utf-8")
                    self._respond(status, body, "application/json; charset=utf-8")

                def send_error(self, code, message=None, explain=None):
                    self._respond(code, b"Request rejected")

                def _allowed_source(self, *, write: bool = False) -> bool:
                    host = f"127.0.0.1:{self.server.server_address[1]}"
                    hosts = self.headers.get_all("Host", [])
                    origins = self.headers.get_all("Origin", [])
                    sec_fetch = self.headers.get_all("Sec-Fetch-Site", [])
                    origin_ok = (
                        origins == [f"http://{host}"]
                        if write
                        else (not origins or origins == [f"http://{host}"])
                    )
                    return (
                        hosts == [host]
                        and origin_ok
                        and sec_fetch in ([], ["same-origin"], ["none"])
                    )

                def do_GET(self):
                    if not self._allowed_source(write=False):
                        self._respond(403, b"Forbidden")
                        return
                    if self.path == "/":
                        self._respond(200, page_bytes, "text/html; charset=utf-8")
                        return
                    if self.path == "/favicon.ico":
                        self._respond(204, b"")
                        return
                    if self.path == "/api/health":
                        self._json(
                            200,
                            {
                                "app": _CONSOLE_APP_ID,
                                "pid": os.getpid(),
                                "port": int(self.server.server_address[1]),
                            },
                        )
                        return
                    if self.path == "/api/state":
                        try:
                            state, _, _ = parent._run_coro(
                                lambda: collect_console_state(app_ctx)
                            )
                            with parent._lock:
                                seq = parent._selection_seq
                                sel_job = parent._selected_job_id
                                sel_insp = parent._selected_inspected_path
                            if seq:
                                state["selection_seq"] = seq
                                if sel_job:
                                    state["selected_job_id"] = sel_job
                                if sel_insp:
                                    state["selected_inspected_path"] = sel_insp
                            self._json(200, state)
                        except Exception:
                            self._json(503, {"error": "State unavailable"})
                        return
                    self._respond(404, b"Not found")

                def do_POST(self):
                    if not self._allowed_source(write=True):
                        self._respond(403, b"Forbidden")
                        return
                    if self.path != "/api/action":
                        self._respond(404, b"Not found")
                        return

                    lengths = self.headers.get_all("Content-Length", [])
                    types = self.headers.get_all("Content-Type", [])
                    if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1:
                        self._json(400, {"error": "无效的请求长度"})
                        return
                    if len(types) != 1 or types[0].lower() not in {
                        "application/json",
                        "application/json; charset=utf-8",
                    }:
                        self._json(415, {"error": "仅接受 UTF-8 JSON 请求"})
                        return

                    try:
                        if not lengths[0].isascii() or not lengths[0].isdecimal():
                            raise ValueError("Invalid Content-Length")
                        length = int(lengths[0])
                        if length > _MAX_BODY:
                            self._json(413, {"error": "请求体超过 64 KiB 限制"})
                            return
                        raw = self.rfile.read(length)
                        if len(raw) != length:
                            raise ValueError("Incomplete body")
                        req = _validate_action_payload(
                            json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
                        )
                    except (ValueError, UnicodeError, RecursionError, TimeoutError) as exc:
                        self._json(400, {"error": str(exc)})
                        return

                    try:
                        _, structured = parent._run_coro(
                            lambda: execute_console_action(
                                app_ctx,
                                action=req["action"],
                                path=req.get("path"),
                                job_id=req.get("job_id"),
                                report_path=req.get("report_path"),
                                max_lines=req.get("max_lines", 300),
                                pid=req.get("pid"),
                            )
                        )
                        self._json(200, structured)
                    except KeyError:
                        self._json(404, {"error": f"未找到任务: {req.get('job_id')}"})
                    except RuntimeError as exc:
                        self._json(409, {"error": str(exc)})
                    except (ValueError, FileNotFoundError, OSError) as exc:
                        self._json(400, {"error": str(exc)})
                    except Exception:
                        self._json(500, {"error": "Action failed"})

                def _unsupported(self):
                    self._respond(405, b"Method not allowed")

                do_PUT = _unsupported
                do_DELETE = _unsupported
                do_PATCH = _unsupported
                do_OPTIONS = _unsupported
                do_HEAD = _unsupported
                do_CONNECT = _unsupported
                do_TRACE = _unsupported

            server = _ConsoleServer((self._host, self._port), Handler)
            self._server = server
            self._url = f"http://127.0.0.1:{server.server_address[1]}/"
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="vivado-console-http",
                daemon=True,
            )
            self._thread.start()
            return self._url

    def close(self) -> None:
        """关闭本地 HTTP 服务。"""
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
