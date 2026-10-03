"""ILA/VIO 的共享调试状态与短操作；网页和 MCP 使用同一执行入口。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import threading
import time
import uuid
from pathlib import Path

_PARAMETERS = {
    "inventory": (set(), set()),
    "select": ({"target", "device"}, set()),
    "refresh": (set(), set()),
    "control": ({"owner"}, set()),
    "write_vio": ({"core", "probe", "value"}, set()),
    "configure_ila": ({"core", "probe", "trigger_value"}, {"trigger_position"}),
    "arm_ila": ({"core"}, {"immediate"}),
    "upload_ila": ({"core"}, set()),
    "export_ila": ({"core", "output_dir"}, set()),
}
_HARDWARE_WRITES = {"write_vio", "configure_ila", "arm_ila", "upload_ila", "export_ila"}


def load_panel(path: str) -> dict:
    """读取消费者工程的有限控件描述，不执行脚本或加载外部网页。"""
    with Path(path).open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("面板描述不能超过 64 KiB")
    return validate_panel(json.loads(raw.decode("utf-8-sig")))


def validate_panel(value: dict) -> dict:
    """首批支持有限整数滑杆/开关，范围描述保留在消费者工程中。"""
    if not isinstance(value, dict) or set(value) != {"title", "controls"}:
        raise ValueError("面板必须且仅包含 title 和 controls")
    if not isinstance(value["title"], str) or not 1 <= len(value["title"]) <= 120:
        raise ValueError("title 必须是 1~120 字符文本")
    controls = value["controls"]
    if not isinstance(controls, list) or len(controls) > 64:
        raise ValueError("controls 必须是最多 64 项的列表")
    bindings = set()
    for item in controls:
        if not isinstance(item, dict):
            raise ValueError("控件必须是对象")
        required = {"kind", "label", "core", "probe"}
        if not required <= set(item) or set(item) - required - {"min", "max", "step"}:
            raise ValueError("控件字段不符合约定")
        if item["kind"] not in {"slider", "toggle"}:
            raise ValueError("控件 kind 仅支持 slider 或 toggle")
        for key in ("label", "core", "probe"):
            if not isinstance(item[key], str) or not 1 <= len(item[key]) <= 1024:
                raise ValueError(f"控件 {key} 必须是非空文本")
        binding = (item["core"], item["probe"])
        if binding in bindings:
            raise ValueError("同一探针不能重复绑定控件")
        bindings.add(binding)
        if item["kind"] == "slider":
            if not {"min", "max", "step"} <= set(item):
                raise ValueError("滑杆需要 min/max/step")
            if any(type(item[key]) is not int for key in ("min", "max", "step")):
                raise ValueError("首批滑杆仅支持整数")
            if not 0 <= item["min"] < item["max"] <= 65535 or item["step"] <= 0:
                raise ValueError("滑杆范围为 0~65535，且 min < max、step > 0")
        elif set(item) != required:
            raise ValueError("toggle 使用固定 0/1，不接受 min/max/step")
    return copy.deepcopy(value)


def _topology(hardware: dict) -> tuple:
    """仅比较目标与核/探针结构，采样值变化不强迫界面重建。"""
    return (
        hardware.get("target"), hardware.get("device"), hardware.get("part"),
        tuple(
            (kind, core["name"], core.get("uuid"), tuple(
                (p["name"], p.get("width"), p.get("direction"))
                for p in core.get("probes", [])
            ))
            for kind in ("ilas", "vios") for core in hardware.get(kind, [])
        ),
    )


class DebugService:
    """同一会话只接受一个短操作，不排队重放陈旧硬件写入。

    control 只协调本服务的网页与 MCP；原生 GUI、run_tcl 和其它进程仍需用户协调。
    超时保留 unknown，并要求成功刷新以后才可再次写入，不自动重试。
    """

    def __init__(self, session, *, backend=None, source="live", poll_interval=3.0):
        if backend is None:
            from vivado_mcp.debug_backend import HardwareDebugBackend
            backend = HardwareDebugBackend(session)
        self.session = session
        self.backend = backend
        self._loop = asyncio.get_running_loop()
        self._lock = threading.RLock()
        self._closed = False
        self._operation: asyncio.Task | None = None
        self._poller: asyncio.Task | None = None
        self._sampling = False
        self._http = None
        self.experiment = None
        self._poll_interval = poll_interval
        self._state = {
            "source": source, "session_id": session.session_id,
            "connection": "unobserved", "observed_at": None,
            "revision": 0, "control": "manual", "busy": False, "error": None,
            "inventory": {"targets": []}, "selection": None, "hardware": None,
            "operations": [], "panel": None, "capabilities": {"stop_ila": False},
        }

    def snapshot(self) -> dict:
        """HTTP 线程和 MCP 只读缓存；读取不调用 EDA。"""
        with self._lock:
            result = copy.deepcopy(self._state)
        if self._closed or not self.session.is_alive:
            result["connection"] = "disconnected"
        return result

    def set_panel(self, panel: dict) -> None:
        panel = validate_panel(panel)
        with self._lock:
            if self._state["busy"] or self._sampling:
                raise RuntimeError("调试正在执行，请完成后再加载面板")
            self._state["panel"] = panel
            self._state["revision"] += 1

    def _session_ready(self) -> None:
        if self._closed or not self.session.is_alive:
            raise RuntimeError("调试会话已断开；请重新连接并选择设备")
        state = getattr(self.session.state, "value", self.session.state)
        if state != "ready":
            raise RuntimeError("Vivado 会话当前不可执行，请等待现有命令完成")

    def submit(self, request: dict, *, source: str, experiment=None) -> dict:
        """在所属事件循环中验证并启动操作，立即返回可查询的操作 ID。"""
        if source not in {"manual", "ai"}:
            raise ValueError("未知操作来源")
        if not isinstance(request, dict) or set(request) != {
            "action", "params", "expected_revision",
        }:
            raise ValueError("需要 action、params、expected_revision")
        action, params = request["action"], request["params"]
        if not isinstance(action, str) or action not in _PARAMETERS:
            raise ValueError("不支持的调试操作")
        if not isinstance(params, dict):
            raise ValueError("params 必须是对象")
        required, optional = _PARAMETERS[action]
        if not required <= set(params) or set(params) - required - optional:
            raise ValueError(f"{action} 参数不符合约定")
        if type(request["expected_revision"]) is not int:
            raise ValueError("expected_revision 必须是整数")
        for key, value in params.items():
            if key == "immediate":
                if type(value) is not bool:
                    raise ValueError("immediate 必须是布尔值")
            elif key == "trigger_position":
                if type(value) is not int or value < 0:
                    raise ValueError("trigger_position 必须是非负整数")
            elif not isinstance(value, str) or not 1 <= len(value) <= 4096:
                raise ValueError(f"{key} 必须是非空文本")
        self._session_ready()
        with self._lock:
            if self._state["busy"] or self._sampling:
                raise RuntimeError("已有调试操作或采样正在执行，请读取状态后重试")
            if request["expected_revision"] != self._state["revision"]:
                raise RuntimeError("状态已变化；请读取最新快照后重新操作")
            lease = self.experiment
            if experiment is not None and (experiment is not lease or not lease.active):
                raise RuntimeError("实验执行身份已失效")
            if lease is not None and lease.active and experiment is not lease:
                if action == "control":
                    if params["owner"] not in {"manual", "ai"}:
                        raise ValueError("owner 仅支持 manual/ai")
                    if params["owner"] != self._state["control"]:
                        lease.interrupt("control_handoff")
                elif action not in {"inventory", "refresh"}:
                    raise RuntimeError("活动实验正在使用共享服务；先暂停观察或中止后再操作")
            if action == "control":
                if params["owner"] not in {"manual", "ai"}:
                    raise ValueError("owner 仅支持 manual/ai")
            elif action not in {"inventory", "refresh"}:
                if self._state["control"] != source:
                    raise RuntimeError("当前控制权属于另一方，请先明确切换控制权")
            if action in _HARDWARE_WRITES:
                if self._state["connection"] != "connected" or not self._state["hardware"]:
                    raise RuntimeError("缺少最新硬件状态，请先选择设备并成功刷新")
            if action == "refresh" and self._state["selection"] is None:
                raise ValueError("请先选择明确的 target/device")
            operation = {
                "id": uuid.uuid4().hex, "action": action, "source": source,
                "status": "running", "started_at": time.time(), "finished_at": None,
                "result": None, "error": None,
            }
            self._state["operations"] = (self._state["operations"] + [operation])[-30:]
            self._state["busy"] = True
            self._state["revision"] += 1
            self._operation = self._loop.create_task(
                self._execute(operation, copy.deepcopy(params), experiment)
            )
        return {"operation_id": operation["id"], "status": "running"}

    async def _inspect(self, selection: dict) -> dict:
        return await self.backend.inspect(selection["target"], selection["device"])

    def _publish(self, hardware: dict) -> None:
        with self._lock:
            previous = self._state["hardware"]
            if previous and _topology(previous) != _topology(hardware):
                self._state["revision"] += 1
            self._state.update(
                hardware=copy.deepcopy(hardware), observed_at=time.time(),
                connection="connected", error=None,
            )

    async def _execute(self, operation: dict, params: dict, experiment=None) -> None:
        action = operation["action"]
        mutation_started = False
        try:
            if action == "control":
                with self._lock:
                    self._state["control"] = params["owner"]
                result = {"control": params["owner"]}
            elif action == "inventory":
                result = await self.backend.inventory()
                with self._lock:
                    self._state["inventory"] = copy.deepcopy(result)
                    # 仅枚举成功不恢复一次失败/未知写入的设备就绪证据。
                    if self._state["selection"] is None:
                        self._state.update(connection="connected", error=None,
                                           observed_at=time.time())
            elif action == "select":
                with self._lock:
                    self._state.update(selection=None, hardware=None, connection="unobserved")
                result = await self._inspect(params)
                with self._lock:
                    self._state["selection"] = copy.deepcopy(params)
                self._publish(result)
                self.start()
            else:
                before = self.snapshot()
                selection = before["selection"]
                current = await self._inspect(selection)
                self._publish(current)
                if action == "refresh":
                    result = current
                else:
                    if _topology(current) != _topology(before["hardware"]):
                        raise ValueError("设备或探针结构已变化，本次操作未执行；请核对新快照")
                    if experiment is not None:
                        experiment.validate_hardware(current)
                    kind = "vios" if action == "write_vio" else "ilas"
                    matches = [c for c in current[kind] if c["name"] == params["core"]]
                    if len(matches) != 1:
                        raise ValueError("必须选择唯一且实际存在的调试核")
                    mutation_started = True
                    result = await getattr(self.backend, action)(
                        selection["target"], selection["device"],
                        **params, expected_uuid=matches[0].get("uuid"),
                    )
                    # 操作结果与其后的状态读取分别处理；读取失败不触发自动重放。
                    with self._lock:
                        operation["result"] = copy.deepcopy(result)
                    if action == "write_vio" and result.get("readback_matches") is False:
                        raise RuntimeError("VIO 写入后的硬件读回与请求值不一致，请核对后再操作")
                    self._publish(await self._inspect(selection))
            with self._lock:
                operation.update(status="succeeded", result=copy.deepcopy(result))
        except asyncio.CancelledError:
            with self._lock:
                operation.update(status="unknown", error="执行被中断；硬件结果未知，不自动重试")
                self._state.update(connection="error", error=operation["error"])
            raise
        except Exception as exc:
            # Tcl error 也可能出现在部分设置已经生效之后，硬件操作保守标为 unknown。
            uncertain = mutation_started or isinstance(exc, (TimeoutError, asyncio.TimeoutError))
            message = str(exc) or type(exc).__name__
            with self._lock:
                operation.update(status="unknown" if uncertain else "failed", error=message)
                self._state.update(connection="error", error=message)
        finally:
            with self._lock:
                operation["finished_at"] = time.time()
                self._state["busy"] = False
                self._state["revision"] += 1

    async def wait_idle(self) -> None:
        """供 CLI 初始化和验证等待已接受的操作，不取消底层命令。"""
        operation = self._operation
        if operation is not None:
            await asyncio.shield(operation)

    async def sample(self) -> None:
        """低频更新选中设备；发生错误后须人工/AI 显式刷新恢复。"""
        snapshot = self.snapshot()
        if self._closed or snapshot["busy"] or self._sampling or not snapshot["selection"]:
            return
        if snapshot["connection"] not in {"connected", "busy"}:
            return
        try:
            self._session_ready()
        except RuntimeError as exc:
            with self._lock:
                self._state.update(
                    connection="busy" if self.session.is_alive else "disconnected", error=str(exc),
                )
            return
        self._sampling = True
        try:
            current = await self._inspect(snapshot["selection"])
            self._publish(current)
        except Exception as exc:
            with self._lock:
                self._state.update(connection="error", error=str(exc) or type(exc).__name__)
        finally:
            self._sampling = False

    def start(self) -> None:
        if self._poller is None:
            self._poller = self._loop.create_task(self._poll())

    async def _poll(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._poll_interval)
            await self.sample()

    def _http_submit(self, request: dict) -> dict:
        """HTTP 线程只投递短提交；回执超时不能自动重放设备动作。"""
        future: concurrent.futures.Future = concurrent.futures.Future()

        def accept():
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(self.submit(request, source="manual"))
            except Exception as exc:
                future.set_exception(exc)

        self._loop.call_soon_threadsafe(accept)
        try:
            return future.result(timeout=2)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RuntimeError("提交回执超时，请读取操作记录核对，不要自动重试") from exc

    def open_view(self) -> str:
        from vivado_mcp.debug_http import DebugHTTP
        self.start()
        if self._http is None:
            self._http = DebugHTTP(self.snapshot, self._http_submit)
        return self._http.start()

    async def close(self) -> None:
        """释放本服务；不停止采集、不关闭 Hardware Manager/Vivado。"""
        if self.experiment is not None:
            self.experiment.interrupt("service_closed")
        self._closed = True
        if self._poller:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
            self._poller = None
        if self._http:
            await asyncio.to_thread(self._http.close)
            self._http = None
        if self.experiment is not None:
            await self.experiment.close()
        # 已接受的短操作保留回执；不能取消后重新交给另一服务执行。
        await self.wait_idle()


class DebugRegistry:
    """每个实际会话一个共享服务；同名重连不能复用旧设备选择。"""

    def __init__(self):
        self._services: dict[str, DebugService] = {}
        self._lifecycle = asyncio.Lock()
        self._closed = False

    async def get(self, session) -> DebugService:
        async with self._lifecycle:
            if self._closed:
                raise RuntimeError("调试服务正在关闭")
            current = self._services.get(session.session_id)
            if current and current.session is session:
                return current
            if current:
                await current.close()
            service = DebugService(session)
            self._services[session.session_id] = service
            return service

    def last(self, session_id: str) -> DebugService | None:
        return self._services.get(session_id)

    async def release(self, session_id: str) -> bool:
        async with self._lifecycle:
            service = self._services.get(session_id)
            if service:
                await service.close()
                self._services.pop(session_id)
            return service is not None

    async def close(self) -> None:
        async with self._lifecycle:
            self._closed = True
            for service in self._services.values():
                await service.close()
            self._services.clear()
