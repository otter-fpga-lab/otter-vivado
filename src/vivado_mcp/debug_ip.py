"""为现有 RTL 准备 ILA/VIO IP 与例化片段，不替用户改写业务连接。

离线计划不要求安装 Vivado。在线操作只经 BaseSession.execute 执行；先核对
当前工程与 IP 目录，创建后再检查该版本实际公开的 CONFIG 属性并读回配置。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vivado_mcp import tcl_scripts as scripts
from vivado_mcp.vivado.tcl_utils import tcl_quote

if TYPE_CHECKING:
    from vivado_mcp.vivado.base_session import BaseSession


class DebugIPError(RuntimeError):
    """只读传输或应用协议失败，不能解释成预检成功。"""


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# 模板采用 Verilog/SystemVerilog 共同的简单标识符子集，拒绝关键字和转义标识符。
_KEYWORDS = frozenset(
    """accept_on alias always always_comb always_ff always_latch and assert assign assume
    automatic before begin bind bins binsof bit break buf bufif0 bufif1 byte case casex casez
    cell chandle checker class clocking cmos config const constraint context continue cover
    covergroup coverpoint cross deassign default defparam design disable dist do edge else end
    endcase endchecker endclass endclocking endconfig endfunction endgenerate endgroup
    endinterface endmodule endpackage endprimitive endprogram endproperty endspecify endsequence
    endtable endtask enum event eventually expect export extends extern final first_match for
    force foreach forever fork forkjoin function generate genvar global highz0 highz1 if iff
    ifnone ignore_bins illegal_bins implements implies import incdir include initial inout
    input inside int integer interconnect interface intersect join join_any join_none large
    let liblist library local localparam logic longint macromodule matches medium modport module
    nand negedge nettype new nexttime nmos nor noshowcancelled not notif0 notif1 null or output
    package packed parameter pmos posedge primitive priority program property protected pull0
    pull1 pulldown pullup pulsestyle_ondetect pulsestyle_onevent pure rand randc randcase
    randsequence rcmos real realtime ref reg reject_on release repeat restrict return rnmos
    rpmos rtran rtranif0 rtranif1 s_always s_eventually s_nexttime s_until s_until_with scalared
    sequence shortint shortreal showcancelled signed small soft solve specify specparam static
    string strong strong0 strong1 struct super supply0 supply1 sync_accept_on sync_reject_on
    table tagged task this throughout time timeprecision timeunit tran tranif0 tranif1 tri tri0
    tri1 triand trior trireg type typedef union unique unique0 unsigned until until_with untyped
    use uwire var vectored virtual void wait wait_order wand weak weak0 weak1 while wildcard
    wire with within wor xnor xor""".split()
)


def _fields(value: object, allowed: set[str], required: set[str], label: str) -> dict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} 必须是以字符串为键的对象")
    if set(value) - allowed:
        raise ValueError(f"{label} 含未知字段: {', '.join(sorted(set(value) - allowed))}")
    if required - set(value):
        raise ValueError(f"{label} 缺少字段: {', '.join(sorted(required - set(value)))}")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or value in _KEYWORDS:
        raise ValueError(f"{label} 必须是非关键字的简单 Verilog 标识符；不接受层次名、位选或表达式")
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} 必须是 {minimum}..{maximum} 范围的整数")
    return value


def _probes(value: object, label: str, maximum_count: int, maximum_width: int) -> list[dict]:
    if not isinstance(value, list) or len(value) > maximum_count:
        raise ValueError(f"{label} 必须是最多 {maximum_count} 项的列表")
    result, names = [], set()
    output = label == "outputs"
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        _fields(
            item,
            {"name", "width", "initial"} if output else {"name", "width"},
            {"name", "width"},
            item_label,
        )
        name = _identifier(item["name"], item_label + ".name")
        if name in names:
            raise ValueError(f"{label} 中信号 {name} 重复")
        names.add(name)
        width = _integer(item["width"], item_label + ".width", 1, maximum_width)
        probe = {"name": name, "width": width}
        if output:
            initial = item.get("initial", 0)
            if isinstance(initial, str):
                if len(initial) > 80 or not re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", initial):
                    raise ValueError(f"{item_label}.initial 必须是无符号十进制或 0x 十六进制整数")
                initial = int(initial, 16 if initial.lower().startswith("0x") else 10)
            initial = _integer(initial, item_label + ".initial", 0, (1 << width) - 1)
            # JSON 客户端可能用双精度数字，十六进制字符串保留最多 256 位的全部位元。
            probe["initial"] = f"0x{initial:X}"
        result.append(probe)
    return result


def validate_debug_ip_spec(spec: dict) -> dict:
    """规范化窄用途 IP 描述；宽总线按一个探针对接，不生成拼接或隐式位截断。"""
    if not isinstance(spec, dict) or spec.get("kind") not in ("ila_ip", "vio_ip"):
        raise ValueError("kind 必须是 ila_ip 或 vio_ip")
    common = {"kind", "name", "clock"}
    ila = spec["kind"] == "ila_ip"
    _fields(
        spec,
        common | ({"probes", "depth"} if ila else {"inputs", "outputs"}),
        common | ({"probes"} if ila else set()),
        "spec",
    )
    result = {
        "kind": spec["kind"],
        "name": _identifier(spec["name"], "name"),
        "clock": _identifier(spec["clock"], "clock"),
    }
    if ila:
        depth = _integer(spec.get("depth", 1024), "depth", 1024, 131072)
        if depth & (depth - 1):
            raise ValueError("depth 必须是 2 的幂")
        result["depth"] = depth
        result["probes"] = _probes(spec["probes"], "probes", 1024, 1024)
        if not result["probes"]:
            raise ValueError("ILA 至少需要一个 probe")
    else:
        result["inputs"] = _probes(spec.get("inputs", []), "inputs", 256, 256)
        result["outputs"] = _probes(spec.get("outputs", []), "outputs", 256, 256)
        if not result["inputs"] and not result["outputs"]:
            raise ValueError("VIO 至少需要一个输入或输出 probe")
        if any(probe["name"] == result["clock"] for probe in result["outputs"]):
            raise ValueError("VIO 输出不能驱动自身的采样时钟")
    signal_widths = {result["clock"]: 1}
    groups = [result["probes"]] if ila else [result["inputs"], result["outputs"]]
    for group in groups:
        for probe in group:
            name, width = probe["name"], probe["width"]
            if name in signal_widths and signal_widths[name] != width:
                raise ValueError(f"同一个完整信号 {name} 的 width 必须一致，采样时钟宽度为 1")
            signal_widths[name] = width
    return result


def _configuration(spec: dict) -> dict[str, int | str]:
    if spec["kind"] == "ila_ip":
        values = {
            "CONFIG.C_NUM_OF_PROBES": len(spec["probes"]),
            "CONFIG.C_DATA_DEPTH": spec["depth"],
        }
        values.update(
            {
                f"CONFIG.C_PROBE{index}_WIDTH": probe["width"]
                for index, probe in enumerate(spec["probes"])
            }
        )
        return values
    values = {
        "CONFIG.C_NUM_PROBE_IN": len(spec["inputs"]),
        "CONFIG.C_NUM_PROBE_OUT": len(spec["outputs"]),
    }
    for direction, probes in (("IN", spec["inputs"]), ("OUT", spec["outputs"])):
        for index, probe in enumerate(probes):
            values[f"CONFIG.C_PROBE_{direction}{index}_WIDTH"] = probe["width"]
            if direction == "OUT":
                values[f"CONFIG.C_PROBE_OUT{index}_INIT_VAL"] = probe["initial"]
    return values


def _verilog(spec: dict) -> str:
    connections = [f"    .clk({spec['clock']})"]
    groups = (
        (("probe", spec["probes"]),)
        if spec["kind"] == "ila_ip"
        else (("probe_in", spec["inputs"]), ("probe_out", spec["outputs"]))
    )
    for prefix, probes in groups:
        for index, probe in enumerate(probes):
            connections.append(f"    .{prefix}{index}({probe['name']})")
    return f"{spec['name']} u_{spec['name']} (\n" + ",\n".join(connections) + "\n);\n"


def plan_debug_ip(spec: dict) -> dict:
    """返回离线配置、完整 Tcl 与可复制片段；生成计划不会执行其中的 Tcl。"""
    spec = validate_debug_ip_spec(spec)
    return {
        "status": "planned",
        "spec": spec,
        "configuration": _configuration(spec),
        "tcl": _script(spec, apply=True),
        "verilog": _verilog(spec),
        "connection_policy": (
            "每个探针连接一个完整的已声明信号；宽度必须一致，不自动拼接、切片或声明信号。"
        ),
        "remaining_steps": [
            "在线 inspect 核对工程、器件与本机 IP 定义；CONFIG 能力只能在新 IP 创建后核对。",
            "执行 apply 创建 IP 与生成输出产物；此操作不会修改业务 RTL 或自动例化。",
            "将 Verilog 片段集成到所属 RTL，核对现有信号宽度、唯一实例名与持续运行的采样时钟。",
            "VIO 输出只连接预先设计好的控制路径，检查多驱动、跨时钟同步与参数生效协议。",
            "集成后再显式综合、实现，生成并核对配套 bit/ltx，最后按需下载和调试。",
        ],
    }


def _project_identity(value: dict) -> dict[str, str]:
    _fields(value, {"name", "directory", "part"}, {"name", "directory", "part"}, "expected_project")
    for key, item in value.items():
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError(f"expected_project.{key} 必须是非空且不含 NUL 的字符串")
    return dict(value)


def _script(spec: dict, *, apply: bool, expected_project: dict | None = None) -> str:
    configuration = _configuration(spec)
    declarations = [
        f"set __name {tcl_quote(spec['name'])}",
        f"set __kind {tcl_quote(spec['kind'].removesuffix('_ip'))}",
        f"set __apply {int(apply)}",
        "set __configuration [dict create "
        + " ".join(
            f"{tcl_quote(key)} {tcl_quote(str(value))}" for key, value in configuration.items()
        )
        + "]",
        "set __expected [dict create "
        + " ".join(
            f"{tcl_quote(key)} {tcl_quote(value)}"
            for key, value in (expected_project or {}).items()
        )
        + "]",
    ]
    return "apply {{} {\n" + "\n".join(declarations) + "\n" + scripts.DEBUG_IP_EXECUTE + "\n}}\n"


def _response(output: str, configuration: dict) -> dict:
    result = {"project": None, "ipdef": None, "readback": {}, "available_properties": []}
    done, summary = 0, 0
    for line in output.splitlines():
        if line == "VMCP_DEBUG_IP_DONE:1":
            done += 1
            continue
        if not line.startswith("VMCP_DEBUG_IP_RECORD:"):
            continue
        kind, *encoded = line.split(":", 1)[1].split("|")
        try:
            fields = [bytes.fromhex(item).decode("utf-8") for item in encoded]
            if kind == "project":
                if result["project"] is not None:
                    raise ValueError("Duplicate project identity")
                name, directory, part = fields
                result["project"] = {"name": name, "directory": directory, "part": part}
            elif kind == "ipdef":
                if result["ipdef"] is not None:
                    raise ValueError("Duplicate IP definition")
                (result["ipdef"],) = fields
            elif kind == "property":
                (property_name,) = fields
                if property_name in result["available_properties"]:
                    raise ValueError("Duplicate property")
                result["available_properties"].append(property_name)
            elif kind == "readback":
                name, value = fields
                if name in result["readback"]:
                    raise ValueError("Duplicate readback")
                result["readback"][name] = value
            elif kind == "result":
                status, stage, attempted, created, generated, error = fields
                if status not in {"ready", "created", "blocked", "partial"}:
                    raise ValueError("Unknown status")
                if attempted not in {"0", "1"} or created not in {"0", "1", "unknown"}:
                    raise ValueError("Invalid mutation state")
                if generated not in {"0", "1"}:
                    raise ValueError("Invalid generation state")
                result.update(
                    status=status,
                    stage=stage,
                    mutation_attempted=attempted == "1",
                    created=None if created == "unknown" else created == "1",
                    generated=generated == "1",
                    error=error or None,
                )
                summary += 1
            else:
                raise ValueError("Unknown record")
        except (ValueError, UnicodeError) as exc:
            raise DebugIPError("调试 IP 响应结构损坏") from exc
    if done != 1 or summary != 1:
        raise DebugIPError("调试 IP 响应不完整，未收到唯一结果与应用结束标记")
    if result["status"] in {"ready", "created"} and (
        result["project"] is None or result["ipdef"] is None or result["error"] is not None
    ):
        raise DebugIPError("调试 IP 响应缺少工程或 IP 定义")
    if result["status"] == "created":
        if (
            not all(result[key] for key in ("mutation_attempted", "created", "generated"))
            or result["stage"] != "complete"
            or set(result["readback"]) != set(configuration)
            or set(result["available_properties"]) != set(configuration)
        ):
            raise DebugIPError("调试 IP 响应缺少完整配置读回或生成完成状态")
        for name, expected in configuration.items():
            observed = result["readback"][name]
            try:
                observed = int(observed, 16 if observed.lower().startswith("0x") else 10)
                expected = int(str(expected), 16 if str(expected).lower().startswith("0x") else 10)
            except ValueError as exc:
                raise DebugIPError("调试 IP 配置读回不是整数") from exc
            if observed != expected:
                raise DebugIPError("调试 IP 配置读回与请求不一致")
    result["configuration_validation"] = (
        "verified"
        if result["stage"] in {"generate_target", "complete"}
        else "not_verified"
        if result["created"]
        else "requires_creation"
    )
    return result


class DebugIPPreparation:
    """人和智能体共用的 IP 准备操作，不启动综合、实现或下载。"""

    def __init__(self, session: BaseSession):
        self.session = session

    async def inspect(self, spec: dict) -> dict:
        """只读核对当前工程和可用定义，返回 apply 必须复核的工程身份。"""
        plan = plan_debug_ip(spec)
        result = await self.session.execute(_script(plan["spec"], apply=False), timeout=30.0)
        if result.is_error:
            raise DebugIPError(f"Vivado 预检失败 (rc={result.return_code}): {result.output}")
        return {**plan, **_response(result.output, plan["configuration"])}

    async def apply(self, spec: dict, expected_project: dict) -> dict:
        """重新核对工程后创建新 IP；失败保留现场，不复用、删除、升级或自动重试。"""
        plan = plan_debug_ip(spec)
        identity = _project_identity(expected_project)
        try:
            result = await self.session.execute(
                _script(plan["spec"], apply=True, expected_project=identity), timeout=120.0
            )
            if result.is_error:
                raise DebugIPError(f"Vivado 命令失败 (rc={result.return_code}): {result.output}")
            response = _response(result.output, plan["configuration"])
        except Exception as exc:
            # 超时或协议丢失后无法证明 create_ip 没有执行，不把它降格成无副作用失败。
            response = {
                "status": "partial",
                "stage": "transport",
                "mutation_attempted": None,
                "created": None,
                "generated": None,
                "error": str(exc),
                "project": None,
                "ipdef": None,
                "readback": {},
                "available_properties": [],
                "configuration_validation": "unknown",
            }
        if response["status"] == "partial":
            response["recovery"] = (
                "保留现有现场；先检查工程中的同名 IP 与生成日志，不自动重试或删除。"
            )
        return {**plan, **response}
