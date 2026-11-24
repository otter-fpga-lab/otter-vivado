"""跨智能体共享会话与运行注册表。

持久化路径位于 ``%LOCALAPPDATA%\\otter-vivado\\registry\\``（受 ``OTTER_VIVADO_CACHE_DIR``
环境变量控制），绝不写入用户工程目录。支持 Codex / Antigravity / Claude / Cursor / CLI
客户端并行运行时的实时状态共享与磁盘日志富化。
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from vivado_mcp.analysis.run_progress_parser import progress_percent, run_state
from vivado_mcp.project_layout import user_cache_root

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_TH32CS_SNAPPROCESS = 0x00000002

_SAFE_SID_RE = re.compile(r"[^a-zA-Z0-9_.-]+")

if sys.platform == "win32":
    from ctypes import wintypes

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


@functools.lru_cache(maxsize=1)
def _win32_kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    return k32


def is_pid_alive(pid: int) -> bool:
    """快速判断进程 PID 是否仍存活。"""
    if type(pid) is not int or pid <= 0:
        return False
    if sys.platform == "win32":
        k32 = _win32_kernel32()
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD(0)
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _ancestor_process_names(max_depth: int = 12) -> list[str]:
    """沿父进程链收集祖先进程可执行文件名（小写），用于客户端来源识别。"""
    names: list[str] = []
    if sys.platform == "win32":
        k32 = _win32_kernel32()
        snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return names
        parents: dict[int, tuple[int, str]] = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                parents[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    str(entry.szExeFile).lower(),
                )
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)

        cur = os.getpid()
        seen: set[int] = set()
        for _ in range(max_depth):
            info = parents.get(cur)
            if not info:
                break
            ppid, _ = info
            if ppid <= 0 or ppid in seen or ppid == cur:
                break
            seen.add(ppid)
            pinfo = parents.get(ppid)
            if not pinfo:
                break
            names.append(pinfo[1])
            cur = ppid
        return names

    cur = os.getppid()
    seen_posix: set[int] = set()
    for _ in range(max_depth):
        if cur <= 1 or cur in seen_posix:
            break
        seen_posix.add(cur)
        stat_path = Path(f"/proc/{cur}/stat")
        if not stat_path.is_file():
            break
        try:
            raw = stat_path.read_text(encoding="utf-8", errors="replace")
            lpar = raw.find("(")
            rpar = raw.rfind(")")
            if lpar != -1 and rpar > lpar:
                names.append(raw[lpar + 1 : rpar].lower())
                rest = raw[rpar + 2 :].split()
                cur = int(rest[1]) if len(rest) >= 2 else 0
            else:
                break
        except (OSError, ValueError):
            break
    return names


@functools.lru_cache(maxsize=1)
def detect_client_source() -> str:
    """识别当前 MCP 进程宿主客户端来源（每进程缓存一次）。"""
    env_upper = {k.upper(): v for k, v in os.environ.items() if v}

    # 1. 强运行时会话标志优先（避免系统级 CODEX_HOME 等静态配置变量误盖活动宿主）
    if any(
        k in env_upper
        for k in (
            "CODEX_THREAD_ID",
            "CODEX_SANDBOX",
            "CODEX_CI",
            "CODEX_SHELL",
            "CODEX_MANAGED_BY_NPM",
            "CODEX_MANAGED_BY_BUN",
        )
    ):
        return "Codex"
    if any(
        k in env_upper
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
    ):
        return "Claude"
    if any(
        k in env_upper
        for k in ("CURSOR_TRACE_ID", "CURSOR_AGENT", "CURSOR_EXTENSION_HOST_ROLE")
    ):
        return "Cursor"
    if any(
        k in env_upper
        for k in (
            "ANTIGRAVITY_AGENT",
            "ANTIGRAVITY_CONVERSATION_ID",
            "ANTIGRAVITY_LS_ADDRESS",
            "ANTIGRAVITY_TRAJECTORY_ID",
            "ANTIGRAVITY_AGENTAPI_EXE",
        )
    ):
        return "Antigravity"

    # 2. 父进程链匹配
    for proc_name in _ancestor_process_names():
        stem = proc_name.removesuffix(".exe")
        if stem == "codex" or stem.startswith("codex-"):
            return "Codex"
        if stem == "claude" or stem.startswith("claude-"):
            return "Claude"
        if stem == "cursor" or stem.startswith("cursor-"):
            return "Cursor"
        if (
            stem == "antigravity"
            or stem == "language_server"
            or stem.startswith("language_server_")
        ):
            return "Antigravity"

    # 3. 通用环境变量前缀兜底（不把静态全局 CODEX_HOME 等配置误判为活动 Codex 会话）
    keys = list(env_upper.keys())
    if any(k.startswith("ANTIGRAVITY_") for k in keys):
        return "Antigravity"
    if any(k.startswith("CLAUDE_") for k in keys):
        return "Claude"
    if any(k.startswith("CURSOR_") for k in keys):
        return "Cursor"
    if "CODEX_SESSION_ID" in env_upper:
        return "Codex"

    return "CLI"


def _safe_sid(session_id: str) -> str:
    cleaned = _SAFE_SID_RE.sub("_", (session_id or "default").strip()).strip("._")
    return (cleaned[:64] or "default")


def _parse_elapsed_sec(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return round(max(0.0, float(value)), 2)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return round(float(text), 2)
    parts = text.split(":")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        return float(h * 3600 + m * 60 + s)
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        m, s = int(parts[0]), int(parts[1])
        return float(m * 60 + s)
    return None


def _normalize_progress(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if 0 <= num <= 100 else None
    return progress_percent(str(value))


def _default_timing() -> dict[str, Any]:
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
        "parse_status": "missing",
        "source_report": "",
    }


def _default_util() -> dict[str, Any]:
    return {"lut": None, "ff": None, "bram": None, "dsp": None, "uram": None}


def _default_diag() -> dict[str, Any]:
    return {
        "errors": 0,
        "critical_warnings": 0,
        "warnings": 0,
        "top_issues": [],
        "all_issues": [],
        "warning_items": [],
    }


class SharedRegistry:
    """线程安全的跨进程会话与构建运行共享注册表。"""

    def __init__(self, registry_dir: Path | None = None):
        self._registry_dir_override = Path(registry_dir) if registry_dir is not None else None
        self._lock = threading.RLock()
        self._local_entries: dict[str, dict[str, Any]] = {}
        self._dir_report_cache: dict[str, tuple[tuple[int, int, int], dict[str, Any]]] = {}

    @property
    def registry_dir(self) -> Path:
        if self._registry_dir_override is not None:
            return self._registry_dir_override
        return user_cache_root() / "registry"

    @property
    def sessions_dir(self) -> Path:
        return self.registry_dir / "sessions"

    def _entry_path(self, entry_id: str) -> Path:
        return self.sessions_dir / f"{entry_id}.json"

    def _write_atomic(self, entry_id: str, data: dict[str, Any]) -> None:
        from vivado_mcp.batch_jobs import _atomic_write_json

        sdir = self.sessions_dir
        sdir.mkdir(parents=True, exist_ok=True)
        target = self._entry_path(entry_id)
        tmp = sdir / f".{entry_id}.{uuid.uuid4().hex[:6]}.tmp"
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        _atomic_write_json(tmp, target, payload)

    def _read_entry_file_ex(self, path: Path) -> tuple[dict[str, Any] | None, bool]:
        """读取注册表 JSON，返回 ``(data, is_corrupt_json)`` 区分坏文件与短暂 IO 锁。"""
        from vivado_mcp.batch_jobs import _is_link_or_junction

        if _is_link_or_junction(path) or not path.is_file():
            return None, False
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None, False
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None, True
        if not isinstance(data, dict):
            return None, True
        return data, False

    def _read_entry_file(self, path: Path) -> dict[str, Any] | None:
        data, _ = self._read_entry_file_ex(path)
        return data

    def upsert_session(
        self,
        session_id: str,
        *,
        mode: str | None = None,
        port: int | None = None,
        vivado_pid: int | None = None,
        vivado_version: str | None = None,
        project_name: str | None = None,
        xpr_path: str | None = None,
        run_name: str | None = None,
        target_step: str | None = None,
        run_dir: str | None = None,
        status: str | None = None,
        state: str | None = None,
        progress: float | str | None = None,
        phase: str | None = None,
        elapsed_sec: float | str | None = None,
        timing: dict[str, Any] | None = None,
        utilization: dict[str, Any] | None = None,
        diagnostics: dict[str, Any] | None = None,
        reports: list[dict[str, Any]] | None = None,
        log_path: str | None = None,
        log_tail: list[dict[str, Any]] | None = None,
        monitor_url: str | None = None,
        debug_url: str | None = None,
        alive: bool | None = None,
        disconnected: bool | None = None,
    ) -> dict[str, Any]:
        """原子更新并持久化当前进程指定 session 的共享状态。"""
        owner_pid = os.getpid()
        sid = (session_id or "default").strip() or "default"
        safe_sid = _safe_sid(sid)
        entry_id = f"{owner_pid}_{safe_sid}"

        with self._lock:
            existing = dict(self._local_entries.get(entry_id) or {})
            if not existing:
                disk_existing = self._read_entry_file(self._entry_path(entry_id))
                if disk_existing:
                    existing = disk_existing

            merged: dict[str, Any] = {
                "entry_id": entry_id,
                "owner_pid": owner_pid,
                "client": detect_client_source(),
                "alive": existing.get("alive", True) if alive is None else bool(alive),
                "disconnected": (
                    existing.get("disconnected", False)
                    if disconnected is None
                    else bool(disconnected)
                ),
                "session_id": sid,
                "mode": mode if mode is not None else existing.get("mode", ""),
                "port": port if port is not None else existing.get("port"),
                "vivado_pid": (
                    vivado_pid if vivado_pid is not None else existing.get("vivado_pid")
                ),
                "vivado_version": (
                    vivado_version
                    if vivado_version not in (None, "", "unknown")
                    else existing.get("vivado_version", "")
                ),
                "project_name": (
                    project_name if project_name is not None else existing.get("project_name", "")
                ),
                "xpr_path": xpr_path if xpr_path is not None else existing.get("xpr_path", ""),
                "run_name": run_name if run_name is not None else existing.get("run_name", ""),
                "target_step": (
                    target_step if target_step is not None else existing.get("target_step", "")
                ),
                "run_dir": run_dir if run_dir is not None else existing.get("run_dir", ""),
                "status": status if status is not None else existing.get("status", ""),
                "state": state if state is not None else existing.get("state", ""),
                "progress": (
                    _normalize_progress(progress)
                    if progress is not None
                    else existing.get("progress")
                ),
                "phase": phase if phase is not None else existing.get("phase", ""),
                "elapsed_sec": (
                    _parse_elapsed_sec(elapsed_sec)
                    if elapsed_sec is not None
                    else existing.get("elapsed_sec")
                ),
                "updated_at": time.time(),
                "timing": (
                    timing
                    if timing is not None
                    else existing.get("timing", _default_timing())
                ),
                "utilization": (
                    utilization
                    if utilization is not None
                    else existing.get("utilization", _default_util())
                ),
                "diagnostics": (
                    diagnostics
                    if diagnostics is not None
                    else existing.get("diagnostics", _default_diag())
                ),
                "reports": reports if reports is not None else existing.get("reports", []),
                "log_path": log_path if log_path is not None else existing.get("log_path", ""),
                "log_tail": log_tail if log_tail is not None else existing.get("log_tail", []),
                "monitor_url": (
                    monitor_url if monitor_url is not None else existing.get("monitor_url", "")
                ),
                "debug_url": debug_url if debug_url is not None else existing.get("debug_url", ""),
            }

            if merged["xpr_path"] and not merged["project_name"]:
                merged["project_name"] = Path(str(merged["xpr_path"])).stem

            if not merged["run_dir"] and merged["xpr_path"] and merged["run_name"]:
                xpr = Path(str(merged["xpr_path"]))
                cand_dir = xpr.parent / f"{xpr.stem}.runs" / str(merged["run_name"])
                if cand_dir.is_dir():
                    merged["run_dir"] = str(cand_dir)

            if merged["run_dir"] and not merged["xpr_path"]:
                rdir = Path(str(merged["run_dir"]))
                if rdir.parent.name.endswith(".runs"):
                    proj_stem = rdir.parent.name[: -len(".runs")]
                    cand_xpr = rdir.parent.parent / f"{proj_stem}.xpr"
                    if cand_xpr.is_file():
                        merged["xpr_path"] = str(cand_xpr)
                        if not merged["project_name"]:
                            merged["project_name"] = proj_stem

            if merged["run_dir"] and not merged["log_path"]:
                cand_log = Path(str(merged["run_dir"])) / "runme.log"
                if cand_log.is_file():
                    merged["log_path"] = str(cand_log)

            self._local_entries[entry_id] = merged
            with contextlib.suppress(OSError):
                self._write_atomic(entry_id, merged)
            return dict(merged)

    def mark_session_disconnected(
        self,
        session_id: str,
        *,
        status: str | None = None,
    ) -> None:
        """会话关闭时更新共享注册表状态为已断开/已结束。"""
        owner_pid = os.getpid()
        sid = (session_id or "default").strip() or "default"
        entry_id = f"{owner_pid}_{_safe_sid(sid)}"
        with self._lock:
            existing = dict(self._local_entries.get(entry_id) or {})
            if not existing:
                disk = self._read_entry_file(self._entry_path(entry_id))
                if not disk:
                    return
                existing = disk
            cur_status = str(existing.get("status") or "").lower()
            vpid = existing.get("vivado_pid")
            viv_alive = is_pid_alive(vpid) if isinstance(vpid, int) and vpid > 0 else False
            if status is not None and str(status).strip():
                next_status = str(status).strip()
            else:
                next_status = (
                    "ended"
                    if cur_status == "running" and not viv_alive
                    else (existing.get("status") or "stopped")
                )
            existing["alive"] = False
            existing["disconnected"] = True
            existing["state"] = "stopped"
            existing["status"] = next_status
            existing["updated_at"] = time.time()
            self._local_entries[entry_id] = existing
            with contextlib.suppress(OSError):
                self._write_atomic(entry_id, existing)

    def close(self) -> None:
        """MCP 进程退出时将自身活跃记录标记断开并清理无构建上下文的空占位。"""
        owner_pid = os.getpid()
        with self._lock:
            sdir = self.sessions_dir
            if sdir.is_dir():
                prefix = f"{owner_pid}_"
                for path in list(sdir.glob(f"{prefix}*.json")):
                    data = self._read_entry_file(path)
                    if not data:
                        continue
                    has_build_ctx = any(
                        bool(data.get(k))
                        for k in ("xpr_path", "run_dir", "run_name", "monitor_url", "debug_url")
                    )
                    if not has_build_ctx:
                        with contextlib.suppress(OSError):
                            path.unlink()
                        continue
                    cur_status = str(data.get("status") or "").lower()
                    data["alive"] = False
                    data["disconnected"] = True
                    data["state"] = "disconnected"
                    if cur_status == "running":
                        data["status"] = "ended"
                    data["updated_at"] = time.time()
                    entry_id = str(data.get("entry_id") or path.stem)
                    with contextlib.suppress(OSError):
                        self._write_atomic(entry_id, data)
            self._local_entries.clear()

    def _enrich_entry(self, raw: dict[str, Any], now: float) -> dict[str, Any]:
        from vivado_mcp.batch_jobs import (
            _dir_report_signature,
            _is_link_or_junction,
            parse_directory_reports,
        )

        owner_pid_raw = raw.get("owner_pid")
        owner_pid = int(owner_pid_raw) if isinstance(owner_pid_raw, int) else 0
        vivado_pid_raw = raw.get("vivado_pid")
        vivado_pid = int(vivado_pid_raw) if isinstance(vivado_pid_raw, int) else None

        disconnected = bool(raw.get("disconnected", False))
        owner_alive = (not disconnected) and is_pid_alive(owner_pid)
        vivado_alive = is_pid_alive(vivado_pid) if vivado_pid else False

        xpr_path = str(raw.get("xpr_path") or "")
        project_name = str(raw.get("project_name") or "")
        if xpr_path and not project_name:
            project_name = Path(xpr_path).stem
        run_name = str(raw.get("run_name") or "")
        run_dir = str(raw.get("run_dir") or "")
        if not run_dir and xpr_path and run_name:
            xp = Path(xpr_path)
            cand = xp.parent / f"{xp.stem}.runs" / run_name
            if cand.is_dir() and not _is_link_or_junction(cand):
                run_dir = str(cand)

        status = str(raw.get("status") or "")
        state = str(raw.get("state") or "")
        progress = _normalize_progress(raw.get("progress"))
        phase = str(raw.get("phase") or "")
        elapsed_sec = _parse_elapsed_sec(raw.get("elapsed_sec"))
        updated_at = float(raw.get("updated_at") or 0.0)
        timing = dict(raw.get("timing") or _default_timing())
        if "wpws" not in timing:
            timing["wpws"] = None
        utilization = dict(raw.get("utilization") or _default_util())
        if "uram" not in utilization:
            utilization["uram"] = None
        diagnostics = dict(raw.get("diagnostics") or _default_diag())
        reports = list(raw.get("reports") or [])
        log_path = str(raw.get("log_path") or "")
        log_tail = list(raw.get("log_tail") or [])
        vivado_version = str(raw.get("vivado_version") or "")

        rdir_path = Path(run_dir) if run_dir else None
        if rdir_path and rdir_path.is_dir() and not _is_link_or_junction(rdir_path):
            begin_f = rdir_path / ".vivado.begin.rst"
            end_f = rdir_path / ".vivado.end.rst"
            err_f = rdir_path / ".vivado.error.rst"
            has_begin = begin_f.is_file() and not _is_link_or_junction(begin_f)
            has_end = end_f.is_file() and not _is_link_or_junction(end_f)
            has_err = err_f.is_file() and not _is_link_or_junction(err_f)

            cand_log = Path(log_path) if log_path else (rdir_path / "runme.log")
            is_failed = has_err or status.lower() == "failed" or state.lower() == "failed"
            scan_nonstandard = not owner_alive or has_err or has_end
            log_arg = cand_log if cand_log.is_file() else None
            dir_sig = _dir_report_signature(rdir_path)
            cache_key = (
                f"{os.path.normcase(str(rdir_path))}|"
                f"{int(is_failed)}|{int(scan_nonstandard)}|{log_arg or ''}"
            )
            parsed: dict[str, Any] | None = None
            with self._lock:
                cached = self._dir_report_cache.get(cache_key)
                if cached is not None and dir_sig is not None and cached[0] == dir_sig:
                    parsed = cached[1]
            if parsed is None:
                parsed = parse_directory_reports(
                    rdir_path,
                    log_path=log_arg,
                    is_failed=is_failed,
                    scan_nonstandard=scan_nonstandard,
                    log_tail_lines=25,
                )
                if dir_sig is not None:
                    with self._lock:
                        if len(self._dir_report_cache) >= 32:
                            oldest = next(iter(self._dir_report_cache))
                            self._dir_report_cache.pop(oldest, None)
                        self._dir_report_cache[cache_key] = (dir_sig, parsed)
            if parsed["timing"].get("parse_status") == "ok" or timing.get("parse_status") != "ok":
                timing = parsed["timing"]
            if any(parsed["utilization"].values()) or not any(utilization.values()):
                utilization = parsed["utilization"]
            if parsed["primary_log"]:
                log_path = parsed["primary_log"]
            if parsed["reports"]:
                reports = [
                    {
                        "name": r["name"],
                        "rel_path": r["rel_path"],
                        "path": r["path"],
                        "size": r["size"],
                        "kind": r["kind"],
                    }
                    for r in parsed["reports"]
                ]
            if parsed["current_phase"]:
                phase = parsed["current_phase"]
            if parsed["log_tail"]:
                log_tail = parsed["log_tail"]
            if parsed["primary_log"] or parsed["reports"]:
                diagnostics = parsed["diagnostics"]
            if not vivado_version and parsed["inferred_version"]:
                vivado_version = parsed["inferred_version"]

            if has_begin:
                with contextlib.suppress(OSError):
                    t_begin = begin_f.stat().st_mtime
                    if has_end:
                        elapsed_sec = round(max(0.0, end_f.stat().st_mtime - t_begin), 2)
                    elif has_err:
                        elapsed_sec = round(max(0.0, err_f.stat().st_mtime - t_begin), 2)
                    elif owner_alive or vivado_alive:
                        elapsed_sec = round(max(0.0, now - t_begin), 2)

            if has_err:
                status = "failed"
                state = "failed"
            elif has_end:
                if (
                    diagnostics.get("errors", 0) > 0
                    or diagnostics.get("nonstandard_count", 0) > 0
                ):
                    status = "failed"
                    state = "failed"
                else:
                    status = "completed"
                    state = "completed"
                    if progress is None:
                        progress = 100.0
            elif has_begin:
                if owner_alive or vivado_alive:
                    status = "running"
                    state = "running"
                else:
                    status = "ended"
                    state = "ended"
            else:
                # 无 .rst 标记时按日志/进程存活判定
                if not owner_alive:
                    if status.lower() == "running" or state.lower() == "running":
                        if (
                            diagnostics.get("errors", 0) > 0
                            or diagnostics.get("nonstandard_count", 0) > 0
                        ):
                            status = "failed"
                            state = "failed"
                        else:
                            status = "ended"
                            state = "ended"
                    elif not status or status.lower() in {"ready", "connected"}:
                        state = "disconnected"
        else:
            # 无 run_dir 时仍须对死进程做 stale running 降级
            if not owner_alive:
                if status.lower() == "running" or state.lower() == "running":
                    status = "ended"
                    state = "ended"
                elif not state or state.lower() in {"ready", "connected"}:
                    state = "disconnected"

        if not status:
            status = state or ("connected" if owner_alive else "disconnected")
        if not state:
            if status.lower() in {"running", "completed", "failed", "cancelled", "ended"}:
                state = status.lower()
            else:
                state = run_state(status) if status else ("connected" if owner_alive else "ended")

        default_eid = f"{owner_pid}_{raw.get('session_id', 'default')}"
        return {
            "entry_id": str(raw.get("entry_id") or default_eid),
            "owner_pid": owner_pid,
            "client": str(raw.get("client") or "CLI"),
            "alive": bool(owner_alive),
            "session_id": str(raw.get("session_id") or "default"),
            "mode": str(raw.get("mode") or ""),
            "port": raw.get("port"),
            "vivado_pid": vivado_pid,
            "vivado_version": vivado_version,
            "project_name": project_name,
            "xpr_path": xpr_path,
            "run_name": run_name,
            "target_step": str(raw.get("target_step") or ""),
            "run_dir": run_dir,
            "status": status,
            "state": state,
            "progress": progress,
            "phase": phase,
            "elapsed_sec": elapsed_sec,
            "updated_at": updated_at,
            "timing": timing,
            "utilization": utilization,
            "diagnostics": diagnostics,
            "reports": reports,
            "log_path": log_path,
            "log_tail": log_tail,
            "monitor_url": str(raw.get("monitor_url") or ""),
            "debug_url": str(raw.get("debug_url") or ""),
        }

    def list_remote_runs(
        self,
        exclude_owner_pid: int | None = -1,
        max_age_hours: float = 24.0,
    ) -> list[dict[str, Any]]:
        """扫描跨进程会话注册表并从磁盘实时富化运行进度/报告。"""
        excl = (
            os.getpid()
            if (exclude_owner_pid is not None and exclude_owner_pid < 0)
            else exclude_owner_pid
        )
        now = time.time()
        cutoff = now - max_age_hours * 3600.0 if max_age_hours and max_age_hours > 0 else 0.0

        with self._lock:
            sdir = self.sessions_dir
            if not sdir.is_dir():
                return []
            files = sorted(sdir.glob("*.json"))

        results: list[dict[str, Any]] = []
        for path in files:
            data = self._read_entry_file(path)
            if not data:
                continue
            opid = data.get("owner_pid")
            if excl is not None and opid == excl:
                continue
            updated = float(data.get("updated_at") or 0.0)
            if cutoff and updated and updated < cutoff:
                continue
            results.append(self._enrich_entry(data, now))

        results.sort(
            key=lambda item: (
                1 if str(item.get("status") or "").lower() == "running" else 0,
                float(item.get("updated_at") or 0.0),
            ),
            reverse=True,
        )
        return results

    def cleanup_stale(self, max_age_hours: float | None = None) -> int:
        """清理已死亡/已断开的注册表条目，返回移除数量。

        ``max_age_hours=None`` 时清除所有所属进程已死亡或已断开且非运行中的条目（用户主动清理）；
        传入阈值时仅清除超过该时长的已死亡非运行条目。
        遇到短暂并发读锁定（OSError）不误删，仅清理明确损坏的 JSON。
        """
        now = time.time()
        removed = 0
        with self._lock:
            sdir = self.sessions_dir
            if not sdir.is_dir():
                return 0
            for path in list(sdir.glob("*.json")):
                data, is_corrupt = self._read_entry_file_ex(path)
                if data is None:
                    if is_corrupt:
                        with contextlib.suppress(OSError):
                            path.unlink()
                            removed += 1
                    continue
                opid = data.get("owner_pid")
                owner_alive = (
                    not bool(data.get("disconnected"))
                    and isinstance(opid, int)
                    and is_pid_alive(opid)
                )
                if owner_alive:
                    continue
                vpid = data.get("vivado_pid")
                viv_alive = isinstance(vpid, int) and is_pid_alive(vpid)
                if max_age_hours is not None:
                    status_low = str(data.get("status") or "").lower()
                    if viv_alive and status_low == "running":
                        continue
                    updated = float(data.get("updated_at") or 0.0)
                    if updated and (now - updated) < max_age_hours * 3600.0:
                        continue
                else:
                    # 显式触发清理时跳过仍有存活 Vivado 的运行中条目
                    if viv_alive and str(data.get("status") or "").lower() == "running":
                        continue
                with contextlib.suppress(OSError):
                    path.unlink()
                    self._local_entries.pop(str(data.get("entry_id") or path.stem), None)
                    removed += 1
        return removed

    def allowed_report_roots(self) -> list[Path]:
        """收集共享注册表中经过严格绝对路径与全链路防链接校验的根目录，供受限报告预览放行。"""
        from vivado_mcp.batch_jobs import _resolve_safe_report_root

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

        with self._lock:
            sdir = self.sessions_dir
            files = list(sdir.glob("*.json")) if sdir.is_dir() else []

        for path in files:
            data = self._read_entry_file(path)
            if not data:
                continue
            rdir = data.get("run_dir")
            lpath = data.get("log_path")
            xpath = data.get("xpr_path")
            add_root(rdir)
            if lpath:
                add_root(str(lpath), is_file_hint=True)
            if xpath:
                xp = Path(str(xpath).strip())
                if xp.is_absolute():
                    add_root(xp, is_file_hint=True)
                    if data.get("run_name"):
                        add_root(xp.parent / f"{xp.stem}.runs" / str(data["run_name"]))
        return roots
