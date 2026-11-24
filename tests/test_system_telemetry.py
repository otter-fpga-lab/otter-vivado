"""主机内存、EDA 进程遥测、内存护栏与白名单进程安全终止测试。"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from vivado_mcp import system_telemetry as st


def test_get_host_memory_returns_positive_metrics():
    mem = st.get_host_memory()
    assert set(mem) == {
        "ram_used_gb",
        "ram_total_gb",
        "ram_avail_gb",
        "ram_pct",
        "cpu_count",
    }
    assert mem["cpu_count"] >= 1
    assert mem["ram_total_gb"] > 0
    assert 0.0 <= mem["ram_pct"] <= 100.0


def test_scan_eda_processes_and_orphan_classification(monkeypatch):
    fake_raw = [
        {
            "pid": 2001,
            "parent_pid": 1001,
            "name": "vivado.exe",
            "rss_mb": 4200.5,
            "uptime_sec": 300.0,
            "command_hint": "vivado.exe -mode batch -source synth.tcl",
            "killable": True,
        },
        {
            "pid": 2002,
            "parent_pid": 9999,
            "name": "vivado.exe",
            "rss_mb": 6100.0,
            "uptime_sec": 1200.0,
            "command_hint": "vivado.exe -mode batch -source old.tcl",
            "killable": True,
        },
        {
            "pid": 2003,
            "parent_pid": 9998,
            "name": "vivado.exe",
            "rss_mb": 2200.0,
            "uptime_sec": 60.0,
            "command_hint": "vivado.exe -mode gui",
            "killable": True,
        },
        {
            "pid": 2004,
            "parent_pid": 100,
            "name": "vsim.exe",
            "rss_mb": 500.0,
            "uptime_sec": 45.0,
            "command_hint": "vsim.exe -c",
            "killable": False,
        },
    ]
    parent_map = {p["pid"]: p["parent_pid"] for p in fake_raw}
    monkeypatch.setattr(st, "_raw_eda_process_list", lambda: (parent_map, fake_raw))

    known = {
        1001: {
            "owner_client": "Codex",
            "owner_pid": 1001,
            "owner_alive": True,
            "label": "demo_top",
            "is_attached_gui": False,
        }
    }
    tel = st.collect_system_telemetry(known, has_attached_gui=True)
    assert set(tel) == {
        "ram_used_gb",
        "ram_total_gb",
        "ram_avail_gb",
        "ram_pct",
        "cpu_count",
        "vivado_count",
        "eda_count",
        "orphan_count",
        "eda_processes",
    }
    assert tel["vivado_count"] == 3
    assert tel["eda_count"] == 4
    # 2001 归属 Codex(存活)，2003 归属 attached GUI，2004 vsim killable=False，仅 2002 为孤儿
    assert tel["orphan_count"] == 1
    by_pid = {p["pid"]: p for p in tel["eda_processes"]}
    assert by_pid[2001]["is_orphan"] is False
    assert by_pid[2001]["owner_client"] == "Codex"
    assert by_pid[2001]["associated_label"] == "demo_top"
    assert by_pid[2002]["is_orphan"] is True
    assert by_pid[2002]["killable"] is True
    assert by_pid[2003]["is_orphan"] is False
    assert by_pid[2003]["owner_client"] == "GUI"
    assert by_pid[2004]["is_orphan"] is False
    assert by_pid[2004]["killable"] is False


def test_kill_eda_process_strict_allowlist_and_guards(monkeypatch):
    # 拒绝受保护 PID 及自身 PID
    for bad_pid in (0, 1, 4, os.getpid()):
        with pytest.raises(ValueError):
            st.kill_eda_process(bad_pid)

    # 对真实存在的 python.exe（非白名单）必须拒绝
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(ValueError, match="安全策略拒绝"):
            st.kill_eda_process(proc.pid)
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # 非 EDA 可执行文件即便在 mock 层也拒绝
    monkeypatch.setattr(st, "_resolve_live_exe_name", lambda pid: "explorer.exe")
    with pytest.raises(ValueError, match="安全策略拒绝"):
        st.kill_eda_process(54321)

    # 白名单 EDA 进程执行安全终止
    assert "vivado.exe" in st.ALLOWED_KILL_PROCESSES
    monkeypatch.setattr(st, "_resolve_live_exe_name", lambda pid: "vivado.exe")
    seen_cmd: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen_cmd.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(st.subprocess, "run", fake_run)
    if sys.platform == "win32":
        res = st.kill_eda_process(54321)
        assert res["ok"] is True
        assert res["pid"] == 54321
        assert res["name"] == "vivado.exe"
        assert seen_cmd == [["taskkill", "/PID", "54321", "/T", "/F"]]


def test_format_memory_guard_warning_thresholds():
    # 健康主机输出空串
    assert (
        st.format_memory_guard_warning(
            {
                "ram_total_gb": 64.0,
                "ram_used_gb": 24.0,
                "ram_avail_gb": 40.0,
                "ram_pct": 37.5,
                "vivado_count": 1,
            }
        )
        == ""
    )
    # 内存占比 >= 78%
    w_pct = st.format_memory_guard_warning(
        {
            "ram_total_gb": 32.0,
            "ram_used_gb": 25.6,
            "ram_avail_gb": 11.0,
            "ram_pct": 80.0,
            "vivado_count": 1,
        }
    )
    assert w_pct.startswith("[MEM]")

    # 可用内存 < 10 GB
    w_avail = st.format_memory_guard_warning(
        {
            "ram_total_gb": 64.0,
            "ram_used_gb": 55.0,
            "ram_avail_gb": 9.0,
            "ram_pct": 70.0,
            "vivado_count": 0,
        }
    )
    assert w_avail.startswith("[MEM]")

    # 并行 Vivado >= 2
    w_par = st.format_memory_guard_warning(
        {
            "ram_total_gb": 64.0,
            "ram_used_gb": 20.0,
            "ram_avail_gb": 44.0,
            "ram_pct": 31.2,
            "vivado_count": 2,
        }
    )
    assert w_par.startswith("[MEM]")
    assert "Vivado 2 个" in w_par


def test_kill_eda_process_rejects_dword_overflow_ppid_and_pid_reuse(monkeypatch):
    # 拒绝超出 32-bit DWORD 范围及父进程 PID
    for bad_pid in (0x1_0000_0001, os.getppid()):
        with pytest.raises(ValueError):
            st.kill_eda_process(bad_pid)

    if sys.platform != "win32":
        return

    # 模拟 taskkill 失败回退 TerminateProcess 时进程创建时间发生跳变（PID 重用）
    class _FakeK32:
        def OpenProcess(self, access, inherit, pid):
            return 101 if access == st._PROCESS_QUERY_LIMITED_INFORMATION else 202

        def CloseHandle(self, handle):
            return True

        def TerminateProcess(self, handle, code):
            raise AssertionError("PID 身份改变后不应调用 TerminateProcess")

    monkeypatch.setattr(st, "_win32_apis", lambda: (_FakeK32(), None))
    times = iter([
        ("vivado.exe", (1000, 2000)),
        ("vivado.exe", (9999, 2000)),  # creation time changed -> PID recycled!
    ])
    monkeypatch.setattr(st, "_win32_query_handle_exe_and_ctime", lambda h: next(times))
    monkeypatch.setattr(
        st.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="err"),
    )
    with pytest.raises(ValueError, match="身份已变化"):
        st.kill_eda_process(54321)
