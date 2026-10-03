"""人和 AI 共用的板上调试后端；只处理已经打开的硬件目标。

命令集中在 tcl_scripts，输出使用 UTF-8 十六进制字段避免信号名换行、分隔符
或 Tcl 元字符干扰协议。所有操作显式选择目标、器件和调试核，不改 current_*。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vivado_mcp import tcl_scripts as scripts
from vivado_mcp.vivado.tcl_utils import tcl_quote

if TYPE_CHECKING:
    from vivado_mcp.vivado.base_session import BaseSession


class DebugBackendError(RuntimeError):
    """硬件命令或应用层协议失败；不得解释成成功或空设备列表。"""


def _integer(value: str) -> int | None:
    """缺失或未知整数保留为 null。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional(value: str) -> str | None:
    return value if value else None


def _name(value: str, field: str) -> str:
    """允许合法层次名和特殊字符，只拒绝空值、NUL 与非字符串。"""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} 必须是非空且不含 NUL 的字符串")
    return value


def _parse_records(output: str) -> list[tuple[str, list[str]]]:
    """仅解析应用前缀，任一错误或缺少结束记录均视为失败。"""
    records = []
    done = False
    for line in output.splitlines():
        if line.startswith("VMCP_DEBUG_ERR:"):
            try:
                message = bytes.fromhex(line.split(":", 1)[1]).decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise DebugBackendError("硬件调试错误记录损坏") from exc
            raise DebugBackendError(message)
        if line == "VMCP_DEBUG_DONE:1":
            done = True
        elif line.startswith("VMCP_DEBUG_RECORD:"):
            kind, *fields = line.split(":", 1)[1].split("|")
            try:
                records.append((kind, [bytes.fromhex(field).decode("utf-8") for field in fields]))
            except (ValueError, UnicodeError) as exc:
                raise DebugBackendError("硬件调试响应记录损坏") from exc
    if not done:
        raise DebugBackendError("硬件调试响应不完整，未收到应用结束标记")
    return records


class HardwareDebugBackend:
    """通过 BaseSession.execute 共用 GUI/TCP 与无头/stdio 两条传输路径。"""

    def __init__(self, session: BaseSession):
        self.session = session

    async def _execute(self, script: str, **values: str) -> list[tuple[str, list[str]]]:
        # 所有外部字符串都走 tcl_quote；不把名称当作 get_* 的 glob/filter。
        declarations = "\n".join(f"set __{key} {tcl_quote(value)}" for key, value in values.items())
        command = scripts.DEBUG_EXECUTE.format(
            script=scripts.DEBUG_COMMON + declarations + "\n" + script
        )
        result = await self.session.execute(command, timeout=30.0)
        if result.is_error:
            raise DebugBackendError(f"Vivado 命令失败 (rc={result.return_code}): {result.output}")
        return _parse_records(result.output)

    @staticmethod
    def _selection(
        target: str, device: str, core: str | None = None, expected_uuid: str | None = None
    ) -> dict[str, str]:
        values = {"target_name": _name(target, "target"), "device_name": _name(device, "device")}
        if core is not None:
            values["core_name"] = _name(core, "core")
            values["expected_uuid"] = (
                _name(expected_uuid, "expected_uuid") if expected_uuid is not None else ""
            )
        return values

    async def inventory(self) -> dict:
        """列出已连接服务器公开的目标；关闭目标不枚举器件也不自动打开。"""
        records = await self._execute(scripts.DEBUG_INVENTORY)
        targets = {}
        try:
            for kind, fields in records:
                if kind == "target":
                    name, opened = fields
                    targets[name] = {"name": name, "is_open": opened == "1", "devices": []}
                elif kind == "device":
                    target, name, part = fields
                    targets[target]["devices"].append({"name": name, "part": part})
                else:
                    raise ValueError(f"Unknown inventory record: {kind}")
        except (ValueError, KeyError) as exc:
            raise DebugBackendError("硬件枚举响应结构损坏") from exc
        return {"targets": list(targets.values())}

    async def inspect(self, target: str, device: str) -> dict:
        """读取调试核与探针；VIO 硬件读回后恢复 GUI 暂存的输出属性。"""
        records = await self._execute(
            scripts.DEBUG_SELECT_DEVICE + scripts.DEBUG_INSPECT, **self._selection(target, device)
        )
        result = {"target": target, "device": device, "part": "", "ilas": [], "vios": []}
        ilas, vios = {}, {}
        selected = False
        try:
            for kind, fields in records:
                if kind == "selected":
                    actual_target, actual_device, result["part"] = fields
                    if (actual_target, actual_device) != (target, device):
                        raise ValueError("Selection mismatch")
                    selected = True
                elif kind == "ila":
                    (
                        name,
                        uuid,
                        status,
                        depth,
                        position,
                        samples,
                        complete,
                        windows,
                        mode,
                        condition,
                    ) = fields
                    ilas[name] = {
                        "name": name,
                        "uuid": _optional(uuid),
                        "status": status,
                        "depth": _integer(depth),
                        "trigger_position": _integer(position),
                        "sample_count": _integer(samples),
                        "capture_complete": complete == "1",
                        "window_count": _integer(windows),
                        "trigger_mode": mode,
                        "trigger_condition": condition,
                        "probes": [],
                    }
                elif kind == "ila_probe":
                    core, name, width, trigger = fields
                    ilas[core]["probes"].append(
                        {
                            "name": name,
                            "width": _integer(width),
                            "trigger_value": _optional(trigger),
                        }
                    )
                elif kind == "vio":
                    name, uuid = fields
                    vios[name] = {"name": name, "uuid": _optional(uuid), "probes": []}
                elif kind == "vio_probe":
                    core, name, direction, width, value, staged = fields
                    vios[core]["probes"].append(
                        {
                            "name": name,
                            "direction": direction,
                            "width": _integer(width),
                            "value": _optional(value),
                            "staged_value": _optional(staged),
                        }
                    )
                else:
                    raise ValueError(f"Unknown inspect record: {kind}")
            if not selected:
                raise ValueError("Missing selected device")
        except (ValueError, KeyError) as exc:
            raise DebugBackendError("硬件调试状态响应结构损坏") from exc
        result["ilas"], result["vios"] = list(ilas.values()), list(vios.values())
        return result

    async def _mutate(
        self,
        operation: str,
        body: str,
        target: str,
        device: str,
        core: str,
        expected_uuid: str | None,
        **values: str,
    ) -> list[str]:
        records = await self._execute(
            scripts.DEBUG_SELECT_DEVICE + body,
            **self._selection(target, device, core, expected_uuid),
            **values,
        )
        if len(records) != 1 or records[0][0] != operation:
            raise DebugBackendError("硬件操作未返回唯一结果")
        return records[0][1]

    async def write_vio(
        self,
        target: str,
        device: str,
        core: str,
        probe: str,
        value: str,
        expected_uuid: str | None = None,
    ) -> dict:
        """对单个已知宽度的输出提交无符号整数；返回硬件读回而非功能生效断言。"""
        if (
            not isinstance(value, str)
            or len(value) > 4096
            or not re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", value)
        ):
            raise ValueError("VIO value 必须是无符号十进制或 0x 十六进制整数字符串")
        value_hex = format(int(value, 16 if value.lower().startswith("0x") else 10), "X")
        fields = await self._mutate(
            "write",
            scripts.DEBUG_SELECT_VIO + scripts.DEBUG_CHECK_UUID + scripts.DEBUG_WRITE_VIO,
            target,
            device,
            core,
            expected_uuid,
            probe_name=_name(probe, "probe"),
            value_hex=value_hex,
        )
        try:
            actual_core, actual_probe, width, written, readback, staged, previous = fields
            matches = int(readback, 16) == int(written, 16)
        except ValueError as exc:
            raise DebugBackendError("VIO 已提交，但硬件读回响应无法解析") from exc
        return {
            "core": actual_core,
            "probe": actual_probe,
            "width": _integer(width),
            "committed": True,
            "requested_hex": written,
            "value": readback,
            "readback_matches": matches,
            "staged_value": staged,
            "previous_staged_value": previous,
            "functional_effect_verified": False,
        }

    async def configure_ila(
        self,
        target: str,
        device: str,
        core: str,
        probe: str,
        trigger_value: str,
        trigger_position: int | None = None,
        expected_uuid: str | None = None,
    ) -> dict:
        """只修改指定探针的基本触发比较值，不清空其他探针或改触发组合方式。"""
        if trigger_position is not None and (
            isinstance(trigger_position, bool)
            or not isinstance(trigger_position, int)
            or trigger_position < 0
        ):
            raise ValueError("trigger_position 必须为非负整数")
        fields = await self._mutate(
            "configure",
            scripts.DEBUG_SELECT_ILA
            + scripts.DEBUG_CHECK_UUID
            + scripts.DEBUG_CHECK_IDLE
            + scripts.DEBUG_CONFIGURE_ILA,
            target,
            device,
            core,
            expected_uuid,
            probe_name=_name(probe, "probe"),
            trigger_value=_name(trigger_value, "trigger_value"),
            trigger_position="" if trigger_position is None else str(trigger_position),
        )
        if len(fields) != 4:
            raise DebugBackendError("ILA 配置响应结构损坏")
        return {
            "core": fields[0],
            "probe": fields[1],
            "trigger_value": fields[2],
            "trigger_position": _integer(fields[3]),
        }

    async def arm_ila(
        self,
        target: str,
        device: str,
        core: str,
        immediate: bool = False,
        expected_uuid: str | None = None,
    ) -> dict:
        """只在明确空闲、单窗口时启动采集，不等待触发或声称采集完成。"""
        if not isinstance(immediate, bool):
            raise ValueError("immediate 必须为布尔值")
        fields = await self._mutate(
            "arm",
            scripts.DEBUG_SELECT_ILA
            + scripts.DEBUG_CHECK_UUID
            + scripts.DEBUG_CHECK_IDLE
            + scripts.DEBUG_ARM_ILA,
            target,
            device,
            core,
            expected_uuid,
            immediate="1" if immediate else "0",
        )
        if len(fields) != 2:
            raise DebugBackendError("ILA 启动响应结构损坏")
        return {"core": fields[0], "status": fields[1], "command_accepted": True}

    async def stop_ila(
        self, target: str, device: str, core: str, expected_uuid: str | None = None
    ) -> dict:
        """公开 UG835 未提供 stop_hw_ila；不能把上传的停止副作用伪装成停止接口。"""
        raise NotImplementedError("首批不支持单独停止 ILA，请在原生 Vivado Hardware Manager 操作")

    async def upload_ila(
        self, target: str, device: str, core: str, expected_uuid: str | None = None
    ) -> dict:
        """完整单窗口采集才上传；使用本次命令返回对象，不读取旧缓存对象。"""
        fields = await self._mutate(
            "upload",
            scripts.DEBUG_SELECT_ILA + scripts.DEBUG_CHECK_UUID + scripts.DEBUG_UPLOAD_ILA,
            target,
            device,
            core,
            expected_uuid,
        )
        if len(fields) != 3:
            raise DebugBackendError("ILA 上传响应结构损坏")
        return {
            "core": fields[0],
            "data": fields[1],
            "status": fields[2],
            "capture_complete": True,
            "exported": False,
        }

    async def export_ila(
        self, target: str, device: str, core: str, output_dir: str,
        expected_uuid: str | None = None,
    ) -> dict:
        """完整单窗口上传并导出 VCD；由共享服务协调，不隐式启动或停止采集。"""
        from vivado_mcp.ila_capture import export_capture

        return await export_capture(self, target, device, core, output_dir, expected_uuid)
