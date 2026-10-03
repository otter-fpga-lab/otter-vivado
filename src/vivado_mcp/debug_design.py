"""调试工程计划与约束注册；生成产物和运行实现的状态保持分开。"""

from __future__ import annotations

import copy
import re

from vivado_mcp import tcl_scripts as scripts
from vivado_mcp.vivado.tcl_utils import tcl_quote


class DebugDesignError(RuntimeError):
    """工程准备的传输/协议失败，不代表操作已回滚。"""


def _identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"{field} 必须是简单标识符")
    if len(value) > 120:
        raise ValueError(f"{field} 过长")
    return value


def _nets(value, field):
    if not isinstance(value, list) or not 1 <= len(value) <= 1024:
        raise ValueError(f"{field} 必须是 1~1024 个完整 net 名称")
    if any(not isinstance(n, str) or not n or len(n) > 1024 or "\x00" in n for n in value):
        raise ValueError(f"{field} 含有无效 net 名称")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} 含重复 net")
    return list(value)


def _list(values):
    # 约束文件保持 ASCII；Vivado 在 Windows 按本地编码 source 时也不损坏 net 名称。
    return "[list " + " ".join(
        f"[encoding convertfrom utf-8 [binary format H* {value.encode('utf-8').hex()}]]"
        for value in values
    ) + "]"


def _constraint_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("spec 必须是对象")
    kind = spec.get("kind")
    if kind == "ila_netlist":
        required = {"kind", "name", "clock", "probes"}
        optional = {"run", "depth"}
    elif kind == "mark_debug":
        required = {"kind", "name", "nets"}
        optional = {"run"}
    else:
        raise ValueError("kind 应为 ila_netlist、mark_debug、ila_ip 或 vio_ip")
    if not required <= spec.keys() or spec.keys() - required - optional:
        raise ValueError(f"{kind} 字段不符合约定")
    result = copy.deepcopy(spec)
    result["name"] = _identifier(spec["name"], "name")
    result["run"] = spec.get("run", "impl_1" if kind == "ila_netlist" else "synth_1")
    if (not isinstance(result["run"], str) or not result["run"]
            or len(result["run"]) > 256 or "\x00" in result["run"]):
        raise ValueError("run 必须是实际运行的完整名称")
    if kind == "mark_debug":
        result["nets"] = _nets(spec["nets"], "nets")
        return result
    result["clock"] = _nets([spec["clock"]], "clock")[0]
    depth = spec.get("depth", 1024)
    if type(depth) is not int or not 1024 <= depth <= 131072 or depth & (depth - 1):
        raise ValueError("depth 必须是 1024~131072 之间的 2 的幂；实际器件能力由 Vivado 验证")
    result["depth"] = depth
    probes = spec["probes"]
    if not isinstance(probes, list) or not 1 <= len(probes) <= 256:
        raise ValueError("probes 必须是 1~256 组探针")
    all_nets = []
    for i, probe in enumerate(probes):
        if not isinstance(probe, dict) or set(probe) != {"nets"}:
            raise ValueError("每个 probe 仅包含按 bit 0 开始排序的 nets 列表")
        all_nets.extend(_nets(probe["nets"], f"probes[{i}].nets"))
    if len(all_nets) > 4096 or len(all_nets) != len(set(all_nets)):
        raise ValueError("首批探针总位数最多 4096，且不能重复观察同一 net")
    return result


def _constraint_plan(spec):
    spec = _constraint_spec(spec)
    ila = spec["kind"] == "ila_netlist"
    groups = [probe["nets"] for probe in spec["probes"]] if ila else [spec["nets"]]
    names = ([spec["clock"]] if ila else []) + [n for group in groups for n in group]
    patterns = ["^" + re.escape(n) + "$" for n in names]
    declarations = (
        f"set __names {_list(names)}\n"
        f"set __patterns {_list(patterns)}\n"
        f"set __name {tcl_quote(spec['name'])}\n"
    )
    body = scripts.DESIGN_RESOLVE_NETS
    if ila:
        declarations += f"set __depth {spec['depth']}\n"
        declarations += f"set __widths [list {' '.join(str(len(g)) for g in groups)}]\n"
        body += scripts.DESIGN_CREATE_ILA
    else:
        body += "set_property MARK_DEBUG true $__resolved\n"
    # unmanaged Tcl 支持严格断言；普通 XDC 不支持 if/foreach/apply。
    content = (
        "# Otter Vivado generated unmanaged Tcl constraints.\n"
        "# Exact net names; probe lists are ordered from bit 0.\n"
        "apply {{} {\n" + declarations + body + "\n}}\n"
    )
    filename = f"{spec['name']}_{'ila' if ila else 'mark_debug'}.tcl"
    return {
        "status": "planned", "kind": spec["kind"], "spec": spec,
        "artifacts": [{"filename": filename, "content": content}],
        "used_in_synthesis": not ila, "used_in_implementation": ila,
        "processing_order": "LATE", "required_nets": names,
        "probe_map": [
            {"port": f"probe{i}", "bits": [{"bit": j, "net": n} for j, n in enumerate(group)]}
            for i, group in enumerate(groups)
        ] if ila else [],
        "remaining_steps": [
            "将本脚本加入指定 run 的约束集；prepare_debug_design 可完成注册。",
            "通过已有构建入口执行受影响 run，并检查原始日志、资源和时序报告。",
            "在实际综合/实现设计核对 MARK_DEBUG、调试核及连接，生成配套 bit/ltx。",
        ],
        "limitations": [
            "计划不能证明时钟自由运行、所有探针同域或资源/时序可满足。",
            "实现阶段标记不能恢复已被综合优化掉的 net；保留信号请先执行 mark_debug 方案。",
            "每组 nets[0] 明确连接 probe bit 0；不从显示顺序推断总线位序。",
        ],
    }


def plan_debug_design(spec: dict) -> dict:
    """不连接 EDA，返回可保存的工程计划；不复制或重写业务 RTL。"""
    if not isinstance(spec, dict):
        raise ValueError("spec 必须是对象")
    if spec.get("kind") in {"ila_ip", "vio_ip"}:
        from vivado_mcp.debug_ip import plan_debug_ip
        plan = plan_debug_ip(spec)
        name = plan["spec"]["name"]
        plan["artifacts"] = [
            {"filename": f"{name}_create.tcl", "content": plan["tcl"]},
            {"filename": f"{name}_instance.vh", "content": plan["verilog"]},
        ]
        return plan
    return _constraint_plan(spec)


def _project_identity(value):
    if not isinstance(value, dict) or set(value) != {"name", "directory", "part"}:
        raise ValueError("expected_project 必须使用 inspect 返回的 name/directory/part")
    if any(not isinstance(v, str) or not v or "\x00" in v for v in value.values()):
        raise ValueError("expected_project 含空或无效字段")
    return value


class DebugDesignPreparation:
    """检查当前 project/run，注册生成约束；不打开或修改当前内存设计。"""

    def __init__(self, session):
        self.session = session

    async def inspect(self, spec: dict) -> dict:
        if not isinstance(spec, dict):
            raise ValueError("spec 必须是对象")
        if spec.get("kind") in {"ila_ip", "vio_ip"}:
            from vivado_mcp.debug_ip import DebugIPPreparation
            return await DebugIPPreparation(self.session).inspect(spec)
        return await self._constraints(spec, expected_project=None)

    async def apply(self, spec: dict, expected_project: dict) -> dict:
        _project_identity(expected_project)
        if not isinstance(spec, dict):
            raise ValueError("spec 必须是对象")
        if spec.get("kind") in {"ila_ip", "vio_ip"}:
            from vivado_mcp.debug_ip import DebugIPPreparation
            return await DebugIPPreparation(self.session).apply(
                spec, expected_project=expected_project,
            )
        return await self._constraints(spec, expected_project=expected_project)

    async def _constraints(self, spec, expected_project):
        plan = _constraint_plan(spec)
        artifact = plan["artifacts"][0]
        values = {
            "run_name": plan["spec"]["run"], "filename": artifact["filename"],
            "content": artifact["content"], "kind": plan["kind"],
            "synthesis": "0" if plan["kind"] == "ila_netlist" else "1",
            "write": "1" if expected_project else "0",
        }
        for key in ("name", "directory", "part"):
            values[f"expected_{key}"] = expected_project[key] if expected_project else ""
        declarations = "\n".join(f"set __{k} {tcl_quote(v)}" for k, v in values.items())
        command = scripts.DESIGN_PREPARE_CONSTRAINTS.replace("__DECLARATIONS__", declarations)
        try:
            result = await self.session.execute(command, timeout=30.0)
        except Exception as exc:
            raise DebugDesignError("工程准备结果未知，请核对项目与生成文件，不自动重试") from exc
        if result.is_error:
            raise DebugDesignError(f"Vivado 返回错误，操作可能部分生效：{result.output}")
        fields = {}
        affected = []
        done = 0
        for line in result.output.splitlines():
            if line == "VMCP_DESIGN_DONE:1":
                done += 1
            elif line.startswith("VMCP_DESIGN_FIELD:"):
                try:
                    key, raw = line.split(":", 1)[1].split("|", 1)
                    value = bytes.fromhex(raw).decode("utf-8")
                except (ValueError, UnicodeError) as exc:
                    raise DebugDesignError("工程准备响应损坏，结果未知") from exc
                if key == "affected_run":
                    affected.append(value)
                elif key in fields:
                    raise DebugDesignError("工程准备响应包含重复字段")
                else:
                    fields[key] = value
        if done != 1 or fields.get("status") not in {
            "ready", "constraints_added", "blocked", "partial",
        }:
            raise DebugDesignError("工程准备响应不完整，结果未知")
        identity = {key: fields.get(key, "") for key in ("name", "directory", "part")}
        if fields["status"] in {"ready", "constraints_added"}:
            applying = expected_project is not None
            wanted_status = "constraints_added" if applying else "ready"
            wanted_flag = "1" if applying else "0"
            if (not all(identity.values()) or not fields.get("path") or not fields.get("constrset")
                    or plan["spec"]["run"] not in affected or fields["status"] != wanted_status
                    or fields.get("file_created") != wanted_flag
                    or fields.get("registered") != wanted_flag):
                raise DebugDesignError("工程准备成功响应缺少证据或与操作阶段不符，结果未知")
        return {
            "status": fields["status"], "project": identity, "plan": plan,
            "run": plan["spec"]["run"], "constraint_set": fields.get("constrset"),
            "affected_runs": affected, "path": fields.get("path"), "error": fields.get("error"),
            "file_created": fields.get("file_created") == "1",
            "registered": fields.get("registered") == "1", "design_modified": False,
            "built": False, "connections_verified": False,
        }
