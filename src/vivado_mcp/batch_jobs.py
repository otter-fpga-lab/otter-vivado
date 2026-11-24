"""托管批处理 Tcl 与独立 OOC 综合任务注册表、持久化及离线报告摘要。

所有任务元数据和默认输出目录落在用户缓存 ``%LOCALAPPDATA%\\otter-vivado\\jobs\\``
（可通过 ``OTTER_VIVADO_CACHE_DIR`` 覆盖），绝不在用户工程内创建 ``.work`` / ``.vmcp``。
"""

from __future__ import annotations

import asyncio
import codecs
import collections
import contextlib
import copy
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vivado_mcp.analysis.report_summary import timing_snapshots
from vivado_mcp.analysis.timing_parser import parse_timing_summary
from vivado_mcp.analysis.util_parser import parse_utilization
from vivado_mcp.analysis.warning_parser import (
    CriticalWarning,
    WarningGroup,
    group_warnings,
    match_silent_run_child_exit,
    parse_critical_warnings,
    parse_errors,
    scan_nonstandard_errors,
)
from vivado_mcp.analysis.xpr_parser import parse_xpr
from vivado_mcp.config import find_vivado, get_vivado_version
from vivado_mcp.local_batch import build_command, validate_batch_paths
from vivado_mcp.project_layout import user_cache_root
from vivado_mcp.run_feedback import inspect_vivado_init_files, query_windows_crash_events
from vivado_mcp.shared_registry import detect_client_source, is_pid_alive
from vivado_mcp.vivado.child_env import sanitize_vivado_child_env
from vivado_mcp.vivado.tcl_utils import (
    decode_vivado_output,
    tcl_quote,
    to_tcl_path,
    validate_identifier,
)

logger = logging.getLogger(__name__)

_MAX_PERSISTED_JOBS = 100
_MAX_SCAN_FILES = 128
_MAX_RPT_BYTES = 2 * 1024 * 1024
_MAX_LOG_BYTES = 8 * 1024 * 1024
_LOG_TAIL_READ_BYTES = 64 * 1024
_STDERR_RING_SIZE = 200
_MIN_WAIT_SECONDS = 1
_MAX_WAIT_SECONDS = 1800

_RESOURCE_NAMES: dict[str, tuple[str, ...]] = {
    "lut": ("Slice LUTs", "CLB LUTs"),
    "ff": ("Slice Registers", "CLB Registers", "Register as Flip Flop"),
    "bram": ("Block RAM Tile",),
    "dsp": ("DSPs",),
    "uram": ("URAM", "UltraRAM"),
}

_STAGE_RANK = {
    "post-route": 4,
    "post-place": 3,
    "post-synth": 2,
    "unknown": 1,
}

_PHASE_RE = re.compile(
    r"^(?:Phase\s+\d+|Starting\s+|Finished\s+|Running\s+|Command:\s+)"
    r"|(?:\b(?:synth_design|opt_design|power_opt_design|place_design|phys_opt_design|"
    r"route_design|write_bitstream)\s+completed successfully\b)",
    re.IGNORECASE,
)
_RE_DESIGN_HDR = re.compile(r"^\|\s*Design\s*:\s*(\S+)", re.MULTILINE)
_RE_DEVICE_HDR = re.compile(r"^\|\s*Device\s*:\s*(\S+)", re.MULTILINE)
_RE_VIVADO_VER = re.compile(r"\bVivado\s+v(20\d{2}\.\d+(?:\.\d+)?)\b")
_RE_POWER_W = re.compile(
    r"Total\s+On-Chip\s+Power\s*\(\s*W\s*\)\s*[|:\s]+([+-]?(?:\d+(?:\.\d*)?|\.\d+))",
    re.IGNORECASE,
)
_RE_DRC_ERR_ROW = re.compile(r"^\|\s*[^|]+\|\s*Error\s*\|", re.MULTILINE | re.IGNORECASE)
_RE_DRC_VIOLATIONS = re.compile(r"Violations\s+found:\s*(\d+)", re.IGNORECASE)
_RE_CLOCK_PORT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.\[\]]*$")
_RE_SYNTH_ARG_TOKEN = re.compile(r"^[a-zA-Z0-9_.:/\\+\-=]+$")
_RE_FILE_LINE_NOISE = re.compile(r"\[(\S+?\.\w+):\d+\]")
_RE_WARNING_ID = re.compile(r"\[(\w+[\s\-][\w\-]+)\]")
_IS_JUNCTION = getattr(os.path, "isjunction", None)


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    if _IS_JUNCTION is not None:
        try:
            return bool(_IS_JUNCTION(path))
        except OSError:
            return False
    return False


def _atomic_write_json(tmp: Path, target: Path, payload: str) -> None:
    """原子落盘 JSON；在 Windows 短暂并发读写冲突时重试 os.replace，并清理残留 tmp。"""
    try:
        tmp.write_text(payload, encoding="utf-8")
        delay = 0.02
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(delay)
                delay = min(0.05, delay + 0.01)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def _dir_report_signature(
    target_path: Path, *, max_depth: int = 3
) -> tuple[int, int, int] | None:
    """采集目录（或 .xpr 及其 .runs）报告与状态标记的轻量指纹，跳过符号链接/junction。"""
    if _is_link_or_junction(target_path):
        return None
    try:
        resolved = target_path.resolve(strict=True)
    except OSError:
        return None
    if _is_link_or_junction(resolved):
        return None
    if resolved.is_file():
        try:
            st = resolved.stat()
            xpr_sig = (1, int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return None
        runs_dir = resolved.parent / f"{resolved.stem}.runs"
        if not runs_dir.is_dir() or _is_link_or_junction(runs_dir):
            return xpr_sig
        sub = _dir_report_signature(runs_dir, max_depth=max_depth)
        if sub is None:
            return xpr_sig
        return (xpr_sig[0] + sub[0], max(xpr_sig[1], sub[1]), xpr_sig[2] + sub[2])

    if not resolved.is_dir():
        return None
    count = 0
    max_mtime_ns = 0
    total_size = 0
    stack: list[tuple[Path, int]] = [(resolved, 0)]
    exts = {".rpt", ".log", ".txt", ".rst"}
    while stack:
        cur, depth = stack.pop()
        if _is_link_or_junction(cur):
            continue
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < max_depth:
                                stack.append((Path(entry.path), depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            name_low = entry.name.lower()
                            if (
                                Path(name_low).suffix in exts
                                or name_low.endswith(".xpr")
                                or name_low == "ooc_synth.tcl"
                            ):
                                st = entry.stat(follow_symlinks=False)
                                count += 1
                                mns = int(st.st_mtime_ns)
                                if mns > max_mtime_ns:
                                    max_mtime_ns = mns
                                total_size += int(st.st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return (count, max_mtime_ns, total_size)


def _resolve_safe_report_root(
    candidate: str | Path | None,
    *,
    is_file_hint: bool = False,
) -> Path | None:
    """严格校验允许读取报告的绝对目录根（拒绝相对路径与任意层级符号链接/junction）。"""
    if not candidate:
        return None
    raw_str = str(candidate).strip()
    if not raw_str or "\x00" in raw_str:
        return None
    raw = Path(raw_str)
    if not raw.is_absolute():
        return None
    if is_file_hint:
        raw = raw.parent
    cur = Path(raw.anchor) if raw.anchor else None
    if cur is None:
        return None
    for part in raw.parts[1:]:
        cur = cur / part
        if _is_link_or_junction(cur):
            return None
    try:
        resolved = raw.resolve(strict=True)
    except OSError:
        return None
    if resolved.is_file():
        resolved = resolved.parent
    if _is_link_or_junction(resolved) or not resolved.is_dir():
        return None
    return resolved


def _decode_utf8_or_fallback(
    raw: bytes, *, offset_nonzero: bool = False, final: bool = True
) -> str:
    """解码文本字节；若从非零偏移起读，先跳过最多 3 个 UTF-8 续字节避免误判为 ANSI。"""
    data = raw
    if offset_nonzero and data:
        skip = 0
        while skip < 3 and skip < len(data) and (data[skip] & 0xC0) == 0x80:
            skip += 1
        if skip:
            data = data[skip:]
    try:
        return codecs.getincrementaldecoder("utf-8")().decode(data, final=final)
    except UnicodeDecodeError:
        return decode_vivado_output(data)


def _read_text_bounded(path: Path, max_bytes: int) -> tuple[str, bool]:
    """有界读取文本文件，返回 ``(text, truncated)``；自动处理 UTF-8 / Windows ANSI。"""
    stat = path.stat()
    size = stat.st_size
    offset_nonzero = False
    if size <= max_bytes:
        raw = path.read_bytes()
        truncated = False
    else:
        # 大日志保留末尾窗口；大报告保留开头表格
        if path.suffix.lower() == ".log":
            offset = max(0, size - max_bytes)
            offset_nonzero = offset > 0
            with path.open("rb") as handle:
                if offset > 0:
                    handle.seek(offset)
                raw = handle.read(max_bytes)
        else:
            with path.open("rb") as handle:
                raw = handle.read(max_bytes)
        truncated = True
    text = _decode_utf8_or_fallback(
        raw, offset_nonzero=offset_nonzero, final=not truncated
    )
    return text.lstrip("\ufeff"), truncated


def _read_log_tail(
    path: Path, max_lines: int = 25
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """快速从日志文件末尾读取阶段行与最后 N 行，不扫描整份大文件。"""
    if max_lines <= 0 and not path.is_file():
        return [], []
    if not path.is_file() or _is_link_or_junction(path):
        return [], []
    try:
        size = path.stat().st_size
        offset = max(0, size - _LOG_TAIL_READ_BYTES)
        with path.open("rb") as handle:
            if offset > 0:
                handle.seek(offset)
            raw = handle.read(_LOG_TAIL_READ_BYTES)
        text = _decode_utf8_or_fallback(
            raw, offset_nonzero=offset > 0, final=offset == 0
        )
    except OSError:
        return [], []

    lines = text.splitlines()
    if offset > 0 and lines:
        lines = lines[1:]
    phases: list[dict[str, Any]] = []
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped and _PHASE_RE.search(stripped):
            phases.append({"line": idx, "text": stripped[:240]})
    tail_slice = lines[-max_lines:] if max_lines > 0 else []
    start_idx = max(1, len(lines) - len(tail_slice) + 1)
    tail = [
        {"line": start_idx + idx, "text": ln.rstrip()}
        for idx, ln in enumerate(tail_slice)
    ]
    return phases[-40:], tail


def _parse_generic_warning_lines(lines: list[tuple[int, str]]) -> list[CriticalWarning]:
    results: list[CriticalWarning] = []
    for lineno, message in lines:
        m_id = _RE_WARNING_ID.search(message)
        wid = m_id.group(1) if m_id else "UNKNOWN"
        results.append(
            CriticalWarning(
                warning_id=wid,
                message=message,
                line_number=lineno,
                source_file="",
                port="",
                pin="",
            )
        )
    return results


def _group_unknown_aware(entries: list[CriticalWarning]) -> list[WarningGroup]:
    """按 warning_id 聚合；无 ID 的 UNKNOWN 按规范化消息细分，避免误合并不同错误。"""
    known = [item for item in entries if item.warning_id != "UNKNOWN"]
    unknown = [item for item in entries if item.warning_id == "UNKNOWN"]
    groups = group_warnings(known)
    buckets: dict[str, list[CriticalWarning]] = {}
    for item in unknown:
        key = re.sub(r"\s+", " ", _RE_FILE_LINE_NOISE.sub(r"[\1]", item.message)).strip()
        buckets.setdefault(key, []).append(item)
    for bucket in buckets.values():
        groups.append(
            WarningGroup(
                warning_id="UNKNOWN",
                category="UNKNOWN",
                count=len(bucket),
                first_line=bucket[0].line_number,
                message_template=bucket[0].message,
                affected_ports=[],
                source_files=list(
                    dict.fromkeys(b.source_file for b in bucket if b.source_file)
                ),
                suggestion="未知警告类型，请查阅 Vivado 日志获取详细信息。",
            )
        )
    groups.sort(key=lambda g: g.first_line)
    return groups


def _format_issue_item(
    group: WarningGroup,
    severity: str,
    files_map: dict[str, set[str]],
    *,
    compact: bool = False,
) -> dict[str, Any]:
    files = sorted(files_map.get(f"{severity}:{group.warning_id}:{group.first_line}", set()))
    suggestion = group.suggestion
    if compact and suggestion:
        suggestion = suggestion.splitlines()[0].strip()
    item: dict[str, Any] = {
        "severity": severity,
        "id": group.warning_id,
        "category": group.category,
        "count": group.count,
        "line": group.first_line,
        "message": group.message_template[:240] if compact else group.message_template[:600],
    }
    if files:
        item["files"] = files[:3]
    if group.affected_ports:
        item["ports"] = group.affected_ports[:5]
    if suggestion and (group.category != "UNKNOWN" or not compact):
        item["suggestion"] = suggestion[:200] if compact else suggestion
    return item


def _timing_rank(path: Path, stage: str) -> tuple[int, int, float]:
    name = path.name.lower()
    stage_score = _STAGE_RANK.get(stage, 0)
    if "route" in name or "impl" in name:
        file_score = 4
    elif "place" in name:
        file_score = 3
    elif "timing_summary" in name:
        file_score = 2
    elif "timing" in name:
        file_score = 1
    else:
        file_score = 0
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (stage_score, file_score, mtime)


def _util_rank(path: Path) -> tuple[int, float]:
    name = path.name.lower()
    if "routed" in name or "impl" in name:
        score = 4
    elif "placed" in name:
        score = 3
    elif "utilization" in name:
        score = 2
    elif "synth" in name:
        score = 1
    else:
        score = 0
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (score, mtime)


def _empty_timing(parse_status: str = "missing") -> dict[str, Any]:
    return {
        "wns": None,
        "tns": None,
        "whs": None,
        "ths": None,
        "wpws": None,
        "met": None,
        "failing_endpoints": None,
        "total_endpoints": None,
        "stage": "unknown",
        "parse_status": parse_status,
        "source_report": "",
    }


def _empty_utilization() -> dict[str, Any]:
    return {"lut": None, "ff": None, "bram": None, "dsp": None, "uram": None}


_RE_OOC_IP_RUN = re.compile(r"^.+_synth_\d+$")


def _subdir_priority_key(
    entry_name: str, prio_map: dict[str, int]
) -> tuple[int, int, str]:
    """排序子目录：impl_* -> synth_1 / 工程设计运行 -> 其他目录 -> OOC IP *_synth_1。"""
    low = entry_name.lower()
    is_ooc_ip = bool(_RE_OOC_IP_RUN.match(low))
    if low == "impl_1" or (low.startswith("impl_") and not is_ooc_ip):
        return (0, prio_map.get(low, 0), low)
    if low == "synth_1":
        return (1, prio_map.get(low, 0), low)
    if low in prio_map and not is_ooc_ip:
        return (1, prio_map[low], low)
    if is_ooc_ip:
        return (3, prio_map.get(low, 0), low)
    return (2, 0, low)


def _collect_artifact_files(
    root: Path,
    extra_log: Path | None = None,
    *,
    max_depth: int = 3,
    priority_dirs: list[str] | None = None,
) -> list[tuple[Path, str]]:
    """收集目录内安全的 .rpt/.log 文件，优先顶层设计与 impl_1/synth_1，跳过符号链接/junction。"""
    collected: list[tuple[Path, str]] = []
    seen: set[str] = set()
    resolved_root = root.resolve()
    prio_map = {
        name.strip().lower(): idx
        for idx, name in enumerate(priority_dirs or [])
        if name and name.strip()
    }

    def add_candidate(path: Path, rel_name: str) -> None:
        if len(collected) >= _MAX_SCAN_FILES:
            return
        if _is_link_or_junction(path) or not path.is_file():
            return
        try:
            real = path.resolve(strict=True)
        except OSError:
            return
        key = os.path.normcase(str(real))
        if key in seen:
            return
        seen.add(key)
        collected.append((real, rel_name))

    if resolved_root.is_dir():
        stack: list[tuple[Path, int]] = [(resolved_root, 0)]
        while stack and len(collected) < _MAX_SCAN_FILES:
            current, depth = stack.pop(0)
            try:
                raw_entries = list(os.scandir(current))
            except OSError:
                continue
            dir_entries: list[os.DirEntry[str]] = []
            file_entries: list[os.DirEntry[str]] = []
            for entry in raw_entries:
                if entry.name.startswith("."):
                    continue
                entry_path = Path(entry.path)
                if _is_link_or_junction(entry_path):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dir_entries.append(entry)
                    elif entry.is_file(follow_symlinks=False):
                        file_entries.append(entry)
                except OSError:
                    continue

            file_entries.sort(key=lambda e: e.name.lower())
            dir_entries.sort(key=lambda e: _subdir_priority_key(e.name, prio_map))

            for entry in file_entries:
                if len(collected) >= _MAX_SCAN_FILES:
                    break
                entry_path = Path(entry.path)
                if entry_path.suffix.lower() in {".rpt", ".log"}:
                    try:
                        rel = entry_path.resolve(strict=True).relative_to(resolved_root)
                    except (OSError, ValueError):
                        continue
                    add_candidate(entry_path, rel.as_posix())

            if depth < max_depth:
                for entry in dir_entries:
                    stack.append((Path(entry.path), depth + 1))

    if extra_log is not None and extra_log.is_file() and not _is_link_or_junction(extra_log):
        try:
            rel_str = extra_log.resolve(strict=True).relative_to(resolved_root).as_posix()
        except (OSError, ValueError):
            rel_str = extra_log.name
        add_candidate(extra_log, rel_str)

    return collected


def parse_directory_reports(
    output_dir: Path,
    *,
    log_path: Path | None = None,
    is_failed: bool = False,
    scan_nonstandard: bool | None = None,
    priority_dirs: list[str] | None = None,
    max_issues: int = 50,
    log_tail_lines: int = 30,
) -> dict[str, Any]:
    """解析输出目录和日志中的时序、资源、诊断、阶段和报告清单。"""
    artifacts = _collect_artifact_files(
        output_dir, extra_log=log_path, priority_dirs=priority_dirs
    )
    check_nonstandard = is_failed if scan_nonstandard is None else bool(scan_nonstandard)
    dirs_with_runme = {
        os.path.normcase(str(path.parent))
        for path, _ in artifacts
        if path.name.lower() == "runme.log"
    }
    reports_meta: list[dict[str, Any]] = []

    best_timing: tuple[tuple[int, int, float], dict[str, Any]] | None = None
    saw_no_timing_data = False
    log_timing_fallback: tuple[float, dict[str, Any]] | None = None

    best_util: tuple[tuple[int, float], dict[str, Any], str] | None = None
    best_power: tuple[tuple[int, float], float] | None = None

    all_errors: list[CriticalWarning] = []
    all_cws: list[CriticalWarning] = []
    warning_count = 0
    all_warnings: list[CriticalWarning] = []
    nonstandard_items: list[dict[str, Any]] = []
    issue_files: dict[str, set[str]] = {}
    synth_8_3848_count = 0
    drc_log_errors = 0
    drc_rpt_errors = 0
    saw_drc_rpt = False

    inferred_top = ""
    inferred_part = ""
    inferred_version = ""
    exited_vivado = False
    last_completed_step = ""

    primary_log: Path | None = log_path if (log_path and log_path.is_file()) else None
    log_candidates: list[tuple[int, float, Path]] = []

    for path, rel_name in artifacts:
        suffix = path.suffix.lower()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        reports_meta.append(
            {
                "name": path.name,
                "rel_path": rel_name,
                "path": str(path),
                "size": size,
                "kind": "log" if suffix == ".log" else "rpt",
            }
        )

        max_bytes = _MAX_LOG_BYTES if suffix == ".log" else _MAX_RPT_BYTES
        try:
            text, _ = _read_text_bounded(path, max_bytes)
        except OSError:
            continue

        if not inferred_version:
            vm = _RE_VIVADO_VER.search(text[:4096])
            if vm:
                inferred_version = vm.group(1)

        if suffix == ".rpt":
            if not inferred_top:
                dm = _RE_DESIGN_HDR.search(text[:4096])
                if dm and dm.group(1) not in {"|", "-"}:
                    inferred_top = dm.group(1)
            if not inferred_part:
                pm = _RE_DEVICE_HDR.search(text[:4096])
                if pm and pm.group(1) not in {"|", "-"}:
                    inferred_part = pm.group(1)

            tr = parse_timing_summary(text)
            if tr.summary.parse_status == "ok":
                rank = _timing_rank(path, tr.source_stage)
                candidate = {
                    "wns": tr.summary.wns,
                    "tns": tr.summary.tns,
                    "whs": tr.summary.whs,
                    "ths": tr.summary.ths,
                    "wpws": tr.summary.wpws,
                    "met": tr.summary.timing_met,
                    "failing_endpoints": tr.summary.failing_endpoints,
                    "total_endpoints": tr.summary.total_endpoints,
                    "stage": tr.source_stage,
                    "parse_status": "ok",
                    "source_report": rel_name,
                }
                if best_timing is None or rank > best_timing[0]:
                    best_timing = (rank, candidate)
            elif tr.summary.parse_status == "no_timing_data":
                saw_no_timing_data = True

            if "power" in path.name.lower() or "Total On-Chip Power" in text[:8192]:
                pm_pwr = _RE_POWER_W.search(text)
                if pm_pwr:
                    with contextlib.suppress(ValueError):
                        pwr_val = float(pm_pwr.group(1))
                        if math.isfinite(pwr_val) and pwr_val >= 0.0:
                            p_rank = _util_rank(path)
                            if best_power is None or p_rank > best_power[0]:
                                best_power = (p_rank, round(pwr_val, 4))

            if "drc" in path.name.lower():
                saw_drc_rpt = True
                err_rows = len(_RE_DRC_ERR_ROW.findall(text))
                if err_rows == 0:
                    err_rows = sum(
                        1
                        for ln in text.splitlines()
                        if ln.lstrip().startswith("ERROR:") and "[DRC" in ln
                    )
                if err_rows == 0:
                    vm_drc = _RE_DRC_VIOLATIONS.search(text)
                    if vm_drc and int(vm_drc.group(1)) > 0 and "Error" in text:
                        err_rows = int(vm_drc.group(1))
                drc_rpt_errors = max(drc_rpt_errors, err_rows)

            if "power" not in path.name.lower():
                util = parse_utilization(text)
                if util.resources:
                    parsed_res: dict[str, Any] = {}
                    for key, labels in _RESOURCE_NAMES.items():
                        res = next((util.get(label) for label in labels if util.get(label)), None)
                        parsed_res[key] = (
                            {
                                "used": res.used,
                                "avail": res.available,
                                "pct": round(res.percent, 2),
                            }
                            if res is not None
                            else None
                        )
                    if any(parsed_res.values()):
                        u_rank = _util_rank(path)
                        if best_util is None or u_rank > best_util[0]:
                            best_util = (u_rank, parsed_res, rel_name)
        else:
            # .log 文件
            if "Exiting Vivado" in text:
                exited_vivado = True
            step = match_silent_run_child_exit("ERROR", text)
            if step:
                last_completed_step = step

            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            prio = (
                4
                if (primary_log and path == primary_log.resolve())
                else (
                    3
                    if ("impl" in rel_name.lower() and path.name.lower() == "runme.log")
                    else (
                        2
                        if path.name.lower() == "runme.log"
                        else (1 if path.name.lower() == "vivado.log" else 0)
                    )
                )
            )
            log_candidates.append((prio, mtime, path))

            # 同目录下存在 runme.log 时跳过 vivado.log 诊断计数，避免双计
            duplicate_vivado_log = (
                path.name.lower() == "vivado.log"
                and os.path.normcase(str(path.parent)) in dirs_with_runme
            )
            if not duplicate_vivado_log:
                err_lines: list[str] = []
                cw_lines: list[str] = []
                warn_pairs: list[tuple[int, str]] = []
                for lineno, line in enumerate(text.splitlines(), 1):
                    if "[Synth 8-3848]" in line:
                        synth_8_3848_count += 1
                    stripped = line.lstrip()
                    if stripped.startswith("ERROR:"):
                        err_lines.append(f"VMCP_RUNLOG_ERR:{lineno}|{stripped}")
                        if "[DRC" in stripped:
                            drc_log_errors += 1
                    elif stripped.startswith("CRITICAL WARNING:"):
                        cw_lines.append(f"VMCP_CW:{lineno}|{stripped}")
                    elif stripped.startswith("WARNING:"):
                        warning_count += 1
                        if len(warn_pairs) < 200:
                            warn_pairs.append((lineno, stripped))

                parsed_errs = parse_errors("\n".join(err_lines)) if err_lines else []
                parsed_cws = parse_critical_warnings("\n".join(cw_lines)) if cw_lines else []
                parsed_warns = _parse_generic_warning_lines(warn_pairs) if warn_pairs else []

                for item in parsed_errs:
                    item_key = f"ERROR:{item.warning_id}:{item.line_number}"
                    issue_files.setdefault(item_key, set()).add(rel_name)
                for item in parsed_cws:
                    item_key = f"CRITICAL WARNING:{item.warning_id}:{item.line_number}"
                    issue_files.setdefault(item_key, set()).add(rel_name)
                all_errors.extend(parsed_errs)
                all_cws.extend(parsed_cws)
                all_warnings.extend(parsed_warns)

                if check_nonstandard and not parsed_errs:
                    for nse in scan_nonstandard_errors(text):
                        nonstandard_items.append(
                            {
                                "severity": "ERROR",
                                "id": nse.keyword,
                                "category": "NONSTANDARD_ERROR",
                                "count": 1,
                                "line": nse.line_number,
                                "files": [rel_name],
                                "message": nse.text[:240],
                                "suggestion": (
                                    "检测到非标准错误关键字（Vivado messageDb 未记录），"
                                    "请查看对应日志行。"
                                ),
                            }
                        )

            for snap in timing_snapshots(text):
                if snap.get("parse_status") == "ok" and snap.get("wns_ns") is not None:
                    whs_val = snap["whs_ns"]
                    met_val = snap["wns_ns"] >= 0 and (whs_val is None or whs_val >= 0)
                    cand = {
                        "wns": snap["wns_ns"],
                        "tns": snap["tns_ns"],
                        "whs": whs_val,
                        "ths": snap["ths_ns"],
                        "wpws": None,
                        "met": met_val,
                        "failing_endpoints": None,
                        "total_endpoints": None,
                        "stage": snap.get("source_stage", "unknown"),
                        "parse_status": "ok",
                        "source_report": rel_name,
                    }
                    if log_timing_fallback is None or mtime >= log_timing_fallback[0]:
                        log_timing_fallback = (mtime, cand)

    if primary_log is None and log_candidates:
        log_candidates.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        primary_log = log_candidates[0][2]

    phases, log_tail = (
        _read_log_tail(primary_log, max_lines=log_tail_lines)
        if primary_log is not None
        else ([], [])
    )

    err_groups = _group_unknown_aware(all_errors)
    cw_groups = _group_unknown_aware(all_cws)
    warn_groups = _group_unknown_aware(all_warnings)

    # 补充聚合文件映射（按 group.first_line 对齐）
    group_files: dict[str, set[str]] = {}
    for sev, lst, grp_list in (
        ("ERROR", all_errors, err_groups),
        ("CRITICAL WARNING", all_cws, cw_groups),
    ):
        by_id: dict[str, set[str]] = {}
        for item in lst:
            by_id.setdefault(item.warning_id, set()).update(
                issue_files.get(f"{sev}:{item.warning_id}:{item.line_number}", set())
            )
        for grp in grp_list:
            key = f"{sev}:{grp.warning_id}:{grp.first_line}"
            group_files[key] = by_id.get(grp.warning_id, set())

    top_issues = [
        _format_issue_item(grp, "ERROR", group_files) for grp in err_groups
    ] + [
        _format_issue_item(grp, "CRITICAL WARNING", group_files) for grp in cw_groups
    ]
    if nonstandard_items:
        top_issues = nonstandard_items + top_issues

    warning_items = [_format_issue_item(grp, "WARNING", {}) for grp in warn_groups[:20]]

    if best_timing is not None:
        timing = best_timing[1]
    elif log_timing_fallback is not None:
        timing = log_timing_fallback[1]
    elif saw_no_timing_data:
        timing = _empty_timing("no_timing_data")
    else:
        timing = _empty_timing("missing")

    utilization = best_util[1] if best_util is not None else _empty_utilization()
    utilization_source = best_util[2] if best_util is not None else ""

    has_log_evidence = bool(primary_log is not None or log_candidates)
    drc_errors: int | None = (
        max(drc_log_errors, drc_rpt_errors) if (has_log_evidence or saw_drc_rpt) else None
    )

    return {
        "timing": timing,
        "utilization": utilization,
        "utilization_source": utilization_source,
        "power_w": best_power[1] if best_power is not None else None,
        "synth_8_3848": synth_8_3848_count if has_log_evidence else None,
        "drc_errors": drc_errors,
        "has_log_evidence": has_log_evidence,
        "diagnostics": {
            "errors": len(all_errors),
            "critical_warnings": len(all_cws),
            "warnings": warning_count,
            "top_issues": top_issues[:max_issues],
            "warning_items": warning_items,
            "nonstandard_count": len(nonstandard_items),
        },
        "reports": reports_meta,
        "primary_log": str(primary_log) if primary_log is not None else "",
        "phases": phases,
        "current_phase": phases[-1]["text"] if phases else "",
        "log_tail": log_tail,
        "exited_vivado": exited_vivado,
        "last_completed_step": last_completed_step,
        "inferred_top": inferred_top,
        "inferred_part": inferred_part,
        "inferred_version": inferred_version,
    }


def _infer_ooc_metadata_from_tcl(directory: Path) -> tuple[str, str]:
    """尝试从工作目录里的 ooc_synth.tcl 提取 top 与 part 提示。"""
    tcl = directory / "ooc_synth.tcl"
    if not tcl.is_file() or _is_link_or_junction(tcl):
        return "", ""
    try:
        head = tcl.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        return "", ""
    m = re.search(r"synth_design\s+-top\s+(\S+)\s+-part\s+(\S+)", head)
    if m:
        return m.group(1), m.group(2)
    return "", ""


def _infer_runs_status(runs_dir: Path, run_ids: list[str]) -> tuple[str, list[dict[str, Any]]]:
    """从 .runs/<run> 目录的标记文件只读推断离线工程状态。"""
    candidates = list(run_ids)
    if not candidates and runs_dir.is_dir():
        candidates = sorted(
            p.name
            for p in runs_dir.iterdir()
            if p.is_dir() and not _is_link_or_junction(p) and not p.name.startswith(".")
        )
    run_entries: list[dict[str, Any]] = []
    overall = "idle"
    for rid in candidates:
        rdir = runs_dir / rid
        if not rdir.is_dir():
            continue
        has_begin = (rdir / ".vivado.begin.rst").is_file()
        has_end = (rdir / ".vivado.end.rst").is_file()
        has_err = (rdir / ".vivado.error.rst").is_file()
        log_file = rdir / "runme.log"
        status = "idle"
        if has_err:
            status = "failed"
        elif has_begin and has_end:
            status = "completed"
        elif has_begin and not has_end:
            status = "running"
        elif log_file.is_file():
            status = "completed"
        run_entries.append({"run_id": rid, "status": status, "dir": str(rdir)})
    if any(r["status"] == "failed" for r in run_entries):
        overall = "failed"
    elif any(r["status"] == "running" for r in run_entries):
        overall = "running"
    elif any(r["status"] == "completed" for r in run_entries):
        overall = "completed"
    return overall, run_entries


def inspect_offline_dir(
    path: str | Path,
    *,
    max_cw_items: int = 8,
    log_tail_lines: int = 25,
) -> dict[str, Any]:
    """统一离线检查入口：支持 .xpr 工程、.runs 目录或任意 batch/OOC 输出目录。"""
    if path is None or not str(path).strip():
        raise ValueError("请提供 .xpr 工程路径、.runs 目录或批处理/OOC 输出目录")
    raw = Path(str(path).strip()).expanduser()
    if not raw.exists():
        raise FileNotFoundError(f"路径不存在: {raw}")
    resolved = raw.resolve(strict=True)

    xpr_path: Path | None = None
    scan_dir: Path = resolved
    project_name = ""
    top = ""
    part = ""
    vivado_version = ""
    runs_summary: list[dict[str, Any]] = []
    kind = "offline_dir"
    status = "unknown"

    if resolved.is_file():
        if resolved.suffix.lower() != ".xpr":
            raise ValueError(f"不支持的文件类型: {resolved.name}；请传入 .xpr 文件或目录")
        xpr_path = resolved
    elif resolved.is_dir():
        xpr_candidates = sorted(
            p for p in resolved.iterdir()
            if p.is_file() and not _is_link_or_junction(p) and p.suffix.lower() == ".xpr"
        )
        if len(xpr_candidates) == 1 and not (resolved / "ooc_synth.tcl").is_file():
            xpr_path = xpr_candidates[0]
    else:
        raise ValueError(f"路径既不是文件也不是目录: {resolved}")

    if xpr_path is not None:
        kind = "xpr_project"
        cfg = parse_xpr(str(xpr_path))
        project_name = xpr_path.stem
        top = cfg.top
        part = cfg.part
        vivado_version = f"{cfg.version}.{cfg.minor}".strip(".") if cfg.version else ""
        runs_dir = xpr_path.parent / f"{xpr_path.stem}.runs"
        scan_dir = runs_dir if runs_dir.is_dir() else xpr_path.parent
        run_ids = [r.run_id for r in cfg.runs]
        if runs_dir.is_dir():
            status, runs_summary = _infer_runs_status(runs_dir, run_ids)
    elif resolved.name.lower().endswith(".runs"):
        kind = "runs_dir"
        project_name = resolved.name[: -len(".runs")]
        status, runs_summary = _infer_runs_status(resolved, [])
        run_ids = [r["run_id"] for r in runs_summary]
    else:
        run_ids = []
        tcl_top, tcl_part = _infer_ooc_metadata_from_tcl(resolved)
        top = tcl_top
        part = tcl_part
        kind = "ooc_dir" if tcl_top else "offline_dir"

    parsed = parse_directory_reports(
        scan_dir,
        is_failed=(status == "failed"),
        scan_nonstandard=(status != "running"),
        priority_dirs=run_ids or None,
        max_issues=max(50, max_cw_items),
        log_tail_lines=log_tail_lines,
    )

    if status == "unknown":
        if parsed["diagnostics"]["errors"] > 0 or parsed["diagnostics"]["nonstandard_count"] > 0:
            status = "failed"
        elif parsed["reports"] or parsed["primary_log"]:
            status = "completed"
        else:
            status = "idle"
    elif status == "completed" and (
        parsed["diagnostics"]["errors"] > 0 or parsed["diagnostics"]["nonstandard_count"] > 0
    ):
        status = "failed"

    top = top or parsed["inferred_top"]
    part = part or parsed["inferred_part"]
    vivado_version = vivado_version or parsed["inferred_version"]

    return {
        "target_path": str(resolved),
        "kind": kind,
        "project": project_name,
        "xpr_path": str(xpr_path) if xpr_path is not None else "",
        "output_dir": str(scan_dir),
        "log_path": parsed["primary_log"],
        "status": status,
        "top": top,
        "part": part,
        "vivado_version": vivado_version,
        "runs": runs_summary,
        "timing": parsed["timing"],
        "utilization": parsed["utilization"],
        "utilization_source": parsed["utilization_source"],
        "diagnostics": {
            "errors": parsed["diagnostics"]["errors"],
            "critical_warnings": parsed["diagnostics"]["critical_warnings"],
            "warnings": parsed["diagnostics"]["warnings"],
            "top_issues": parsed["diagnostics"]["top_issues"][:max_cw_items],
            "all_issues": parsed["diagnostics"]["top_issues"],
            "warning_items": parsed["diagnostics"]["warning_items"],
        },
        "reports": [
            {
                "name": r["name"],
                "rel_path": r["rel_path"],
                "path": r["path"],
                "size": r["size"],
                "kind": r["kind"],
            }
            for r in parsed["reports"]
        ],
        "phases": parsed["phases"],
        "current_phase": parsed["current_phase"],
        "log_tail": parsed["log_tail"],
        "inspected_at": time.time(),
    }


def compact_summary_from_record(
    record: dict[str, Any],
    *,
    max_cw_items: int = 8,
    include_log_tail: bool = False,
) -> dict[str, Any]:
    """构造超紧凑 MCP 工具返回值（典型 < 2 KB）。"""
    diag = record.get("diagnostics") or {}
    issues = diag.get("all_issues") or diag.get("top_issues") or []
    compact_issues: list[dict[str, Any]] = []
    for item in issues[:max_cw_items]:
        entry = {
            "severity": item.get("severity", ""),
            "id": item.get("id", "UNKNOWN"),
            "count": item.get("count", 1),
            "line": item.get("line", 0),
            "message": str(item.get("message", ""))[:200],
        }
        if item.get("suggestion") and item.get("category") != "UNKNOWN":
            entry["suggestion"] = str(item["suggestion"]).splitlines()[0][:140]
        compact_issues.append(entry)

    timing_raw = record.get("timing") or _empty_timing()
    timing_compact = {
        "wns": timing_raw.get("wns"),
        "tns": timing_raw.get("tns"),
        "whs": timing_raw.get("whs"),
        "ths": timing_raw.get("ths"),
        "met": timing_raw.get("met"),
        "stage": timing_raw.get("stage", "unknown"),
        "parse_status": timing_raw.get("parse_status", "missing"),
    }

    reports_compact = [
        {"name": r["name"], "path": r["path"]}
        for r in (record.get("reports") or [])
    ]

    result: dict[str, Any] = {
        "job_id": record.get("job_id", ""),
        "kind": record.get("kind", "batch_tcl"),
        "status": record.get("status", "unknown"),
        "exit_code": record.get("exit_code"),
        "elapsed_sec": record.get("elapsed_sec"),
        "top": record.get("top", ""),
        "part": record.get("part", ""),
        "timing": timing_compact,
        "utilization": record.get("utilization") or _empty_utilization(),
        "diagnostics": {
            "errors": diag.get("errors", 0),
            "critical_warnings": diag.get("critical_warnings", 0),
            "warnings": diag.get("warnings", 0),
            "top_issues": compact_issues,
        },
        "reports": reports_compact,
        "log_path": record.get("log_path", ""),
        "output_dir": record.get("output_dir", ""),
    }
    if record.get("wait_timed_out"):
        result["wait_timed_out"] = True
    if record.get("current_phase"):
        result["current_phase"] = record["current_phase"]
    if record.get("error_message"):
        result["error_message"] = record["error_message"]
    if record.get("forensics"):
        result["forensics"] = record["forensics"]
    if include_log_tail or record.get("status") in {"failed", "timeout", "cancelled"}:
        tail = record.get("log_tail") or []
        if tail:
            limit = len(tail) if include_log_tail else 12
            result["log_tail"] = [
                line["text"] if isinstance(line, dict) else str(line)
                for line in tail[-limit:]
            ]
    return result


def _find_build_artifact(
    search_dirs: list[Path],
    suffix: str,
    *,
    top: str = "",
) -> str | None:
    """在指定目录中查找最新构建产物（.bit / .ltx / .xsa），不存在返回 None。"""
    candidates: list[tuple[int, float, Path]] = []
    seen: set[str] = set()
    top_low = top.lower().strip()
    for base in search_dirs:
        if not base.is_dir() or _is_link_or_junction(base):
            continue
        try:
            items = list(base.iterdir())
        except OSError:
            continue
        for item in items:
            if not item.is_file() or _is_link_or_junction(item):
                continue
            if item.suffix.lower() != suffix:
                continue
            norm = os.path.normcase(str(item))
            if norm in seen:
                continue
            seen.add(norm)
            name_low = item.stem.lower()
            prio = 2 if (top_low and name_low == top_low) else (
                0 if name_low == "debug_nets" else 1
            )
            try:
                mtime = item.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((prio, mtime, item))
    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1], row[2].name), reverse=True)
    return str(candidates[0][2])


def summarize_build_target(
    target: str | Path,
    *,
    run_name: str = "impl_1",
) -> dict[str, Any]:
    """提取 .xpr / .runs / OOC 或批处理输出目录的超紧凑签核摘要（< 700B JSON）。"""
    if target is None or not str(target).strip():
        raise ValueError("请提供 .xpr 工程路径、.runs 目录或批处理/OOC 输出目录")
    raw = Path(str(target).strip()).expanduser()
    if not raw.exists():
        raise FileNotFoundError(f"路径不存在: {raw}")
    resolved = raw.resolve(strict=True)

    xpr_path: Path | None = None
    runs_dir: Path | None = None
    scan_dir: Path = resolved
    requested_run = (run_name or "impl_1").strip() or "impl_1"
    top = ""
    part = ""
    status = "unknown"

    if resolved.is_file():
        if resolved.suffix.lower() != ".xpr":
            raise ValueError(f"不支持的文件类型: {resolved.name}；请传入 .xpr 文件或目录")
        xpr_path = resolved
    elif resolved.is_dir():
        xpr_candidates = sorted(
            p for p in resolved.iterdir()
            if p.is_file() and not _is_link_or_junction(p) and p.suffix.lower() == ".xpr"
        )
        if len(xpr_candidates) == 1 and not (resolved / "ooc_synth.tcl").is_file():
            xpr_path = xpr_candidates[0]
    else:
        raise ValueError(f"路径既不是文件也不是目录: {resolved}")

    if xpr_path is not None:
        cfg = parse_xpr(str(xpr_path))
        top = cfg.top
        part = cfg.part
        cand_runs = xpr_path.parent / f"{xpr_path.stem}.runs"
        if cand_runs.is_dir():
            runs_dir = cand_runs
    elif resolved.name.lower().endswith(".runs"):
        runs_dir = resolved

    parsed: dict[str, Any] | None = None
    synth_8_3848_extra: int | None = None

    if runs_dir is not None:
        run_dir = runs_dir / requested_run
        if run_dir.is_dir() and not _is_link_or_junction(run_dir):
            scan_dir = run_dir
            status, _ = _infer_runs_status(runs_dir, [requested_run])
            parsed = parse_directory_reports(
                scan_dir,
                is_failed=(status == "failed"),
                scan_nonstandard=(status != "running"),
                max_issues=5,
                log_tail_lines=5,
            )
        else:
            scan_dir = runs_dir
            status = "idle"
        if requested_run != "synth_1":
            synth_log = runs_dir / "synth_1" / "runme.log"
            if synth_log.is_file() and not _is_link_or_junction(synth_log):
                try:
                    synth_text, _ = _read_text_bounded(synth_log, 2 * 1024 * 1024)
                    synth_8_3848_extra = sum(
                        1 for ln in synth_text.splitlines() if "[Synth 8-3848]" in ln
                    )
                except OSError:
                    pass
    else:
        if (
            (resolved / ".vivado.begin.rst").is_file()
            or (resolved / ".vivado.error.rst").is_file()
            or (resolved / ".vivado.end.rst").is_file()
        ):
            _, entries = _infer_runs_status(resolved.parent, [resolved.name])
            if entries:
                status = entries[0]["status"]
        tcl_top, tcl_part = _infer_ooc_metadata_from_tcl(resolved)
        top = top or tcl_top
        part = part or tcl_part
        parsed = parse_directory_reports(
            scan_dir,
            is_failed=(status == "failed"),
            scan_nonstandard=(status != "running"),
            max_issues=5,
            log_tail_lines=5,
        )

    if parsed is None:
        timing_raw = _empty_timing()
        util_raw = _empty_utilization()
        has_log = False
        err_cnt: int | None = None
        cw_cnt: int | None = None
        drc_errors: int | None = None
        synth_8_3848: int | None = synth_8_3848_extra
        power_w: float | None = None
    else:
        timing_raw = parsed["timing"]
        util_raw = parsed["utilization"]
        has_log = bool(parsed.get("has_log_evidence"))
        diag_raw = parsed["diagnostics"]
        err_cnt = diag_raw["errors"] if has_log else None
        cw_cnt = diag_raw["critical_warnings"] if has_log else None
        drc_errors = parsed.get("drc_errors")
        synth_8_3848 = parsed.get("synth_8_3848")
        if synth_8_3848_extra is not None:
            synth_8_3848 = (synth_8_3848 or 0) + synth_8_3848_extra
        power_w = parsed.get("power_w")
        top = top or parsed.get("inferred_top", "")
        part = part or parsed.get("inferred_part", "")

        if status == "unknown":
            if diag_raw["errors"] > 0 or diag_raw["nonstandard_count"] > 0:
                status = "failed"
            elif parsed["reports"] or parsed["primary_log"]:
                status = "completed"
            else:
                status = "idle"
        elif status == "completed" and (
            diag_raw["errors"] > 0 or diag_raw["nonstandard_count"] > 0
        ):
            status = "failed"

    search_dirs: list[Path] = []
    if scan_dir.is_dir():
        search_dirs.append(scan_dir)
    if xpr_path is not None and xpr_path.parent not in search_dirs:
        search_dirs.append(xpr_path.parent)
    elif resolved != scan_dir and resolved.is_dir() and resolved not in search_dirs:
        search_dirs.append(resolved)

    artifacts: dict[str, str | None] = {
        "bit": _find_build_artifact(search_dirs, ".bit", top=top),
        "ltx": _find_build_artifact(search_dirs, ".ltx", top=top),
        "xsa": _find_build_artifact(search_dirs, ".xsa", top=top),
    }

    summary: dict[str, Any] = {
        "status": status,
        "top": top or None,
        "part": part or None,
        "timing": {
            "wns": timing_raw.get("wns"),
            "tns": timing_raw.get("tns"),
            "whs": timing_raw.get("whs"),
            "ths": timing_raw.get("ths"),
            "wpws": timing_raw.get("wpws"),
            "met": timing_raw.get("met"),
        },
        "utilization": {
            k: util_raw.get(k) for k in ("lut", "ff", "dsp", "bram", "uram")
        },
        "diagnostics": {
            "errors": err_cnt,
            "cw": cw_cnt,
            "synth_8_3848": synth_8_3848,
            "drc_errors": drc_errors,
        },
        "power_w": power_w,
        "artifacts": artifacts,
    }

    def _encoded_len(obj: dict[str, Any]) -> int:
        return len(
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

    if _encoded_len(summary) >= 700:
        base_root = xpr_path.parent if xpr_path is not None else scan_dir
        for key, val in list(artifacts.items()):
            if val:
                try:
                    artifacts[key] = str(Path(val).relative_to(base_root))
                except ValueError:
                    artifacts[key] = Path(val).name
        if _encoded_len(summary) >= 700:
            for key, val in list(artifacts.items()):
                if val:
                    artifacts[key] = Path(val).name
        if _encoded_len(summary) >= 700:
            for rk, rv in list(summary["utilization"].items()):
                if isinstance(rv, dict) and "avail" in rv:
                    summary["utilization"][rk] = {
                        "used": rv.get("used"),
                        "pct": rv.get("pct"),
                    }
    return summary


def _normalize_kv_items(
    raw: dict[str, Any] | list[str] | None,
    *,
    kind: str,
    allow_empty_val: bool,
) -> list[str]:
    items: list[str] = []
    if raw is None:
        return items
    pairs: list[tuple[str, Any]] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(f"{kind} 条目必须是非空字符串")
            text = entry.strip()
            if "=" in text:
                k, v = text.split("=", 1)
                pairs.append((k.strip(), v.strip()))
            else:
                pairs.append((text, None))
    else:
        raise ValueError(f"{kind} 必须是字典或字符串列表")

    for key, val in pairs:
        validate_identifier(str(key), kind)
        if val is None or val == "":
            if not allow_empty_val:
                raise ValueError(f"{kind} '{key}' 缺少值")
            items.append(str(key))
            continue
        val_str = str(val)
        if any(ch in val_str for ch in ("\n", "\r", "\x00")):
            raise ValueError(f"{kind} '{key}' 的值不能包含换行或 NUL")
        items.append(f"{key}={val_str}")
    return items


def build_ooc_synth_tcl(
    *,
    top: str,
    part: str,
    files: list[str],
    output_dir: Path,
    job_dir: Path,
    include_dirs: list[str] | None = None,
    defines: dict[str, Any] | list[str] | None = None,
    generics: dict[str, Any] | list[str] | None = None,
    clock_port: str | None = None,
    clock_period_ns: float | None = None,
    synth_args: list[str] | None = None,
) -> tuple[str, str | None, list[Path]]:
    """生成确定性的 OOC 综合 Tcl 脚本与可选时钟 XDC。"""
    validate_identifier(top, "top")
    validate_identifier(part, "part")
    if not isinstance(files, list) or not files:
        raise ValueError("files 必须是非空源文件路径列表")

    resolved_files: list[Path] = []
    read_lines: list[str] = []
    for raw_file in files:
        if not isinstance(raw_file, str) or not raw_file.strip():
            raise ValueError("files 中的路径不能为空")
        fpath = Path(raw_file.strip()).expanduser().resolve()
        if not fpath.is_file():
            raise FileNotFoundError(f"设计文件不存在: {fpath}")
        if not str(fpath).isascii():
            raise ValueError(
                f"检测到非 ASCII 源文件路径，已停止生成 OOC 综合脚本：\n源文件: {fpath}"
            )
        ext = fpath.suffix.lower()
        qpath = to_tcl_path(str(fpath))
        if ext == ".sv":
            read_lines.append(f"read_verilog -sv {qpath}")
        elif ext == ".v":
            read_lines.append(f"read_verilog {qpath}")
        elif ext in {".vhd", ".vhdl"}:
            read_lines.append(f"read_vhdl {qpath}")
        elif ext == ".xci":
            read_lines.append(f"read_ip {qpath}")
        elif ext == ".xdc":
            read_lines.append(f"read_xdc {qpath}")
        else:
            raise ValueError(
                f"不支持的文件类型: {ext or '(无扩展名)'} ({fpath})；"
                "支持 .sv/.v/.vhd/.vhdl/.xci/.xdc"
            )
        resolved_files.append(fpath)

    resolved_inc: list[str] = []
    for raw_inc in include_dirs or []:
        if not isinstance(raw_inc, str) or not raw_inc.strip():
            raise ValueError("include_dirs 中的路径不能为空")
        ipath = Path(raw_inc.strip()).expanduser().resolve()
        if not ipath.is_dir():
            raise FileNotFoundError(f"include 目录不存在: {ipath}")
        if not str(ipath).isascii():
            raise ValueError(
                f"检测到非 ASCII include 目录，已停止生成 OOC 综合脚本：\ninclude 目录: {ipath}"
            )
        resolved_inc.append(to_tcl_path(str(ipath)))

    define_items = _normalize_kv_items(defines, kind="defines", allow_empty_val=True)
    generic_items = _normalize_kv_items(generics, kind="generics", allow_empty_val=False)

    if (clock_port is None) != (clock_period_ns is None):
        raise ValueError("clock_port 和 clock_period_ns 必须同时提供")

    xdc_text: str | None = None
    if clock_port is not None and clock_period_ns is not None:
        port_clean = str(clock_port).strip()
        if not port_clean or not _RE_CLOCK_PORT.fullmatch(port_clean):
            raise ValueError(f"clock_port 含非法字符: {clock_port!r}")
        period = float(clock_period_ns)
        if not math.isfinite(period) or period <= 0:
            raise ValueError(f"clock_period_ns 必须是大于 0 的有限数字，收到 {clock_period_ns!r}")
        xdc_path = job_dir / "ooc_clock.xdc"
        xdc_text = f"create_clock -name ooc_clk -period {period:g} [get_ports {{{port_clean}}}]\n"
        read_lines.append(f"read_xdc {to_tcl_path(str(xdc_path))}")

    extra_tokens: list[str] = []
    for tok in synth_args or []:
        if not isinstance(tok, str) or not tok.strip():
            raise ValueError("synth_args 参数不能为空")
        clean_tok = tok.strip()
        if not _RE_SYNTH_ARG_TOKEN.fullmatch(clean_tok):
            raise ValueError(f"synth_args 含非法字符: {tok!r}")
        extra_tokens.append(clean_tok)

    synth_parts = [
        "synth_design",
        f"-top {top}",
        f"-part {part}",
        "-mode out_of_context",
    ]
    if resolved_inc:
        synth_parts.append(f"-include_dirs [list {' '.join(resolved_inc)}]")
    for item in define_items:
        synth_parts.append(f"-verilog_define {tcl_quote(item)}")
    for item in generic_items:
        synth_parts.append(f"-generic {tcl_quote(item)}")
    synth_parts.extend(extra_tokens)

    timing_rpt = to_tcl_path(str(output_dir / "timing_summary.rpt"))
    util_rpt = to_tcl_path(str(output_dir / "utilization.rpt"))

    lines = [
        "# Generated by otter-vivado run_ooc_synth",
        f"# Top: {top} | Part: {part}",
        *read_lines,
        " ".join(synth_parts),
        f"report_timing_summary -file {timing_rpt}",
        f"report_utilization -file {util_rpt}",
        "",
    ]
    return "\n".join(lines), xdc_text, resolved_files


@dataclass
class BatchJob:
    """单个托管批处理或 OOC 综合任务。"""

    job_id: str
    kind: str  # "batch_tcl" | "ooc_synth"
    status: str  # "running" | "completed" | "failed" | "cancelled" | "timeout"
    started_at: float
    finished_at: float | None = None
    elapsed_sec: float | None = None
    exit_code: int | None = None
    pid: int | None = None
    top: str = ""
    part: str = ""
    project: str = ""
    tcl_path: str = ""
    output_dir: str = ""
    log_path: str = ""
    cwd: str = ""
    vivado_path: str = ""
    vivado_version: str = ""
    current_phase: str = ""
    phases: list[dict[str, Any]] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=_empty_timing)
    utilization: dict[str, Any] = field(default_factory=_empty_utilization)
    utilization_source: str = ""
    diagnostics: dict[str, Any] = field(
        default_factory=lambda: {
            "errors": 0,
            "critical_warnings": 0,
            "warnings": 0,
            "top_issues": [],
            "all_issues": [],
            "warning_items": [],
        }
    )
    reports: list[dict[str, Any]] = field(default_factory=list)
    log_tail: list[dict[str, Any]] = field(default_factory=list)
    error_message: str = ""
    forensics: dict[str, Any] | None = None
    owner_pid: int = 0
    client: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": self.elapsed_sec,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "owner_pid": self.owner_pid,
            "client": self.client,
            "is_local_owner": (self.owner_pid == os.getpid()),
            "top": self.top,
            "part": self.part,
            "project": self.project,
            "tcl_path": self.tcl_path,
            "output_dir": self.output_dir,
            "log_path": self.log_path,
            "cwd": self.cwd,
            "vivado_path": self.vivado_path,
            "vivado_version": self.vivado_version,
            "current_phase": self.current_phase,
            "phases": copy.deepcopy(self.phases),
            "timing": copy.deepcopy(self.timing),
            "utilization": copy.deepcopy(self.utilization),
            "utilization_source": self.utilization_source,
            "diagnostics": copy.deepcopy(self.diagnostics),
            "reports": copy.deepcopy(self.reports),
            "log_tail": copy.deepcopy(self.log_tail),
            "error_message": self.error_message,
            "forensics": copy.deepcopy(self.forensics),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchJob:
        return cls(
            job_id=str(data.get("job_id", "")),
            kind=str(data.get("kind", "batch_tcl")),
            status=str(data.get("status", "failed")),
            started_at=float(data.get("started_at", 0.0)),
            finished_at=data.get("finished_at"),
            elapsed_sec=data.get("elapsed_sec"),
            exit_code=data.get("exit_code"),
            pid=data.get("pid"),
            top=str(data.get("top", "")),
            part=str(data.get("part", "")),
            project=str(data.get("project", "")),
            tcl_path=str(data.get("tcl_path", "")),
            output_dir=str(data.get("output_dir", "")),
            log_path=str(data.get("log_path", "")),
            cwd=str(data.get("cwd", "")),
            vivado_path=str(data.get("vivado_path", "")),
            vivado_version=str(data.get("vivado_version", "")),
            current_phase=str(data.get("current_phase", "")),
            phases=list(data.get("phases") or []),
            timing=dict(data.get("timing") or _empty_timing()),
            utilization=dict(data.get("utilization") or _empty_utilization()),
            utilization_source=str(data.get("utilization_source", "")),
            diagnostics=dict(
                data.get("diagnostics")
                or {
                    "errors": 0,
                    "critical_warnings": 0,
                    "warnings": 0,
                    "top_issues": [],
                    "all_issues": [],
                    "warning_items": [],
                }
            ),
            reports=list(data.get("reports") or []),
            log_tail=list(data.get("log_tail") or []),
            error_message=str(data.get("error_message", "")),
            forensics=data.get("forensics"),
            owner_pid=int(data.get("owner_pid") or 0),
            client=str(data.get("client") or ""),
        )


def _clamp_wait_seconds(value: int, *, minimum: int) -> int:
    return max(minimum, min(int(value), _MAX_WAIT_SECONDS))


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """跨平台终止 Vivado 批处理子进程树，避免遗留孤儿 vivado.exe。"""
    if proc.returncode is not None:
        return
    pid = getattr(proc, "pid", None)
    if sys.platform == "win32" and pid:
        try:
            kwargs: dict[str, Any] = {}
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            res = await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=8,
                **kwargs,
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            if proc.returncode is not None:
                return
            if getattr(res, "returncode", 1) == 0:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                if proc.returncode is not None:
                    return
        except Exception:
            pass
    elif hasattr(os, "killpg") and pid:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            if proc.returncode is not None:
                return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)


class BatchJobRegistry:
    """管理批处理 Tcl / OOC 综合子进程、轻量 JSON 元数据及离线检查列表。"""

    def __init__(self, jobs_dir: Path | None = None):
        self._jobs_dir_override = Path(jobs_dir) if jobs_dir is not None else None
        self._lock = threading.RLock()
        self._jobs: dict[str, BatchJob] = {}
        self._inspected: dict[str, dict[str, Any]] = {}
        self._inspected_sigs: dict[str, tuple[int, int, int] | None] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._file_cache: dict[str, tuple[int, int]] = {}
        self._job_filenames: dict[str, str] = {}
        self._loaded_root: Path | None = None
        self._closed = False

    @property
    def jobs_dir(self) -> Path:
        if self._jobs_dir_override is not None:
            return self._jobs_dir_override
        return user_cache_root() / "jobs"

    def _is_remote_job_alive(self, job: BatchJob) -> bool:
        if job.job_id in self._processes:
            return True
        pid_alive = bool(
            job.pid is not None and int(job.pid) > 0 and is_pid_alive(int(job.pid))
        )
        owner_alive = bool(
            job.owner_pid > 0
            and job.owner_pid != os.getpid()
            and is_pid_alive(int(job.owner_pid))
        )
        return pid_alive or owner_alive

    def _mark_orphaned_job(self, job: BatchJob) -> None:
        job.status = "failed"
        job.finished_at = job.finished_at or time.time()
        job.elapsed_sec = round(max(0.0, job.finished_at - job.started_at), 2)
        if not job.error_message:
            job.error_message = "MCP 服务重启后历史进程已脱离托管，结果未确认"

    def _sync_external_jobs(self, root: Path) -> None:
        """热同步其他进程写入的 *.json 任务文件（按 (mtime_ns, size) 跳过未变更文件）。"""
        if not root.is_dir():
            return
        seen_names: set[str] = set()
        for meta_file in sorted(root.glob("*.json")):
            if meta_file.name == "inspected.json" or _is_link_or_junction(meta_file):
                continue
            try:
                st = meta_file.stat()
            except OSError:
                continue
            sig = (int(st.st_mtime_ns), int(st.st_size))
            seen_names.add(meta_file.name)
            if self._file_cache.get(meta_file.name) == sig:
                continue
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                job = BatchJob.from_dict(data)
            except (OSError, ValueError, TypeError):
                continue
            self._file_cache[meta_file.name] = sig
            if not job.job_id:
                continue
            self._job_filenames[job.job_id] = meta_file.name
            if job.job_id in self._processes:
                continue
            if job.status == "running" and not self._is_remote_job_alive(job):
                self._mark_orphaned_job(job)
            self._jobs[job.job_id] = job

        for fname in list(self._file_cache):
            if fname not in seen_names:
                self._file_cache.pop(fname, None)

        for jid in list(self._jobs):
            if jid in self._processes or jid in self._tasks:
                continue
            fname = self._job_filenames.get(jid, f"{jid}.json")
            if fname not in seen_names:
                self._jobs.pop(jid, None)
                self._job_filenames.pop(jid, None)
                continue
            job = self._jobs[jid]
            if job.status == "running" and not self._is_remote_job_alive(job):
                self._mark_orphaned_job(job)

        if len(self._jobs) > _MAX_PERSISTED_JOBS:
            ordered = sorted(
                self._jobs.values(),
                key=lambda j: (j.job_id in self._processes, j.started_at),
                reverse=True,
            )
            keep_ids = {j.job_id for j in ordered[:_MAX_PERSISTED_JOBS]}
            for jid in list(self._jobs):
                if jid not in keep_ids and jid not in self._processes:
                    self._jobs.pop(jid, None)
                    self._job_filenames.pop(jid, None)

    def _ensure_loaded(self) -> None:
        with self._lock:
            root = self.jobs_dir
            if self._loaded_root == root:
                self._sync_external_jobs(root)
                return
            self._jobs.clear()
            self._inspected.clear()
            self._inspected_sigs.clear()
            self._file_cache.clear()
            self._job_filenames.clear()
            if root.is_dir():
                loaded: list[BatchJob] = []
                for meta_file in sorted(root.glob("*.json")):
                    if meta_file.name == "inspected.json" or _is_link_or_junction(meta_file):
                        continue
                    try:
                        st = meta_file.stat()
                        data = json.loads(meta_file.read_text(encoding="utf-8"))
                        job = BatchJob.from_dict(data)
                    except (OSError, ValueError, TypeError):
                        continue
                    self._file_cache[meta_file.name] = (int(st.st_mtime_ns), int(st.st_size))
                    if not job.job_id:
                        continue
                    self._job_filenames[job.job_id] = meta_file.name
                    if job.status == "running" and not self._is_remote_job_alive(job):
                        self._mark_orphaned_job(job)
                    loaded.append(job)
                loaded.sort(key=lambda j: j.started_at, reverse=True)
                for job in loaded[:_MAX_PERSISTED_JOBS]:
                    self._jobs[job.job_id] = job

                inspected_file = root / "inspected.json"
                if inspected_file.is_file() and not _is_link_or_junction(inspected_file):
                    try:
                        entries = json.loads(inspected_file.read_text(encoding="utf-8"))
                        if isinstance(entries, list):
                            for item in entries[:30]:
                                if isinstance(item, dict) and item.get("target_path"):
                                    self._inspected[str(item["target_path"])] = item
                    except (OSError, ValueError, TypeError):
                        pass
            self._loaded_root = root

    def _persist_job(self, job: BatchJob) -> None:
        with self._lock:
            root = self.jobs_dir
            root.mkdir(parents=True, exist_ok=True)
            target = root / f"{job.job_id}.json"
            tmp = root / f".{job.job_id}.{uuid.uuid4().hex[:6]}.tmp"
            payload = json.dumps(job.to_dict(), ensure_ascii=False, indent=2)
            _atomic_write_json(tmp, target, payload)
            with contextlib.suppress(OSError):
                st = target.stat()
                self._file_cache[target.name] = (int(st.st_mtime_ns), int(st.st_size))
                self._job_filenames[job.job_id] = target.name

    def _persist_inspected(self) -> None:
        with self._lock:
            root = self.jobs_dir
            root.mkdir(parents=True, exist_ok=True)
            target = root / "inspected.json"
            tmp = root / f".inspected.{uuid.uuid4().hex[:6]}.tmp"
            entries = list(self._inspected.values())
            payload = json.dumps(entries, ensure_ascii=False, indent=2)
            _atomic_write_json(tmp, target, payload)

    def _new_job_id(self, prefix: str) -> str:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        return f"{stamp}_{prefix}_{uuid.uuid4().hex[:6]}"

    async def _spawn_process(
        self,
        command: list[str],
        cwd: Path,
        env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": env,
        }
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            if flags:
                kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        try:
            return await asyncio.create_subprocess_exec(*command, **kwargs)
        except TypeError:
            # 兼容测试里精简签名的 create_subprocess_exec mock
            kwargs.pop("creationflags", None)
            kwargs.pop("start_new_session", None)
            return await asyncio.create_subprocess_exec(*command, **kwargs)

    async def _drain_stream(
        self,
        stream: asyncio.StreamReader | None,
        ring: collections.deque[str],
    ) -> None:
        if stream is None:
            return
        pending = bytearray()
        try:
            while True:
                try:
                    raw = await stream.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    # 单行超出 64 KiB 上限时按块抽干至换行或 EOF，防止阻塞管道
                    preview_chunks: list[bytes] = []
                    preview_len = 0
                    if pending:
                        take = bytes(pending[:512])
                        preview_chunks.append(take)
                        preview_len += len(take)
                        pending.clear()
                    while True:
                        chunk = await stream.read(65536)
                        if not chunk:
                            if preview_chunks:
                                msg = decode_vivado_output(b"".join(preview_chunks)).rstrip("\r\n")
                                if msg:
                                    ring.append(f"{msg[:512]} ... [line truncated]")
                            return
                        nl_idx = chunk.find(b"\n")
                        if nl_idx == -1:
                            if preview_len < 512:
                                take = chunk[: 512 - preview_len]
                                preview_chunks.append(take)
                                preview_len += len(take)
                            continue
                        if preview_len < 512:
                            take = chunk[: min(nl_idx, 512 - preview_len)]
                            preview_chunks.append(take)
                        if preview_chunks:
                            msg = decode_vivado_output(b"".join(preview_chunks)).rstrip("\r\n")
                            if msg:
                                ring.append(f"{msg[:512]} ... [line truncated]")
                        rest = chunk[nl_idx + 1 :]
                        if rest:
                            last_nl = rest.rfind(b"\n")
                            if last_nl != -1:
                                for extra in decode_vivado_output(
                                    rest[: last_nl + 1]
                                ).splitlines():
                                    clean = extra.rstrip("\r\n")
                                    if clean:
                                        ring.append(clean)
                                rem = rest[last_nl + 1 :]
                                if rem:
                                    pending.extend(rem)
                            else:
                                pending.extend(rest)
                        break
                    continue

                if not raw:
                    if pending:
                        tail = decode_vivado_output(bytes(pending)).rstrip("\r\n")
                        if tail:
                            ring.append(tail)
                    break

                if pending:
                    raw = bytes(pending) + raw
                    pending.clear()
                line = decode_vivado_output(raw).rstrip("\r\n")
                if line:
                    ring.append(line)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _collect_failure_forensics(
        self,
        job: BatchJob,
        stderr_lines: list[str],
        stdout_lines: list[str],
    ) -> dict[str, Any]:
        init_settings = inspect_vivado_init_files(job.vivado_path)
        init_info = [
            {
                "path": item.path,
                "exists": item.exists,
                "max_threads": item.max_threads,
                "line_number": item.line_number,
                "error": item.error,
            }
            for item in init_settings
        ]
        silent_exit = (
            job.exit_code not in (None, 0)
            and job.diagnostics.get("errors", 0) == 0
            and not bool(job.error_message and "Tcl" in job.error_message)
        )
        forensics: dict[str, Any] = {
            "vivado_init": init_info,
            "silent_exit_suspected": False,
        }
        if stderr_lines:
            forensics["stderr_tail"] = stderr_lines[-10:]
        if silent_exit:
            log_file = Path(job.log_path) if job.log_path else None
            has_exiting = False
            log_mtime = 0
            if log_file and log_file.is_file():
                with contextlib.suppress(OSError):
                    log_mtime = int(log_file.stat().st_mtime)
                _, tail = _read_log_tail(log_file, max_lines=40)
                has_exiting = any("Exiting Vivado" in item["text"] for item in tail)
            if not has_exiting:
                forensics["silent_exit_suspected"] = True
                if not (log_file and log_file.is_file() and log_file.stat().st_size > 0):
                    forensics["reason"] = (
                        "Vivado 未写入有效日志即非零退出（常见原因：Vivado_init.tcl 异常、"
                        "环境缺少 PROCESSOR_ARCHITECTURE 或许可证/安全软件拦截）"
                    )
                else:
                    forensics["reason"] = (
                        "日志未见 'Exiting Vivado' 即非零退出（疑似子进程崩溃或被终止）"
                    )
                if sys.platform == "win32":
                    ev_status, ev_note, ev_window, events = await asyncio.to_thread(
                        query_windows_crash_events,
                        log_mtime,
                        timeout_sec=2.5,
                    )
                    forensics["event_log_status"] = ev_status
                    forensics["event_log_window"] = ev_window
                    if ev_note:
                        forensics["event_log_note"] = ev_note
                    if events:
                        forensics["crash_events"] = [
                            {
                                "event_id": ev.event_id,
                                "exception_code": ev.exception_code,
                                "faulting_module": ev.faulting_module,
                                "summary": ev.summary,
                            }
                            for ev in events[:5]
                        ]
        if stdout_lines and not forensics.get("stderr_tail"):
            forensics["stdout_tail"] = stdout_lines[-8:]
        return forensics

    async def _watch_job(
        self,
        job: BatchJob,
        proc: asyncio.subprocess.Process,
        event: asyncio.Event,
    ) -> None:
        stdout_ring: collections.deque[str] = collections.deque(maxlen=_STDERR_RING_SIZE)
        stderr_ring: collections.deque[str] = collections.deque(maxlen=_STDERR_RING_SIZE)
        drain_out = asyncio.create_task(self._drain_stream(proc.stdout, stdout_ring))
        drain_err = asyncio.create_task(self._drain_stream(proc.stderr, stderr_ring))
        try:
            rc = await proc.wait()
            await asyncio.gather(drain_out, drain_err, return_exceptions=True)
            finished_at = time.time()
            with self._lock:
                job.finished_at = finished_at
                job.elapsed_sec = round(max(0.0, finished_at - job.started_at), 2)
                job.exit_code = rc
                if job.status != "cancelled":
                    job.status = "completed" if rc == 0 else "failed"
                out_dir = Path(job.output_dir)
                log_file = Path(job.log_path) if job.log_path else None
                job_failed = job.status != "completed"

            parsed = await asyncio.to_thread(
                parse_directory_reports,
                out_dir,
                log_path=log_file,
                is_failed=job_failed,
                max_issues=50,
                log_tail_lines=40,
            )
            with self._lock:
                job.timing = parsed["timing"]
                job.utilization = parsed["utilization"]
                job.utilization_source = parsed["utilization_source"]
                job.diagnostics = {
                    "errors": parsed["diagnostics"]["errors"],
                    "critical_warnings": parsed["diagnostics"]["critical_warnings"],
                    "warnings": parsed["diagnostics"]["warnings"],
                    "top_issues": parsed["diagnostics"]["top_issues"][:8],
                    "all_issues": parsed["diagnostics"]["top_issues"],
                    "warning_items": parsed["diagnostics"]["warning_items"],
                }
                job.reports = parsed["reports"]
                job.phases = parsed["phases"]
                job.current_phase = parsed["current_phase"]
                job.log_tail = parsed["log_tail"]
                if not job.top and parsed["inferred_top"]:
                    job.top = parsed["inferred_top"]
                if not job.part and parsed["inferred_part"]:
                    job.part = parsed["inferred_part"]
                if parsed["inferred_version"]:
                    job.vivado_version = parsed["inferred_version"]

                # 若日志文件为空，回填子进程 stdout/stderr 尾部方便排错
                if not job.log_tail and (stderr_ring or stdout_ring):
                    combined = list(stdout_ring)[-15:] + list(stderr_ring)[-15:]
                    job.log_tail = [
                        {"line": idx, "text": ln} for idx, ln in enumerate(combined, 1)
                    ]
                need_forensics = job.status == "failed"

            if need_forensics:
                forensics = await self._collect_failure_forensics(
                    job, list(stderr_ring), list(stdout_ring)
                )
                with self._lock:
                    job.forensics = forensics
                    if not job.error_message:
                        if forensics.get("reason"):
                            job.error_message = str(forensics["reason"])
                        elif job.diagnostics.get("all_issues"):
                            job.error_message = str(
                                job.diagnostics["all_issues"][0].get("message", "")
                            )
                        else:
                            job.error_message = f"Vivado 退出码 {rc}"
        except asyncio.CancelledError:
            drain_out.cancel()
            drain_err.cancel()
            raise
        except Exception as exc:
            logger.exception("批处理任务监视异常 (%s)", job.job_id)
            with self._lock:
                job.finished_at = time.time()
                job.elapsed_sec = round(max(0.0, job.finished_at - job.started_at), 2)
                if job.status == "running":
                    job.status = "failed"
                job.error_message = f"任务监控异常: {exc}"
        finally:
            with self._lock:
                self._processes.pop(job.job_id, None)
                with contextlib.suppress(OSError):
                    self._persist_job(job)
            event.set()

    def _refresh_live_tail(self, job: BatchJob) -> None:
        """在不干扰子进程的情况下从磁盘刷新运行中任务的阶段与日志尾。"""
        with self._lock:
            if job.status != "running":
                return
            started_at = job.started_at
            log_path_str = job.log_path
        phases: list[dict[str, Any]] = []
        tail: list[dict[str, Any]] = []
        if log_path_str:
            phases, tail = _read_log_tail(Path(log_path_str), max_lines=30)
        with self._lock:
            if job.status != "running":
                return
            job.elapsed_sec = round(max(0.0, time.time() - started_at), 2)
            if phases:
                job.phases = phases
                job.current_phase = phases[-1]["text"]
            if tail:
                job.log_tail = tail

    async def start_batch_tcl(
        self,
        *,
        tcl_file: str | None = None,
        tcl_content: str | None = None,
        cwd: str | None = None,
        output_dir: str | None = None,
        vivado_path: str | None = None,
        tcl_args: list[str] | None = None,
        notrace: bool = True,
        kind: str = "batch_tcl",
        top: str = "",
        part: str = "",
        extra_clock_xdc: tuple[Path, str] | None = None,
        preallocated_job_id: str | None = None,
    ) -> BatchJob:
        self._ensure_loaded()
        if self._closed:
            raise RuntimeError("批处理任务注册表已关闭")

        has_file = bool(tcl_file and tcl_file.strip())
        has_content = tcl_content is not None and bool(str(tcl_content).strip())
        if has_file == has_content:
            raise ValueError("tcl_file 和 tcl_content 必须且只能提供其中之一")

        job_id = preallocated_job_id or self._new_job_id("ooc" if kind == "ooc_synth" else "tcl")
        job_dir = self.jobs_dir / job_id

        resolved_out = (
            Path(output_dir.strip()).expanduser().resolve()
            if output_dir and output_dir.strip()
            else job_dir
        )

        if has_file:
            script_path = Path(str(tcl_file).strip()).expanduser().resolve()
            if not script_path.is_file():
                raise FileNotFoundError(f"Tcl 脚本不存在: {script_path}")
        else:
            script_name = "ooc_synth.tcl" if kind == "ooc_synth" else "batch_run.tcl"
            script_path = job_dir / script_name

        if cwd and cwd.strip():
            cwd_path = Path(cwd.strip()).expanduser().resolve()
            if not cwd_path.is_dir():
                raise FileNotFoundError(f"工作目录不存在或不是目录: {cwd_path}")
        else:
            cwd_path = resolved_out

        log_path = (
            job_dir / "vivado.log" if resolved_out != job_dir else resolved_out / "vivado.log"
        )

        # 路径 ASCII 检查必须在创建任何输出目录之前执行（沿用 local_batch 契约）
        validate_batch_paths(script_path, cwd_path, log_path)
        if not str(resolved_out).isascii():
            raise ValueError(
                f"检测到非 ASCII 输出目录，已停止启动 Vivado：\n输出目录: {resolved_out}"
            )
        if extra_clock_xdc is not None and not str(extra_clock_xdc[0]).isascii():
            raise ValueError(
                f"检测到非 ASCII 时钟约束路径，已停止启动 Vivado：\n约束文件: {extra_clock_xdc[0]}"
            )

        resolved_vivado = find_vivado(vivado_path)
        args_list = [str(arg) for arg in (tcl_args or [])]
        command = build_command(
            resolved_vivado, script_path, log_path, args_list, notrace=notrace
        )

        job_dir.mkdir(parents=True, exist_ok=True)
        resolved_out.mkdir(parents=True, exist_ok=True)
        if not has_file:
            script_path.write_text(str(tcl_content), encoding="utf-8", newline="\n")
        if extra_clock_xdc is not None:
            extra_clock_xdc[0].write_text(extra_clock_xdc[1], encoding="utf-8", newline="\n")

        now = time.time()
        job = BatchJob(
            job_id=job_id,
            kind=kind,
            status="running",
            started_at=now,
            owner_pid=os.getpid(),
            client=detect_client_source(),
            top=top,
            part=part,
            tcl_path=str(script_path),
            output_dir=str(resolved_out),
            log_path=str(log_path),
            cwd=str(cwd_path),
            vivado_path=resolved_vivado,
            vivado_version=get_vivado_version(resolved_vivado),
        )
        with self._lock:
            self._jobs[job_id] = job
            event = asyncio.Event()
            self._events[job_id] = event

        try:
            proc = await self._spawn_process(
                command, cwd_path, sanitize_vivado_child_env()
            )
        except (OSError, FileNotFoundError) as exc:
            forensics = await self._collect_failure_forensics(job, [], [])
            with self._lock:
                job.status = "failed"
                job.finished_at = time.time()
                job.elapsed_sec = round(max(0.0, job.finished_at - job.started_at), 2)
                job.error_message = f"无法启动 Vivado: {exc}"
                job.forensics = forensics
                self._persist_job(job)
            event.set()
            return job

        with self._lock:
            job.pid = getattr(proc, "pid", None)
            self._processes[job_id] = proc
            self._persist_job(job)
            self._tasks[job_id] = asyncio.create_task(self._watch_job(job, proc, event))
        return job

    async def start_ooc_synth(
        self,
        *,
        top: str,
        part: str,
        files: list[str],
        include_dirs: list[str] | None = None,
        defines: dict[str, Any] | list[str] | None = None,
        generics: dict[str, Any] | list[str] | None = None,
        clock_port: str | None = None,
        clock_period_ns: float | None = None,
        synth_args: list[str] | None = None,
        output_dir: str | None = None,
        vivado_path: str | None = None,
    ) -> BatchJob:
        self._ensure_loaded()
        job_id = self._new_job_id("ooc")
        job_dir = self.jobs_dir / job_id
        resolved_out = (
            Path(output_dir.strip()).expanduser().resolve()
            if output_dir and output_dir.strip()
            else job_dir
        )
        tcl_text, xdc_text, _ = build_ooc_synth_tcl(
            top=top,
            part=part,
            files=files,
            output_dir=resolved_out,
            job_dir=job_dir,
            include_dirs=include_dirs,
            defines=defines,
            generics=generics,
            clock_port=clock_port,
            clock_period_ns=clock_period_ns,
            synth_args=synth_args,
        )
        extra_xdc = (job_dir / "ooc_clock.xdc", xdc_text) if xdc_text is not None else None
        return await self.start_batch_tcl(
            tcl_content=tcl_text,
            cwd=None,
            output_dir=str(resolved_out),
            vivado_path=vivado_path,
            kind="ooc_synth",
            top=top,
            part=part,
            extra_clock_xdc=extra_xdc,
            preallocated_job_id=job_id,
        )

    async def wait_job(
        self,
        job_id: str,
        timeout_seconds: int,
        *,
        ctx: Any = None,
    ) -> tuple[BatchJob, bool]:
        """等待任务结束；超时不杀死 Vivado，返回 ``(job, wait_timed_out)``。"""
        with self._lock:
            self._ensure_loaded()
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status != "running":
                return job, False
            event = self._events.get(job_id)
        if event is None:
            return job, False

        wait_limit = _clamp_wait_seconds(timeout_seconds, minimum=_MIN_WAIT_SECONDS)
        started = time.monotonic()
        deadline = started + wait_limit
        last_phase = ""
        while not event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._refresh_live_tail(job)
                return job, True
            step = min(2.0, remaining)
            try:
                await asyncio.wait_for(event.wait(), timeout=step)
            except asyncio.TimeoutError:
                pass
            self._refresh_live_tail(job)
            if ctx is not None and hasattr(ctx, "report_progress"):
                elapsed = round(min(float(wait_limit), time.monotonic() - started), 1)
                with self._lock:
                    msg = job.current_phase or f"Vivado 批处理运行中 ({elapsed:.0f}s)"
                if msg != last_phase or elapsed > 0:
                    last_phase = msg
                    with contextlib.suppress(Exception):
                        await ctx.report_progress(
                            progress=elapsed, total=float(wait_limit), message=msg
                        )

        return job, False

    def get_job(self, job_id: str, *, log_tail_lines: int = 25) -> BatchJob | None:
        with self._lock:
            self._ensure_loaded()
            job = self._jobs.get(job_id)
            if job is None:
                return None
            status = job.status
            tail_len = len(job.log_tail)
            log_path_str = job.log_path
        if status == "running":
            self._refresh_live_tail(job)
        elif log_tail_lines > tail_len and log_path_str:
            phases, tail = _read_log_tail(Path(log_path_str), max_lines=log_tail_lines)
            with self._lock:
                if tail:
                    job.log_tail = tail
                if phases and not job.phases:
                    job.phases = phases
        return job

    def list_jobs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            jobs = sorted(
                self._jobs.values(), key=lambda j: j.started_at, reverse=True
            )[:limit]
        for job in jobs:
            self._refresh_live_tail(job)
        with self._lock:
            return [job.to_dict() for job in jobs]

    async def cancel_job(self, job_id: str) -> BatchJob:
        with self._lock:
            self._ensure_loaded()
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status != "running":
                return job
            job.status = "cancelled"
            job.error_message = "任务已被用户取消"
            proc = self._processes.get(job_id)
            task = self._tasks.get(job_id)
        if proc is not None:
            await _terminate_process_tree(proc)
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=4.0)
        with self._lock:
            if job.finished_at is None:
                job.finished_at = time.time()
                job.elapsed_sec = round(max(0.0, job.finished_at - job.started_at), 2)
            self._persist_job(job)
            return job

    def clear_finished(self) -> int:
        with self._lock:
            self._ensure_loaded()
            to_remove = [jid for jid, job in self._jobs.items() if job.status != "running"]
            for jid in to_remove:
                self._jobs.pop(jid, None)
                self._events.pop(jid, None)
                self._tasks.pop(jid, None)
                fname = self._job_filenames.pop(jid, f"{jid}.json")
                self._file_cache.pop(fname, None)
                meta = self.jobs_dir / fname
                with contextlib.suppress(OSError):
                    if meta.is_file():
                        meta.unlink()
            return len(to_remove)

    def inspect_and_remember(self, path: str) -> dict[str, Any]:
        entry = inspect_offline_dir(path, max_cw_items=8, log_tail_lines=40)
        key = entry["target_path"]
        sig = _dir_report_signature(Path(key))
        with self._lock:
            self._ensure_loaded()
            self._inspected[key] = entry
            self._inspected_sigs[key] = sig
            if len(self._inspected) > 30:
                oldest = next(iter(self._inspected))
                self._inspected.pop(oldest, None)
                self._inspected_sigs.pop(oldest, None)
            with contextlib.suppress(OSError):
                self._persist_inspected()
            return copy.deepcopy(entry)

    def remove_inspected(self, path: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            removed = False
            if path in self._inspected:
                self._inspected.pop(path, None)
                self._inspected_sigs.pop(path, None)
                removed = True
            else:
                norm = os.path.normcase(os.path.normpath(path))
                matches = [
                    k for k in self._inspected if os.path.normcase(os.path.normpath(k)) == norm
                ]
                for k in matches:
                    self._inspected.pop(k, None)
                    self._inspected_sigs.pop(k, None)
                    removed = True
            if removed:
                with contextlib.suppress(OSError):
                    self._persist_inspected()
            return removed

    def list_inspected(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            snapshot = [
                (k, copy.deepcopy(v), self._inspected_sigs.get(k))
                for k, v in self._inspected.items()
            ]

        updates: dict[str, tuple[dict[str, Any], tuple[int, int, int] | None]] = {}
        for target_path, _cached, prev_sig in snapshot:
            cur_sig = _dir_report_signature(Path(target_path))
            if prev_sig is not None and cur_sig is not None and prev_sig == cur_sig:
                continue
            try:
                updated = inspect_offline_dir(
                    target_path, max_cw_items=8, log_tail_lines=40
                )
                updates[target_path] = (updated, cur_sig)
            except (OSError, ValueError):
                pass

        with self._lock:
            for target_path, (updated, cur_sig) in updates.items():
                if target_path in self._inspected:
                    self._inspected[target_path] = updated
                    self._inspected_sigs[target_path] = cur_sig
            ordered = [copy.deepcopy(v) for v in self._inspected.values()]
        ordered.sort(key=lambda item: float(item.get("inspected_at", 0.0)), reverse=True)
        return ordered

    def allowed_report_roots(self, run_monitors_snapshots: list[dict] | None = None) -> list[Path]:
        """收集控制台允许读取报告的白名单根目录（只包含已知任务、观察器和离线检查目录）。"""
        with self._lock:
            self._ensure_loaded()
            job_paths = [(job.output_dir, job.log_path) for job in self._jobs.values()]
            inspected_paths = [
                (item.get("output_dir"), item.get("log_path"))
                for item in self._inspected.values()
            ]

        roots: list[Path] = []
        seen: set[str] = set()

        def add_root(candidate: str | Path | None, *, is_file_hint: bool = False) -> None:
            resolved = _resolve_safe_report_root(candidate, is_file_hint=is_file_hint)
            if resolved is None:
                return
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                roots.append(resolved)

        for out_dir, log_path in job_paths:
            add_root(out_dir)
            if log_path:
                add_root(log_path, is_file_hint=True)
        for out_dir, log_path in inspected_paths:
            add_root(out_dir)
            if log_path:
                add_root(str(log_path), is_file_hint=True)
        for snap in run_monitors_snapshots or []:
            run = snap.get("run") or {}
            add_root(run.get("directory"))
            if run.get("log_path"):
                add_root(str(run["log_path"]), is_file_hint=True)
            for rep in snap.get("reports") or []:
                if rep.get("path"):
                    add_root(str(rep["path"]), is_file_hint=True)
        return roots

    def known_tcl_paths(self) -> set[str]:
        with self._lock:
            self._ensure_loaded()
            tcl_list = [job.tcl_path for job in self._jobs.values() if job.tcl_path]
        paths: set[str] = set()
        for tcl_path in tcl_list:
            with contextlib.suppress(OSError):
                paths.add(os.path.normcase(str(Path(tcl_path).resolve())))
        return paths

    async def close(self) -> None:
        with self._lock:
            self._closed = True
            running_entries = [
                (jid, self._processes.get(jid))
                for jid, job in self._jobs.items()
                if job.status == "running"
            ]
        for jid, proc in running_entries:
            if proc is not None:
                await _terminate_process_tree(proc)
            with self._lock:
                job = self._jobs.get(jid)
                if job is not None:
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    job.elapsed_sec = round(max(0.0, job.finished_at - job.started_at), 2)
                    job.error_message = "MCP 服务关闭，已停止后台批处理任务"
                    with contextlib.suppress(OSError):
                        self._persist_job(job)

        with self._lock:
            tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self._lock:
            self._tasks.clear()
            self._processes.clear()
