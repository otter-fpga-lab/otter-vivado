"""调试产物离线核对；元数据一致不能证明 bit/ltx 来自同一实现。"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from vivado_mcp.analysis.bit_header_parser import parse_bit
from vivado_mcp.analysis.ltx_parser import parse_ltx


def _uuid(value: str) -> str | None:
    compact = value.replace("-", "").strip("{}")
    return compact.lower() if re.fullmatch(r"[0-9a-fA-F]{32}", compact) else None


def _part(value: str) -> str:
    """去掉常见速度/温度后缀，保留器件与封装；不吞掉 UltraScale 封装段。"""
    value = value.lower()
    value = re.sub(r"-(?:[1-3]l?|l[1-3])(?:-[a-z0-9]+)*$", "", value)
    return value.replace("-", "")


def _name(value, field):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError(f"{field} 必须是非空名称")


def _expectations(expected):
    if expected is None:
        return {}
    if not isinstance(expected, dict) or expected.keys() - {"part", "cores"}:
        raise ValueError("expected 仅支持 part 与 cores，schema 见 docs/DEBUG_ARTIFACTS.md")
    if "part" in expected:
        _name(expected["part"], "part")
        if not re.fullmatch(r"[a-zA-Z0-9-]+", expected["part"]):
            raise ValueError("part 必须是完整器件/封装名称")
    cores = expected.get("cores", [])
    if not isinstance(cores, list) or ("cores" in expected and not 1 <= len(cores) <= 256):
        raise ValueError("cores 必须是 1~256 个预期核")
    names = set()
    for core in cores:
        if (not isinstance(core, dict) or not {"name", "type", "probes"} <= core.keys()
                or core.keys() - {"name", "type", "uuid", "probes"}):
            raise ValueError("预期核必须包含 name/type/probes，可选 uuid")
        _name(core["name"], "core.name")
        if core["name"] in names:
            raise ValueError("预期核 name 重复")
        names.add(core["name"])
        if core["type"] not in ("ILA_V3", "VIO_V2", "VIO_V3"):
            raise ValueError("预期核 type 仅支持 ILA_V3/VIO_V2/VIO_V3")
        if "uuid" in core and (not isinstance(core["uuid"], str) or not _uuid(core["uuid"])):
            raise ValueError("预期 uuid 必须是 128 位十六进制")
        probes = core["probes"]
        if not isinstance(probes, list) or not 1 <= len(probes) <= 1024:
            raise ValueError("probes 必须是 1~1024 个预期探针")
        probe_names = set()
        for probe in probes:
            if (not isinstance(probe, dict) or not {"name", "width"} <= probe.keys()
                    or probe.keys() - {"name", "width", "direction", "port_index"}):
                raise ValueError("预期探针必须包含 name/width，可选 direction/port_index")
            _name(probe["name"], "probe.name")
            if probe["name"] in probe_names:
                raise ValueError("预期探针 name 重复")
            probe_names.add(probe["name"])
            if type(probe["width"]) is not int or not 1 <= probe["width"] <= 65536:
                raise ValueError("width 必须是 1~65536 的整数")
            if "direction" in probe and probe["direction"] not in ("IN", "OUT", "INOUT"):
                raise ValueError("direction 必须是 IN/OUT/INOUT")
            if "port_index" in probe and (
                type(probe["port_index"]) is not int or not 0 <= probe["port_index"] <= 65535
            ):
                raise ValueError("port_index 必须是 0~65535 的整数")
    return expected


def _identity(path):
    stat = os.stat(path)
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def check_debug_artifacts(
    bit_path: str, ltx_path: str, expected: dict | None = None,
    manifest_path: str | None = None,
) -> dict:
    """只读解析和散列两份本地文件，核对消费者给出的实际实例/探针要求。"""
    expected = _expectations(expected)
    checks = []

    def add(code, status, detail):
        checks.append({"code": code, "status": status, "detail": detail})

    result = {
        "schema_version": 1, "status": "incomplete", "pairing": "unverified",
        "hardware_verified": False, "checks": checks, "bit": None, "ltx": None,
        "limitations": [
            "文件名、目录、时间和 LTX UUID 不能证明 .bit 内部调试核与 .ltx 匹配。",
            "SHA256 标识本次读取的文件；没有解析配置载荷的 UUID/CRC，也没有连接设备。",
            "consistent 仅表示提供的离线检查一致；不能替代构建、时序或硬件签核。",
        ],
        "next_steps": [
            "核对调试 RTL/约束并完成综合和实现，审阅原始时序、资源及 DRC 报告。",
            "从同一已实现设计导出 .bit 与 write_debug_probes 生成的 .ltx，保留构建来源。",
            "本机经授权加载对应产物后，核对实际器件、调试核及探针并做一次采集/读回。",
        ],
    }
    paths = {"bit": Path(bit_path).expanduser().absolute(),
             "ltx": Path(ltx_path).expanduser().absolute()}
    identities = {}
    for kind, path in paths.items():
        try:
            identities[kind] = _identity(path)
        except OSError as exc:
            add(f"{kind}.read", "blocked", str(exc))
    bit = ltx = None
    for kind, parser in (("bit", parse_bit), ("ltx", parse_ltx)):
        if kind not in identities:
            continue
        try:
            parsed = parser(str(paths[kind]))
            result[kind] = parsed.to_dict()
            if kind == "bit":
                bit = parsed
            else:
                ltx = parsed
        except (OSError, ValueError) as exc:
            add(f"{kind}.parse", "blocked", str(exc))
    if bit:
        add("bit.payload", "match" if bit.payload_complete else "blocked",
            f"声明 {bit.bitstream_size} 字节，实际 {bit.payload_size_actual} 字节")
        if not bit.design or not bit.part_raw:
            add("bit.header", "blocked", "缺少设计名或目标器件")
        if "part" not in expected:
            add("bit.part", "unknown", "未提供预期器件，不能判断是否选对目标")
        else:
            actual, wanted = _part(bit.part_raw), _part(expected["part"])
            # 仅为已知 xc 省略前缀的标准格式补回 xc；不能猜工业/车规家族。
            if not actual.startswith(("xc", "xa", "xq")):
                actual = "xc" + actual
            if not wanted.startswith(("xc", "xa", "xq")):
                wanted = "xc" + wanted
            add("bit.part", "match" if actual == wanted else "blocked",
                f"头部 {bit.part_raw}；预期 {expected['part']}；只核对器件/封装，不核对速度等级")
    if ltx:
        debug = [core for core in ltx.cores if core.is_ila or core.is_vio]
        if not debug:
            add("ltx.cores", "unknown", "未发现可识别的 ILA/VIO 核")
        names = Counter(core.name for core in ltx.cores)
        if any(count > 1 for count in names.values()):
            add("ltx.core_names", "blocked", "存在重复核名；可能含多个探针集，不能自动择一")
        uuids = [_uuid(core.uuid) for core in debug if _uuid(core.uuid)]
        if len(uuids) != len(set(uuids)):
            add("ltx.uuids", "blocked", "多个调试核使用同一 UUID")
        for core in ltx.cores:
            if not core.name:
                add("ltx.core_name", "unknown", "缺少核实例名")
            if not core.is_ila and not core.is_vio:
                if not core.is_dbg_hub:
                    add("ltx.core_type", "unknown", f"不支持的核类型：{core.core_type}")
                continue
            if not _uuid(core.uuid):
                add("ltx.uuid", "unknown", f"{core.name} 缺少有效 UUID")
            if not core.probes:
                add("ltx.probes", "unknown", f"{core.name} 未提供探针")
            if len({p.name for p in core.probes}) != len(core.probes):
                add("ltx.probe_names", "blocked", f"{core.name} 存在重复探针名")
            for probe in core.probes:
                if not probe.name or probe.width is None or not probe.direction:
                    add("ltx.probe_metadata", "unknown",
                        f"{core.name}/{probe.name} 缺少名称/位宽/方向")
                elif probe.direction not in {"IN", "OUT", "INOUT"}:
                    add("ltx.probe_direction", "unknown", f"未知方向：{probe.direction}")
                if probe.is_vector is False and probe.width not in (None, 1):
                    add("ltx.probe_width", "blocked", f"{core.name}/{probe.name} 标量的位宽不为 1")
        if not expected.get("cores"):
            add("ltx.expected", "unknown", "未提供预期核和探针；只列出产物中观察到的信息")
        for wanted in expected.get("cores", []):
            matches = [core for core in debug if core.name == wanted["name"]]
            if len(matches) != 1:
                add("ltx.expected_core", "blocked", f"预期核 {wanted['name']} 缺失或不唯一")
                continue
            core = matches[0]
            add("ltx.core_type", "match" if core.core_type == wanted["type"] else "blocked",
                f"{core.name}: {core.core_type}，预期 {wanted['type']}")
            if "uuid" in wanted:
                state = "unknown" if not _uuid(core.uuid) else (
                    "match" if _uuid(core.uuid) == _uuid(wanted["uuid"]) else "blocked")
                add("ltx.expected_uuid", state, f"{core.name}: {core.uuid}")
            for probe in wanted["probes"]:
                found = [p for p in core.probes if p.name == probe["name"]]
                if len(found) != 1:
                    add("ltx.expected_probe", "blocked",
                        f"{core.name}/{probe['name']} 缺失或不唯一")
                    continue
                for field in ("width", "direction", "port_index"):
                    if field not in probe:
                        continue
                    value = getattr(found[0], field)
                    state = "unknown" if value in (None, "") else (
                        "match" if value == probe[field] else "blocked")
                    add(f"ltx.probe.{field}", state,
                        f"{core.name}/{probe['name']}: {value}，预期 {probe[field]}")
    for kind, identity in identities.items():
        try:
            if _identity(paths[kind]) != identity:
                raise ValueError("文件在核对期间改变，请在构建结束后重新核对")
        except (OSError, ValueError) as exc:
            add(f"{kind}.changed", "blocked", str(exc))
    if manifest_path:
        from vivado_mcp.debug_bundle import verify_debug_bundle

        try:
            if not bit or not ltx:
                raise ValueError("bit/ltx 未解析成功，不能核对交付清单")
            result["bundle"] = verify_debug_bundle(manifest_path, bit.sha256, ltx.sha256)
            add("bundle.manifest", "match", result["bundle"]["note"])
        except (OSError, ValueError) as exc:
            result["bundle"] = {"status": "blocked", "error": str(exc)}
            add("bundle.manifest", "blocked", str(exc))
    states = {check["status"] for check in checks}
    result["status"] = "blocked" if "blocked" in states else (
        "incomplete" if "unknown" in states else "consistent")
    return result
