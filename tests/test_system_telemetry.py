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


def test_infer_client_from_ancestors_pass1_ai_before_editors_and_no_cmd_terminal():
    # 1. Codex Desktop: explorer -> codex -> python(.venv) -> python -> cmd -> vivado
    parent_map = {
        600: 500,  # vivado -> cmd
        500: 400,  # cmd -> python
        400: 300,  # python -> python
        300: 200,  # python -> codex
        200: 100,  # codex -> explorer
    }
    proc_names = {
        600: "vivado.exe",
        500: "cmd.exe",
        400: "python.exe",
        300: "python.exe",
        200: "codex.exe",
        100: "explorer.exe",
    }
    assert st.infer_client_from_ancestors(600, parent_map, proc_names) == ("Codex", 200)

    # 2. VS Code Codex Ext: Code.exe -> codex.exe -> ... ALWAYS yields Codex, NEVER VS Code
    proc_names_vscode_codex = {**proc_names, 100: "Code.exe"}
    assert st.infer_client_from_ancestors(600, parent_map, proc_names_vscode_codex) == (
        "Codex",
        200,
    )

    # 3. Antigravity via language_server_* and Claude / Cursor
    assert st.infer_client_from_ancestors(
        30,
        {30: 20, 20: 10},
        {30: "vivado.exe", 20: "python.exe", 10: "language_server_windows_x64.exe"},
    ) == ("Antigravity", 10)
    assert st.infer_client_from_ancestors(
        30, {30: 20, 20: 10}, {30: "vivado.exe", 20: "bash", 10: "claude.exe"}
    ) == ("Claude", 10)
    assert st.infer_client_from_ancestors(
        30, {30: 20, 20: 10}, {30: "vivado.exe", 20: "zsh", 10: "cursor.exe"}
    ) == ("Cursor", 10)

    # 4. Interpreter cmdline matching (max 4 interpreters, ignores vivado_mcp codex path!)
    calls: list[int] = []
    pm_interp = {90: 80, 80: 70, 70: 60, 60: 50, 50: 40, 40: 30}
    pn_interp = {
        90: "vivado.exe",
        80: "python.exe",
        70: "python3.exe",
        60: "node.exe",
        50: "bun.exe",
        40: "deno.exe",
        30: "Code.exe",
    }

    def _loader_skip_vivado_mcp(pid: int) -> str:
        calls.append(pid)
        if pid == 80:
            return r"C:\Users\dev\codex\otter-vivado\.venv\Scripts\python.exe -m vivado_mcp"
        if pid == 50:
            return r"bun C:\npm\@openai\codex\bin\codex.js"
        return ""

    assert st.infer_client_from_ancestors(
        90, pm_interp, pn_interp, cmdline_loader=_loader_skip_vivado_mcp
    ) == ("Codex", 50)
    assert len(calls) <= 4

    # 5th interpreter not inspected once cap=4 reached -> falls back to Pass 2 VS Code
    calls.clear()

    def _loader_5th_only(pid: int) -> str:
        calls.append(pid)
        if pid == 40:
            return "deno run @anthropic-ai/claude-code"
        return ""

    assert st.infer_client_from_ancestors(
        90, pm_interp, pn_interp, cmdline_loader=_loader_5th_only
    ) == ("VS Code", 30)
    assert len(calls) == 4

    # Cursor resources/app & Antigravity cmdline
    assert st.infer_client_from_ancestors(
        20,
        {20: 10},
        {20: "vivado.exe", 10: "node.exe"},
        cmdline_loader=lambda _p: r"node C:\Program Files\cursor\resources\app\main.js",
    ) == ("Cursor", 10)
    assert st.infer_client_from_ancestors(
        20,
        {20: 10},
        {20: "vivado.exe", 10: "node.exe"},
        cmdline_loader=lambda _p: "node /opt/antigravity/language_server_linux_x64",
    ) == ("Antigravity", 10)

    # Negative cmdline guards: vivado_mcp path and bare folder substrings must NEVER misclassify
    assert st.infer_client_from_ancestors(
        20,
        {20: 10},
        {20: "vivado.exe", 10: "python.exe"},
        cmdline_loader=lambda _p: r"python.exe D:\antigravity_demo\run.py",
    ) == ("", None)
    assert st.infer_client_from_ancestors(
        20,
        {20: 10},
        {20: "vivado.exe", 10: "python.exe"},
        cmdline_loader=lambda _p: r"python.exe D:\projects\codex\sim.py",
    ) == ("", None)
    assert st.infer_client_from_ancestors(
        30,
        {30: 20, 20: 10},
        {30: "vivado.exe", 20: "python.exe", 10: "node.exe"},
        cmdline_loader=lambda pid: (
            r"C:\work\antigravity\otter-vivado\.venv\Scripts\python.exe -m vivado_mcp"
            if pid == 20
            else r"node C:\npm\@openai\codex\bin\codex.js"
        ),
    ) == ("Codex", 10)
    assert st.infer_client_from_ancestors(True, {1: 2}, {1: "codex.exe"}) == ("", None)
    assert st.infer_client_from_ancestors(
        10, {10: True}, {1: "codex.exe", 10: "vivado.exe"}  # type: ignore[dict-item]
    ) == ("", None)

    # 5. Pass 2 Terminal / VS Code hosts, and bare cmd.exe is NEVER "终端"
    assert st.infer_client_from_ancestors(
        30, {30: 20, 20: 10}, {30: "vivado.exe", 20: "powershell.exe", 10: "WindowsTerminal.exe"}
    ) == ("终端", 20)
    assert st.infer_client_from_ancestors(
        20, {20: 10}, {20: "vivado.exe", 10: "cmd.exe"}
    ) == ("", None)

    # 6. Cycle safety & 14-hop depth within max_depth=16
    assert st.infer_client_from_ancestors(
        10, {10: 20, 20: 10}, {10: "python.exe", 20: "cmd.exe"}
    ) == ("", None)
    deep_pm = {i: i - 1 for i in range(2, 16)}
    deep_pn = {i: "python.exe" for i in range(2, 16)}
    deep_pn[1] = "codex.exe"
    assert st.infer_client_from_ancestors(15, deep_pm, deep_pn) == ("Codex", 1)


def test_infer_pids_clients_and_scan_eda_processes_upgrade(monkeypatch):
    parent_map = {
        3001: 2001,
        2001: 1001,
        3002: 2002,
        2002: 1002,
        3003: 9999,
    }
    proc_names = {
        3001: "vivado.exe",
        2001: "cmd.exe",
        1001: "codex.exe",
        3002: "vivado.exe",
        2002: "pwsh.exe",
        1002: "WindowsTerminal.exe",
        3003: "vivado.exe",
        9999: "cmd.exe",
    }
    monkeypatch.setattr(st, "_snapshot_process_tree", lambda: (parent_map, proc_names))
    monkeypatch.setattr(st, "_query_process_cmdline", lambda pid: "")
    inferred = st.infer_pids_clients([3001, 3002, 3003])
    assert inferred == {3001: ("Codex", 1001), 3002: ("终端", 2002)}

    # scan_eda_processes upgrades generic owner_client and attributes unregistered live ancestors
    fake_raw = [
        {
            "pid": 3001,
            "parent_pid": 2001,
            "name": "vivado.exe",
            "rss_mb": 4000.0,
            "uptime_sec": 100.0,
            "command_hint": r"vivado.exe -mode batch -log E:\proj\isp.runs\impl_1\runme.log",
            "killable": True,
            "inferred_client": "Codex",
            "inferred_client_pid": 1001,
            "project_hint": "isp/impl_1",
        },
        {
            "pid": 3002,
            "parent_pid": 2002,
            "name": "vivado.exe",
            "rss_mb": 1500.0,
            "uptime_sec": 50.0,
            "command_hint": r"vivado.exe -mode batch -source D:\work\build.tcl",
            "killable": True,
            "inferred_client": "终端",
            "inferred_client_pid": 2002,
            "project_hint": "build.tcl",
        },
        {
            "pid": 3003,
            "parent_pid": 9999,
            "name": "vivado.exe",
            "rss_mb": 900.0,
            "uptime_sec": 20.0,
            "command_hint": r"vivado.exe -mode gui D:\fpga\top.xpr",
            "killable": True,
            "inferred_client": "",
            "inferred_client_pid": None,
            "project_hint": "top.xpr",
        },
    ]
    monkeypatch.setattr(st, "_raw_eda_process_list", lambda: (parent_map, fake_raw))
    known = {
        3001: {
            "owner_client": "",
            "owner_pid": None,
            "owner_alive": True,
            "label": "",
            "is_attached_gui": False,
        }
    }
    procs = {p["pid"]: p for p in st.scan_eda_processes(known, has_attached_gui=False)}
    assert procs[3001]["owner_client"] == "Codex"
    assert procs[3001]["owner_pid"] == 1001
    assert procs[3001]["associated_label"] == "isp/impl_1"
    assert procs[3001]["is_orphan"] is False

    assert procs[3002]["owner_client"] == "终端"
    assert procs[3002]["owner_pid"] == 2002
    assert procs[3002]["associated_label"] == "build.tcl"
    assert procs[3002]["is_orphan"] is False

    assert procs[3003]["owner_client"] == "Vivado GUI"
    assert procs[3003]["associated_label"] == "top.xpr"
    assert procs[3003]["is_orphan"] is False
