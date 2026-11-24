"""主机内存与本机 EDA 进程遥测、内存护栏及白名单进程安全终止。

纯 Python 标准库 + Win32 ctypes 实现，零第三方依赖。
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ALLOWED_KILL_PROCESSES: frozenset[str] = frozenset(
    {
        "vivado.exe",
        "hw_server.exe",
        "cs_server.exe",
        "xsim.exe",
        "xelab.exe",
        "xvlog.exe",
        "xvhdl.exe",
    }
)

_OBSERVED_SIM_PROCESSES: frozenset[str] = frozenset(
    {
        "vsim.exe",
        "vlog.exe",
        "vopt.exe",
        "questa.exe",
    }
)

_POSIX_KILL_NAMES: frozenset[str] = frozenset(
    {name.removesuffix(".exe") for name in ALLOWED_KILL_PROCESSES}
)
_POSIX_OBSERVED_NAMES: frozenset[str] = frozenset(
    {name.removesuffix(".exe") for name in _OBSERVED_SIM_PROCESSES}
)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_COMMAND_LINE_INFORMATION = 60

if sys.platform == "win32":
    from ctypes import wintypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

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

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_ushort),
            ("MaximumLength", ctypes.c_ushort),
            ("Buffer", ctypes.c_void_p),
        ]


@functools.lru_cache(maxsize=1)
def _win32_apis():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    k32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.K32GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    ntdll = None
    with contextlib.suppress(OSError):
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtQueryInformationProcess.argtypes = [
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long

    return k32, ntdll


def get_host_memory() -> dict[str, Any]:
    """查询主机物理内存与逻辑 CPU 核数。"""
    cpu_count = os.cpu_count() or 0
    if sys.platform == "win32":
        k32, _ = _win32_apis()
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if k32.GlobalMemoryStatusEx(ctypes.byref(stat)) and stat.ullTotalPhys > 0:
            total = stat.ullTotalPhys
            avail = stat.ullAvailPhys
            used = max(0, total - avail)
            gib = float(1024**3)
            return {
                "ram_used_gb": round(used / gib, 2),
                "ram_total_gb": round(total / gib, 2),
                "ram_avail_gb": round(avail / gib, 2),
                "ram_pct": round((used / total) * 100.0, 1),
                "cpu_count": cpu_count,
            }

    # POSIX fallback via /proc/meminfo
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        with contextlib.suppress(OSError, ValueError):
            values: dict[str, int] = {}
            for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    values[parts[0][:-1]] = int(parts[1]) * 1024
            total = values.get("MemTotal", 0)
            avail = values.get("MemAvailable", values.get("MemFree", 0))
            if total > 0:
                used = max(0, total - avail)
                gib = float(1024**3)
                return {
                    "ram_used_gb": round(used / gib, 2),
                    "ram_total_gb": round(total / gib, 2),
                    "ram_avail_gb": round(avail / gib, 2),
                    "ram_pct": round((used / total) * 100.0, 1),
                    "cpu_count": cpu_count,
                }

    return {
        "ram_used_gb": 0.0,
        "ram_total_gb": 0.0,
        "ram_avail_gb": 0.0,
        "ram_pct": 0.0,
        "cpu_count": cpu_count,
    }


def _query_win32_proc_details(pid: int) -> tuple[float, float, str]:
    """查询单个 Windows 进程的 (rss_mb, uptime_sec, command_hint)。"""
    k32, ntdll = _win32_apis()
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0.0, 0.0, ""
    rss_mb = 0.0
    uptime_sec = 0.0
    cmd_hint = ""
    try:
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if k32.K32GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            rss_mb = round(pmc.WorkingSetSize / (1024.0 * 1024.0), 1)

        ct = wintypes.FILETIME()
        et = wintypes.FILETIME()
        kt = wintypes.FILETIME()
        ut = wintypes.FILETIME()
        if k32.GetProcessTimes(
            handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
        ):
            ft_val = (ct.dwHighDateTime << 32) | ct.dwLowDateTime
            if ft_val > 116444736000000000:
                created_unix = (ft_val - 116444736000000000) / 10_000_000.0
                uptime_sec = round(max(0.0, time.time() - created_unix), 1)

        if ntdll is not None:
            needed = wintypes.ULONG(0)
            ntdll.NtQueryInformationProcess(
                handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(needed)
            )
            if ctypes.sizeof(_UNICODE_STRING) <= needed.value <= 65536:
                buf = ctypes.create_string_buffer(needed.value)
                st = ntdll.NtQueryInformationProcess(
                    handle,
                    _PROCESS_COMMAND_LINE_INFORMATION,
                    buf,
                    needed.value,
                    ctypes.byref(needed),
                )
                if st == 0:
                    us = _UNICODE_STRING.from_buffer_copy(buf)
                    if us.Buffer and us.Length > 0:
                        raw_cmd = ctypes.wstring_at(us.Buffer, us.Length // 2)
                        cmd_hint = _compact_command_hint(raw_cmd)
    except Exception:
        pass
    finally:
        k32.CloseHandle(handle)
    return rss_mb, uptime_sec, cmd_hint


def _compact_command_hint(raw_cmd: str) -> str:
    text = " ".join((raw_cmd or "").split())
    if len(text) <= 180:
        return text
    return f"{text[:85]} ... {text[-85:]}"


def _is_eda_target(name_lower: str) -> tuple[bool, bool]:
    """返回 ``(is_tracked_eda, killable)``。"""
    if sys.platform == "win32":
        if name_lower in ALLOWED_KILL_PROCESSES:
            return True, True
        if name_lower in _OBSERVED_SIM_PROCESSES:
            return True, False
        return False, False
    stem = name_lower.removesuffix(".exe")
    if stem in _POSIX_KILL_NAMES or name_lower in ALLOWED_KILL_PROCESSES:
        return True, True
    if stem in _POSIX_OBSERVED_NAMES or name_lower in _OBSERVED_SIM_PROCESSES:
        return True, False
    return False, False


def _raw_eda_process_list() -> tuple[dict[int, int], list[dict[str, Any]]]:
    """扫描系统活进程，返回 ``(parent_map, eda_raw_entries)``。"""
    parent_map: dict[int, int] = {}
    eda_entries: list[dict[str, Any]] = []

    if sys.platform == "win32":
        k32, _ = _win32_apis()
        snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return parent_map, eda_entries
        candidates: list[tuple[int, int, str, bool]] = []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                pid = int(entry.th32ProcessID)
                ppid = int(entry.th32ParentProcessID)
                exe_name = str(entry.szExeFile)
                parent_map[pid] = ppid
                tracked, killable = _is_eda_target(exe_name.lower())
                if tracked and pid > 4:
                    candidates.append((pid, ppid, exe_name, killable))
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)

        for pid, ppid, exe_name, killable in candidates:
            rss_mb, uptime_sec, cmd_hint = _query_win32_proc_details(pid)
            eda_entries.append(
                {
                    "pid": pid,
                    "parent_pid": ppid,
                    "name": exe_name,
                    "rss_mb": rss_mb,
                    "uptime_sec": uptime_sec,
                    "command_hint": cmd_hint,
                    "killable": killable,
                }
            )
        return parent_map, eda_entries

    proc_root = Path("/proc")
    if proc_root.is_dir():
        for pdir in proc_root.iterdir():
            if not pdir.name.isdigit():
                continue
            pid = int(pdir.name)
            try:
                stat_text = (pdir / "stat").read_text(encoding="utf-8", errors="replace")
                lpar = stat_text.find("(")
                rpar = stat_text.rfind(")")
                if lpar == -1 or rpar <= lpar:
                    continue
                comm = stat_text[lpar + 1 : rpar]
                rest = stat_text[rpar + 2 :].split()
                ppid = int(rest[1]) if len(rest) >= 2 else 0
                parent_map[pid] = ppid
                tracked, killable = _is_eda_target(comm.lower())
                if not tracked:
                    continue
                cmd_hint = ""
                with contextlib.suppress(OSError):
                    cmd_raw = (pdir / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                        "utf-8", errors="replace"
                    )
                    cmd_hint = _compact_command_hint(cmd_raw)
                rss_mb = 0.0
                with contextlib.suppress(OSError, ValueError):
                    statm = (pdir / "statm").read_text().split()
                    if len(statm) >= 2:
                        rss_mb = round((int(statm[1]) * os.sysconf("SC_PAGE_SIZE")) / (1024**2), 1)
                eda_entries.append(
                    {
                        "pid": pid,
                        "parent_pid": ppid,
                        "name": comm,
                        "rss_mb": rss_mb,
                        "uptime_sec": 0.0,
                        "command_hint": cmd_hint,
                        "killable": killable,
                    }
                )
            except (OSError, ValueError):
                continue
    return parent_map, eda_entries


def _match_owner_info(
    pid: int,
    parent_map: dict[int, int],
    known_pids: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    cur = pid
    seen: set[int] = set()
    for _ in range(10):
        if cur in known_pids:
            return known_pids[cur]
        ppid = parent_map.get(cur)
        if not ppid or ppid <= 0 or ppid in seen or ppid == cur:
            break
        seen.add(ppid)
        cur = ppid
    return None


def scan_eda_processes(
    known_pids: dict[int, dict[str, Any]] | None = None,
    *,
    has_attached_gui: bool = False,
) -> list[dict[str, Any]]:
    """扫描当前主机上的 Vivado / hw_server / xsim 等 EDA 进程并标记归属与疑似残留。"""
    known = known_pids or {}
    parent_map, raw_procs = _raw_eda_process_list()
    results: list[dict[str, Any]] = []

    for proc in raw_procs:
        pid = int(proc["pid"])
        name = str(proc["name"])
        name_low = name.lower()
        cmd_hint = str(proc.get("command_hint") or "")
        owner = _match_owner_info(pid, parent_map, known)

        owner_client = ""
        owner_pid: int | None = None
        associated_label = ""
        is_orphan = False

        if owner is not None:
            owner_alive = owner.get("owner_alive", True)
            is_gui = bool(owner.get("is_attached_gui", False))
            owner_client = str(owner.get("owner_client") or ("GUI" if is_gui else ""))
            opid = owner.get("owner_pid")
            owner_pid = int(opid) if isinstance(opid, int) else None
            associated_label = str(owner.get("label") or owner.get("associated_label") or "")
            is_orphan = (not owner_alive) and (not is_gui)
        else:
            # 若已有活跃 attach/external GUI 会话，将交互式 GUI vivado.exe 归为 GUI
            cmd_low = cmd_hint.lower()
            is_vivado = name_low in {"vivado.exe", "vivado"}
            looks_gui = is_vivado and (
                "-mode gui" in cmd_low
                or (
                    has_attached_gui
                    and "-mode batch" not in cmd_low
                    and "-mode tcl" not in cmd_low
                )
            )
            if has_attached_gui and looks_gui:
                owner_client = "GUI"
                associated_label = "Attached Vivado GUI"
                is_orphan = False
            elif proc.get("killable", True):
                is_orphan = True

        results.append(
            {
                "pid": pid,
                "parent_pid": int(proc.get("parent_pid") or 0),
                "name": name,
                "rss_mb": round(float(proc.get("rss_mb") or 0.0), 1),
                "uptime_sec": round(float(proc.get("uptime_sec") or 0.0), 1),
                "command_hint": cmd_hint,
                "owner_client": owner_client,
                "owner_pid": owner_pid,
                "is_orphan": is_orphan,
                "associated_label": associated_label,
                "killable": bool(proc.get("killable", False)),
            }
        )

    results.sort(key=lambda item: (1 if item["is_orphan"] else 0, item["rss_mb"]), reverse=True)
    return results


def collect_system_telemetry(
    known_pids: dict[int, dict[str, Any]] | None = None,
    *,
    has_attached_gui: bool = False,
) -> dict[str, Any]:
    """收集控制台所需主机内存与 EDA 进程完整遥测字典。"""
    mem = get_host_memory()
    procs = scan_eda_processes(known_pids, has_attached_gui=has_attached_gui)
    vivado_count = sum(1 for p in procs if p["name"].lower() in {"vivado.exe", "vivado"})
    orphan_count = sum(1 for p in procs if p["is_orphan"])
    return {
        **mem,
        "vivado_count": vivado_count,
        "eda_count": len(procs),
        "orphan_count": orphan_count,
        "eda_processes": procs,
    }


def _win32_query_handle_exe_and_ctime(
    handle: int,
) -> tuple[str, tuple[int, int] | None]:
    k32, _ = _win32_apis()
    exe_name = ""
    buf_len = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(1024)
    if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)) and buf.value:
        exe_name = Path(buf.value).name.lower()
    ct = wintypes.FILETIME()
    et = wintypes.FILETIME()
    kt = wintypes.FILETIME()
    ut = wintypes.FILETIME()
    ctime: tuple[int, int] | None = None
    if k32.GetProcessTimes(
        handle,
        ctypes.byref(ct),
        ctypes.byref(et),
        ctypes.byref(kt),
        ctypes.byref(ut),
    ):
        ctime = (int(ct.dwLowDateTime), int(ct.dwHighDateTime))
    return exe_name, ctime


def _resolve_live_exe_name(pid: int) -> str:
    """在 OS 内核层面核验目标 PID 的真实可执行文件名。"""
    if sys.platform == "win32":
        k32, _ = _win32_apis()
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            raise FileNotFoundError(f"找不到进程或无访问权限: PID {pid}")
        try:
            exe_name, _ = _win32_query_handle_exe_and_ctime(handle)
            if exe_name:
                return exe_name
        finally:
            k32.CloseHandle(handle)

        # 回退 Toolhelp32 查询
        snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snap and snap != ctypes.c_void_p(-1).value:
            try:
                entry = _PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                ok = k32.Process32FirstW(snap, ctypes.byref(entry))
                while ok:
                    if int(entry.th32ProcessID) == pid:
                        return str(entry.szExeFile).lower()
                    ok = k32.Process32NextW(snap, ctypes.byref(entry))
            finally:
                k32.CloseHandle(snap)
        raise FileNotFoundError(f"无法确认进程可执行文件名: PID {pid}")

    comm_path = Path(f"/proc/{pid}/comm")
    if comm_path.is_file():
        return comm_path.read_text(encoding="utf-8", errors="replace").strip().lower()
    exe_path = Path(f"/proc/{pid}/exe")
    if exe_path.exists():
        return Path(os.readlink(exe_path)).name.lower()
    raise FileNotFoundError(f"找不到进程: PID {pid}")


def kill_eda_process(pid: int) -> dict[str, Any]:
    """严格白名单与防 PID 重用 TOCTOU 校验后，安全终止指定 EDA 进程树。"""
    if (
        type(pid) is not int
        or pid <= 4
        or pid > 0xFFFFFFFF
        or pid == os.getpid()
        or pid == os.getppid()
    ):
        raise ValueError(f"拒绝终止非法或受保护的进程 PID: {pid!r}")

    if sys.platform == "win32":
        k32, _ = _win32_apis()
        hold_handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        pinned_ctime: tuple[int, int] | None = None
        if hold_handle:
            exe_name, pinned_ctime = _win32_query_handle_exe_and_ctime(hold_handle)
            if not exe_name:
                k32.CloseHandle(hold_handle)
                hold_handle = None
                exe_name = _resolve_live_exe_name(pid)
        else:
            exe_name = _resolve_live_exe_name(pid)

        try:
            _, killable = _is_eda_target(exe_name)
            if not killable:
                raise ValueError(
                    f"安全策略拒绝终止非白名单 EDA 进程: {exe_name} (PID {pid})"
                )

            kwargs: dict[str, Any] = {}
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            res = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                **kwargs,
            )
            if res.returncode != 0:
                term_handle = k32.OpenProcess(
                    _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
                    False,
                    pid,
                )
                if not term_handle:
                    detail = (res.stderr or res.stdout or f"taskkill rc={res.returncode}").strip()
                    raise OSError(f"终止进程 {exe_name} (PID {pid}) 失败: {detail}")
                try:
                    cur_exe, cur_ctime = _win32_query_handle_exe_and_ctime(term_handle)
                    if cur_exe != exe_name or (
                        pinned_ctime is not None
                        and cur_ctime is not None
                        and cur_ctime != pinned_ctime
                    ):
                        raise ValueError(
                            f"进程 PID {pid} 身份已变化（拒绝误杀重用 PID: {cur_exe or '未知'}）"
                        )
                    if not k32.TerminateProcess(term_handle, 1):
                        raise OSError(f"TerminateProcess 失败: PID {pid}")
                finally:
                    k32.CloseHandle(term_handle)
        finally:
            if hold_handle:
                k32.CloseHandle(hold_handle)
    else:
        exe_name = _resolve_live_exe_name(pid)
        _, killable = _is_eda_target(exe_name)
        if not killable:
            raise ValueError(
                f"安全策略拒绝终止非白名单 EDA 进程: {exe_name} (PID {pid})"
            )
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    return {
        "ok": True,
        "pid": pid,
        "name": exe_name,
        "message": f"已终止 {exe_name} (PID {pid})",
    }


def format_memory_guard_warning(telemetry: dict[str, Any]) -> str:
    """主机内存偏高或多 Vivado 并行时返回单行 [MEM] 警示；健康时返回空串。"""
    if not telemetry:
        return ""
    total = float(telemetry.get("ram_total_gb") or 0.0)
    used = float(telemetry.get("ram_used_gb") or 0.0)
    avail = float(telemetry.get("ram_avail_gb") or 0.0)
    pct = float(telemetry.get("ram_pct") or 0.0)
    viv_count = int(telemetry.get("vivado_count") or 0)

    has_mem = total > 0.0
    high_mem = has_mem and (pct >= 78.0 or avail < 10.0)
    parallel_vivado = viv_count >= 2
    if not (high_mem or parallel_vivado):
        return ""

    mem_part = (
        f"内存 {used:.1f}/{total:.1f} GB ({pct:.0f}%, 可用 {avail:.1f} GB)"
        if has_mem
        else "内存遥测未知"
    )
    return (
        f"[MEM] 主机{mem_part}，当前活跃 Vivado {viv_count} 个；"
        "综合/实现高峰期注意 OOM 风险，必要时先清理残留进程。"
    )
