"""显式合成演示：修改本地模型，不连接 EDA，不按时间编造硬件结果。"""

from __future__ import annotations

import copy
import re

DEMO_TARGET = "demo://isp"
DEMO_DEVICE = "demo-fpga"


class DemoDebugBackend:
    """与硬件后端相同的短操作协议；所有返回结果都只用于界面演示。"""

    def __init__(self):
        self._hardware = {
            "target": DEMO_TARGET, "device": DEMO_DEVICE, "part": "SYNTHETIC-NO-FPGA",
            "ilas": [{
                "name": "demo_ila", "uuid": "demo-ila-v1", "status": "IDLE",
                "depth": 16, "trigger_position": 0, "sample_count": 0,
                "capture_complete": False, "window_count": 1,
                "trigger_mode": "BASIC_ONLY", "trigger_condition": "AND",
                "probes": [{"name": "frame_valid", "width": 1, "trigger_value": "eq1'b1"}],
            }],
            "vios": [{
                "name": "demo_vio", "uuid": "demo-vio-v1", "probes": [
                    {"name": "gain", "width": 12, "direction": "out",
                     "value": "100", "staged_value": "100"},
                    {"name": "bypass", "width": 1, "direction": "out",
                     "value": "0", "staged_value": "0"},
                    {"name": "frame_count", "width": 32, "direction": "in",
                     "value": "0", "staged_value": None},
                ],
            }],
        }

    def _select(self, target, device, kind=None, core=None, expected_uuid=None):
        if (target, device) != (DEMO_TARGET, DEMO_DEVICE):
            raise ValueError("演示仅包含 demo://isp / demo-fpga")
        if kind is None:
            return self._hardware
        matches = [item for item in self._hardware[kind] if item["name"] == core]
        if len(matches) != 1:
            raise ValueError("演示调试核不存在")
        selected = matches[0]
        if expected_uuid is not None and expected_uuid != selected["uuid"]:
            raise ValueError("演示调试核 UUID 不匹配")
        return selected

    @staticmethod
    def _probe(core, name):
        matches = [item for item in core["probes"] if item["name"] == name]
        if len(matches) != 1:
            raise ValueError("演示探针不存在")
        return matches[0]

    @staticmethod
    def _idle(core):
        if core["status"] != "IDLE":
            raise ValueError("演示 ILA 已在等待触发；重启 demo 可清除本地状态")

    async def inventory(self) -> dict:
        return {"targets": [{
            "name": DEMO_TARGET, "is_open": True,
            "devices": [{"name": DEMO_DEVICE, "part": self._hardware["part"]}],
        }]}

    async def inspect(self, target: str, device: str) -> dict:
        return copy.deepcopy(self._select(target, device))

    async def write_vio(self, target: str, device: str, core: str, probe: str, value: str,
                        expected_uuid: str | None = None) -> dict:
        selected = self._select(target, device, "vios", core, expected_uuid)
        pin = self._probe(selected, probe)
        if pin["direction"] != "out":
            raise ValueError("演示 VIO 输入不能写入")
        if (not isinstance(value, str) or len(value) > 4096
                or not re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", value)):
            raise ValueError("VIO value 必须是无符号十进制或 0x 十六进制整数字符串")
        number = int(value, 16 if value.lower().startswith("0x") else 10)
        if number >= 1 << pin["width"]:
            raise ValueError("VIO value 超出探针位宽")
        previous = pin["staged_value"]
        pin["value"] = pin["staged_value"] = format(number, "X")
        return {
            "source": "demo", "core": core, "probe": probe, "width": pin["width"],
            "committed": True, "requested_hex": pin["value"], "value": pin["value"],
            "readback_matches": True, "staged_value": pin["staged_value"],
            "previous_staged_value": previous, "functional_effect_verified": False,
        }

    async def configure_ila(self, target: str, device: str, core: str, probe: str,
                            trigger_value: str, trigger_position: int | None = None,
                            expected_uuid: str | None = None) -> dict:
        selected = self._select(target, device, "ilas", core, expected_uuid)
        self._idle(selected)
        pin = self._probe(selected, probe)
        if not isinstance(trigger_value, str) or not trigger_value or "\x00" in trigger_value:
            raise ValueError("trigger_value 必须为非空文本")
        if trigger_position is not None and (
            type(trigger_position) is not int or not 0 <= trigger_position < selected["depth"]
        ):
            raise ValueError("trigger_position 超出演示采样深度")
        # 演示仅保存文本，不声称检验了 Vivado 的触发语法。
        pin["trigger_value"] = trigger_value
        if trigger_position is not None:
            selected["trigger_position"] = trigger_position
        return {
            "source": "demo", "core": core, "probe": probe, "trigger_value": trigger_value,
            "trigger_position": selected["trigger_position"],
        }

    async def arm_ila(self, target: str, device: str, core: str, immediate: bool = False,
                      expected_uuid: str | None = None) -> dict:
        if type(immediate) is not bool:
            raise ValueError("immediate 必须为布尔值")
        selected = self._select(target, device, "ilas", core, expected_uuid)
        self._idle(selected)
        selected.update(
            status="IDLE" if immediate else "WAITING_FOR_TRIGGER",
            sample_count=selected["depth"] if immediate else 0,
            capture_complete=immediate,
        )
        return {
            "source": "demo", "core": core, "status": selected["status"],
            "command_accepted": True,
        }

    async def upload_ila(self, target: str, device: str, core: str,
                         expected_uuid: str | None = None) -> dict:
        selected = self._select(target, device, "ilas", core, expected_uuid)
        if not selected["capture_complete"]:
            raise ValueError("演示尚无完整采集，请先在空闲时执行立即采集")
        return {
            "source": "demo", "core": core, "data": "demo_only_ila_capture",
            "status": selected["status"], "capture_complete": True, "exported": False,
            "note": "固定合成采集；此对象不存在于 Vivado，不包含真实波形",
        }
