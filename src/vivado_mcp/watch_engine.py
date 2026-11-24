"""自适应工程规模的事件驱动 Watch 与心跳引擎。

纯只读观测：
- 不启动/停止/取消 Vivado 进程，超时也不杀死 Vivado；
- 自适应阈值基于真实磁盘报告/工程/历史耗时估算（显式标明自适应阈值，非 ETA）；
- 仅在日志静默接近阈值时采样进程 CPU，避免误报重型布线阶段为卡死；
- 统一输出 5 类事件与 CLI 退出码：
  0 (completed/reached) / 2 (failed/silent_exit/early_error) /
  3 (stalled) / 4 (timeout) / 5 (memory_critical)。
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import inspect
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from vivado_mcp.analysis.util_parser import parse_utilization
from vivado_mcp.analysis.warning_parser import (
    detect_silent_run_child_exit,
    is_route_power_silent_exit,
    scan_nonstandard_errors,
)
from vivado_mcp.analysis.xpr_parser import parse_xpr
from vivado_mcp.project_layout import user_cache_root
from vivado_mcp.shared_registry import is_pid_alive
from vivado_mcp.system_telemetry import get_host_memory

EXIT_CODES: dict[str, int] = {
    "completed": 0,
    "reached": 0,
    "failed": 2,
    "silent_exit": 2,
    "early_error": 2,
    "cancelled": 2,
    "stalled": 3,
    "timeout": 4,
    "memory_critical": 5,
}

WATCH_UNTIL_CHOICES: tuple[str, ...] = (
    "complete",
    "synth_design",
    "opt_design",
    "place_design",
    "phys_opt_design",
    "route_design",
    "write_bitstream",
)

_ALL_RUN_STEPS: tuple[str, ...] = (
    "synth_design",
    "opt_design",
    "power_opt_design",
    "place_design",
    "post_place_power_opt_design",
    "phys_opt_design",
    "route_design",
    "post_route_phys_opt_design",
    "write_bitstream",
)

PROGRESS_HEARTBEAT_SEC = 10.0
MEM_CRITICAL_PCT = 92.0
MEM_CRITICAL_AVAIL_GB = 4.0
MEM_CRITICAL_CONSECUTIVE = 2
CPU_ACTIVE_PCT = 5.0
CPU_IDLE_PCT = 1.0
STALL_ACTIVE_GRACE_MULTIPLIER = 1.5

_SMALL_PART_RE = re.compile(
    r"^(?:xc)?(?:7a(?:12|15|25|35|50)|7s\d+|7z0(?:07|10))",
    re.IGNORECASE,
)
_LARGE_PART_RE = re.compile(
    r"^(?:xc)?(?:zu(?:[5-9]|[1-6]\d)|ku|vu|7k325|7k410|7k480|7vx|7v|vp|vc|vm|ve|vh|vr)",
    re.IGNORECASE,
)

_FATAL_ERROR_LINE_RE = re.compile(r"^\s*ERROR:\s+\[")
_PHASE_LINE_RE = re.compile(
    r"^(?:Phase\s+\d+|Starting\s+|Finished\s+|Running\s+|Command:\s+)"
    r"|(?:\b(?:synth_design|opt_design|power_opt_design|place_design|phys_opt_design|"
    r"route_design|write_bitstream)\s+completed successfully\b)",
    re.IGNORECASE,
)
_STEP_COMPLETED_RE = re.compile(
    r"\b(synth_design|opt_design|power_opt_design|place_design|"
    r"post_place_power_opt_design|phys_opt_design|route_design|"
    r"post_route_phys_opt_design|write_bitstream)\s+completed successfully\b",
    re.IGNORECASE,
)
_CMD_ELAPSED_RE = re.compile(
    r"^(?:synth_design|opt_design|power_opt_design|place_design|"
    r"post_place_power_opt_design|phys_opt_design|route_design|"
    r"post_route_phys_opt_design|write_bitstream):\s+Time\s*\(s\):.*?"
    r"elapsed\s*=\s*(\d+:\d+:\d+|\d+(?:\.\d+)?)",
    re.IGNORECASE | re.MULTILINE,
)
_ANY_ELAPSED_RE = re.compile(
    r"elapsed\s*=\s*(\d+:\d+:\d+|\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RST_PID_RE = re.compile(r'\bPid\s*=\s*"(\d+)"', re.IGNORECASE)
_XPR_PART_RE = re.compile(
    r'<Option\s+Name="Part"\s+Val="([^"]+)"', re.IGNORECASE
)
_XPR_TOP_RE = re.compile(
    r'<Option\s+Name="TopModule"\s+Val="([^"]+)"', re.IGNORECASE
)
_XPR_FILE_RE = re.compile(r'<File\s+Path="([^"]+)"', re.IGNORECASE)

_DEFAULT_CLI_MAX_TIMEOUT = 3600
_MIN_LOG_SCAN_TAIL = 64 * 1024
_MAX_UTIL_SCAN_BYTES = 256 * 1024
_MAX_XPR_FULL_PARSE_BYTES = 2 * 1024 * 1024
_MAX_COMPACT_JSON_BYTES = 700
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


def _clamp(val: int, low: int, high: int) -> int:
    return max(low, min(int(val), high))


def _parse_hms_seconds(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return float(int(parts[0]) * 60 + int(parts[1]))
    return None


@dataclass(frozen=True)
class WatchProfile:
    """自适应工程规模看守阈值（显式标注依据，非完工 ETA）。"""

    tier: str
    stall_seconds: int
    timeout_seconds: int
    basis: str
    lut_estimate: int | None = None
    prev_elapsed_sec: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "stall_seconds": self.stall_seconds,
            "timeout_seconds": self.timeout_seconds,
            "basis": self.basis,
            "lut_estimate": self.lut_estimate,
            "prev_elapsed_sec": self.prev_elapsed_sec,
        }

    def to_compact_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "stall_seconds": self.stall_seconds,
            "timeout_seconds": self.timeout_seconds,
            "basis": self.basis,
        }


@dataclass
class WatchResult:
    """统一 5 事件 watch 结果。"""

    event: str
    exit_code: int
    target: str
    run_name: str
    until: str
    elapsed_sec: float
    phase: str = ""
    progress: float | None = None
    log_path: str = ""
    log_size: int = 0
    log_growth_bytes: int = 0
    cpu_pct: float | None = None
    ram_pct: float | None = None
    ram_avail_gb: float | None = None
    profile: WatchProfile = field(
        default_factory=lambda: WatchProfile("medium", 300, 900, "default")
    )
    summary: dict[str, Any] | None = None
    error_lines: list[str] = field(default_factory=list)
    hint: str = ""
    next_action: str = ""

    def to_compact_dict(self, max_bytes: int = _MAX_COMPACT_JSON_BYTES) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": self.event,
            "exit_code": self.exit_code,
            "target": self.target,
            "until": self.until,
            "elapsed_sec": round(self.elapsed_sec, 1),
            "phase": self.phase,
            "ram_pct": self.ram_pct,
            "ram_avail_gb": self.ram_avail_gb,
            "profile": {
                "tier": self.profile.tier,
                "stall_seconds": self.profile.stall_seconds,
                "timeout_seconds": self.profile.timeout_seconds,
            },
        }
        if self.progress is not None:
            payload["progress"] = self.progress
        if self.cpu_pct is not None:
            payload["cpu_pct"] = round(self.cpu_pct, 1)
        if self.summary is not None:
            payload["summary"] = dict(self.summary)
        if self.error_lines:
            payload["errors"] = [ln[:160] for ln in self.error_lines[:4]]
        if self.hint:
            payload["hint"] = self.hint[:200]
        if self.next_action:
            payload["next"] = self.next_action[:140]
        if self.event == "timeout":
            payload["log_growth_b"] = self.log_growth_bytes

        def _encoded_len(obj: dict[str, Any]) -> int:
            return len(
                json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )

        if _encoded_len(payload) < max_bytes:
            return payload

        for trim_key in ("hint", "next", "phase"):
            val = payload.get(trim_key)
            if isinstance(val, str) and len(val) > 64:
                payload[trim_key] = val[:61] + "..."
                if _encoded_len(payload) < max_bytes:
                    return payload
        if "errors" in payload and len(payload["errors"]) > 1:
            payload["errors"] = [payload["errors"][0][:120]]
            if _encoded_len(payload) < max_bytes:
                return payload
        if isinstance(payload.get("target"), str) and len(payload["target"]) > 48:
            payload["target"] = "..." + payload["target"][-45:]
            if _encoded_len(payload) < max_bytes:
                return payload
        for drop_key in ("hint", "next", "phase"):
            if drop_key in payload and _encoded_len(payload) >= max_bytes:
                payload.pop(drop_key, None)
        if isinstance(payload.get("summary"), dict) and _encoded_len(payload) >= max_bytes:
            payload["summary"] = {
                k: v for k, v in payload["summary"].items() if v is not None
            }
        return payload

    def format_human(self) -> str:
        lines = [
            f"[{self.event.upper()} exit={self.exit_code}] {self.target} "
            f"(until={self.until}, elapsed={self.elapsed_sec:.1f}s)",
            f"阈值方案: tier={self.profile.tier}, stall={self.profile.stall_seconds}s, "
            f"timeout={self.profile.timeout_seconds}s ({self.profile.basis})",
        ]
        if self.phase:
            lines.append(f"阶段: {self.phase}")
        if self.ram_pct is not None and self.ram_avail_gb is not None:
            lines.append(f"内存: {self.ram_pct:.1f}% (可用 {self.ram_avail_gb:.1f} GB)")
        if self.cpu_pct is not None:
            lines.append(f"Vivado CPU: {self.cpu_pct:.1f}%")
        if self.summary:
            s = format_signoff_line(self.summary)
            if s:
                lines.append(s)
        if self.error_lines:
            lines.append("捕获 ERROR:")
            lines.extend(f"  {ln}" for ln in self.error_lines[:6])
        if self.hint:
            lines.append(f"提示: {self.hint}")
        if self.next_action:
            lines.append(f"建议: {self.next_action}")
        return "\n".join(lines)


def _job_field(job: Any, key: str, default: Any = None) -> Any:
    if job is None:
        return default
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


def _infer_xpr_from_run_dir(run_dir: Path) -> Path | None:
    parent = run_dir.parent
    if parent.name.lower().endswith(".runs"):
        stem = parent.name[: -len(".runs")]
        cand = parent.parent / f"{stem}.xpr"
        if cand.is_file() and not _is_link_or_junction(cand):
            return cand
        with contextlib.suppress(OSError):
            xprs = [
                p
                for p in parent.parent.iterdir()
                if p.is_file()
                and not _is_link_or_junction(p)
                and p.suffix.lower() == ".xpr"
            ]
            if len(xprs) == 1:
                return xprs[0]
    return None


def _fast_parse_xpr_scale(
    xpr_path: Path, run_name: str
) -> tuple[str, str, int, int]:
    """快速从 .xpr 提取 (part, top, file_count, ip_count)。"""
    try:
        if not xpr_path.is_file() or _is_link_or_junction(xpr_path):
            return "", "", 0, 0
        size = xpr_path.stat().st_size
    except OSError:
        return "", "", 0, 0

    if size <= _MAX_XPR_FULL_PARSE_BYTES:
        try:
            cfg = parse_xpr(str(xpr_path))
            part = cfg.part
            if not part and cfg.runs:
                for run in cfg.runs:
                    if run.run_id == run_name and run.part:
                        part = run.part
                        break
            return part, cfg.top, len(cfg.design_srcs), len(cfg.ips)
        except Exception:
            pass

    # 超大 .xpr 回退有界文本扫描，保持 <25ms
    try:
        with xpr_path.open("rb") as fh:
            head = fh.read(512 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return "", "", 0, 0
    m_part = _XPR_PART_RE.search(head)
    m_top = _XPR_TOP_RE.search(head)
    files = _XPR_FILE_RE.findall(head)
    ips = sum(1 for p in files if p.lower().endswith((".xci", ".bd")))
    return (
        m_part.group(1).strip() if m_part else "",
        m_top.group(1).strip() if m_top else "",
        len(files),
        ips,
    )


def _extract_util_from_dirs(
    dirs: list[Path],
) -> tuple[int | None, int | None, float | None]:
    """按优先级从候选目录读取 utilization .rpt 并返回 (lut, dsp, bram)。"""
    seen_dirs: set[str] = set()
    candidates: list[tuple[int, float, Path]] = []
    for base in dirs:
        if not base.is_dir() or _is_link_or_junction(base):
            continue
        key = os.path.normcase(str(base.resolve()))
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        try:
            with os.scandir(base) as it:
                for entry in it:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    low = entry.name.lower()
                    if not low.endswith(".rpt"):
                        continue
                    if any(skip in low for skip in ("timing", "power", "drc", "clock")):
                        continue
                    prio = 0
                    if "util" in low:
                        prio = 4 if ("route" in low or "impl" in low) else (
                            3 if "place" in low else (2 if "synth" in low else 1)
                        )
                    elif low.startswith("report_"):
                        prio = 0
                    else:
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                        if st.st_size == 0 or st.st_size > 4 * 1024 * 1024:
                            continue
                        candidates.append((prio, float(st.st_mtime), Path(entry.path)))
                    except OSError:
                        continue
        except OSError:
            continue

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _, _, rpt_path in candidates[:4]:
        try:
            with rpt_path.open("rb") as fh:
                raw = fh.read(_MAX_UTIL_SCAN_BYTES)
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            continue
        util = parse_utilization(text)
        if not util.resources:
            continue
        lut_res = util.get("Slice LUTs") or util.get("CLB LUTs")
        dsp_res = util.get("DSPs")
        bram_res = util.get("Block RAM Tile")
        if lut_res is not None or dsp_res is not None or bram_res is not None:
            return (
                int(lut_res.used) if lut_res is not None else None,
                int(dsp_res.used) if dsp_res is not None else None,
                float(bram_res.used) if bram_res is not None else None,
            )
    return None, None, None


def _read_tail_bytes(path: Path, max_bytes: int = _MIN_LOG_SCAN_TAIL) -> str:
    try:
        st = path.stat()
        size = st.st_size
        offset = max(0, size - max_bytes)
        with path.open("rb") as fh:
            if offset > 0:
                fh.seek(offset)
            raw = fh.read(max_bytes)
        if offset > 0 and raw:
            skip = 0
            while skip < 3 and skip < len(raw) and (raw[skip] & 0xC0) == 0x80:
                skip += 1
            if skip:
                raw = raw[skip:]
        return raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    except OSError:
        return ""


def _slice_last_session(text: str) -> str:
    """若日志含多段 `# Start of session at:` 记录，仅截取最后一段会话。"""
    marker = "# Start of session at:"
    idx = text.rfind(marker)
    return text[idx:] if idx >= 0 else text


def _parse_prior_runme_elapsed(run_dir: Path) -> float | None:
    """若 run_dir 存在先前已完成的 runme.log，提取其总耗时（秒）。"""
    if not run_dir.is_dir() or _is_link_or_junction(run_dir):
        return None
    begin_f = run_dir / ".vivado.begin.rst"
    end_f = run_dir / ".vivado.end.rst"
    err_f = run_dir / ".vivado.error.rst"
    log_f = run_dir / "runme.log"
    if err_f.is_file() and not _is_link_or_junction(err_f):
        return None

    has_begin = begin_f.is_file() and not _is_link_or_junction(begin_f)
    has_end = end_f.is_file() and not _is_link_or_junction(end_f)
    begin_mtime = 0.0
    end_mtime = 0.0
    if has_begin:
        with contextlib.suppress(OSError):
            begin_mtime = begin_f.stat().st_mtime
    if has_end:
        with contextlib.suppress(OSError):
            end_mtime = end_f.stat().st_mtime

    pid = _extract_rst_pid(begin_f) if has_begin else None
    pid_alive = is_pid_alive(pid) if pid is not None else False
    is_stale_end = False
    if has_begin and has_end:
        if begin_mtime > end_mtime and pid_alive:
            is_stale_end = True
        elif (begin_mtime - end_mtime) > 2.0:
            is_stale_end = True

    if has_begin and (not has_end or is_stale_end or (begin_mtime >= end_mtime and pid_alive)):
        return None

    raw_tail = _read_tail_bytes(log_f) if log_f.is_file() else ""
    tail = _slice_last_session(raw_tail) if raw_tail else ""
    is_completed_log = (has_end and not is_stale_end) or ("Exiting Vivado" in tail)
    if not is_completed_log:
        return None
    if tail and any(_FATAL_ERROR_LINE_RE.match(ln) for ln in tail.splitlines()):
        return None

    if tail:
        cmd_matches = _CMD_ELAPSED_RE.findall(tail)
        if cmd_matches:
            total = 0.0
            for m in cmd_matches:
                sec = _parse_hms_seconds(m)
                if sec is not None:
                    total += sec
            if total > 0:
                return round(total, 2)
        any_matches = _ANY_ELAPSED_RE.findall(tail)
        if any_matches:
            sec = _parse_hms_seconds(any_matches[-1])
            if sec is not None and sec > 0:
                return round(sec, 2)

    if has_begin and has_end and not is_stale_end:
        delta = end_mtime - begin_mtime
        if delta >= 1.0:
            return round(delta, 2)
    return None


def _find_prev_job_elapsed(
    jobs_dir: Path,
    *,
    top: str,
    project: str,
    exclude_job_id: str = "",
    kind: str = "",
) -> float | None:
    if not (top or project) or not jobs_dir.is_dir() or _is_link_or_junction(jobs_dir):
        return None
    candidates: list[tuple[float, Path]] = []
    try:
        with os.scandir(jobs_dir) as it:
            for entry in it:
                if (
                    entry.is_symlink()
                    or not entry.is_file(follow_symlinks=False)
                    or not entry.name.endswith(".json")
                    or entry.name == "inspected.json"
                ):
                    continue
                with contextlib.suppress(OSError):
                    st = entry.stat(follow_symlinks=False)
                    if st.st_size <= 512 * 1024:
                        candidates.append((float(st.st_mtime), Path(entry.path)))
    except OSError:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, path in candidates[:25]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "completed":
            continue
        if exclude_job_id and data.get("job_id") == exclude_job_id:
            continue
        if kind and data.get("kind") and data.get("kind") != kind:
            continue
        same_top = bool(top and data.get("top") == top)
        same_proj = bool(project and data.get("project") == project)
        if not (same_top or same_proj):
            continue
        elapsed = data.get("elapsed_sec")
        if isinstance(elapsed, (int, float)) and elapsed > 0:
            return round(float(elapsed), 2)
    return None


def estimate_watch_profile(
    *,
    run_dir: str | Path | None = None,
    xpr_path: str | Path | None = None,
    job: Any = None,
    part: str = "",
    run_name: str = "impl_1",
    until: str = "complete",
    file_count: int = 0,
    ip_count: int = 0,
    is_ooc: bool = False,
    jobs_dir: str | Path | None = None,
) -> WatchProfile:
    """快速（<25ms）基于磁盘信号估算自适应 Watch 阈值（非完工 ETA）。"""
    eff_part = (part or "").strip()
    eff_run_name = (run_name or "impl_1").strip() or "impl_1"
    eff_until = (until or "complete").strip().lower() or "complete"
    eff_ooc = bool(is_ooc)
    eff_files = max(0, int(file_count or 0))
    eff_ips = max(0, int(ip_count or 0))
    eff_top = ""
    eff_project = ""
    job_id = ""
    job_kind = ""

    rdir: Path | None = Path(run_dir) if run_dir else None
    xpath: Path | None = Path(xpr_path) if xpr_path else None

    if job is not None:
        job_id = str(_job_field(job, "job_id", "") or "")
        job_kind = str(_job_field(job, "kind", "") or "")
        if job_kind == "ooc_synth":
            eff_ooc = True
        if job_kind and run_name == "impl_1":
            eff_run_name = job_kind
        if not eff_part:
            eff_part = str(_job_field(job, "part", "") or "").strip()
        eff_top = str(_job_field(job, "top", "") or "").strip()
        eff_project = str(_job_field(job, "project", "") or "").strip()
        out_dir = _job_field(job, "output_dir", "")
        if rdir is None and out_dir:
            rdir = Path(str(out_dir))

    if rdir is not None:
        if rdir.parent.name.lower().endswith(".runs") and run_name == "impl_1":
            eff_run_name = rdir.name
        if xpath is None:
            xpath = _infer_xpr_from_run_dir(rdir)

    if xpath is not None:
        if not eff_project:
            eff_project = xpath.stem
        x_part, x_top, x_files, x_ips = _fast_parse_xpr_scale(xpath, eff_run_name)
        if not eff_part and x_part:
            eff_part = x_part
        if not eff_top and x_top:
            eff_top = x_top
        if eff_files <= 0 and x_files > 0:
            eff_files = x_files
        if eff_ips <= 0 and x_ips > 0:
            eff_ips = x_ips
        if rdir is None:
            cand_rdir = xpath.parent / f"{xpath.stem}.runs" / eff_run_name
            if cand_rdir.is_dir():
                rdir = cand_rdir

    # Signal A: 历史利用率 .rpt 与已完成耗时
    search_dirs: list[Path] = []
    if rdir is not None:
        search_dirs.append(rdir)
        if rdir.parent.name.lower().endswith(".runs"):
            for sib in ("impl_1", "synth_1"):
                search_dirs.append(rdir.parent / sib)
    elif xpath is not None:
        runs_root = xpath.parent / f"{xpath.stem}.runs"
        search_dirs.extend([runs_root / eff_run_name, runs_root / "impl_1", runs_root / "synth_1"])

    lut_count, dsp_count, bram_count = _extract_util_from_dirs(search_dirs)
    if lut_count is None and job is not None:
        util_dict = _job_field(job, "utilization", None)
        if isinstance(util_dict, dict):
            lut_info = util_dict.get("lut")
            dsp_info = util_dict.get("dsp")
            bram_info = util_dict.get("bram")
            if isinstance(lut_info, dict) and lut_info.get("used") is not None:
                lut_count = int(lut_info["used"])
            if isinstance(dsp_info, dict) and dsp_info.get("used") is not None:
                dsp_count = int(dsp_info["used"])
            if isinstance(bram_info, dict) and bram_info.get("used") is not None:
                bram_count = float(bram_info["used"])

    prev_elapsed_sec: float | None = None
    if rdir is not None:
        prev_elapsed_sec = _parse_prior_runme_elapsed(rdir)
    if prev_elapsed_sec is None and job is not None:
        jdir = Path(jobs_dir) if jobs_dir is not None else (user_cache_root() / "jobs")
        prev_elapsed_sec = _find_prev_job_elapsed(
            jdir,
            top=eff_top,
            project=eff_project,
            exclude_job_id=job_id,
            kind=job_kind,
        )

    # Signal B: 分档判定
    is_small_part = bool(eff_part and _SMALL_PART_RE.match(eff_part))
    is_large_part = bool(eff_part and _LARGE_PART_RE.match(eff_part))

    large_by_metrics = (
        (lut_count is not None and lut_count >= 60000)
        or (dsp_count is not None and dsp_count >= 200)
        or (bram_count is not None and bram_count >= 120)
        or (prev_elapsed_sec is not None and prev_elapsed_sec >= 600)
    )
    large_by_design = is_large_part or eff_files >= 60 or eff_ips >= 5

    if eff_ooc and not large_by_metrics:
        tier = "small"
    elif large_by_metrics or large_by_design:
        tier = "large"
    elif (lut_count is not None and lut_count < 12000) or (
        lut_count is None and eff_files <= 15 and eff_ips <= 1 and is_small_part
    ):
        tier = "small"
    else:
        tier = "medium"

    if tier == "small":
        stall_sec, timeout_sec = 120, 300
    elif tier == "large":
        stall_sec, timeout_sec = 600, 1800
    else:
        stall_sec, timeout_sec = 300, 900

    base_stall = stall_sec
    base_timeout = timeout_sec
    if prev_elapsed_sec is not None and prev_elapsed_sec > 0:
        refined_timeout = _clamp(int(prev_elapsed_sec * 2.5), 180, 1800)
        if prev_elapsed_sec < 30:
            timeout_sec = max(base_timeout, refined_timeout)
        else:
            timeout_sec = refined_timeout
        stall_sec = _clamp(max(base_stall, int(prev_elapsed_sec * 0.45)), 120, 720)

    # Signal C: 步骤感知乘数
    run_low = eff_run_name.lower()
    if eff_until in {"write_bitstream", "opt_design"} or "bitstream" in run_low:
        stall_sec = max(120, int(stall_sec * 0.6))
    elif eff_until == "synth_design" or run_low.startswith("synth"):
        stall_sec = max(120, int(stall_sec * 0.75))

    basis_parts = [f"自适应阈值(非ETA): tier={tier}"]
    if eff_ooc:
        basis_parts.append("ooc=True")
    if eff_part:
        basis_parts.append(f"part={eff_part}")
    if lut_count is not None:
        basis_parts.append(f"lut={lut_count}")
    if eff_files or eff_ips:
        basis_parts.append(f"files={eff_files},ips={eff_ips}")
    if prev_elapsed_sec is not None:
        basis_parts.append(f"prev={prev_elapsed_sec:.0f}s")
    step_label = eff_until if eff_until != "complete" else eff_run_name
    basis_parts.append(f"step={step_label}")

    return WatchProfile(
        tier=tier,
        stall_seconds=stall_sec,
        timeout_seconds=timeout_sec,
        basis=" | ".join(basis_parts),
        lut_estimate=lut_count,
        prev_elapsed_sec=prev_elapsed_sec,
    )


def sample_process_cpu_active(pid: int, interval_sec: float = 0.35) -> float | None:
    """采样进程 PID 在短窗口内的 CPU 占用率（% of 1 core），不可用返回 None。"""
    if type(pid) is not int or pid <= 0:
        return None
    wait_sec = max(0.05, min(float(interval_sec), 2.0))
    if sys.platform == "win32":
        from ctypes import wintypes

        from vivado_mcp.system_telemetry import (
            _PROCESS_QUERY_LIMITED_INFORMATION,
            _win32_apis,
        )

        k32, _ = _win32_apis()
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            ct0, et0, kt0, ut0 = (
                wintypes.FILETIME(),
                wintypes.FILETIME(),
                wintypes.FILETIME(),
                wintypes.FILETIME(),
            )
            if not k32.GetProcessTimes(
                handle,
                ctypes.byref(ct0),
                ctypes.byref(et0),
                ctypes.byref(kt0),
                ctypes.byref(ut0),
            ):
                return None
            if et0.dwLowDateTime != 0 or et0.dwHighDateTime != 0:
                return None
            t0 = time.monotonic()
            time.sleep(wait_sec)
            ct1, et1, kt1, ut1 = (
                wintypes.FILETIME(),
                wintypes.FILETIME(),
                wintypes.FILETIME(),
                wintypes.FILETIME(),
            )
            if not k32.GetProcessTimes(
                handle,
                ctypes.byref(ct1),
                ctypes.byref(et1),
                ctypes.byref(kt1),
                ctypes.byref(ut1),
            ):
                return None
            t1 = time.monotonic()
            if et1.dwLowDateTime != 0 or et1.dwHighDateTime != 0:
                return None
            dt_wall = t1 - t0
            if dt_wall <= 0:
                return None
            cpu0 = ((kt0.dwHighDateTime << 32) | kt0.dwLowDateTime) + (
                (ut0.dwHighDateTime << 32) | ut0.dwLowDateTime
            )
            cpu1 = ((kt1.dwHighDateTime << 32) | kt1.dwLowDateTime) + (
                (ut1.dwHighDateTime << 32) | ut1.dwLowDateTime
            )
            dt_cpu_sec = max(0, cpu1 - cpu0) / 10_000_000.0
            return round((dt_cpu_sec / dt_wall) * 100.0, 2)
        except Exception:
            return None
        finally:
            k32.CloseHandle(handle)
    return None


def _sample_watched_cpu_pct(pid: int | None) -> float | None:
    """优先采样目标 PID；若其为 cmd.exe 包装器则回退检测其存活子 Vivado 进程。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    cpu = sample_process_cpu_active(pid)
    if cpu is None or cpu >= CPU_ACTIVE_PCT or sys.platform != "win32":
        return cpu
    try:
        from vivado_mcp.system_telemetry import _raw_eda_process_list

        best_cpu = cpu
        parent_map, raw_procs = _raw_eda_process_list()
        for proc in raw_procs:
            cpid = int(proc.get("pid") or 0)
            if cpid <= 0 or cpid == pid:
                continue
            cur = cpid
            for _ in range(6):
                ppid = parent_map.get(cur)
                if ppid == pid:
                    child_cpu = sample_process_cpu_active(cpid)
                    if child_cpu is not None and child_cpu > best_cpu:
                        best_cpu = child_cpu
                    if best_cpu >= CPU_ACTIVE_PCT:
                        return best_cpu
                    break
                if not ppid or ppid <= 0:
                    break
                cur = ppid
    except Exception:
        return cpu
    return best_cpu


def _extract_rst_pid(begin_rst: Path) -> int | None:
    if not begin_rst.is_file() or _is_link_or_junction(begin_rst):
        return None
    try:
        text = begin_rst.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    match = _RST_PID_RE.search(text)
    if not match:
        return None
    val = int(match.group(1))
    return val if val > 0 else None


def _lookup_shared_registry_pid(
    run_dir: Path | None, xpr_path: Path | None, run_name: str
) -> int | None:
    sdir = user_cache_root() / "registry" / "sessions"
    if not sdir.is_dir() or _is_link_or_junction(sdir):
        return None
    target_rdir = os.path.normcase(str(run_dir.resolve())) if run_dir and run_dir.exists() else ""
    target_xpr = os.path.normcase(str(xpr_path.resolve())) if xpr_path and xpr_path.exists() else ""
    try:
        files = sorted(sdir.glob("*.json"))
    except OSError:
        return None
    for path in files:
        if _is_link_or_junction(path):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("disconnected"):
            continue
        vpid = data.get("vivado_pid")
        if not isinstance(vpid, int) or vpid <= 0:
            continue
        r_str = str(data.get("run_dir") or "").strip()
        if target_rdir and r_str:
            with contextlib.suppress(OSError):
                if os.path.normcase(str(Path(r_str).resolve())) == target_rdir:
                    return vpid
        x_str = str(data.get("xpr_path") or "").strip()
        if target_xpr and x_str and str(data.get("run_name") or "") == run_name:
            with contextlib.suppress(OSError):
                if os.path.normcase(str(Path(x_str).resolve())) == target_xpr:
                    return vpid
    return None


def extract_compact_signoff(
    target_dir: Path,
    *,
    log_path: Path | None = None,
    run_name: str = "",
) -> dict[str, Any]:
    """复用 parse_directory_reports 提取紧凑签收指标（绝不伪造缺失项为 0）。"""
    from vivado_mcp.batch_jobs import parse_directory_reports

    parsed = parse_directory_reports(
        target_dir,
        log_path=log_path,
        is_failed=False,
        scan_nonstandard=False,
        max_issues=5,
        log_tail_lines=5,
    )
    timing = parsed.get("timing") or {}
    util = parsed.get("utilization") or {}
    diag = parsed.get("diagnostics") or {}
    has_log = bool(parsed.get("has_log_evidence"))
    synth_8_3848 = parsed.get("synth_8_3848")

    eff_run = (run_name or "").strip()
    if eff_run != "synth_1" and target_dir.parent.name.lower().endswith(".runs"):
        synth_log = target_dir.parent / "synth_1" / "runme.log"
        if synth_log.is_file() and not _is_link_or_junction(synth_log):
            try:
                from vivado_mcp.batch_jobs import _read_text_bounded

                synth_text, _ = _read_text_bounded(synth_log, 2 * 1024 * 1024)
                extra_3848 = sum(1 for ln in synth_text.splitlines() if "[Synth 8-3848]" in ln)
                synth_8_3848 = (synth_8_3848 or 0) + extra_3848
            except OSError:
                pass

    def _used(key: str) -> int | float | None:
        item = util.get(key)
        return item.get("used") if isinstance(item, dict) else None

    return {
        "wns": timing.get("wns"),
        "tns": timing.get("tns"),
        "whs": timing.get("whs"),
        "wpws": timing.get("wpws"),
        "lut": _used("lut"),
        "ff": _used("ff"),
        "bram": _used("bram"),
        "uram": _used("uram"),
        "dsp": _used("dsp"),
        "errors": diag.get("errors") if has_log else None,
        "cw": diag.get("critical_warnings") if has_log else None,
        "synth_8_3848": synth_8_3848,
    }


def format_signoff_line(summary: dict[str, Any] | None) -> str:
    """将签收指标格式化为单行紧凑摘要；全缺失时返回空串。"""
    if not summary:
        return ""
    parts: list[str] = []
    if summary.get("wns") is not None:
        parts.append(f"WNS={summary['wns']:+.3f}ns")
    if summary.get("tns") is not None:
        parts.append(f"TNS={summary['tns']:.3f}ns")
    if summary.get("whs") is not None:
        parts.append(f"WHS={summary['whs']:+.3f}ns")
    if summary.get("wpws") is not None:
        parts.append(f"WPWS={summary['wpws']:+.3f}ns")
    for label, key in (
        ("LUT", "lut"),
        ("FF", "ff"),
        ("BRAM", "bram"),
        ("URAM", "uram"),
        ("DSP", "dsp"),
    ):
        val = summary.get(key)
        if val is not None:
            parts.append(f"{label}={val}")
    if summary.get("cw") is not None:
        parts.append(f"CW={summary['cw']}")
    if summary.get("errors") is not None:
        parts.append(f"ERR={summary['errors']}")
    if summary.get("synth_8_3848") is not None and summary.get("synth_8_3848", 0) > 0:
        parts.append(f"Synth8-3848={summary['synth_8_3848']}")
    if not parts:
        return ""
    return "签收摘要: " + " | ".join(parts)


def format_heartbeat_message(
    *,
    status: str = "",
    phase: str = "",
    elapsed_sec: float = 0.0,
    ram_pct: float | None = None,
    cpu_active_grace: bool = False,
) -> str:
    """构造 ~10s MCP 进度心跳消息（含实时阶段、耗时、主机内存及 CPU 活跃标注）。"""
    bits: list[str] = []
    if status:
        bits.append(status)
    if phase:
        bits.append(phase[:80])
    elif not status:
        bits.append("运行中")
    bits.append(f"{int(max(0.0, elapsed_sec))}s")
    if ram_pct is not None:
        bits.append(f"RAM {ram_pct:.0f}%")
    if cpu_active_grace:
        bits.append("日志静默但 Vivado CPU 活跃")
    return " · ".join(bits)


async def report_mcp_progress(
    ctx: Any,
    *,
    progress: float,
    total: float | None = None,
    message: str | None = None,
) -> None:
    """兼容不同 MCP SDK / Mock 签名的进度上报封装。"""
    if ctx is None or not hasattr(ctx, "report_progress"):
        return
    fn = ctx.report_progress
    kwargs: dict[str, Any] = {"progress": progress, "total": total}
    if message is not None:
        try:
            sig = inspect.signature(fn)
            params = sig.parameters
            accepts_msg = "message" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            if accepts_msg:
                kwargs["message"] = message
        except (TypeError, ValueError):
            pass
    with contextlib.suppress(Exception):
        await fn(**kwargs)


class LogWatchState:
    """共享增量日志、卡死+CPU闸门、早错及内存危急追踪器。"""

    def __init__(
        self,
        *,
        started_monotonic: float,
        stall_seconds: int,
        fail_fast: bool = True,
        mem_guard: bool = True,
    ) -> None:
        self.started_at = started_monotonic
        self.stall_seconds = max(0, int(stall_seconds))
        self.fail_fast = bool(fail_fast)
        self.mem_guard = bool(mem_guard)

        self.initial_log_size: int | None = None
        self.log_size: int = 0
        self.scan_offset: int = 0
        self._last_ino: int = 0
        self._session_seeded: bool = False
        self.latest_phase: str = ""
        self.last_activity_at: float = started_monotonic
        self.last_cpu_check_at: float = 0.0
        self.last_cpu_pct: float | None = None
        self.cpu_active_grace: bool = False

        self.fatal_errors: list[str] = []
        self.first_error_at: float | None = None

        self.mem_critical_streak: int = 0
        self.ram_pct: float | None = None
        self.ram_avail_gb: float | None = None

    @staticmethod
    def _find_last_session_offset(log_path: Path, size: int) -> int:
        marker = b"# Start of session at:"
        window = min(size, 1024 * 1024)
        base = size - window
        try:
            with log_path.open("rb") as fh:
                if base > 0:
                    fh.seek(base)
                data = fh.read(window)
            pos = data.rfind(marker)
            return base + pos if pos >= 0 else 0
        except OSError:
            return 0

    def scan_log(
        self,
        log_path: Path | None,
        now: float,
        *,
        begin_mtime: float | None = None,
    ) -> tuple[bool, list[str]]:
        """增量扫描日志文件；返回 ``(changed, new_fatal_errors)``。"""
        if log_path is None or not log_path.is_file() or _is_link_or_junction(log_path):
            return False, []
        try:
            st = log_path.stat()
            size = st.st_size
        except OSError:
            return False, []

        if begin_mtime is not None and begin_mtime > 0 and (st.st_mtime + 0.5) < begin_mtime:
            return False, []

        ino = int(getattr(st, "st_ino", 0) or 0)
        replaced = self._last_ino != 0 and ino != 0 and ino != self._last_ino
        if replaced or size < self.scan_offset:
            # 日志被截断重写或替换
            self.scan_offset = 0
            self.initial_log_size = 0
            self.latest_phase = ""
            self._session_seeded = False
            self.fatal_errors.clear()
            self.first_error_at = None

        self._last_ino = ino
        if not self._session_seeded:
            if size > 0:
                seed_off = self._find_last_session_offset(log_path, size)
                if seed_off > 0:
                    self.scan_offset = seed_off
                    if self.initial_log_size is None:
                        self.initial_log_size = seed_off
            self._session_seeded = True

        if self.initial_log_size is None:
            self.initial_log_size = size

        changed = size != self.log_size
        self.log_size = size
        new_errors: list[str] = []

        if size > self.scan_offset:
            to_read = min(size - self.scan_offset, 1024 * 1024)
            if size - self.scan_offset > to_read:
                self.scan_offset = size - to_read
            try:
                with log_path.open("rb") as fh:
                    fh.seek(self.scan_offset)
                    raw = fh.read(to_read)
                last_nl = raw.rfind(b"\n")
                if last_nl >= 0:
                    chunk = raw[: last_nl + 1]
                    self.scan_offset += last_nl + 1
                elif len(raw) > 0:
                    # 无换行尾行也先解析但不移动 offset，待换行落盘后确认
                    chunk = raw
                else:
                    chunk = b""
                if chunk:
                    text = chunk.decode("utf-8", errors="replace").lstrip("\ufeff")
                    for raw_line in text.splitlines():
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        if _PHASE_LINE_RE.search(stripped):
                            new_phase = stripped[:200]
                            if new_phase != self.latest_phase:
                                self.latest_phase = new_phase
                                changed = True
                        if _FATAL_ERROR_LINE_RE.match(raw_line):
                            if stripped not in self.fatal_errors:
                                self.fatal_errors.append(stripped)
                                new_errors.append(stripped)
                                if self.first_error_at is None:
                                    self.first_error_at = now
            except OSError:
                pass
        elif not self.latest_phase and size > 0:
            tail = _read_tail_bytes(log_path)
            for raw_line in tail.splitlines():
                stripped = raw_line.strip()
                if stripped and _PHASE_LINE_RE.search(stripped):
                    self.latest_phase = stripped[:200]

        if changed:
            self.last_activity_at = now
            self.cpu_active_grace = False
            self.last_cpu_pct = None
        return changed, new_errors

    def check_memory(self) -> tuple[bool, float | None, float | None]:
        """采样主机内存并判定是否连续 2 次满足内存危急条件。"""
        try:
            mem = get_host_memory()
        except Exception:
            return False, self.ram_pct, self.ram_avail_gb
        total = float(mem.get("ram_total_gb") or 0.0)
        if total <= 0.0:
            return False, self.ram_pct, self.ram_avail_gb
        pct = float(mem.get("ram_pct") or 0.0)
        avail = float(mem.get("ram_avail_gb") or 0.0)
        self.ram_pct = pct
        self.ram_avail_gb = avail
        if self.mem_guard and pct >= MEM_CRITICAL_PCT and avail < MEM_CRITICAL_AVAIL_GB:
            self.mem_critical_streak += 1
        else:
            self.mem_critical_streak = 0
        triggered = self.mem_guard and self.mem_critical_streak >= MEM_CRITICAL_CONSECUTIVE
        return triggered, pct, avail

    def evaluate_stall(self, now: float, pid: int | None) -> bool:
        """检查是否确认卡死；必要时调用 CPU 活性门控授予 1.5x 宽限。"""
        if self.stall_seconds <= 0:
            return False
        silent = now - self.last_activity_at
        if silent < self.stall_seconds:
            return False

        grace_limit = self.stall_seconds * STALL_ACTIVE_GRACE_MULTIPLIER
        if pid is None or pid <= 0:
            return True

        sample_cooldown = min(15.0, max(2.0, self.stall_seconds * 0.1))
        if (now - self.last_cpu_check_at) >= sample_cooldown or self.last_cpu_check_at == 0.0:
            self.last_cpu_pct = _sample_watched_cpu_pct(pid)
            self.last_cpu_check_at = now

        cpu = self.last_cpu_pct
        if cpu is not None and cpu >= CPU_ACTIVE_PCT:
            if silent < grace_limit:
                self.cpu_active_grace = True
                return False
            return True
        if cpu is not None and cpu < CPU_IDLE_PCT:
            return True
        # CPU 未知（POSIX fallback）或 1~5% 灰色区：宽限至 1.5x 后判定卡死
        return silent >= grace_limit

    @property
    def log_growth_bytes(self) -> int:
        if self.initial_log_size is None:
            return 0
        return max(0, self.log_size - self.initial_log_size)


def _is_rst_fresh(
    marker: Path,
    *,
    begin_mtime: float,
    pid_alive: bool,
) -> bool:
    if not marker.is_file() or _is_link_or_junction(marker):
        return False
    if begin_mtime <= 0:
        return True
    try:
        m_mtime = marker.stat().st_mtime
    except OSError:
        return False
    if pid_alive:
        return m_mtime >= begin_mtime
    return (begin_mtime - m_mtime) <= 0.1


def _check_step_reached(
    run_dir: Path,
    log_text: str,
    until: str,
    *,
    log_mtime: float = 0.0,
    pid_alive: bool = False,
) -> bool:
    if until == "complete":
        return False
    target_idx = _ALL_RUN_STEPS.index(until) if until in _ALL_RUN_STEPS else -1
    if target_idx < 0:
        return False
    begin_f = run_dir / ".vivado.begin.rst"
    begin_mtime = 0.0
    if begin_f.is_file() and not _is_link_or_junction(begin_f):
        with contextlib.suppress(OSError):
            begin_mtime = begin_f.stat().st_mtime

    for idx in range(target_idx, len(_ALL_RUN_STEPS)):
        step = _ALL_RUN_STEPS[idx]
        step_end = run_dir / f".{step}.end.rst"
        if _is_rst_fresh(step_end, begin_mtime=begin_mtime, pid_alive=pid_alive):
            return True
        if idx > target_idx:
            step_begin = run_dir / f".{step}.begin.rst"
            if _is_rst_fresh(step_begin, begin_mtime=begin_mtime, pid_alive=pid_alive):
                return True

    if begin_mtime > 0 and log_mtime > 0 and log_mtime < (begin_mtime - 0.1):
        return False
    scoped_log = _slice_last_session(log_text or "")
    for match in _STEP_COMPLETED_RE.finditer(scoped_log):
        step = match.group(1).lower()
        if step in _ALL_RUN_STEPS and _ALL_RUN_STEPS.index(step) >= target_idx:
            return True
    return False


def _build_silent_exit_hint(log_text: str) -> str:
    if is_route_power_silent_exit(log_text):
        return (
            "命中 Vivado route 后自动 report_power 静默退出特征（*_routed.dcp 未写出）；"
            "恢复: run_implementation(skip_route_power_report=True) 或从 physopt.dcp 续跑。"
        )
    diag = detect_silent_run_child_exit("ERROR", log_text)
    if diag:
        for ln in diag.splitlines():
            s = ln.strip()
            if s and not s.startswith("==="):
                return s[:200]
    return (
        "Vivado 进程未正常退出（缺失 .vivado.end.rst / Exiting Vivado），"
        "请检查 runme.log 与系统事件日志。"
    )


def _read_job_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or _is_link_or_junction(path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


async def _emit_progress(
    cb: Callable[[float | None, str], Any] | None,
    progress_pct: float | None,
    message: str,
) -> None:
    if cb is None:
        return
    try:
        res = cb(progress_pct, message)
        if inspect.isawaitable(res):
            await res
    except Exception:
        pass


async def watch_target_async(
    *,
    job_id: str | None = None,
    run_dir: str | Path | None = None,
    xpr_path: str | Path | None = None,
    run_name: str = "impl_1",
    until: str = "complete",
    stall_seconds: int | str | None = "auto",
    timeout_seconds: int | str | None = "auto",
    poll_seconds: int = 2,
    fail_fast: bool = True,
    mem_guard: bool = True,
    on_progress: Callable[[float | None, str], Any] | None = None,
    jobs_dir: str | Path | None = None,
    max_timeout_seconds: int = _DEFAULT_CLI_MAX_TIMEOUT,
    error_settle_seconds: float = 2.0,
) -> WatchResult:
    """只读事件驱动等待（支持 job_id / run_dir / xpr+run），返回 WatchResult。"""
    targets = [bool(job_id and str(job_id).strip()), bool(run_dir), bool(xpr_path)]
    if sum(targets) != 1:
        raise ValueError("必须且只能指定 --job、--run-dir 或 --xpr 之一")

    eff_until = (until or "complete").strip().lower()
    if eff_until not in WATCH_UNTIL_CHOICES:
        raise ValueError(f"无效的 until={until!r}，可选: {', '.join(WATCH_UNTIL_CHOICES)}")

    eff_poll = _clamp(int(poll_seconds), 1, 60)
    resolved_jobs_dir = Path(jobs_dir) if jobs_dir is not None else (user_cache_root() / "jobs")

    mode = "job" if targets[0] else "run_dir"
    target_label = ""
    eff_run_name = (run_name or "impl_1").strip() or "impl_1"
    resolved_rdir: Path | None = None
    resolved_xpr: Path | None = None
    job_meta_path: Path | None = None
    initial_job_data: dict[str, Any] | None = None

    if mode == "job":
        jid = str(job_id).strip()
        job_meta_path = resolved_jobs_dir / f"{jid}.json"
        initial_job_data = _read_job_json(job_meta_path)
        if initial_job_data is None:
            raise FileNotFoundError(f"找不到批处理任务: {jid} ({job_meta_path})")
        target_label = jid
        out_str = str(initial_job_data.get("output_dir") or "").strip()
        if out_str:
            resolved_rdir = Path(out_str)
    elif run_dir:
        rpath = Path(str(run_dir).strip()).expanduser()
        if not rpath.is_dir() or _is_link_or_junction(rpath):
            raise FileNotFoundError(f"run 目录不存在或不可访问: {rpath}")
        resolved_rdir = rpath.resolve()
        if resolved_rdir.parent.name.lower().endswith(".runs"):
            eff_run_name = resolved_rdir.name
        resolved_xpr = _infer_xpr_from_run_dir(resolved_rdir)
        target_label = str(resolved_rdir)
    else:
        xpath = Path(str(xpr_path).strip()).expanduser()
        if not xpath.is_file() or _is_link_or_junction(xpath):
            raise FileNotFoundError(f".xpr 文件不存在: {xpath}")
        resolved_xpr = xpath.resolve()
        cand_rdir = resolved_xpr.parent / f"{resolved_xpr.stem}.runs" / eff_run_name
        if not cand_rdir.is_dir() or _is_link_or_junction(cand_rdir):
            raise FileNotFoundError(f"run 目录不存在: {cand_rdir}")
        resolved_rdir = cand_rdir
        target_label = f"{resolved_xpr.name}:{eff_run_name}"

    profile = estimate_watch_profile(
        run_dir=resolved_rdir,
        xpr_path=resolved_xpr,
        job=initial_job_data,
        run_name=eff_run_name,
        until=eff_until,
        is_ooc=(
            bool(initial_job_data and initial_job_data.get("kind") == "ooc_synth")
        ),
        jobs_dir=resolved_jobs_dir,
    )

    if timeout_seconds is None or str(timeout_seconds).strip().lower() == "auto":
        eff_timeout = _clamp(profile.timeout_seconds, 1, max_timeout_seconds)
    else:
        eff_timeout = _clamp(int(timeout_seconds), 1, max_timeout_seconds)

    if stall_seconds is None or str(stall_seconds).strip().lower() == "auto":
        eff_stall = profile.stall_seconds
    else:
        eff_stall = int(stall_seconds)
        if eff_stall < 0:
            raise ValueError(f"stall_seconds 不能为负数: {eff_stall}")

    started = time.monotonic()
    deadline = started + eff_timeout
    tracker = LogWatchState(
        started_monotonic=started,
        stall_seconds=eff_stall,
        fail_fast=fail_fast,
        mem_guard=mem_guard,
    )
    last_heartbeat_at = started
    watched_pid: int | None = None
    dead_pid_seen_since: float | None = None

    while True:
        now = time.monotonic()
        elapsed = max(0.0, now - started)

        mem_crit, ram_pct, ram_avail = await asyncio.to_thread(tracker.check_memory)

        if mode == "job":
            assert job_meta_path is not None
            jdata = _read_job_json(job_meta_path) or initial_job_data or {}
            status = str(jdata.get("status") or "unknown").lower()
            raw_pid = jdata.get("pid")
            if isinstance(raw_pid, int) and raw_pid > 0:
                watched_pid = raw_pid
            log_str = str(jdata.get("log_path") or "").strip()
            log_file = Path(log_str) if log_str else (
                resolved_rdir / "vivado.log" if resolved_rdir else None
            )
            tracker.scan_log(log_file, now)
            phase = str(jdata.get("current_phase") or tracker.latest_phase or "")

            if status == "completed":
                out_dir_raw = str(jdata.get("output_dir") or "").strip()
                out_dir = Path(out_dir_raw) if out_dir_raw else resolved_rdir
                summary = (
                    await asyncio.to_thread(
                        extract_compact_signoff, out_dir, log_path=log_file, run_name=eff_run_name
                    )
                    if out_dir and out_dir.is_dir()
                    else None
                )
                return WatchResult(
                    event="completed",
                    exit_code=0,
                    target=target_label,
                    run_name=eff_run_name,
                    until=eff_until,
                    elapsed_sec=elapsed,
                    phase=phase,
                    progress=100.0,
                    log_path=str(log_file or ""),
                    log_size=tracker.log_size,
                    log_growth_bytes=tracker.log_growth_bytes,
                    ram_pct=ram_pct,
                    ram_avail_gb=ram_avail,
                    profile=profile,
                    summary=summary,
                )

            if status in {"failed", "cancelled"}:
                forensics = jdata.get("forensics") or {}
                tail_text = (
                    _read_tail_bytes(log_file)
                    if log_file and log_file.is_file()
                    else ""
                )
                is_silent = bool(
                    forensics.get("silent_exit_suspected")
                ) or is_route_power_silent_exit(tail_text)
                event_name = (
                    "silent_exit"
                    if is_silent
                    else ("cancelled" if status == "cancelled" else "failed")
                )
                err_lines = list(tracker.fatal_errors)
                if not err_lines and jdata.get("error_message"):
                    err_lines = [str(jdata["error_message"])]
                hint = (
                    _build_silent_exit_hint(tail_text)
                    if is_silent
                    else str(jdata.get("error_message") or "批处理任务失败，请查看日志")
                )
                return WatchResult(
                    event=event_name,
                    exit_code=2,
                    target=target_label,
                    run_name=eff_run_name,
                    until=eff_until,
                    elapsed_sec=elapsed,
                    phase=phase,
                    log_path=str(log_file or ""),
                    log_size=tracker.log_size,
                    log_growth_bytes=tracker.log_growth_bytes,
                    ram_pct=ram_pct,
                    ram_avail_gb=ram_avail,
                    profile=profile,
                    error_lines=err_lines,
                    hint=hint,
                )

            # status == "running"：检查是否进程已静默退出但 JSON 未落盘
            if watched_pid is not None and not is_pid_alive(watched_pid):
                if dead_pid_seen_since is None:
                    dead_pid_seen_since = now
                    await asyncio.sleep(0.5)
                    continue
                owner_pid = jdata.get("owner_pid")
                owner_alive = (
                    isinstance(owner_pid, int)
                    and owner_pid > 0
                    and is_pid_alive(owner_pid)
                )
                tail_text = (
                    _read_tail_bytes(log_file)
                    if log_file and log_file.is_file()
                    else ""
                )
                has_exiting = "Exiting Vivado" in tail_text
                dead_age = now - dead_pid_seen_since
                should_emit_silent = (
                    dead_age >= 1.5
                    if not owner_alive
                    else (not has_exiting and dead_age >= 15.0)
                )
                if should_emit_silent:
                    return WatchResult(
                        event="silent_exit",
                        exit_code=2,
                        target=target_label,
                        run_name=eff_run_name,
                        until=eff_until,
                        elapsed_sec=elapsed,
                        phase=phase,
                        log_path=str(log_file or ""),
                        log_size=tracker.log_size,
                        log_growth_bytes=tracker.log_growth_bytes,
                        ram_pct=ram_pct,
                        ram_avail_gb=ram_avail,
                        profile=profile,
                        error_lines=list(tracker.fatal_errors),
                        hint=_build_silent_exit_hint(tail_text),
                    )
            else:
                dead_pid_seen_since = None

        else:
            assert resolved_rdir is not None
            log_file = resolved_rdir / "runme.log"
            begin_f = resolved_rdir / ".vivado.begin.rst"
            end_f = resolved_rdir / ".vivado.end.rst"
            err_f = resolved_rdir / ".vivado.error.rst"
            has_begin = begin_f.is_file() and not _is_link_or_junction(begin_f)
            begin_mtime = 0.0
            if has_begin:
                with contextlib.suppress(OSError):
                    begin_mtime = begin_f.stat().st_mtime

            rst_pid = _extract_rst_pid(begin_f) if has_begin else None
            if rst_pid is not None:
                watched_pid = rst_pid
            elif watched_pid is None:
                watched_pid = _lookup_shared_registry_pid(
                    resolved_rdir, resolved_xpr, eff_run_name
                )
            pid_alive = bool(watched_pid is not None and is_pid_alive(watched_pid))

            tracker.scan_log(log_file, now, begin_mtime=begin_mtime)
            phase = tracker.latest_phase

            has_end = _is_rst_fresh(end_f, begin_mtime=begin_mtime, pid_alive=pid_alive)
            has_err = _is_rst_fresh(err_f, begin_mtime=begin_mtime, pid_alive=pid_alive)

            if has_err:
                tail_text = _read_tail_bytes(log_file) if log_file.is_file() else ""
                is_silent = not tracker.fatal_errors
                return WatchResult(
                    event="silent_exit" if is_silent else "failed",
                    exit_code=2,
                    target=target_label,
                    run_name=eff_run_name,
                    until=eff_until,
                    elapsed_sec=elapsed,
                    phase=phase,
                    log_path=str(log_file),
                    log_size=tracker.log_size,
                    log_growth_bytes=tracker.log_growth_bytes,
                    ram_pct=ram_pct,
                    ram_avail_gb=ram_avail,
                    profile=profile,
                    error_lines=list(tracker.fatal_errors),
                    hint=(
                        _build_silent_exit_hint(tail_text)
                        if is_silent
                        else "检测到 .vivado.error.rst 与 ERROR 日志行。"
                    ),
                )

            log_mtime = 0.0
            if log_file.is_file():
                with contextlib.suppress(OSError):
                    log_mtime = log_file.stat().st_mtime
            tail_text = _read_tail_bytes(log_file) if log_file.is_file() else ""
            if eff_until != "complete" and _check_step_reached(
                resolved_rdir,
                tail_text,
                eff_until,
                log_mtime=log_mtime,
                pid_alive=pid_alive,
            ):
                summary = await asyncio.to_thread(
                    extract_compact_signoff, resolved_rdir, log_path=log_file, run_name=eff_run_name
                )
                return WatchResult(
                    event="reached",
                    exit_code=0,
                    target=target_label,
                    run_name=eff_run_name,
                    until=eff_until,
                    elapsed_sec=elapsed,
                    phase=phase,
                    log_path=str(log_file),
                    log_size=tracker.log_size,
                    log_growth_bytes=tracker.log_growth_bytes,
                    ram_pct=ram_pct,
                    ram_avail_gb=ram_avail,
                    profile=profile,
                    summary=summary,
                )

            if has_end:
                nonstd = scan_nonstandard_errors(tail_text) if tail_text else []
                if tracker.fatal_errors or nonstd:
                    errs = list(tracker.fatal_errors) or [e.text for e in nonstd[:4]]
                    return WatchResult(
                        event="failed",
                        exit_code=2,
                        target=target_label,
                        run_name=eff_run_name,
                        until=eff_until,
                        elapsed_sec=elapsed,
                        phase=phase,
                        log_path=str(log_file),
                        log_size=tracker.log_size,
                        log_growth_bytes=tracker.log_growth_bytes,
                        ram_pct=ram_pct,
                        ram_avail_gb=ram_avail,
                        profile=profile,
                        error_lines=errs,
                        hint="run 已结束但日志中存在 ERROR / 非标致命错误。",
                    )
                if eff_until != "complete" and not _check_step_reached(
                    resolved_rdir,
                    tail_text,
                    eff_until,
                    log_mtime=log_mtime,
                    pid_alive=pid_alive,
                ):
                    return WatchResult(
                        event="failed",
                        exit_code=2,
                        target=target_label,
                        run_name=eff_run_name,
                        until=eff_until,
                        elapsed_sec=elapsed,
                        phase=phase,
                        log_path=str(log_file),
                        log_size=tracker.log_size,
                        log_growth_bytes=tracker.log_growth_bytes,
                        ram_pct=ram_pct,
                        ram_avail_gb=ram_avail,
                        profile=profile,
                        hint=f"run 已正常结束(.vivado.end.rst)，但未到达目标步骤 {eff_until}。",
                    )
                summary = await asyncio.to_thread(
                    extract_compact_signoff, resolved_rdir, log_path=log_file, run_name=eff_run_name
                )
                return WatchResult(
                    event="completed",
                    exit_code=0,
                    target=target_label,
                    run_name=eff_run_name,
                    until=eff_until,
                    elapsed_sec=elapsed,
                    phase=phase,
                    progress=100.0,
                    log_path=str(log_file),
                    log_size=tracker.log_size,
                    log_growth_bytes=tracker.log_growth_bytes,
                    ram_pct=ram_pct,
                    ram_avail_gb=ram_avail,
                    profile=profile,
                    summary=summary,
                )

            if watched_pid is not None and not is_pid_alive(watched_pid):
                if dead_pid_seen_since is None:
                    dead_pid_seen_since = now
                    await asyncio.sleep(0.5)
                    continue
                tail_text = _read_tail_bytes(log_file) if log_file.is_file() else ""
                return WatchResult(
                    event="silent_exit",
                    exit_code=2,
                    target=target_label,
                    run_name=eff_run_name,
                    until=eff_until,
                    elapsed_sec=elapsed,
                    phase=phase,
                    log_path=str(log_file),
                    log_size=tracker.log_size,
                    log_growth_bytes=tracker.log_growth_bytes,
                    ram_pct=ram_pct,
                    ram_avail_gb=ram_avail,
                    profile=profile,
                    error_lines=list(tracker.fatal_errors),
                    hint=_build_silent_exit_hint(tail_text),
                )
            dead_pid_seen_since = None

        # 事件 2 (early_error): fail_fast=True 且命中 ^ERROR:\s+\[，等待 2s settle
        if fail_fast and tracker.fatal_errors:
            if error_settle_seconds > 0:
                await asyncio.sleep(min(error_settle_seconds, max(0.05, deadline - now)))
                tracker.scan_log(log_file, time.monotonic())
            return WatchResult(
                event="early_error",
                exit_code=2,
                target=target_label,
                run_name=eff_run_name,
                until=eff_until,
                elapsed_sec=max(0.0, time.monotonic() - started),
                phase=tracker.latest_phase,
                log_path=str(log_file or ""),
                log_size=tracker.log_size,
                log_growth_bytes=tracker.log_growth_bytes,
                ram_pct=ram_pct,
                ram_avail_gb=ram_avail,
                profile=profile,
                error_lines=list(tracker.fatal_errors),
                hint="检测到致命 ERROR 日志行（fail_fast 提前唤醒，未杀死 Vivado）。",
            )

        # 事件 5 (memory_critical): 连续 2 次 RAM >= 92% 且 avail < 4GB
        if mem_crit:
            return WatchResult(
                event="memory_critical",
                exit_code=5,
                target=target_label,
                run_name=eff_run_name,
                until=eff_until,
                elapsed_sec=elapsed,
                phase=phase,
                log_path=str(log_file or ""),
                log_size=tracker.log_size,
                log_growth_bytes=tracker.log_growth_bytes,
                ram_pct=ram_pct,
                ram_avail_gb=ram_avail,
                profile=profile,
                hint=f"主机 RAM {ram_pct:.1f}% 且仅剩 {ram_avail:.1f} GB（未杀死 Vivado）。",
                next_action="检查孤儿 vivado.exe 或降低并发 jobs 后继续观察。",
            )

        # 事件 3 (stalled): 字节大小与阶段长期不变且 CPU 门控确认非活跃/超宽限
        is_stalled = await asyncio.to_thread(tracker.evaluate_stall, now, watched_pid)
        if is_stalled:
            silent_sec = int(max(0.0, now - tracker.last_activity_at))
            cpu_note = (
                f"CPU {tracker.last_cpu_pct:.1f}%"
                if tracker.last_cpu_pct is not None
                else "无活跃 CPU 信号"
            )
            return WatchResult(
                event="stalled",
                exit_code=3,
                target=target_label,
                run_name=eff_run_name,
                until=eff_until,
                elapsed_sec=elapsed,
                phase=phase,
                log_path=str(log_file or ""),
                log_size=tracker.log_size,
                log_growth_bytes=tracker.log_growth_bytes,
                cpu_pct=tracker.last_cpu_pct,
                ram_pct=ram_pct,
                ram_avail_gb=ram_avail,
                profile=profile,
                hint=f"日志连续 {silent_sec}s 无增长且无新阶段（{cpu_note}，未杀死 Vivado）。",
                next_action="确认 Vivado 是否死锁/等许可，必要时手动取消。",
            )

        # 周期性 ~10s 心跳回调
        if on_progress is not None and (now - last_heartbeat_at) >= PROGRESS_HEARTBEAT_SEC:
            msg = format_heartbeat_message(
                phase=phase,
                elapsed_sec=elapsed,
                ram_pct=ram_pct,
                cpu_active_grace=tracker.cpu_active_grace,
            )
            await _emit_progress(on_progress, None, msg)
            last_heartbeat_at = now

        # 事件 4 (timeout): 健康运行中达到观察上限（绝不杀进程）
        remaining = deadline - now
        if remaining <= 0:
            next_cmd = (
                f"vivado-mcp watch --job {target_label}"
                if mode == "job"
                else f"vivado-mcp watch --run-dir {target_label} --until {eff_until}"
            )
            return WatchResult(
                event="timeout",
                exit_code=4,
                target=target_label,
                run_name=eff_run_name,
                until=eff_until,
                elapsed_sec=elapsed,
                phase=phase,
                log_path=str(log_file or ""),
                log_size=tracker.log_size,
                log_growth_bytes=tracker.log_growth_bytes,
                cpu_pct=tracker.last_cpu_pct,
                ram_pct=ram_pct,
                ram_avail_gb=ram_avail,
                profile=profile,
                hint="达到观察超时上限，Vivado 仍在健康运行（未终止）。",
                next_action=next_cmd,
            )

        await asyncio.sleep(min(float(eff_poll), remaining))


def watch_target(**kwargs: Any) -> WatchResult:
    """``watch_target_async`` 的同步封装。"""
    return asyncio.run(watch_target_async(**kwargs))
