"""Vivado 分阶段日志解析、历史耗时基准缓存与阶段加权进度/区间估算。

纯 Python 标准库实现；缓存仅落盘在用户缓存目录 ``%LOCALAPPDATA%\\otter-vivado\\history\\``
（受 ``OTTER_VIVADO_CACHE_DIR`` 覆盖），绝不写入用户工程目录。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import statistics
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from vivado_mcp.batch_jobs import (
    _atomic_write_json,
    _decode_utf8_or_fallback,
    _is_link_or_junction,
)
from vivado_mcp.project_layout import user_cache_root

_MAX_SAMPLES_PER_KEY = 5
_MAX_SEEN_SIGS = 64
_MAX_STORE_KEYS = 200
_SESSION_MARKER = "# Start of session at:"
_GENERIC_TCL_STEMS: frozenset[str] = frozenset(
    {"ooc_synth", "batch_run", "run", "build", "script", "tcl", "vivado", "vivado_run"}
)

# (step_id, label, optional, default_weight)
_IMPL_STEP_SPECS: tuple[tuple[str, str, bool, float], ...] = (
    ("init_design", "初始化", False, 4.0),
    ("opt_design", "优化", False, 8.0),
    ("power_opt_design", "功耗优化", True, 5.0),
    ("place_design", "布局", False, 30.0),
    ("post_place_power_opt_design", "布局后功耗", True, 4.0),
    ("phys_opt_design", "物理优化", False, 10.0),
    ("route_design", "布线", False, 38.0),
    ("post_route_phys_opt_design", "布线后优化", True, 6.0),
    ("write_bitstream", "比特流", True, 10.0),
)
_IMPL_STEP_SET: frozenset[str] = frozenset(spec[0] for spec in _IMPL_STEP_SPECS)
_IMPL_LABEL_MAP: dict[str, str] = {spec[0]: spec[1] for spec in _IMPL_STEP_SPECS}
_IMPL_OPTIONAL_SET: frozenset[str] = frozenset(spec[0] for spec in _IMPL_STEP_SPECS if spec[2])
_IMPL_CORE_REQUIRED: frozenset[str] = frozenset(
    {"init_design", "opt_design", "place_design", "route_design"}
)
_IMPL_DEFAULT_WEIGHTS: dict[str, float] = {spec[0]: spec[3] for spec in _IMPL_STEP_SPECS}

# (step_id, label, cmd, default_weight)
_SYNTH_STEP_SPECS: tuple[tuple[str, str, str, float], ...] = (
    ("elaborate", "RTL 展开", "elaborate", 12.0),
    ("opt_rtl", "RTL 优化", "opt_rtl", 38.0),
    ("tech_map", "工艺映射", "tech_map", 32.0),
    ("io_buf", "IO 与缓存", "io_buf", 10.0),
    ("netlist", "网表与报告", "synth_design", 8.0),
)
_SYNTH_ORDER: tuple[str, ...] = tuple(spec[0] for spec in _SYNTH_STEP_SPECS)
_SYNTH_INDEX: dict[str, int] = {sid: idx for idx, sid in enumerate(_SYNTH_ORDER)}
_SYNTH_LABEL_MAP: dict[str, str] = {spec[0]: spec[1] for spec in _SYNTH_STEP_SPECS}
_SYNTH_CMD_MAP: dict[str, str] = {spec[0]: spec[2] for spec in _SYNTH_STEP_SPECS}
_SYNTH_DEFAULT_WEIGHTS: dict[str, float] = {spec[0]: spec[3] for spec in _SYNTH_STEP_SPECS}

_RUNNING_STATUSES: frozenset[str] = frozenset({"running", "queued"})
_COMPLETED_STATUSES: frozenset[str] = frozenset(
    {"completed", "succeeded", "stage_complete", "reached"}
)
_FAILED_STATUSES: frozenset[str] = frozenset(
    {
        "failed",
        "cancelled",
        "canceled",
        "silent_exit",
        "early_error",
        "error",
        "stalled",
        "memory_critical",
        "timeout",
    }
)

_IMPL_STEP_ALT = (
    "init_design|opt_design|power_opt_design|place_design|"
    "post_place_power_opt_design|phys_opt_design|route_design|"
    "post_route_phys_opt_design|write_bitstream"
)

_RE_IMPL_CMD = re.compile(rf"^\s*Command:\s+({_IMPL_STEP_ALT})\b", re.IGNORECASE)
_RE_IMPL_TIME = re.compile(
    rf"^\s*({_IMPL_STEP_ALT}):\s+Time\s*\(s\):.*?\belapsed\s*=\s*(\d+:\d+(?::\d+)?|\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_IMPL_DONE = re.compile(
    rf"\b({_IMPL_STEP_ALT})\s+completed\s+successfully\b",
    re.IGNORECASE,
)

_RE_SYNTH_CMD = re.compile(r"^\s*Command:\s+synth_design\b", re.IGNORECASE)
_RE_SYNTH_TIME = re.compile(
    r"^\s*synth_design:\s+Time\s*\(s\):.*?\belapsed\s*=\s*(\d+:\d+(?::\d+)?|\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_SYNTH_DONE = re.compile(r"\bsynth_design\s+completed\s+successfully\b", re.IGNORECASE)

_SYNTH_START_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("elaborate", re.compile(r"^\s*Start(?:ing)?\s+RTL\s+Elaboration\b", re.IGNORECASE)),
    (
        "opt_rtl",
        re.compile(
            r"^\s*Start(?:ing)?\s+(?:RTL\s+Optimization|Cross\s+Boundary\s+and\s+Area\s+Optimization)\b",
            re.IGNORECASE,
        ),
    ),
    ("tech_map", re.compile(r"^\s*Start(?:ing)?\s+Technology\s+Mapping\b", re.IGNORECASE)),
    ("io_buf", re.compile(r"^\s*Start(?:ing)?\s+IO\s+Insertion\b", re.IGNORECASE)),
    (
        "netlist",
        re.compile(r"^\s*Start(?:ing)?\s+Writing\s+Synthesis\s+Report\b", re.IGNORECASE),
    ),
)

_SYNTH_FINISH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "elaborate",
        re.compile(
            r"^\s*Finished\s+RTL\s+Elaboration\b(?:.*?Time\s*\(s\):.*?\belapsed\s*=\s*(\d+:\d+(?::\d+)?|\d+(?:\.\d+)?))?",
            re.IGNORECASE,
        ),
    ),
    (
        "opt_rtl",
        re.compile(
            r"^\s*Finished\s+(?:Cross\s+Boundary\s+and\s+Area\s+Optimization|RTL\s+Optimization\b[^:]*)(?:.*?Time\s*\(s\):.*?\belapsed\s*=\s*(\d+:\d+(?::\d+)?|\d+(?:\.\d+)?))?",
            re.IGNORECASE,
        ),
    ),
    (
        "tech_map",
        re.compile(
            r"^\s*Finished\s+Technology\s+Mapping\b(?:.*?Time\s*\(s\):.*?\belapsed\s*=\s*(\d+:\d+(?::\d+)?|\d+(?:\.\d+)?))?",
            re.IGNORECASE,
        ),
    ),
    (
        "io_buf",
        re.compile(
            r"^\s*Finished\s+IO\s+Insertion\b(?:.*?Time\s*\(s\):.*?\belapsed\s*=\s*(\d+:\d+(?::\d+)?|\d+(?:\.\d+)?))?",
            re.IGNORECASE,
        ),
    ),
    (
        "netlist",
        re.compile(
            r"^\s*Finished\s+Writing\s+Synthesis\s+Report\b(?:.*?Time\s*\(s\):.*?\belapsed\s*=\s*(\d+:\d+(?::\d+)?|\d+(?:\.\d+)?))?",
            re.IGNORECASE,
        ),
    ),
)

_RE_GENERIC_CMD = re.compile(r"^\s*Command:\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_RE_GENERIC_CMD_TIME = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s+Time\s*\(s\):.*?\belapsed\s*=\s*(\d+:\d+(?::\d+)?|\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_ANY_ELAPSED = re.compile(
    r"\belapsed\s*=\s*(\d+:\d+(?::\d+)?|\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RE_PHASE_LINE = re.compile(r"^\s*(Phase\s+\d+(?:\.\d+)?\b[^|]*|Starting\s+[^:]+)", re.IGNORECASE)
_RE_ERROR_LINE = re.compile(r"^\s*ERROR:\s*")
_RE_NONSTANDARD_FATAL = re.compile(
    r"Abnormal program termination|EXCEPTION_ACCESS_VIOLATION|Segmentation fault|Out of memory",
    re.IGNORECASE,
)
_RE_RUNS_LOG_REF = re.compile(
    r'([^\s"\'<>|]+?\.runs[/\\][^/\s"\'\\<>|]+(?:[/\\]runme\.log)?)',
    re.IGNORECASE,
)


def _parse_elapsed_sec(raw: str | float | int | None) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return round(max(0.0, val), 2) if val >= 0 else None
    text = str(raw).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return round(float(text), 2)
    parts = text.split(":")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return float(int(parts[0]) * 60 + int(parts[1]))
    return None


def _read_log_session_text(log_path: Path, max_bytes: int) -> tuple[str, str] | None:
    """安全只读读取日志末段并截取最后一个会话，返回 ``(session_text, sample_sig)``。"""
    if (
        _is_link_or_junction(log_path)
        or _is_link_or_junction(log_path.parent)
        or not log_path.is_file()
    ):
        return None
    try:
        st = log_path.stat()
    except OSError:
        return None
    size = int(st.st_size)
    offset = max(0, size - max_bytes) if size > max_bytes else 0
    try:
        with log_path.open("rb") as handle:
            if offset > 0:
                handle.seek(offset)
            raw = handle.read(max_bytes)
    except OSError:
        return None
    text = _decode_utf8_or_fallback(
        raw, offset_nonzero=offset > 0, final=offset == 0
    ).lstrip("\ufeff")
    if offset > 0:
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1 :]
    idx = text.rfind(_SESSION_MARKER)
    session_text = text[idx:] if idx >= 0 else text
    sig = f"{os.path.normcase(os.path.abspath(log_path))}|{int(st.st_mtime_ns)}|{size}"
    return session_text, sig


def _clean_subphase(line: str) -> str:
    text = line.strip()
    if ":" in text and "Time (s)" in text:
        text = text.split(":", 1)[0].strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    return text[:80].strip()


def _infer_impl_step_from_phase(line_low: str) -> str:
    if "router" in line_low or "routing" in line_low:
        return "route_design"
    if "physopt" in line_low or "physical synthesis" in line_low:
        return "phys_opt_design"
    if "placer" in line_low or "placement" in line_low:
        return "place_design"
    if "power" in line_low and "opt" in line_low:
        return "power_opt_design"
    if "opt" in line_low or "sweep" in line_low or "constant propagation" in line_low:
        return "opt_design"
    if "drc" in line_low and "bitstream" in line_low:
        return "write_bitstream"
    return ""


def parse_runme_step_timings(
    log_path: Path,
    *,
    run_name: str = "",
    target_step: str = "",
    max_bytes: int = 4 * 1024 * 1024,
) -> dict[str, Any]:
    """只读解析 Vivado ``runme.log`` / ``vivado.log`` 的分阶段耗时与当前进度。"""
    empty: dict[str, Any] = {
        "flow": "",
        "active_step_id": "",
        "active_subphase": "",
        "completed_step_ids": [],
        "completed_steps_sec": {},
        "seen_steps": [],
        "total_sec": None,
        "latest_log_elapsed_sec": None,
        "is_completed": False,
        "has_error": False,
        "sample_sig": "",
        "batch_steps": [],
    }
    loaded = _read_log_session_text(Path(log_path), max_bytes=max_bytes)
    if loaded is None:
        return empty
    session_text, sample_sig = loaded

    run_low = (run_name or "").strip().lower()
    target_low = (target_step or "").strip().lower()

    saw_impl = False
    saw_synth = False
    has_error = False
    exited_vivado = False

    impl_seen: list[str] = []
    impl_done_order: list[str] = []
    impl_sec: dict[str, float] = {}
    impl_active = ""
    impl_saw_cmd = False

    synth_cum: dict[str, float] = {}
    synth_seen: list[str] = []
    synth_done_set: set[str] = set()
    synth_active = ""
    synth_total: float | None = None
    synth_completed_flag = False

    batch_order: list[str] = []
    batch_done_set: set[str] = set()
    batch_sec: dict[str, float] = {}
    batch_active = ""

    active_subphase = ""
    latest_elapsed: float | None = None

    def _add_unique(lst: list[str], item: str) -> None:
        if item and item not in lst:
            lst.append(item)

    for raw_line in session_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if _RE_ERROR_LINE.match(line) or _RE_NONSTANDARD_FATAL.search(line):
            has_error = True
        if "Exiting Vivado" in line:
            exited_vivado = True

        m_el = _RE_ANY_ELAPSED.search(line)
        if m_el:
            el_val = _parse_elapsed_sec(m_el.group(1))
            if el_val is not None:
                latest_elapsed = el_val

        # 1. Impl command / timing / completion
        m_icmd = _RE_IMPL_CMD.match(line)
        if m_icmd:
            step = m_icmd.group(1).lower()
            saw_impl = True
            impl_saw_cmd = True
            _add_unique(impl_seen, step)
            if impl_active != step:
                active_subphase = ""
            impl_active = step
            continue

        m_itime = _RE_IMPL_TIME.match(line)
        if m_itime:
            step = m_itime.group(1).lower()
            sec = _parse_elapsed_sec(m_itime.group(2))
            saw_impl = True
            _add_unique(impl_seen, step)
            _add_unique(impl_done_order, step)
            if sec is not None:
                impl_sec[step] = round(sec, 2)
            if impl_active == step:
                impl_active = ""
                active_subphase = ""
            continue

        m_idone = _RE_IMPL_DONE.search(line)
        if m_idone:
            step = m_idone.group(1).lower()
            saw_impl = True
            _add_unique(impl_seen, step)
            _add_unique(impl_done_order, step)
            if impl_active == step:
                impl_active = ""
                active_subphase = ""
            continue

        # 2. Synth command / sub-stages / completion
        if _RE_SYNTH_CMD.match(line) or line.lower().startswith("starting synth_design"):
            saw_synth = True
            if not synth_active:
                synth_active = "elaborate"
                _add_unique(synth_seen, "elaborate")
            continue

        m_stime = _RE_SYNTH_TIME.match(line)
        if m_stime:
            saw_synth = True
            sec = _parse_elapsed_sec(m_stime.group(1))
            if sec is not None:
                synth_total = round(sec, 2)
            synth_done_set.add("netlist")
            _add_unique(synth_seen, "netlist")
            if synth_active == "netlist":
                synth_active = ""
                active_subphase = ""
            continue

        if _RE_SYNTH_DONE.search(line) or "finished synth_design" in line.lower():
            saw_synth = True
            synth_completed_flag = True
            synth_done_set.update(_SYNTH_ORDER)
            for sid in _SYNTH_ORDER:
                _add_unique(synth_seen, sid)
            synth_active = ""
            active_subphase = ""
            continue

        matched_synth_stage = False
        for sid, pat in _SYNTH_START_PATTERNS:
            if pat.match(line):
                saw_synth = True
                _add_unique(synth_seen, sid)
                synth_done_set.discard(sid)
                synth_cum.pop(sid, None)
                idx = _SYNTH_INDEX[sid]
                for prev_sid in _SYNTH_ORDER[:idx]:
                    synth_done_set.add(prev_sid)
                    _add_unique(synth_seen, prev_sid)
                synth_active = sid
                active_subphase = _clean_subphase(line)
                matched_synth_stage = True
                break
        if matched_synth_stage:
            continue

        for sid, pat in _SYNTH_FINISH_PATTERNS:
            m_sfin = pat.match(line)
            if m_sfin:
                saw_synth = True
                _add_unique(synth_seen, sid)
                idx = _SYNTH_INDEX[sid]
                for prev_sid in _SYNTH_ORDER[: idx + 1]:
                    synth_done_set.add(prev_sid)
                    _add_unique(synth_seen, prev_sid)
                el_raw = m_sfin.group(1)
                if el_raw:
                    el_sec = _parse_elapsed_sec(el_raw)
                    if el_sec is not None:
                        synth_cum[sid] = max(synth_cum.get(sid, 0.0), el_sec)
                if synth_active == sid:
                    synth_active = ""
                    active_subphase = ""
                matched_synth_stage = True
                break
        if matched_synth_stage:
            continue

        # 3. Generic Command / Phase lines
        m_gcmd = _RE_GENERIC_CMD.match(line)
        if m_gcmd:
            cmd_name = m_gcmd.group(1)
            _add_unique(batch_order, cmd_name)
            batch_active = cmd_name
            active_subphase = ""
            # Non-impl command after impl step completed clears active subphase
            if impl_active and impl_active in impl_done_order:
                impl_active = ""
            continue

        m_gtime = _RE_GENERIC_CMD_TIME.match(line)
        if m_gtime:
            cmd_name = m_gtime.group(1)
            sec = _parse_elapsed_sec(m_gtime.group(2))
            if cmd_name in batch_order:
                batch_done_set.add(cmd_name)
                if sec is not None:
                    batch_sec[cmd_name] = round(sec, 2)
                if batch_active == cmd_name:
                    batch_active = ""
            continue

        m_phase = _RE_PHASE_LINE.match(line)
        if m_phase:
            cleaned = _clean_subphase(m_phase.group(1))
            if cleaned:
                active_subphase = cleaned
            if not impl_saw_cmd and not saw_synth:
                inferred_step = _infer_impl_step_from_phase(line.lower())
                if inferred_step:
                    saw_impl = True
                    _add_unique(impl_seen, inferred_step)
                    impl_active = inferred_step

    # Determine flow
    flow = ""
    if saw_impl or run_low.startswith("impl") or target_low in (_IMPL_STEP_SET - {"synth_design"}):
        flow = "impl"
    elif (
        saw_synth
        or run_low.startswith("synth")
        or run_low.startswith("ooc")
        or target_low == "synth_design"
    ):
        flow = "synth"
    elif batch_order:
        flow = "batch"

    if flow == "impl":
        # If phase fallback detected e.g. route_design without Command lines, mark prior steps done
        if not impl_saw_cmd and impl_active and not impl_done_order:
            canonical_ids = [
                spec[0] for spec in _IMPL_STEP_SPECS if spec[0] not in _IMPL_OPTIONAL_SET
            ]
            if impl_active in canonical_ids:
                idx = canonical_ids.index(impl_active)
                for prev in canonical_ids[:idx]:
                    _add_unique(impl_done_order, prev)

        done_set = set(impl_done_order)
        # Mark earlier core steps done if a later step has already started
        canon_ids = [s[0] for s in _IMPL_STEP_SPECS]
        furthest_idx = -1
        for sid in impl_seen:
            if sid in canon_ids:
                furthest_idx = max(furthest_idx, canon_ids.index(sid))
        if furthest_idx > 0:
            for sid in canon_ids[:furthest_idx]:
                if sid in _IMPL_CORE_REQUIRED and sid != impl_active:
                    _add_unique(impl_done_order, sid)
                    done_set.add(sid)

        if impl_active in done_set:
            impl_active = ""

        total_sec: float | None = None
        if impl_sec:
            total_sec = round(sum(impl_sec.values()), 2)

        target_reached = bool(target_low and target_low in done_set)
        is_completed = bool(
            (exited_vivado and (bool(done_set) or not has_error))
            or target_reached
            or ("write_bitstream" in done_set)
            or ("route_design" in done_set and target_low in {"", "route_design"})
        )
        return {
            "flow": "impl",
            "active_step_id": "" if is_completed and not impl_active else impl_active,
            "active_subphase": "" if is_completed and not impl_active else active_subphase,
            "completed_step_ids": impl_done_order,
            "completed_steps_sec": impl_sec,
            "seen_steps": impl_seen,
            "total_sec": total_sec,
            "latest_log_elapsed_sec": latest_elapsed,
            "exited_vivado": exited_vivado,
            "is_completed": is_completed,
            "has_error": has_error,
            "sample_sig": sample_sig,
            "batch_steps": [],
        }

    if flow == "synth":
        completed_steps_sec: dict[str, float] = {}
        prev_cum = 0.0
        for sid in _SYNTH_ORDER:
            cum_val = synth_cum.get(sid)
            if sid == "netlist" and synth_total is not None:
                cum_val = max(cum_val or 0.0, synth_total)
            if cum_val is not None:
                delta = max(0.5, cum_val - prev_cum)
                completed_steps_sec[sid] = round(delta, 2)
                prev_cum = max(prev_cum, cum_val)
                synth_done_set.add(sid)

        is_completed = bool(
            synth_completed_flag
            or (synth_total is not None and not has_error)
            or (exited_vivado and bool(synth_done_set) and not has_error)
        )
        if is_completed:
            synth_done_set.update(_SYNTH_ORDER)
            synth_active = ""

        completed_ids = [sid for sid in _SYNTH_ORDER if sid in synth_done_set]
        if synth_active in synth_done_set:
            synth_active = ""

        total_sec_synth: float | None = None
        if completed_steps_sec:
            total_sec_synth = round(sum(completed_steps_sec.values()), 2)
            if synth_total is not None and synth_total > total_sec_synth:
                total_sec_synth = round(synth_total, 2)
        elif synth_total is not None:
            total_sec_synth = round(synth_total, 2)

        return {
            "flow": "synth",
            "active_step_id": synth_active,
            "active_subphase": "" if is_completed else active_subphase,
            "completed_step_ids": completed_ids,
            "completed_steps_sec": completed_steps_sec,
            "seen_steps": synth_seen,
            "total_sec": total_sec_synth,
            "latest_log_elapsed_sec": latest_elapsed,
            "exited_vivado": exited_vivado,
            "is_completed": is_completed,
            "has_error": has_error,
            "sample_sig": sample_sig,
            "batch_steps": [],
        }

    if flow == "batch":
        completed_ids = [c for c in batch_order if c in batch_done_set]
        is_completed = bool(exited_vivado and not has_error and bool(batch_order))
        if is_completed:
            completed_ids = list(batch_order)
            batch_active = ""
        total_sec_batch = round(sum(batch_sec.values()), 2) if batch_sec else None
        return {
            "flow": "batch",
            "active_step_id": batch_active,
            "active_subphase": "" if is_completed else active_subphase,
            "completed_step_ids": completed_ids,
            "completed_steps_sec": batch_sec,
            "seen_steps": batch_order,
            "total_sec": total_sec_batch,
            "latest_log_elapsed_sec": latest_elapsed,
            "exited_vivado": exited_vivado,
            "is_completed": is_completed,
            "has_error": has_error,
            "sample_sig": sample_sig,
            "batch_steps": batch_order,
        }

    return empty


def _has_step_signals(parsed: dict[str, Any]) -> bool:
    return bool(
        parsed.get("flow") in {"impl", "synth"}
        and (
            parsed.get("seen_steps")
            or parsed.get("completed_step_ids")
            or parsed.get("completed_steps_sec")
            or parsed.get("active_step_id")
        )
    )


def find_best_log_for_target(
    *,
    log_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    run_dir: str | Path | None = None,
    xpr_path: str | Path | None = None,
    runs: list[dict[str, Any]] | None = None,
    finished_at: float | None = None,
    status: str = "",
) -> tuple[Path | None, Path | None, str]:
    """为 Batch/OOC/RunMonitor/离线检视条目安全定位最合适的日志文件与 run 目录。

    返回 ``(best_log_path, resolved_run_dir, inferred_run_name)``；只读且拦截符号链接/junction。
    """

    def _safe_file(p: Path | None) -> Path | None:
        if p is None or _is_link_or_junction(p) or not p.is_file():
            return None
        if _is_link_or_junction(p.parent):
            return None
        return p

    def _safe_dir(p: Path | None) -> Path | None:
        if p is None or _is_link_or_junction(p) or not p.is_dir():
            return None
        return p

    is_finished_job = (
        isinstance(finished_at, (int, float))
        and finished_at > 0
        and (status or "").strip().lower() != "running"
    )

    def _is_overwritten_after_finish(cand: Path) -> bool:
        if not is_finished_job:
            return False
        try:
            return cand.stat().st_mtime > float(finished_at) + 15.0
        except OSError:
            return False

    # 1. Explicit run_dir -> runme.log
    rdir = _safe_dir(Path(run_dir)) if run_dir else None
    if rdir is not None:
        runme = _safe_file(rdir / "runme.log")
        if runme is not None:
            return runme, rdir, rdir.name

    # 2. Explicit runs summary (from inspected .xpr / .runs)
    if runs:
        ranked: list[tuple[int, int, Path, str]] = []
        for idx, item in enumerate(runs):
            if not isinstance(item, dict):
                continue
            r_raw = item.get("dir")
            rid = str(item.get("run_id") or "")
            st = str(item.get("status") or "").lower()
            cand_dir = _safe_dir(Path(r_raw)) if r_raw else None
            if cand_dir is None:
                continue
            cand_log = _safe_file(cand_dir / "runme.log")
            if cand_log is None:
                continue
            st_score = 3 if st == "running" else (2 if st in {"completed", "failed"} else 1)
            rid_low = rid.lower()
            type_score = 2 if rid_low.startswith("impl") else (1 if rid_low == "synth_1" else 0)
            ranked.append((st_score, type_score, cand_log, rid))
        if ranked:
            ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
            _, _, best_log, best_rid = ranked[0]
            return best_log, best_log.parent, best_rid

    # 3. Direct log_path (e.g. OOC vivado.log or RunMonitor log_path)
    lpath = _safe_file(Path(log_path)) if log_path else None
    if lpath is not None:
        parsed_direct = parse_runme_step_timings(lpath)
        if _has_step_signals(parsed_direct):
            rname = lpath.parent.name if lpath.parent.parent.name.lower().endswith(".runs") else ""
            return lpath, rdir or lpath.parent, rname

        # Check if log_path references a .runs/<run>/runme.log
        loaded = _read_log_session_text(lpath, max_bytes=256 * 1024)
        if loaded is not None:
            text, _ = loaded
            for m in _RE_RUNS_LOG_REF.finditer(text):
                raw_ref = m.group(1).strip()
                ref_p = Path(raw_ref)
                if ref_p.name.lower() != "runme.log":
                    ref_p = ref_p / "runme.log"
                if not ref_p.is_absolute():
                    continue
                safe_ref = _safe_file(ref_p)
                if safe_ref is not None and not _is_overwritten_after_finish(safe_ref):
                    return safe_ref, safe_ref.parent, safe_ref.parent.name

    # 4. output_dir -> runme.log or nested .runs/<run>/runme.log
    odir = _safe_dir(Path(output_dir)) if output_dir else None
    if odir is not None:
        direct_runme = _safe_file(odir / "runme.log")
        if direct_runme is not None and not _is_overwritten_after_finish(direct_runme):
            return direct_runme, odir, odir.name
        # Check immediate impl_*/synth_* or *.runs/impl_*
        search_roots = [odir]
        with contextlib.suppress(OSError):
            for entry in sorted(odir.iterdir()):
                if (
                    entry.is_dir()
                    and not _is_link_or_junction(entry)
                    and entry.name.lower().endswith(".runs")
                ):
                    search_roots.append(entry)
        for root in search_roots:
            for sub_name in ("impl_1", "synth_1"):
                cand = _safe_file(root / sub_name / "runme.log")
                if cand is not None and not _is_overwritten_after_finish(cand):
                    return cand, cand.parent, sub_name

    # 5. Fallback to xpr_path -> .runs/impl_1/runme.log or synth_1
    if xpr_path:
        xp = Path(xpr_path)
        runs_root = _safe_dir(xp.parent / f"{xp.stem}.runs")
        if runs_root is not None:
            for sub_name in ("impl_1", "synth_1"):
                cand = _safe_file(runs_root / sub_name / "runme.log")
                if cand is not None and not _is_overwritten_after_finish(cand):
                    return cand, cand.parent, sub_name

    if lpath is not None:
        return lpath, rdir or lpath.parent, ""
    return None, rdir, ""


def _runs_project_key_from_path(path_str: str) -> str:
    """若路径位于 ``<Project>.runs/...`` 内，优先还原对应 ``xpr:`` 或 ``proj:`` 键。"""
    if not path_str:
        return ""
    p = Path(path_str)
    for anc in (p, *p.parents):
        if anc.name.lower().endswith(".runs") and len(anc.name) > 5:
            proj_stem = anc.name[: -len(".runs")]
            cand_xpr = anc.parent / f"{proj_stem}.xpr"
            if cand_xpr.is_file() and not _is_link_or_junction(cand_xpr):
                norm_xp = os.path.normcase(os.path.normpath(str(cand_xpr))).replace("\\", "/")
                return f"xpr:{norm_xp}"
            return f"proj:{proj_stem.lower()}"
    return ""


def make_history_key(
    *,
    xpr_path: str = "",
    project: str = "",
    top: str = "",
    run_name: str = "",
    kind: str = "",
    flow: str = "",
    part: str = "",
    run_dir: str = "",
    best_log: str = "",
    tcl_path: str = "",
) -> tuple[str, str, str]:
    """构造规范化历史查询键 ``(project_key, run_key, part_key)``；匿名脚本返回空键。"""
    xp = (xpr_path or "").strip()
    proj = (project or "").strip()
    tp = (top or "").strip()
    rdir = (run_dir or "").strip()
    blog = (best_log or "").strip()
    tpath = (tcl_path or "").strip()

    project_key = ""
    if xp:
        norm_xp = os.path.normcase(os.path.normpath(xp)).replace("\\", "/")
        project_key = f"xpr:{norm_xp}"
    if not project_key:
        project_key = _runs_project_key_from_path(blog) or _runs_project_key_from_path(rdir)
    if not project_key and proj:
        project_key = f"proj:{proj.lower()}"
    if not project_key and tp:
        project_key = f"top:{tp.lower()}"
    if not project_key and tpath:
        stem = Path(tpath).stem.strip().lower()
        if (
            stem
            and stem not in _GENERIC_TCL_STEMS
            and not stem.startswith(("tmp", "temp"))
        ):
            project_key = f"tcl:{stem}"

    run_clean = (run_name or "").strip().lower()
    kind_clean = (kind or "").strip().lower()
    if kind_clean in {"ooc_synth", "ooc_dir"}:
        kind_clean = "ooc_synth"
    flow_clean = (flow or "").strip().lower()
    run_key = run_clean or kind_clean or flow_clean or "default"
    part_key = (part or "").strip().lower()
    return project_key, run_key, part_key


def _check_run_dir_clean_completion(rdir: Path | None) -> tuple[bool, bool]:
    """检查 run 目录 `.vivado.*.rst` 状态，返回 ``(eligible, has_valid_end_rst)``。"""
    if rdir is None:
        return True, False
    if _is_link_or_junction(rdir) or not rdir.is_dir():
        return False, False
    err_f = rdir / ".vivado.error.rst"
    if err_f.is_file() and not _is_link_or_junction(err_f):
        return False, False
    begin_f = rdir / ".vivado.begin.rst"
    end_f = rdir / ".vivado.end.rst"
    has_begin = begin_f.is_file() and not _is_link_or_junction(begin_f)
    has_end = end_f.is_file() and not _is_link_or_junction(end_f)
    if has_begin and not has_end:
        return False, False
    if has_begin and has_end:
        try:
            if end_f.stat().st_mtime < begin_f.stat().st_mtime - 1.0:
                return False, False
        except OSError:
            return False, False
    return True, has_end


class BuildHistoryStore:
    """按工程/运行/器件存储最近 5 次成功构建的分阶段耗时中位数。"""

    def __init__(self, store_path: Path | None = None):
        self._store_path_override = Path(store_path) if store_path is not None else None
        self._lock = threading.RLock()
        self._loaded_path: Path | None = None
        self._loaded_sig: tuple[int, int] | None = None
        self._data: dict[str, dict[str, Any]] = {}
        self._bootstrapped_sigs: set[str] = set()

    @property
    def store_path(self) -> Path:
        if self._store_path_override is not None:
            return self._store_path_override
        return user_cache_root() / "history" / "step_durations.json"

    @staticmethod
    def _encode_key(key: tuple[str, str, str]) -> str:
        return json.dumps([key[0], key[1], key[2]], ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_key(raw: str) -> tuple[str, str, str] | None:
        try:
            parts = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if isinstance(parts, list) and len(parts) == 3:
            return str(parts[0]), str(parts[1]), str(parts[2])
        return None

    def _ensure_loaded(self) -> None:
        path = self.store_path
        disk_sig: tuple[int, int] | None = None
        if not _is_link_or_junction(path) and path.is_file():
            with contextlib.suppress(OSError):
                st = path.stat()
                disk_sig = (int(st.st_mtime_ns), int(st.st_size))

        if self._loaded_path == path:
            if disk_sig == self._loaded_sig:
                return
        else:
            self._bootstrapped_sigs.clear()

        self._data = {}
        if disk_sig is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    entries = raw.get("entries")
                    if isinstance(entries, dict):
                        for k, v in entries.items():
                            if (
                                isinstance(k, str)
                                and isinstance(v, dict)
                                and isinstance(v.get("samples"), list)
                            ):
                                self._data[k] = v
            except (OSError, ValueError, TypeError):
                self._data = {}
        self._loaded_path = path
        self._loaded_sig = disk_sig

    def _persist(self) -> None:
        path = self.store_path
        if _is_link_or_junction(path):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(self._data) > _MAX_STORE_KEYS:
            ordered = sorted(
                self._data.items(),
                key=lambda kv: float(kv[1].get("updated_at") or 0.0),
                reverse=True,
            )
            self._data = dict(ordered[:_MAX_STORE_KEYS])
        payload = json.dumps(
            {"version": 1, "entries": self._data},
            ensure_ascii=False,
            indent=2,
        )
        tmp = path.parent / f".step_durations.{uuid.uuid4().hex[:6]}.tmp"
        with contextlib.suppress(OSError):
            _atomic_write_json(tmp, path, payload)
            st = path.stat()
            self._loaded_path = path
            self._loaded_sig = (int(st.st_mtime_ns), int(st.st_size))

    def record_if_completed(
        self,
        key: tuple[str, str, str],
        parsed: dict[str, Any],
        sample_sig: str = "",
        *,
        run_dir: str | Path | None = None,
        target_step: str = "",
    ) -> bool:
        """若本次构建在日志/磁盘层面证实完整干净结束，按 sample_sig 去重写入滚动窗口。"""
        proj_k, run_k, _part_k = key
        if not proj_k or not run_k:
            return False
        if not parsed.get("is_completed") or parsed.get("has_error"):
            return False

        rdir = Path(run_dir) if run_dir else None
        eligible_dir, has_valid_end = _check_run_dir_clean_completion(rdir)
        if not eligible_dir:
            return False

        if "exited_vivado" in parsed:
            if not (has_valid_end or bool(parsed.get("exited_vivado"))):
                return False

        steps_sec = parsed.get("completed_steps_sec") or {}
        total_sec = parsed.get("total_sec")
        if len(steps_sec) < 1 or not isinstance(total_sec, (int, float)) or float(total_sec) < 1.0:
            return False

        flow = str(parsed.get("flow") or "")
        if flow == "impl":
            t_low = (target_step or "").strip().lower()
            if t_low in {"", "complete", "route_design"}:
                if "route_design" not in steps_sec and "write_bitstream" not in steps_sec:
                    return False
            elif t_low == "write_bitstream":
                if "write_bitstream" not in steps_sec:
                    return False
            elif t_low not in steps_sec:
                return False

        sig = sample_sig or str(parsed.get("sample_sig") or "")
        sample = {
            "sig": sig,
            "flow": flow,
            "total_sec": round(float(total_sec), 2),
            "steps_sec": {str(k): round(float(v), 2) for k, v in steps_sec.items()},
            "recorded_at": time.time(),
        }
        kstr = self._encode_key(key)
        with self._lock:
            self._ensure_loaded()
            bucket = self._data.get(kstr)
            if bucket is None:
                bucket = {"samples": [], "seen_sigs": [], "updated_at": 0.0}
                self._data[kstr] = bucket
            samples: list[dict[str, Any]] = list(bucket.get("samples") or [])
            seen_sigs: list[str] = list(bucket.get("seen_sigs") or [])
            existing_sigs = {str(s.get("sig") or "") for s in samples if s.get("sig")}
            if sig and (sig in seen_sigs or sig in existing_sigs):
                return False
            samples.append(sample)
            if len(samples) > _MAX_SAMPLES_PER_KEY:
                samples = samples[-_MAX_SAMPLES_PER_KEY:]
            if sig:
                seen_sigs.append(sig)
                if len(seen_sigs) > _MAX_SEEN_SIGS:
                    seen_sigs = seen_sigs[-_MAX_SEEN_SIGS:]
            bucket["samples"] = samples
            bucket["seen_sigs"] = seen_sigs
            bucket["updated_at"] = sample["recorded_at"]
            self._persist()
            return True

    def bootstrap_from_disk(
        self,
        key: tuple[str, str, str],
        run_dir: str | Path | None,
        log_path: str | Path | None = None,
        *,
        run_name: str = "",
        target_step: str = "",
    ) -> bool:
        """从磁盘已存在且干净完成的 runme.log / vivado.log 冷启动历史样本。"""
        rdir = Path(run_dir) if run_dir else None
        cand_log = Path(log_path) if log_path else None
        if rdir is not None:
            eligible_dir, _ = _check_run_dir_clean_completion(rdir)
            if not eligible_dir:
                return False
            if cand_log is None:
                cand_log = rdir / "runme.log"

        if (
            cand_log is None
            or _is_link_or_junction(cand_log)
            or _is_link_or_junction(cand_log.parent)
            or not cand_log.is_file()
        ):
            return False
        try:
            st = cand_log.stat()
        except OSError:
            return False
        norm_log = os.path.normcase(os.path.abspath(cand_log))
        sig = f"{norm_log}|{int(st.st_mtime_ns)}|{int(st.st_size)}"
        with self._lock:
            if sig in self._bootstrapped_sigs:
                return False
            self._bootstrapped_sigs.add(sig)
            if len(self._bootstrapped_sigs) > 512:
                self._bootstrapped_sigs.clear()

        parsed = parse_runme_step_timings(
            cand_log,
            run_name=run_name or (rdir.name if rdir else ""),
            target_step=target_step,
        )
        return self.record_if_completed(
            key,
            parsed,
            sig,
            run_dir=rdir,
            target_step=target_step,
        )

    def lookup(self, key: tuple[str, str, str]) -> dict[str, Any]:
        """查询历史中位数统计；精确键无样本时自动回退到忽略 part_key 的匹配。"""
        proj_k, run_k, _part_k = key
        empty_stats = {
            "has_history": False,
            "sample_count": 0,
            "step_medians": {},
            "hist_total_sec": None,
            "min_total": None,
            "max_total": None,
            "flow": "",
        }
        if not proj_k or not run_k:
            return empty_stats

        with self._lock:
            self._ensure_loaded()
            kstr = self._encode_key(key)
            bucket = self._data.get(kstr)
            samples = list(bucket.get("samples") or []) if bucket else []
            if not samples:
                # Fallback ignoring part_key
                best_bucket: dict[str, Any] | None = None
                for raw_k, cand_b in self._data.items():
                    decoded = self._decode_key(raw_k)
                    if decoded is None:
                        continue
                    if decoded[0] == proj_k and decoded[1] == run_k and cand_b.get("samples"):
                        if best_bucket is None or (
                            len(cand_b["samples"]),
                            float(cand_b.get("updated_at") or 0.0),
                        ) > (
                            len(best_bucket["samples"]),
                            float(best_bucket.get("updated_at") or 0.0),
                        ):
                            best_bucket = cand_b
                if best_bucket is not None:
                    samples = list(best_bucket.get("samples") or [])

        if not samples:
            return empty_stats

        step_values: dict[str, list[float]] = {}
        totals: list[float] = []
        flow = ""
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            tot = sample.get("total_sec")
            if isinstance(tot, (int, float)) and tot > 0:
                totals.append(float(tot))
            if sample.get("flow"):
                flow = str(sample["flow"])
            smap = sample.get("steps_sec") or {}
            if isinstance(smap, dict):
                for sid, sec in smap.items():
                    if isinstance(sec, (int, float)) and sec >= 0:
                        step_values.setdefault(str(sid), []).append(float(sec))

        if not totals or not step_values:
            return empty_stats

        step_medians = {
            sid: round(float(statistics.median(vals)), 1)
            for sid, vals in step_values.items()
            if vals
        }
        hist_total = round(float(statistics.median(totals)), 1)
        return {
            "has_history": True,
            "sample_count": len(samples),
            "step_medians": step_medians,
            "hist_total_sec": hist_total,
            "min_total": round(min(totals), 1),
            "max_total": round(max(totals), 1),
            "flow": flow,
        }


_DEFAULT_STORE = BuildHistoryStore()


def get_default_history_store() -> BuildHistoryStore:
    return _DEFAULT_STORE


def _normalize_weights(raw_weights: list[float]) -> list[float]:
    if not raw_weights:
        return []
    total = sum(raw_weights)
    if total <= 0:
        even = round(100.0 / len(raw_weights), 1)
        out = [even] * len(raw_weights)
        out[-1] = round(max(0.1, 100.0 - sum(out[:-1])), 1)
        return out
    scaled = [round(100.0 * w / total, 1) for w in raw_weights]
    diff = round(100.0 - sum(scaled), 1)
    if diff != 0.0 and scaled:
        max_idx = max(range(len(scaled)), key=lambda i: scaled[i])
        scaled[max_idx] = round(max(0.1, scaled[max_idx] + diff), 1)
    return scaled


def _normalize_status(status: str, parsed: dict[str, Any]) -> str:
    low = (status or "").strip().lower()
    if low in _RUNNING_STATUSES:
        return "running"
    if low in _FAILED_STATUSES:
        return "failed"
    if low in _COMPLETED_STATUSES:
        return "completed"
    if low == "ended":
        is_ok = bool(parsed.get("is_completed") and not parsed.get("has_error"))
        return "completed" if is_ok else "failed"
    if parsed.get("has_error"):
        return "failed"
    if parsed.get("is_completed"):
        return "completed"
    if parsed.get("flow"):
        return "running"
    return ""


def compute_build_eta(
    *,
    status: str = "",
    flow_hint: str = "",
    run_name: str = "",
    target_step: str = "",
    kind: str = "",
    project: str = "",
    top: str = "",
    part: str = "",
    xpr_path: str = "",
    run_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    log_path: str | Path | None = None,
    tcl_path: str | Path | None = None,
    elapsed_sec: float | str | None = None,
    finished_at: float | None = None,
    runs: list[dict[str, Any]] | None = None,
    store: BuildHistoryStore | None = None,
    record_history: bool = True,
) -> dict[str, Any] | None:
    """结合实时日志阶段与历史中位数基准计算诚实的构建进度与 ETA 区间。"""
    hist_store = store or _DEFAULT_STORE
    best_log, resolved_rdir, inferred_run_name = find_best_log_for_target(
        log_path=log_path,
        output_dir=output_dir,
        run_dir=run_dir,
        xpr_path=xpr_path,
        runs=runs,
        finished_at=finished_at,
        status=status,
    )
    eff_run_name = (run_name or inferred_run_name or "").strip()
    eff_target_step = (target_step or "").strip()

    if best_log is not None:
        parsed = parse_runme_step_timings(
            best_log,
            run_name=eff_run_name,
            target_step=eff_target_step,
        )
    else:
        parsed = {
            "flow": "",
            "active_step_id": "",
            "active_subphase": "",
            "completed_step_ids": [],
            "completed_steps_sec": {},
            "seen_steps": [],
            "total_sec": None,
            "latest_log_elapsed_sec": None,
            "exited_vivado": False,
            "is_completed": False,
            "has_error": False,
            "sample_sig": "",
            "batch_steps": [],
        }

    flow = parsed["flow"] or (flow_hint or "").strip().lower()
    if not flow:
        run_low = eff_run_name.lower()
        t_low = eff_target_step.lower()
        if kind == "ooc_synth" or run_low.startswith("synth") or t_low == "synth_design":
            flow = "synth"
        elif run_low.startswith("impl") or t_low in _IMPL_STEP_SET:
            flow = "impl"

    if flow not in {"impl", "synth", "batch"}:
        return None

    eff_status = _normalize_status(status, parsed)
    if not eff_status:
        return None
    if eff_status == "failed" and not parsed.get("has_error"):
        parsed["has_error"] = True

    key = make_history_key(
        xpr_path=xpr_path,
        project=project,
        top=top,
        run_name=eff_run_name,
        kind=kind,
        flow=flow,
        part=part,
        run_dir=str(resolved_rdir or run_dir or output_dir or ""),
        best_log=str(best_log) if best_log is not None else "",
        tcl_path=str(tcl_path or ""),
    )
    has_valid_key = bool(key[0] and key[1])

    if record_history and has_valid_key:
        if eff_status == "completed" and parsed.get("is_completed") and not parsed.get("has_error"):
            hist_store.record_if_completed(
                key,
                parsed,
                parsed.get("sample_sig") or "",
                run_dir=resolved_rdir or run_dir,
                target_step=eff_target_step,
            )

    hist = (
        hist_store.lookup(key)
        if has_valid_key
        else {
            "has_history": False,
            "sample_count": 0,
            "step_medians": {},
            "hist_total_sec": None,
            "min_total": None,
            "max_total": None,
            "flow": "",
        }
    )
    has_history = bool(hist["has_history"])
    sample_count = int(hist["sample_count"])
    hist_medians: dict[str, float] = dict(hist["step_medians"])
    hist_total_sec: float | None = hist["hist_total_sec"] if has_history else None

    completed_ids = set(parsed.get("completed_step_ids") or [])
    completed_sec: dict[str, float] = dict(parsed.get("completed_steps_sec") or {})
    seen_steps = set(parsed.get("seen_steps") or [])
    active_step_id = str(parsed.get("active_step_id") or "")
    active_subphase = str(parsed.get("active_subphase") or "")

    # Build canonical step descriptors for the chosen flow
    step_defs: list[tuple[str, str, str, float]] = []
    if flow == "impl":
        t_low = eff_target_step.lower()
        saw_bit = (
            "write_bitstream" in seen_steps
            or "write_bitstream" in completed_ids
            or active_step_id == "write_bitstream"
        )
        hist_has_bit = "write_bitstream" in hist_medians
        include_bit = (
            saw_bit
            or t_low == "write_bitstream"
            or (
                t_low != "route_design"
                and hist_has_bit
                and not (eff_status == "completed" and not saw_bit)
            )
            or (t_low == "complete" and not has_history and eff_status != "completed")
        )

        saw_phys = (
            "phys_opt_design" in seen_steps
            or "phys_opt_design" in completed_ids
            or active_step_id == "phys_opt_design"
        )
        past_place = (
            "route_design" in seen_steps
            or "route_design" in completed_ids
            or active_step_id == "route_design"
            or eff_status == "completed"
        )
        include_phys = (
            saw_phys
            or (not past_place and (not has_history or "phys_opt_design" in hist_medians))
            or (has_history and "phys_opt_design" in hist_medians and eff_status != "completed")
        )

        for sid, label, is_opt, def_w in _IMPL_STEP_SPECS:
            if sid == "write_bitstream":
                if not include_bit:
                    continue
            elif sid == "phys_opt_design":
                if not include_phys:
                    continue
            elif is_opt:
                if not (
                    sid in seen_steps
                    or sid in completed_ids
                    or active_step_id == sid
                    or (has_history and sid in hist_medians)
                ):
                    continue
            step_defs.append((sid, label, sid, def_w))
    elif flow == "synth":
        for sid, label, cmd, def_w in _SYNTH_STEP_SPECS:
            step_defs.append((sid, label, cmd, def_w))
    else:  # batch
        batch_names = list(parsed.get("batch_steps") or [])
        if not batch_names:
            return None
        even_w = round(100.0 / len(batch_names), 1)
        for cmd_name in batch_names:
            step_defs.append((cmd_name, cmd_name, cmd_name, even_w))

    if not step_defs:
        return None

    # Resolve elapsed_sec
    elapsed_val = _parse_elapsed_sec(elapsed_sec)
    if elapsed_val is None and resolved_rdir is not None and eff_status == "running":
        begin_f = resolved_rdir / ".vivado.begin.rst"
        if begin_f.is_file() and not _is_link_or_junction(begin_f):
            with contextlib.suppress(OSError):
                elapsed_val = round(max(0.0, time.time() - begin_f.stat().st_mtime), 1)
    if elapsed_val is None and eff_status == "completed" and parsed.get("total_sec") is not None:
        elapsed_val = round(float(parsed["total_sec"]), 1)
    if elapsed_val is None and parsed.get("latest_log_elapsed_sec") is not None:
        elapsed_val = round(float(parsed["latest_log_elapsed_sec"]), 1)
    if elapsed_val is None and completed_sec:
        elapsed_val = round(sum(completed_sec.values()), 1)

    # Determine step states
    step_ids_list = [s[0] for s in step_defs]
    if active_step_id and active_step_id not in step_ids_list:
        active_step_id = ""

    if eff_status == "completed":
        completed_ids.update(step_ids_list)
        active_step_id = ""
        active_subphase = ""
    elif eff_status in {"running", "failed"}:
        if not active_step_id:
            for sid in step_ids_list:
                if sid not in completed_ids:
                    active_step_id = sid
                    break

    # Compute historical weights & hist_total matching active step subset
    raw_weights: list[float] = []
    step_hist_secs: list[float | None] = []
    if has_history:
        # Use medians for included steps; fallback to scaled default weight when unseen
        matched_hist_sum = sum(
            hist_medians.get(sid, 0.0) for sid in step_ids_list if sid in hist_medians
        )
        matched_def_sum = sum(
            def_w for sid, _, _, def_w in step_defs if sid in hist_medians
        )
        scale = (
            (matched_hist_sum / matched_def_sum)
            if matched_def_sum > 0 and matched_hist_sum > 0
            else 1.0
        )
        for sid, _, _, def_w in step_defs:
            if sid in hist_medians:
                hsec = round(float(hist_medians[sid]), 1)
                step_hist_secs.append(hsec)
                raw_weights.append(max(0.5, hsec))
            else:
                step_hist_secs.append(None)
                raw_weights.append(max(0.5, def_w * scale))
        # Adjust hist_total_sec if subset excludes optional steps (e.g. route_design only)
        if hist_medians:
            subset_hist_sum = sum(
                hist_medians[sid] for sid in step_ids_list if sid in hist_medians
            )
            if subset_hist_sum > 0 and (
                len(hist_medians) > len([s for s in step_ids_list if s in hist_medians])
            ):
                hist_total_sec = round(subset_hist_sum, 1)
    else:
        for _, _, _, def_w in step_defs:
            step_hist_secs.append(None)
            raw_weights.append(def_w)

    weights = _normalize_weights(raw_weights)
    done_actual_sum = sum(
        completed_sec.get(sid, 0.0) for sid in step_ids_list if sid in completed_ids
    )

    steps_out: list[dict[str, Any]] = []
    active_step_label = ""
    active_elapsed_val: float | None = None
    active_hist_sec: float | None = None
    active_weight_pct = 0.0

    for idx, (sid, label, cmd, _) in enumerate(step_defs):
        hsec = step_hist_secs[idx]
        w_pct = weights[idx]
        if eff_status == "completed" or sid in completed_ids:
            state = "done"
            s_elapsed = round(float(completed_sec[sid]), 1) if sid in completed_sec else None
            step_pct: float | None = 100.0
        elif sid == active_step_id:
            active_step_label = label
            if eff_status == "failed":
                state = "failed"
                s_elapsed = (
                    round(max(0.0, elapsed_val - done_actual_sum), 1)
                    if elapsed_val is not None
                    else None
                )
                step_pct = None
            else:
                state = "active"
                if elapsed_val is not None:
                    active_elapsed_val = round(max(0.0, elapsed_val - done_actual_sum), 1)
                    s_elapsed = active_elapsed_val
                    if hsec is not None and hsec > 0:
                        ratio_pct = (active_elapsed_val / hsec) * 100.0
                        step_pct = round(min(95.0, max(0.0, ratio_pct)), 1)
                    else:
                        step_pct = None
                else:
                    s_elapsed = None
                    step_pct = None
                active_hist_sec = hsec
                active_weight_pct = w_pct
        else:
            state = "pending"
            s_elapsed = None
            step_pct = 0.0

        steps_out.append(
            {
                "id": sid,
                "label": label,
                "cmd": cmd,
                "state": state,
                "weight_pct": w_pct,
                "elapsed_sec": s_elapsed,
                "hist_sec": hsec,
                "step_pct": step_pct,
            }
        )

    completed_count = sum(1 for s in steps_out if s["state"] == "done")
    total_count = len(steps_out)

    # Progress & ETA math (strictly honest!)
    estimated_pct: float | None = None
    overrun = False
    overrun_sec = 0.0
    remaining_min_sec: float | None = None
    remaining_max_sec: float | None = None

    if eff_status == "completed":
        estimated_pct = 100.0
    elif eff_status == "running" and has_history and hist_total_sec and hist_total_sec > 0:
        done_weight = sum(s["weight_pct"] for s in steps_out if s["state"] == "done")
        active_contrib = 0.0
        if (
            active_step_id
            and active_hist_sec
            and active_hist_sec > 0
            and active_elapsed_val is not None
        ):
            step_ratio = min(0.95, active_elapsed_val / active_hist_sec)
            active_contrib = active_weight_pct * step_ratio
        estimated_pct = round(min(99.0, max(0.0, done_weight + active_contrib)), 1)

        no_pending = all(s["state"] != "pending" for s in steps_out)
        history_covers_flow = (
            not active_step_id or active_hist_sec is not None
        ) and all(
            s["hist_sec"] is not None
            for s in steps_out
            if s["state"] in {"active", "pending"}
        )
        active_overrun = bool(
            active_hist_sec
            and active_hist_sec > 0
            and active_elapsed_val is not None
            and active_elapsed_val > active_hist_sec * 1.15
            and no_pending
        )
        total_overrun = bool(
            history_covers_flow
            and elapsed_val is not None
            and elapsed_val > hist_total_sec
        )
        if total_overrun or active_overrun:
            overrun = True
            if total_overrun and elapsed_val is not None:
                overrun_sec = round(max(0.0, elapsed_val - hist_total_sec), 1)
            elif active_elapsed_val is not None and active_hist_sec is not None:
                overrun_sec = round(max(0.0, active_elapsed_val - active_hist_sec), 1)
        elif history_covers_flow:
            pending_rem = sum(
                (s["hist_sec"] or 0.0) for s in steps_out if s["state"] == "pending"
            )
            active_rem = (
                max(0.0, active_hist_sec - (active_elapsed_val or 0.0))
                if (active_hist_sec and active_hist_sec > 0)
                else 0.0
            )
            rem_mid = pending_rem + active_rem
            if (
                sample_count >= 2
                and hist["max_total"] is not None
                and hist["min_total"] is not None
            ):
                raw_spread = (hist["max_total"] - hist["min_total"]) / (2.0 * hist_total_sec)
                sample_spread_ratio = min(0.60, max(0.0, raw_spread))
            else:
                sample_spread_ratio = 0.25
            spread = max(0.20, sample_spread_ratio)
            remaining_min_sec = round(max(10.0, rem_mid * (1.0 - spread)), 1)
            remaining_max_sec = round(max(remaining_min_sec + 15.0, rem_mid * (1.0 + spread)), 1)

    if has_history:
        basis = "历史参考（1 次）" if sample_count == 1 else f"近 {sample_count} 次成功中位数"
    else:
        basis = "首次运行 · 按阶段追踪"

    return {
        "flow": flow,
        "has_history": has_history,
        "sample_count": sample_count,
        "basis": basis,
        "elapsed_sec": round(elapsed_val, 1) if elapsed_val is not None else None,
        "hist_total_sec": hist_total_sec,
        "estimated_pct": estimated_pct,
        "overrun": overrun,
        "overrun_sec": overrun_sec,
        "remaining_min_sec": remaining_min_sec,
        "remaining_max_sec": remaining_max_sec,
        "active_step_id": active_step_id,
        "active_step_label": active_step_label,
        "active_subphase": active_subphase,
        "completed_steps": completed_count,
        "total_steps": total_count,
        "steps": steps_out,
    }
