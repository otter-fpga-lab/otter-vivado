"""只读提取普通目录中的 Vivado 时序、资源和 Critical Warning。"""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path

from vivado_mcp.analysis.timing_parser import parse_timing_summary
from vivado_mcp.analysis.util_parser import parse_utilization
from vivado_mcp.analysis.warning_parser import group_warnings, parse_critical_warnings

_TIMING_HEADER = re.compile(r"^\s*WNS\(ns\)\s+TNS\(ns\)")
_INLINE_METRIC = re.compile(
    r"\b(WNS|TNS|WHS|THS)\s*=\s*(NA|[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)",
    re.IGNORECASE,
)
_RESOURCE_NAMES = {
    "lut": ("Slice LUTs", "CLB LUTs"),
    "ff": ("Slice Registers", "CLB Registers", "Register as Flip Flop"),
    "bram": ("Block RAM Tile",),
    "dsp": ("DSPs",),
}


def timing_snapshots(text: str) -> list[dict]:
    """保留每个整体时序表和日志进度行，不把各阶段数值相互覆盖。"""
    lines = text.splitlines()
    snapshots = []
    metadata = []
    for index, line in enumerate(lines):
        if re.match(r"^[ \t|]*Design State\s*:", line):
            metadata = [line]
        if _TIMING_HEADER.match(line):
            report = parse_timing_summary("\n".join([*metadata, *lines[index : index + 5]]))
            summary = report.summary
            metrics = {
                f"{name}_ns": getattr(summary, name) if summary.parse_status == "ok" else None
                for name in ("wns", "tns", "whs", "ths")
            }
            snapshots.append(
                {
                    "line": index + 1,
                    "source": "table",
                    "parse_status": summary.parse_status,
                    "source_stage": report.source_stage,
                    **metrics,
                }
            )
        else:
            matches = _INLINE_METRIC.findall(line)
            if not matches:
                continue
            metrics = {f"{name}_ns": None for name in ("wns", "tns", "whs", "ths")}
            for name, value in matches:
                number = None if value.upper() == "NA" else float(value)
                if number is not None and not (-float("inf") < number < float("inf")):
                    number = None
                metrics[f"{name.lower()}_ns"] = number
            snapshots.append(
                {
                    "line": index + 1,
                    "source": "log",
                    "source_stage": "unknown",
                    "parse_status": "ok"
                    if all(v is not None for v in metrics.values())
                    else "partial",
                    **metrics,
                }
            )
    return snapshots


def summarize_directory(directory: Path) -> dict:
    """逐文件复用已有解析器；警告次数表示文件中的出现次数。"""
    root = directory.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"报告目录不存在或不是目录：{root}")
    files = []
    skipped = []
    all_warnings = []
    warning_files: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in {".log", ".rpt", ".txt"}:
            continue
        name = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            skipped.append({"path": name, "reason": "链接文件或路径越界"})
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            skipped.append({"path": name, "reason": str(exc)})
            continue
        raw_warnings = "\n".join(
            f"VMCP_CW:{i}|{line.strip()}"
            if line.lstrip().startswith("CRITICAL WARNING:")
            else line.strip()
            if line.lstrip().startswith("VMCP_CW:")
            else ""
            for i, line in enumerate(text.splitlines(), 1)
        )
        warnings = parse_critical_warnings(raw_warnings)
        all_warnings.extend(warnings)
        for warning in warnings:
            warning_files.setdefault(warning.warning_id, set()).add(name)
        utilization = parse_utilization(text)
        resources = {}
        for key, candidates in _RESOURCE_NAMES.items():
            resource = next((r for label in candidates if (r := utilization.get(label))), None)
            resources[key] = asdict(resource) if resource else None
        files.append(
            {
                "path": name,
                "timing": timing_snapshots(text),
                "utilization": resources,
                "utilization_parse_error": utilization.parse_error,
                "critical_warning_count": len(warnings),
                "critical_warnings": [asdict(group) for group in group_warnings(warnings)],
            }
        )
    return {
        "schema_version": 1,
        "source_directory": str(root),
        "files": files,
        "skipped_files": skipped,
        "critical_warning_count": len(all_warnings),
        "critical_warnings": [
            {**asdict(group), "files": sorted(warning_files[group.warning_id])}
            for group in group_warnings(all_warnings)
        ],
        "note": "按文件及出现顺序保留数据；重复日志/报告中的警告按出现次数统计。"
        "缺失指标为 null，未推断构建或签核通过。",
    }


def format_summary(summary: dict) -> str:
    """生成人可读的简短摘要，未知值不显示为零。"""
    lines = [f"Vivado 报告摘要：{len(summary['files'])} 个文件"]
    for item in summary["files"]:
        lines.append(f"\n{item['path']}")
        for timing in item["timing"]:
            values = "  ".join(
                f"{name.upper()}={timing[f'{name}_ns']} ns"
                if timing[f"{name}_ns"] is not None
                else f"{name.upper()}=未知"
                for name in ("wns", "tns", "whs", "ths")
            )
            lines.append(f"  行 {timing['line']} [{timing['parse_status']}] {values}")
        resources = item["utilization"]
        if any(resources.values()):
            lines.append(
                "  "
                + "  ".join(
                    f"{name.upper()}={value['used']}/{value['available']} ({value['percent']}%)"
                    if value
                    else f"{name.upper()}=未知"
                    for name, value in resources.items()
                )
            )
        if not item["timing"] and not any(resources.values()):
            lines.append("  未解析到时序/资源指标")
        if item["utilization_parse_error"]:
            lines.append(f"  资源解析提示：{item['utilization_parse_error']}")
    lines.append(f"\nCritical Warning：{summary['critical_warning_count']} 条")
    for group in summary["critical_warnings"]:
        lines.append(f"  [{group['warning_id']}] {group['count']} 条：{group['message_template']}")
        lines.append(f"    文件：{', '.join(group['files'])}；建议：{group['suggestion']}")
    for skipped in summary["skipped_files"]:
        lines.append(f"跳过 {skipped['path']}：{skipped['reason']}")
    lines.append(summary["note"])
    return "\n".join(lines)
