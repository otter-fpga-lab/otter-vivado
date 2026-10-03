"""LTX 文件解析器（离线 ILA 探针清单）。

纯 Python 模块，不依赖 Vivado。解析 Vivado 调试探针文件 .ltx，
提取各 ILA 核的 probe 名/位宽/类型/映射网线，供连板前离线核对触发设置。

关键事实（真机 Vivado 2019.1 验证）：
  .ltx 在 2019.1 是 **JSON**，不是 XML（旧版可能是 XML）。
  本解析器嗅探首个非空字符：`{` 视为 JSON 正常解析；`<` 视为旧版 XML，
  给出明确 SKIP 提示（raise ValueError）而非崩溃。

JSON 结构关键路径：
  ltx_root
    ├─ version / minor
    └─ ltx_data[]                  # 探针集（通常 1 个，name 如 "EDA_PROBESET"）
         └─ debug_cores[]
              ├─ type == "XSDB_V3" # 调试集线器 dbg_hub（无 pins）
              └─ type == "ILA_V3"  # ILA 核（含 pins）
                   └─ pins[]
                        ├─ name        # 如 "probe0"
                        ├─ type        # 如 "DATA_TRIGGER"
                        ├─ direction   # 如 "IN"
                        ├─ isVector
                        ├─ leftIndex / rightIndex   # 位宽 = abs(right - left) + 1
                        └─ nets[]{name}             # 映射到设计中的网线名
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

# 文件大小上限（10MB）
_MAX_FILE_SIZE = 10 * 1024 * 1024

# 调试核类型
_TYPE_DBG_HUB = "XSDB_V3"
_TYPE_ILA = "ILA_V3"


# ====================================================================== #
#  数据结构
# ====================================================================== #


@dataclass(frozen=True)
class LtxProbe:
    """单个 ILA probe（探针引脚）。"""

    name: str                  # probe 名（如 "probe0"）
    probe_type: str            # probe 类型（如 "DATA_TRIGGER"）
    direction: str             # 方向（如 "IN"）
    left_index: int | None     # LTX leftIndex；缺失保持未知
    right_index: int | None    # LTX rightIndex；不假定总线方向
    is_vector: bool | None     # 是否为向量
    nets: tuple[str, ...] = ()  # 映射到的网线名（顶层 net，不含逐位 subnets）

    port_index: int | None = None
    subnets: tuple[str, ...] = ()

    @property
    def width(self) -> int | None:
        """兼容两种下标方向；缺少下标不猜成一位。"""
        if self.left_index is None or self.right_index is None:
            return None
        return abs(self.right_index - self.left_index) + 1


@dataclass(frozen=True)
class LtxCore:
    """单个调试核（ILA 或 dbg_hub）。"""

    name: str                       # 核实例名
    core_type: str                  # 核类型（ILA_V3 / XSDB_V3）
    spec: str                       # 核规格（如 "labtools_ila_v6"）
    ip_name: str                    # IP 名（如 "ila"，dbg_hub 为空）
    probes: tuple[LtxProbe, ...] = ()  # 探针列表（dbg_hub 为空）

    uuid: str = ""

    @property
    def is_vio(self) -> bool:
        """VIO 核；未知类型保留原文。"""
        return self.core_type in {"VIO_V2", "VIO_V3"}

    @property
    def is_ila(self) -> bool:
        """是否为 ILA 核（含 probe）。"""
        return self.core_type == _TYPE_ILA

    @property
    def is_dbg_hub(self) -> bool:
        """是否为调试集线器 dbg_hub。"""
        return self.core_type == _TYPE_DBG_HUB


@dataclass(frozen=True)
class LtxConfig:
    """单个 LTX 文件的解析结果。"""

    file_path: str
    version: str                    # ltx_root.version（原样字符串）
    minor: str                      # ltx_root.minor（原样字符串）
    cores: tuple[LtxCore, ...] = ()  # 所有调试核（ILA + dbg_hub）

    sha256: str = ""
    file_size: int = 0

    @property
    def vio_cores(self) -> tuple[LtxCore, ...]:
        """仅 VIO 核。"""
        return tuple(c for c in self.cores if c.is_vio)

    @property
    def ila_cores(self) -> tuple[LtxCore, ...]:
        """仅 ILA 核。"""
        return tuple(c for c in self.cores if c.is_ila)

    @property
    def dbg_hubs(self) -> tuple[LtxCore, ...]:
        """仅 dbg_hub 核。"""
        return tuple(c for c in self.cores if c.is_dbg_hub)

    def to_dict(self) -> dict:
        """转换为可 JSON 序列化的字典。"""
        return {
            "file_path": self.file_path,
            "version": self.version,
            "minor": self.minor,
            "sha256": self.sha256,
            "file_size": self.file_size,
            "cores": [
                {
                    "name": c.name,
                    "type": c.core_type,
                    "spec": c.spec,
                    "ip_name": c.ip_name,
                    "is_ila": c.is_ila,
                    "is_vio": c.is_vio,
                    "uuid": c.uuid,
                    "is_dbg_hub": c.is_dbg_hub,
                    "probes": [
                        {
                            "name": p.name,
                            "type": p.probe_type,
                            "direction": p.direction,
                            "left_index": p.left_index,
                            "right_index": p.right_index,
                            "width": p.width,
                            "is_vector": p.is_vector,
                            "nets": list(p.nets),
                            "port_index": p.port_index,
                            "subnets": list(p.subnets),
                        }
                        for p in c.probes
                    ],
                }
                for c in self.cores
            ],
        }


# ====================================================================== #
#  解析函数
# ====================================================================== #


def _objects(value, field):
    """结构错误不能静默丢弃成空探针清单。"""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"LTX {field} 必须是对象数组")
    return value


def _text(obj, field):
    value = obj.get(field, "")
    if not isinstance(value, str):
        raise ValueError(f"LTX {field} 必须是字符串")
    return value


def _index(obj, field):
    value = obj.get(field)
    if value is None:
        return None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 0 <= value <= 0x7FFFFFFF:
        raise ValueError(f"LTX {field} 必须是非负整数")
    return value


def _parse_probe(pin: dict) -> LtxProbe:
    """保留端口、方向和子网名；不从列表顺序推断位序。"""
    nets = _objects(pin.get("nets", []), "nets")
    vector = pin.get("isVector")
    if vector is not None and type(vector) is not bool:
        raise ValueError("LTX isVector 必须是布尔值")
    return LtxProbe(
        name=_text(pin, "name"),
        probe_type=_text(pin, "type"),
        direction=_text(pin, "direction"),
        left_index=_index(pin, "leftIndex"),
        right_index=_index(pin, "rightIndex"),
        is_vector=vector,
        nets=tuple(_text(n, "name") for n in nets),
        port_index=_index(pin, "portIndex"),
        subnets=tuple(
            _text(subnet, "name") for net in nets
            for subnet in _objects(net.get("subnets", []), "subnets")
        ),
    )


def _parse_core(core: dict) -> LtxCore:
    """保留 ILA/VIO 身份和探针，缺失证据交给核对层标记。"""
    return LtxCore(
        name=_text(core, "name"), core_type=_text(core, "type"),
        spec=_text(core, "spec"), ip_name=_text(core, "ipName"),
        uuid=_text(core, "uuid"),
        probes=tuple(_parse_probe(p) for p in _objects(core.get("pins", []), "pins")),
    )


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"LTX JSON 含重复字段 {key!r}")
        result[key] = value
    return result


def parse_ltx(file_path: str) -> LtxConfig:
    """解析单个 LTX 文件，提取 ILA 探针清单。

    Args:
        file_path: LTX 文件的绝对路径。

    Returns:
        LtxConfig 实例。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件过大、为旧版 XML 格式、或 JSON 结构非法。
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"LTX 文件不存在: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size > _MAX_FILE_SIZE:
        raise ValueError(
            f"LTX 文件过大 ({file_size / 1024 / 1024:.1f}MB)，"
            f"上限 {_MAX_FILE_SIZE / 1024 / 1024:.0f}MB"
        )

    with open(file_path, "rb") as f:
        before = os.fstat(f.fileno())
        raw = f.read(_MAX_FILE_SIZE + 1)
        after = os.fstat(f.fileno())
    if len(raw) > _MAX_FILE_SIZE:
        raise ValueError("LTX 文件过大")

    def identity(stat):
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    if identity(before) != identity(after) or identity(after) != identity(os.stat(file_path)):
        raise ValueError("LTX 文件在读取期间发生变化，请等待构建完成后重试")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("LTX 必须是有效 UTF-8，不能替换损坏的探针名称") from exc

    # 嗅探首个非空字符判断格式：{ = JSON，< = 旧版 XML
    stripped = text.lstrip()
    if not stripped:
        raise ValueError(f"LTX 文件为空: {file_path}")

    first_char = stripped[0]
    if first_char == "<":
        raise ValueError(
            "检测到旧版 XML 格式 .ltx，本版本仅支持 Vivado 2019.1+ 的 JSON 格式，"
            f"暂跳过（SKIP）: {file_path}"
        )
    if first_char != "{":
        raise ValueError(
            f"无法识别的 LTX 格式（首字符为 {first_char!r}，"
            f"应为 '{{' JSON 或 '<' XML）: {file_path}"
        )

    try:
        data = json.loads(text, object_pairs_hook=_unique_keys)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("LTX JSON 根不是对象")

    root = data.get("ltx_root")
    if not isinstance(root, dict):
        raise ValueError("LTX 文件缺少 ltx_root 对象")

    version = str(root.get("version", ""))
    minor = str(root.get("minor", ""))

    cores: list[LtxCore] = []
    for block in _objects(root.get("ltx_data", []), "ltx_data"):
        for core in _objects(block.get("debug_cores", []), "debug_cores"):
            cores.append(_parse_core(core))

    return LtxConfig(
        file_path=file_path,
        version=version,
        minor=minor,
        cores=tuple(cores),
        sha256=hashlib.sha256(raw).hexdigest(),
        file_size=len(raw),
    )


# ====================================================================== #
#  格式化
# ====================================================================== #


def format_ltx(result: LtxConfig) -> str:
    """将 LtxConfig 格式化为人类可读的中文摘要。

    按 ILA 分组列出每个 probe（名/位宽/类型/映射 net），并单列 dbg_hub。

    Args:
        result: 解析结果。

    Returns:
        中文摘要文本。
    """
    lines: list[str] = []

    lines.append("=== LTX 探针清单 ===")
    lines.append(f"文件: {result.file_path}")
    lines.append(f"版本: {result.version}.{result.minor}")

    ila_cores = result.ila_cores
    dbg_hubs = result.dbg_hubs
    total_probes = sum(len(c.probes) for c in (*ila_cores, *result.vio_cores))
    lines.append(
        f"ILA 核: {len(ila_cores)} 个，VIO 核: {len(result.vio_cores)} 个，"
        f"dbg_hub: {len(dbg_hubs)} 个，"
        f"probe 合计: {total_probes} 个"
    )
    lines.append("")

    if not ila_cores:
        lines.append("（未发现 ILA 核）")
    for core in (*ila_cores, *result.vio_cores):
        kind = "ILA" if core.is_ila else "VIO"
        lines.append(f"--- {kind}: {core.name} ---")
        lines.append(f"  UUID: {core.uuid or '未知'}")
        if core.ip_name:
            lines.append(f"  IP: {core.ip_name}  规格: {core.spec}")
        if not core.probes:
            lines.append("  （无 probe）")
        for p in core.probes:
            net_str = ", ".join(p.nets) if p.nets else "(未映射)"
            lines.append(
                f"  {p.name}  [{p.right_index}:{p.left_index}] "
                f"宽={p.width}  类型={p.probe_type}"
            )
            lines.append(f"    net: {net_str}")
        lines.append("")

    if dbg_hubs:
        lines.append("--- 调试集线器 (dbg_hub) ---")
        for hub in dbg_hubs:
            lines.append(f"  {hub.name}  规格: {hub.spec}")
        lines.append("")

    return "\n".join(lines)
