"""自适应 Watch 引擎、CLI、wait_run 心跳与批处理早醒测试（全模拟，不启动 Vivado）。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.mcpserver import Context

from vivado_mcp import batch_jobs, watch_engine
from vivado_mcp.server import AppContext, mcp
from vivado_mcp.tools.flow_tools import wait_run
from vivado_mcp.vivado.base_session import SessionState
from vivado_mcp.vivado.session_manager import SessionManager
from vivado_mcp.vivado.tcl_utils import TclResult
from vivado_mcp.watch_cli import watch_main
from vivado_mcp.watch_engine import (
    estimate_watch_profile,
    sample_process_cpu_active,
    watch_target_async,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _result(output: str, error: bool = False) -> TclResult:
    return TclResult(output=output, return_code=int(error), is_error=error)


class _FakeClock:
    """不会因 asyncio 事件循环额外读取而抛出 StopIteration 的可控时钟。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


def _write_xpr(
    path: Path,
    *,
    part: str,
    top: str = "top_mod",
    n_files: int = 3,
    n_ips: int = 0,
) -> Path:
    files_xml = []
    for i in range(n_files):
        files_xml.append(f'<File Path="$PPRDIR/rtl/mod_{i}.sv"/>')
    for i in range(n_ips):
        files_xml.append(f'<File Path="$PPRDIR/ip/ip_{i}/ip_{i}.xci"/>')
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Project Version="7" Minor="60">\n'
        f'  <Configuration><Option Name="Part" Val="{part}"/></Configuration>\n'
        '  <FileSets><FileSet Name="sources_1" Type="DesignSrcs">\n'
        f'    {"".join(files_xml)}\n'
        f'    <Config><Option Name="TopModule" Val="{top}"/></Config>\n'
        '  </FileSet></FileSets>\n'
        '  <Runs><Run Id="synth_1" Type="Ft3:Synth"/>'
        '<Run Id="impl_1" Type="Ft2:EntireDesign" SynthRun="synth_1"/></Runs>\n'
        '</Project>\n'
    )
    path.write_text(xml, encoding="utf-8")
    return path


def _write_util_rpt(path: Path, *, lut: int, ff: int = 1000, bram: int = 4, dsp: int = 2) -> None:
    path.write_text(
        "+-------------------------+-------+-------+-----------+-------+\n"
        "|        Site Type        |  Used | Fixed | Available | Util% |\n"
        "+-------------------------+-------+-------+-----------+-------+\n"
        f"| Slice LUTs              | {lut:5d} |     0 |    203800 |  5.00 |\n"
        f"| Slice Registers         | {ff:5d} |     0 |    407600 |  1.00 |\n"
        f"| Block RAM Tile          | {bram:5d} |     0 |       445 |  2.00 |\n"
        f"| DSPs                    | {dsp:5d} |     0 |       840 |  1.00 |\n"
        "+-------------------------+-------+-------+-----------+-------+\n",
        encoding="utf-8",
    )


def _write_timing_rpt(path: Path, *, wns: float = 0.217, wpws: float = 1.5) -> None:
    path.write_text(
        "Design State : Routed\n"
        "    WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints"
        "  WHS(ns)  THS(ns)  THS Failing Endpoints  THS Total Endpoints"
        "  WPWS(ns)  TPWS(ns)  TPWS Failing Endpoints  TPWS Total Endpoints\n"
        "    -------  -------  ---------------------  -------------------"
        "  -------  -------  ---------------------  -------------------"
        "  --------  --------  ----------------------  --------------------\n"
        f"    {wns:7.3f}    0.000                      0                 2000"
        "    0.055    0.000                      0                 2000"
        f"     {wpws:5.3f}     0.000                       0                  800\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
#  1. 自适应阈值估算 estimate_watch_profile
# --------------------------------------------------------------------------- #


def test_estimate_watch_profile_tiers_and_multipliers(tmp_path):
    # Small OOC
    p_ooc = estimate_watch_profile(is_ooc=True, part="xc7a35tcpg236-1")
    assert p_ooc.tier == "small"
    assert p_ooc.stall_seconds == 120
    assert p_ooc.timeout_seconds == 300
    assert "非ETA" in p_ooc.basis

    # Small project (part + <=15 files + <=1 IP)
    p_small = estimate_watch_profile(part="xc7a35tcsg324-1", file_count=8, ip_count=1)
    assert p_small.tier == "small"
    assert (p_small.stall_seconds, p_small.timeout_seconds) == (120, 300)

    # Medium xc7z020
    p_med = estimate_watch_profile(part="xc7z020clg400-1", file_count=20, ip_count=2)
    assert p_med.tier == "medium"
    assert (p_med.stall_seconds, p_med.timeout_seconds) == (300, 900)

    # Large xczu7ev & xc7k325t impl_1 (full 1.0x grace)
    for large_part in ("xczu7ev-ffvc1156-2-e", "xc7k325tffg900-2"):
        p_large = estimate_watch_profile(part=large_part, run_name="impl_1", until="complete")
        assert p_large.tier == "large"
        assert (p_large.stall_seconds, p_large.timeout_seconds) == (600, 1800)

    # Step-aware multipliers
    p_bit = estimate_watch_profile(
        part="xczu7ev-ffvc1156-2-e", run_name="impl_1", until="write_bitstream"
    )
    assert p_bit.tier == "large"
    assert p_bit.stall_seconds == 360  # 600 * 0.6

    p_synth = estimate_watch_profile(
        part="xc7k325tffg900-2", run_name="synth_1", until="synth_design"
    )
    assert p_synth.tier == "large"
    assert p_synth.stall_seconds == 450  # 600 * 0.75

    # .xpr file/ip scale upgrade
    xpr = _write_xpr(tmp_path / "big.xpr", part="xc7z020clg400-1", n_files=20, n_ips=6)
    p_xpr = estimate_watch_profile(xpr_path=xpr)
    assert p_xpr.tier == "large"
    assert (p_xpr.stall_seconds, p_xpr.timeout_seconds) == (600, 1800)


def test_estimate_watch_profile_historical_rpt_elapsed_and_speed(tmp_path):
    proj_dir = tmp_path / "hist_proj"
    runs_dir = proj_dir / "demo.runs"
    synth_dir = runs_dir / "synth_1"
    impl_dir = runs_dir / "impl_1"
    synth_dir.mkdir(parents=True)
    impl_dir.mkdir(parents=True)
    xpr = _write_xpr(proj_dir / "demo.xpr", part="xc7z020clg400-1", top="isp_top", n_files=12)

    # Sibling synth_1 has high LUT count -> upgrades medium device to large
    _write_util_rpt(synth_dir / "synth_utilization.rpt", lut=72000, dsp=24, bram=30)
    # Prior completed impl_1 runme.log with 500s elapsed
    (impl_dir / ".vivado.begin.rst").write_text("<ProcessHandle/>", encoding="utf-8")
    (impl_dir / ".vivado.end.rst").write_text("<ProcessHandle/>", encoding="utf-8")
    (impl_dir / "runme.log").write_text(
        "opt_design: Time (s): cpu = 00:01:00 ; elapsed = 00:01:40 . Memory (MB): peak = 2000\n"
        "route_design: Time (s): cpu = 00:06:00 ; elapsed = 00:06:40 . Memory (MB): peak = 3200\n"
        "INFO: [Common 17-206] Exiting Vivado...\n",
        encoding="utf-8",
    )

    t0 = time.perf_counter()
    prof = estimate_watch_profile(run_dir=impl_dir, xpr_path=xpr)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert elapsed_ms < 25.0
    assert prof.tier == "large"
    assert prof.lut_estimate == 72000
    assert prof.prev_elapsed_sec == 500.0
    assert prof.timeout_seconds == 1250  # clamp(int(500 * 2.5), 180, 1800)
    assert prof.stall_seconds == 600     # max(600, int(500 * 0.45))

    # Prior completed BatchJobRegistry record override
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "20261124_010000_tcl_abc123.json").write_text(
        json.dumps(
            {
                "job_id": "20261124_010000_tcl_abc123",
                "kind": "batch_tcl",
                "status": "completed",
                "top": "isp_top",
                "elapsed_sec": 700.0,
            }
        ),
        encoding="utf-8",
    )
    prof_job = estimate_watch_profile(
        job={"job_id": "cur", "kind": "batch_tcl", "top": "isp_top", "part": "xc7z020clg400-1"},
        jobs_dir=jobs_dir,
    )
    assert prof_job.tier == "large"
    assert prof_job.prev_elapsed_sec == 700.0
    assert prof_job.timeout_seconds == 1750


# --------------------------------------------------------------------------- #
#  2. CPU 活性门控 sample_process_cpu_active
# --------------------------------------------------------------------------- #


def test_sample_process_cpu_active_guards_and_win32_delta(monkeypatch):
    assert sample_process_cpu_active(0) is None
    assert sample_process_cpu_active(-5) is None

    monkeypatch.setattr(watch_engine.sys, "platform", "linux")
    assert sample_process_cpu_active(os.getpid()) is None

    if sys.platform != "win32":
        return

    monkeypatch.setattr(watch_engine.sys, "platform", "win32")
    monkeypatch.setattr(watch_engine.time, "sleep", lambda _s: None)
    mono_vals = iter([10.0, 10.5])
    monkeypatch.setattr(watch_engine.time, "monotonic", lambda: next(mono_vals))

    # 模拟 0.5s 墙钟内使用 0.25s 内核+用户态 CPU -> 50.0%
    calls = {"n": 0}

    class _FakeK32:
        def OpenProcess(self, access, inherit, pid):
            return 1234

        def CloseHandle(self, handle):
            return True

        def GetProcessTimes(self, handle, ct, et, kt, ut):
            calls["n"] += 1
            kt_obj = kt._obj
            ut_obj = ut._obj
            if calls["n"] == 1:
                kt_obj.dwLowDateTime = 1_000_000
                ut_obj.dwLowDateTime = 2_000_000
            else:
                kt_obj.dwLowDateTime = 2_000_000
                ut_obj.dwLowDateTime = 3_500_000  # delta = 2_500_000 * 100ns = 0.25s
            return True

    monkeypatch.setattr("vivado_mcp.system_telemetry._win32_apis", lambda: (_FakeK32(), None))
    cpu = sample_process_cpu_active(9999, interval_sec=0.35)
    assert cpu == 50.0


# --------------------------------------------------------------------------- #
#  3. 统一 5 事件 watcher（退出码 0 / 2 / 3 / 4 / 5）
# --------------------------------------------------------------------------- #


async def test_watch_event_0_completed_and_reached_with_summary(tmp_path):
    proj_dir = tmp_path / "p0"
    synth_dir = proj_dir / "p0.runs" / "synth_1"
    impl_dir = proj_dir / "p0.runs" / "impl_1"
    synth_dir.mkdir(parents=True)
    impl_dir.mkdir(parents=True)
    _write_xpr(proj_dir / "p0.xpr", part="xc7a35tcpg236-1", top="demo_top")

    (synth_dir / "runme.log").write_text(
        "WARNING: [Synth 8-3848] Net u0 in module demo_top does not have driver.\n",
        encoding="utf-8",
    )
    (impl_dir / ".vivado.begin.rst").write_text("<ProcessHandle/>", encoding="utf-8")
    (impl_dir / ".vivado.end.rst").write_text("<ProcessHandle/>", encoding="utf-8")
    (impl_dir / "runme.log").write_text(
        "Phase 4.1 Global Routing\n"
        "CRITICAL WARNING: [Constraints 18-5210] Minor note.\n"
        "route_design completed successfully\n"
        "Exiting Vivado...\n",
        encoding="utf-8",
    )
    _write_timing_rpt(impl_dir / "route_timing_summary.rpt", wns=0.312, wpws=1.25)
    _write_util_rpt(impl_dir / "route_utilization.rpt", lut=3200, ff=4100, bram=6, dsp=4)

    res = await watch_target_async(run_dir=impl_dir, poll_seconds=1)
    assert res.event == "completed"
    assert res.exit_code == 0
    assert res.summary is not None
    assert res.summary["wns"] == 0.312
    assert res.summary["wpws"] == 1.25
    assert res.summary["lut"] == 3200
    assert res.summary["ff"] == 4100
    assert res.summary["bram"] == 6
    assert res.summary["dsp"] == 4
    assert res.summary["errors"] == 0
    assert res.summary["cw"] == 1
    assert res.summary["synth_8_3848"] == 1
    compact_bytes = json.dumps(
        res.to_compact_dict(), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(compact_bytes) < 700

    # --until place_design: .place_design.end.rst 出现即以 reached (0) 结束
    run2 = tmp_path / "p1.runs" / "impl_1"
    run2.mkdir(parents=True)
    (run2 / ".vivado.begin.rst").write_text('<Process Pid="999999"/>', encoding="utf-8")
    (run2 / ".place_design.end.rst").write_text("<ProcessHandle/>", encoding="utf-8")
    (run2 / "runme.log").write_text("place_design completed successfully\n", encoding="utf-8")
    with patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True):
        res_step = await watch_target_async(run_dir=run2, until="place_design", poll_seconds=1)
    assert res_step.event == "reached"
    assert res_step.exit_code == 0


async def test_watch_event_2_early_error_and_report_power_silent_exit(tmp_path):
    # A. early_error: 不匹配 "0 Errors encountered"，匹配 ^ERROR: [ 且等待 settle 捕获邻近行
    rdir = tmp_path / "err.runs" / "impl_1"
    rdir.mkdir(parents=True)
    (rdir / ".vivado.begin.rst").write_text('<Process Pid="4444"/>', encoding="utf-8")
    log_f = rdir / "runme.log"
    log_f.write_text(
        "INFO: [Vivado_Tcl 4-198] 0 Errors encountered.\n"
        "Phase 1 RTL Elaboration\n",
        encoding="utf-8",
    )

    sleep_calls: list[float] = []

    async def fake_sleep(sec):
        sleep_calls.append(sec)
        if len(sleep_calls) == 1:
            log_f.write_text(
                log_f.read_text(encoding="utf-8")
                + "ERROR: [Synth 8-27] procedural assignment to non-register [top.v:12]\n",
                encoding="utf-8",
            )
        elif len(sleep_calls) == 2:
            log_f.write_text(
                log_f.read_text(encoding="utf-8")
                + "ERROR: [Common 17-69] Command failed: Synthesis failed\n",
                encoding="utf-8",
            )

    ok_mem = {"ram_total_gb": 64.0, "ram_pct": 40.0, "ram_avail_gb": 38.0}
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=ok_mem),
        patch("vivado_mcp.watch_engine.asyncio.sleep", side_effect=fake_sleep),
    ):
        res = await watch_target_async(
            run_dir=rdir, poll_seconds=1, timeout_seconds=30, error_settle_seconds=2.0
        )

    assert res.event == "early_error"
    assert res.exit_code == 2
    assert len(res.error_lines) == 2
    assert "Synth 8-27" in res.error_lines[0]
    assert "Common 17-69" in res.error_lines[1]
    assert "0 Errors encountered" not in "\n".join(res.error_lines)
    assert 2.0 in sleep_calls

    # B. report_power silent_exit (死 PID 且无 .vivado.end.rst)
    sdir = tmp_path / "silent.runs" / "impl_1"
    sdir.mkdir(parents=True)
    (sdir / ".vivado.begin.rst").write_text('<Process Pid="5555"/>', encoding="utf-8")
    (sdir / "runme.log").write_text(
        "route_design completed successfully\n"
        "Command: report_power -file top_power_routed.rpt\n",
        encoding="utf-8",
    )
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=False),
        patch("vivado_mcp.watch_engine.asyncio.sleep", new_callable=AsyncMock),
    ):
        res_silent = await watch_target_async(run_dir=sdir, poll_seconds=1)
    assert res_silent.event == "silent_exit"
    assert res_silent.exit_code == 2
    assert "report_power" in res_silent.hint
    assert "skip_route_power_report=True" in res_silent.hint


async def test_watch_event_3_stall_cpu_idle_vs_active_grace(tmp_path):
    rdir = tmp_path / "stall.runs" / "impl_1"
    rdir.mkdir(parents=True)
    (rdir / ".vivado.begin.rst").write_text('<Process Pid="6666"/>', encoding="utf-8")
    (rdir / "runme.log").write_text("Phase 4.1 Global Routing\n", encoding="utf-8")

    # Case 1: CPU idle (< 1.0%) -> immediate stall at stall_seconds (120s)
    clock_idle = _FakeClock(0.0)

    async def sleep_idle(_sec):
        clock_idle.advance(121.0)

    ok_mem = {"ram_total_gb": 64.0, "ram_pct": 45.0, "ram_avail_gb": 35.0}
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=ok_mem),
        patch("vivado_mcp.watch_engine.sample_process_cpu_active", return_value=0.2),
        patch("vivado_mcp.watch_engine.time.monotonic", clock_idle),
        patch("vivado_mcp.watch_engine.asyncio.sleep", side_effect=sleep_idle),
    ):
        res_idle = await watch_target_async(
            run_dir=rdir, stall_seconds=120, timeout_seconds=500, poll_seconds=2
        )
    assert res_idle.event == "stalled"
    assert res_idle.exit_code == 3
    assert res_idle.cpu_pct == 0.2

    # Case 2: CPU active (>= 5.0%) -> 1.5x grace + heartbeat annotation, only stalls after 180s
    heartbeats: list[str] = []
    clock_active = _FakeClock(0.0)

    async def sleep_active(_sec):
        clock_active.advance(65.0)

    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=ok_mem),
        patch("vivado_mcp.watch_engine.sample_process_cpu_active", return_value=88.0),
        patch("vivado_mcp.watch_engine.time.monotonic", clock_active),
        patch("vivado_mcp.watch_engine.asyncio.sleep", side_effect=sleep_active),
    ):
        res_active = await watch_target_async(
            run_dir=rdir,
            stall_seconds=120,
            timeout_seconds=500,
            poll_seconds=2,
            on_progress=lambda _p, msg: heartbeats.append(msg),
        )
    assert res_active.event == "stalled"
    assert res_active.exit_code == 3
    assert res_active.elapsed_sec >= 180.0
    assert any("日志静默但 Vivado CPU 活跃" in msg for msg in heartbeats)


async def test_watch_event_4_timeout_non_destructive_and_event_5_memory_critical(tmp_path):
    rdir = tmp_path / "to.runs" / "impl_1"
    rdir.mkdir(parents=True)
    (rdir / ".vivado.begin.rst").write_text('<Process Pid="7777"/>', encoding="utf-8")
    log_f = rdir / "runme.log"
    log_f.write_text("Phase 1 Starting\n", encoding="utf-8")

    clock_to = _FakeClock(0.0)

    async def grow_log(_sec):
        log_f.write_text(log_f.read_text(encoding="utf-8") + "Phase 2 Routing\n", encoding="utf-8")
        clock_to.advance(16.0)

    mem_ok = {"ram_total_gb": 64.0, "ram_pct": 55.0, "ram_avail_gb": 28.0}
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=mem_ok),
        patch("vivado_mcp.watch_engine.time.monotonic", clock_to),
        patch("vivado_mcp.watch_engine.asyncio.sleep", side_effect=grow_log),
    ):
        res_to = await watch_target_async(
            run_dir=rdir, stall_seconds=0, timeout_seconds=30, poll_seconds=5
        )
    assert res_to.event == "timeout"
    assert res_to.exit_code == 4
    assert res_to.log_growth_bytes > 0
    assert "未终止" in res_to.hint

    # Event 5: 1-tick spike must NOT trigger; 2 consecutive critical samples DO trigger
    mem_samples = iter([
        {"ram_total_gb": 64.0, "ram_pct": 94.0, "ram_avail_gb": 2.5},  # spike 1
        {"ram_total_gb": 64.0, "ram_pct": 85.0, "ram_avail_gb": 9.0},  # recovered -> resets streak
        {"ram_total_gb": 64.0, "ram_pct": 93.0, "ram_avail_gb": 3.1},  # critical 1
        {"ram_total_gb": 64.0, "ram_pct": 95.0, "ram_avail_gb": 2.0},  # critical 2 -> wakes!
    ])
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True),
        patch("vivado_mcp.watch_engine.get_host_memory", side_effect=lambda: next(mem_samples)),
        patch("vivado_mcp.watch_engine.asyncio.sleep", new_callable=AsyncMock),
    ):
        res_mem = await watch_target_async(
            run_dir=rdir, stall_seconds=0, timeout_seconds=300, poll_seconds=1
        )
    assert res_mem.event == "memory_critical"
    assert res_mem.exit_code == 5
    assert res_mem.ram_pct == 95.0
    assert res_mem.ram_avail_gb == 2.0


# --------------------------------------------------------------------------- #
#  4. vivado-mcp watch CLI 子命令与 JSON/退出码
# --------------------------------------------------------------------------- #


def test_watch_cli_exit_codes_json_and_invalid_args(tmp_path, capsys):
    # Invalid args exit code 1 (never 2!)
    with pytest.raises(SystemExit) as exc:
        watch_main(["--until", "bogus"])
    assert exc.value.code == 1
    assert watch_main([]) == 1
    assert watch_main(["--run-dir", str(tmp_path / "missing")]) == 1

    # Completed run via --xpr --run impl_1 -> exit 0, compact JSON < 700 B
    proj_dir = tmp_path / "cli_proj"
    impl_dir = proj_dir / "cli_proj.runs" / "impl_1"
    impl_dir.mkdir(parents=True)
    xpr = _write_xpr(proj_dir / "cli_proj.xpr", part="xc7a35tcpg236-1", top="cli_top")
    (impl_dir / ".vivado.begin.rst").write_text("b", encoding="utf-8")
    (impl_dir / ".vivado.end.rst").write_text("e", encoding="utf-8")
    (impl_dir / "runme.log").write_text("Exiting Vivado...\n", encoding="utf-8")
    _write_timing_rpt(impl_dir / "route_timing_summary.rpt", wns=0.512)
    _write_util_rpt(impl_dir / "route_utilization.rpt", lut=1200)

    capsys.readouterr()
    rc = watch_main(["--xpr", str(xpr), "--run", "impl_1", "--json"])
    assert rc == 0
    out_line = capsys.readouterr().out.strip()
    assert "\n" not in out_line
    assert len(out_line.encode("utf-8")) < 700
    parsed = json.loads(out_line)
    assert parsed["event"] == "completed"
    assert parsed["summary"]["wns"] == 0.512
    assert parsed["summary"]["lut"] == 1200

    # Watching --job failed -> exit 2 & --pretty human output
    jobs_dir = tmp_path / "cli_jobs"
    jobs_dir.mkdir()
    (jobs_dir / "job_fail_1.json").write_text(
        json.dumps(
            {
                "job_id": "job_fail_1",
                "kind": "ooc_synth",
                "status": "failed",
                "top": "alu",
                "part": "xc7a35t",
                "error_message": "ERROR: [Synth 8-27] fail",
            }
        ),
        encoding="utf-8",
    )
    rc2 = watch_main(["--job", "job_fail_1", "--pretty"], jobs_dir=jobs_dir)
    assert rc2 == 2
    pretty_out = capsys.readouterr().out
    assert "FAILED" in pretty_out

    # Real subprocess smoke check via `python -m vivado_mcp watch`
    proc = subprocess.run(
        [sys.executable, "-m", "vivado_mcp", "watch", "--xpr", str(xpr), "--run", "impl_1"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    sub_json = json.loads(proc.stdout.strip())
    assert sub_json["exit_code"] == 0


# --------------------------------------------------------------------------- #
#  5. wait_run 心跳、早醒与完成签收摘要
# --------------------------------------------------------------------------- #


async def test_wait_run_10s_heartbeat_at_constant_40_pct_and_inline_summary(tmp_path):
    rdir = tmp_path / "wproj.runs" / "impl_1"
    rdir.mkdir(parents=True)
    (rdir / ".vivado.begin.rst").write_text('<Process Pid="8888"/>', encoding="utf-8")
    (rdir / "runme.log").write_text("Phase 2.1 Global Placement\n", encoding="utf-8")
    _write_timing_rpt(rdir / "route_timing_summary.rpt", wns=0.217, wpws=1.1)
    _write_util_rpt(rdir / "route_utilization.rpt", lut=4500, ff=6200, bram=12, dsp=8)

    statuses = iter([
        ("route_design Running...", "40%"),
        ("route_design Running...", "40%"),
        ("route_design Running...", "40%"),
        ("route_design Complete!", "100%"),
    ])

    session = AsyncMock()
    session.session_id = "default"
    session.state = SessionState.READY

    async def execute(cmd, timeout=None):
        if "VMCP_WAIT_META" in cmd:
            return _result(f"VMCP_WAIT_META:dir={rdir}\nVMCP_WAIT_META:part=xc7z020clg400-1")
        if "VMCP_POLL" in cmd:
            st, pct = next(statuses)
            return _result(f"VMCP_POLL|{st}|{pct}|00:01:00")
        if "VMCP_RUN_DONE" in cmd:
            return _result(
                "VMCP_RUN:status=route_design Complete!\nVMCP_RUN:progress=100%\n"
                f"VMCP_RUN:dir={rdir}\nVMCP_RUN:log_exists=1\nVMCP_RUN:log_size=100\n"
                f"VMCP_RUN:log_mtime={int(time.time())}\nVMCP_RUN:log_offset=0\n"
                "VMCP_RUN:total_lines=2\nVMCP_RUN_PHASE:1|Phase 4\n"
                "VMCP_RUN_TAIL:2|route_design completed successfully\nVMCP_RUN_DONE"
            )
        if "VMCP_HOOK" in cmd:
            return _result("VMCP_HOOK_DONE")
        return _result("")

    session.execute.side_effect = execute

    progress_events: list[tuple] = []

    async def fake_report_progress(progress, total=None, message=None):
        progress_events.append((progress, total, message))

    ctx = MagicMock()
    ctx.report_progress = fake_report_progress

    clock = _FakeClock(0.0)

    async def sleep_advance(_sec):
        clock.advance(12.0)

    with (
        patch("vivado_mcp.tools.flow_tools._require_session", return_value=session),
        patch("vivado_mcp.tools.flow_tools.time.monotonic", clock),
        patch("vivado_mcp.tools.flow_tools.asyncio.sleep", side_effect=sleep_advance),
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True),
        patch(
            "vivado_mcp.watch_engine.get_host_memory",
            return_value={"ram_total_gb": 64.0, "ram_pct": 52.0, "ram_avail_gb": 30.0},
        ),
    ):
        res = await wait_run(run_name="impl_1", until="route_design", poll_seconds=10, ctx=ctx)

    # PROGRESS 始终为 40% 时仍每 >=10s 发送心跳并附带 message
    assert len(progress_events) == 4
    assert [e[0] for e in progress_events] == [40.0, 40.0, 40.0, 100.0]
    assert all(e[2] and "RAM 52%" in e[2] for e in progress_events[:3])
    # 完成后自带 1 行紧凑签收摘要
    assert "签收摘要:" in res
    assert "WNS=+0.217ns" in res
    assert "LUT=4500" in res
    assert "DSP=8" in res


@pytest.mark.parametrize("case", ["early_error", "stalled", "memory_critical"])
async def test_wait_run_early_wake_events_never_cancel(tmp_path, case):
    rdir = tmp_path / f"{case}.runs" / "impl_1"
    rdir.mkdir(parents=True)
    (rdir / ".vivado.begin.rst").write_text('<Process Pid="9001"/>', encoding="utf-8")
    log_f = rdir / "runme.log"
    log_f.write_text(
        "ERROR: [Synth 8-27] bad rtl\n" if case == "early_error" else "Phase 3 Placement\n",
        encoding="utf-8",
    )

    session = AsyncMock()
    session.session_id = "default"
    session.state = SessionState.READY

    async def execute(cmd, timeout=None):
        if "VMCP_WAIT_META" in cmd:
            return _result(f"VMCP_WAIT_META:dir={rdir}\nVMCP_WAIT_META:part=xc7a35tcpg236-1")
        if "VMCP_POLL" in cmd:
            return _result("VMCP_POLL|route_design Running...|50%|00:01:00")
        if "VMCP_RUN_DONE" in cmd:
            return _result(
                "VMCP_RUN:status=route_design Running...\nVMCP_RUN:progress=50%\n"
                f"VMCP_RUN:dir={rdir}\nVMCP_RUN:log_exists=1\nVMCP_RUN:log_size=64\n"
                f"VMCP_RUN:log_mtime={int(time.time())}\nVMCP_RUN:log_offset=0\n"
                "VMCP_RUN:total_lines=1\nVMCP_RUN_TAIL:1|Phase 3\nVMCP_RUN_DONE"
            )
        if "VMCP_HOOK" in cmd:
            return _result("VMCP_HOOK_DONE")
        return _result("")

    session.execute.side_effect = execute
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()

    clock = _FakeClock(0.0)

    async def sleep_advance(_sec):
        clock.advance(200.0 if case == "stalled" else 5.0)

    mem_val = (
        {"ram_total_gb": 64.0, "ram_pct": 95.0, "ram_avail_gb": 2.0}
        if case == "memory_critical"
        else {"ram_total_gb": 64.0, "ram_pct": 40.0, "ram_avail_gb": 38.0}
    )

    with (
        patch("vivado_mcp.tools.flow_tools._require_session", return_value=session),
        patch("vivado_mcp.tools.flow_tools.time.monotonic", clock),
        patch("vivado_mcp.tools.flow_tools.asyncio.sleep", side_effect=sleep_advance),
        patch("vivado_mcp.watch_engine.sample_process_cpu_active", return_value=0.0),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=mem_val),
    ):
        res = await wait_run(
            run_name="impl_1",
            until="route_design",
            stall_seconds=120 if case == "stalled" else 0,
            poll_seconds=5,
            ctx=ctx,
        )

    assert "run 未取消" in res
    for call in session.execute.await_args_list:
        assert "stop_run" not in call.args[0]
        assert "reset_run" not in call.args[0]
    if case == "early_error":
        assert "提前捕获" in res and "Synth 8-27" in res
    elif case == "stalled":
        assert "疑似卡死" in res
    else:
        assert "主机内存危急" in res


# --------------------------------------------------------------------------- #
#  6. BatchJobRegistry.wait_job_ex & get_batch_job 适配
# --------------------------------------------------------------------------- #


async def test_batch_job_wait_includes_profile_and_early_error_without_killing(
    monkeypatch, tmp_path
):
    jobs_dir = tmp_path / "jobs"
    app_ctx = AppContext(
        session_manager=SessionManager(vivado_path="C:/Xilinx/Vivado/2024.2/bin/vivado.bat"),
        batch_jobs=batch_jobs.BatchJobRegistry(jobs_dir=jobs_dir),
    )
    progress_calls: list[tuple] = []

    async def fake_rp(progress, total=None, message=None):
        progress_calls.append((progress, total, message))

    req = SimpleNamespace(
        lifespan_context=app_ctx,
        session=SimpleNamespace(report_progress=fake_rp),
    )
    ctx = Context(request_context=req, mcp_server=mcp)
    monkeypatch.setattr(batch_jobs, "find_vivado", lambda p=None: "C:/Vivado/bin/vivado.bat")

    class _SlowProc:
        pid = 54321
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self._ev = asyncio.Event()

        async def wait(self):
            await self._ev.wait()
            return 0

        def terminate(self):
            self.returncode = -15
            self._ev.set()

        def kill(self):
            self.returncode = -9
            self._ev.set()

    proc = _SlowProc()

    async def fake_spawn(command, cwd, env):
        log_file = Path(command[command.index("-log") + 1])
        log_file.write_text(
            "Phase 1 Starting\nERROR: [Place 30-58] IO placement failed\n",
            encoding="utf-8",
        )
        return proc

    monkeypatch.setattr(app_ctx.batch_jobs, "_spawn_process", fake_spawn)
    started = json.loads(
        (
            await mcp.call_tool(
                "run_batch_tcl", {"tcl_content": "place_design", "wait": False}, ctx
            )
        ).content[0].text
    )
    jid = started["job_id"]

    res = json.loads(
        (
            await mcp.call_tool(
                "get_batch_job",
                {"job_id": jid, "wait_seconds": 30},
                ctx,
            )
        ).content[0].text
    )
    assert res["status"] == "running"
    assert res["watch_event"] == "early_error"
    assert "Place 30-58" in res["early_error_lines"][0]
    assert res["watch_profile"]["tier"] in {"small", "medium", "large"}
    assert proc.returncode is None  # 绝未杀死 Vivado

    proc.terminate()
    await app_ctx.batch_jobs.close()


async def test_batch_job_10s_heartbeat_and_timeout_returns_watch_profile(monkeypatch, tmp_path):
    jobs_dir = tmp_path / "jobs2"
    app_ctx = AppContext(
        session_manager=SessionManager(vivado_path="C:/Xilinx/Vivado/2024.2/bin/vivado.bat"),
        batch_jobs=batch_jobs.BatchJobRegistry(jobs_dir=jobs_dir),
    )
    progress_calls: list[tuple] = []

    async def fake_rp(progress, total=None, message=None):
        progress_calls.append((progress, total, message))

    req = SimpleNamespace(
        lifespan_context=app_ctx,
        session=SimpleNamespace(report_progress=fake_rp),
    )
    ctx = Context(request_context=req, mcp_server=mcp)
    monkeypatch.setattr(batch_jobs, "find_vivado", lambda p=None: "C:/Vivado/bin/vivado.bat")

    clock = _FakeClock(0.0)

    class _NeverExitProc:
        pid = 65432
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self._ev = asyncio.Event()

        async def wait(self):
            await self._ev.wait()
            return 0

        def terminate(self):
            self.returncode = -15
            self._ev.set()

        def kill(self):
            self.returncode = -9
            self._ev.set()

    proc = _NeverExitProc()

    async def fake_spawn(command, cwd, env):
        log_file = Path(command[command.index("-log") + 1])
        log_file.write_text("Phase 1.1 Starting\n", encoding="utf-8")
        return proc

    async def fake_wait_for(awaitable, timeout):
        # Close coroutine passed to wait_for to avoid unawaited warning, advance clock by 11s
        if hasattr(awaitable, "close"):
            awaitable.close()
        clock.advance(11.0)
        raise asyncio.TimeoutError

    monkeypatch.setattr(app_ctx.batch_jobs, "_spawn_process", fake_spawn)
    monkeypatch.setattr(batch_jobs.time, "monotonic", clock)
    monkeypatch.setattr(batch_jobs.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(
        watch_engine,
        "get_host_memory",
        lambda: {"ram_total_gb": 64.0, "ram_pct": 48.0, "ram_avail_gb": 33.0},
    )

    timed = json.loads(
        (
            await mcp.call_tool(
                "run_batch_tcl",
                {
                    "tcl_content": "after 1000",
                    "timeout_seconds": 30,
                    "stall_seconds": 0,
                    "wait": True,
                },
                ctx,
            )
        ).content[0].text
    )
    assert timed["status"] == "running"
    assert timed["wait_timed_out"] is True
    assert timed["watch_profile"]["tier"] in {"small", "medium", "large"}
    assert "自适应阈值" in timed["watch_profile"]["basis"]
    assert len(progress_calls) >= 2
    assert all(call[2] and "RAM 48%" in call[2] for call in progress_calls)
    assert proc.returncode is None

    proc.terminate()
    await app_ctx.batch_jobs.close()


def test_watch_cli_exits_3_4_5(tmp_path, capsys):
    rdir = tmp_path / "cli_events.runs" / "impl_1"
    rdir.mkdir(parents=True)
    (rdir / ".vivado.begin.rst").write_text('<Process Pid="9876"/>', encoding="utf-8")
    (rdir / "runme.log").write_text("Phase 4 Routing\n", encoding="utf-8")

    ok_mem = {"ram_total_gb": 64.0, "ram_pct": 40.0, "ram_avail_gb": 38.0}
    crit_mem = {"ram_total_gb": 64.0, "ram_pct": 94.0, "ram_avail_gb": 2.5}

    # Exit 3 (stalled)
    clock3 = _FakeClock(0.0)
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True),
        patch("vivado_mcp.watch_engine.sample_process_cpu_active", return_value=0.1),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=ok_mem),
        patch("vivado_mcp.watch_engine.time.monotonic", clock3),
        patch(
            "vivado_mcp.watch_engine.asyncio.sleep",
            side_effect=lambda _s: clock3.advance(125.0),
        ),
    ):
        rc3 = watch_main(["--run-dir", str(rdir), "--stall-seconds", "120", "--timeout", "300"])
    assert rc3 == 3
    j3 = json.loads(capsys.readouterr().out.strip())
    assert j3["event"] == "stalled" and j3["exit_code"] == 3

    # Exit 4 (timeout)
    clock4 = _FakeClock(0.0)
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=ok_mem),
        patch("vivado_mcp.watch_engine.time.monotonic", clock4),
        patch(
            "vivado_mcp.watch_engine.asyncio.sleep",
            side_effect=lambda _s: clock4.advance(20.0),
        ),
    ):
        rc4 = watch_main(["--run-dir", str(rdir), "--stall-seconds", "0", "--timeout", "15"])
    assert rc4 == 4
    j4 = json.loads(capsys.readouterr().out.strip())
    assert j4["event"] == "timeout" and j4["exit_code"] == 4

    # Exit 5 (memory_critical)
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=True),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=crit_mem),
        patch("vivado_mcp.watch_engine.asyncio.sleep", new_callable=AsyncMock),
    ):
        rc5 = watch_main(["--run-dir", str(rdir), "--stall-seconds", "0", "--timeout", "300"])
    assert rc5 == 5
    j5 = json.loads(capsys.readouterr().out.strip())
    assert j5["event"] == "memory_critical" and j5["exit_code"] == 5


def test_mcp_schema_remains_compact():
    tools = asyncio.run(mcp.list_tools())
    model_vis = [
        {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
        for t in tools
    ]
    encoded = json.dumps(model_vis, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= 42 * 1024


def test_p1_1_profile_ignores_incomplete_error_short_prev_and_unrelated_job(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "other.json").write_text(
        json.dumps({"job_id": "other", "status": "completed", "elapsed_sec": 6.0, "top": "other"}),
        encoding="utf-8",
    )

    proj = tmp_path / "k7_big"
    rdir = proj / "k7_big.runs" / "impl_1"
    rdir.mkdir(parents=True)
    xpr = _write_xpr(proj / "k7_big.xpr", part="xcku040-ffva1156-2-e", top="big_top")

    # Multi-session log: session 1 finished, session 2 still in progress -> ignore elapsed
    log_f = rdir / "runme.log"
    log_f.write_text(
        "# Start of session at: S1\n"
        "Time (s): cpu = 00:01:00 ; elapsed = 00:02:00\nExiting Vivado...\n"
        "# Start of session at: S2\nPhase 1 Starting\n",
        encoding="utf-8",
    )
    prof_multi = estimate_watch_profile(
        run_dir=rdir, xpr_path=xpr, run_name="impl_1", jobs_dir=jobs_dir
    )
    assert prof_multi.tier == "large"
    assert prof_multi.prev_elapsed_sec is None
    assert prof_multi.timeout_seconds == 1800

    # Completed log with ERROR line -> ignore elapsed
    log_f.write_text(
        "# Start of session at: S2\nERROR: [Place 30-1] fail\n"
        "Time (s): cpu = 00:00:04 ; elapsed = 00:00:05\nExiting Vivado...\n",
        encoding="utf-8",
    )
    prof_err = estimate_watch_profile(
        run_dir=rdir, xpr_path=xpr, run_name="impl_1", jobs_dir=jobs_dir
    )
    assert prof_err.prev_elapsed_sec is None
    assert prof_err.timeout_seconds == 1800

    # Short clean completed log (< 30s) -> must NOT shrink large base timeout below 1800s
    log_f.write_text(
        "# Start of session at: S3\n"
        "Time (s): cpu = 00:00:10 ; elapsed = 00:00:12\nExiting Vivado...\n",
        encoding="utf-8",
    )
    prof_short = estimate_watch_profile(
        run_dir=rdir, xpr_path=xpr, run_name="impl_1", jobs_dir=jobs_dir
    )
    assert prof_short.prev_elapsed_sec == 12.0
    assert prof_short.timeout_seconds == 1800


def test_p1_2_scan_log_ignores_stale_session_and_resets_on_truncation(tmp_path):
    rdir = tmp_path / "stale.runs" / "impl_1"
    rdir.mkdir(parents=True)
    log_f = rdir / "runme.log"
    log_f.write_text("# Start of session at: Old\nERROR: [Old 1-1] boom\n", encoding="utf-8")
    os.utime(log_f, (100.0, 100.0))

    tracker = watch_engine.LogWatchState(started_monotonic=0.0, stall_seconds=120)
    changed, new_errs = tracker.scan_log(log_f, 1.0, begin_mtime=200.0)
    assert not changed and new_errs == []
    assert tracker.initial_log_size is None

    # Rewritten log with prior failed session + fresh active session
    log_f.write_text(
        "# Start of session at: S1\nERROR: [S1-1] stale error\n"
        "# Start of session at: S2\nPhase 2 Placer\n",
        encoding="utf-8",
    )
    os.utime(log_f, (205.0, 205.0))
    changed, new_errs = tracker.scan_log(log_f, 2.0, begin_mtime=200.0)
    assert changed and new_errs == [] and tracker.fatal_errors == []
    assert tracker.latest_phase == "Phase 2 Placer"

    # Real new error in active session triggers fail-fast
    log_f.write_text(
        log_f.read_text(encoding="utf-8") + "ERROR: [Place 30-99] real err\n",
        encoding="utf-8",
    )
    os.utime(log_f, (206.0, 206.0))
    changed, new_errs = tracker.scan_log(log_f, 3.0, begin_mtime=200.0)
    assert changed and any("[Place 30-99]" in e for e in new_errs)

    # Truncating log resets initial_log_size & latest_phase cleanly
    log_f.write_text("Phase 1 Reset\n", encoding="utf-8")
    os.utime(log_f, (210.0, 210.0))
    tracker.scan_log(log_f, 4.0, begin_mtime=200.0)
    assert tracker.initial_log_size == 0
    assert tracker.latest_phase == "Phase 1 Reset"


async def test_p1_3_watch_ignores_stale_rst_and_refreshes_pid(tmp_path):
    rdir = tmp_path / "rerun.runs" / "impl_1"
    rdir.mkdir(parents=True)
    begin_f = rdir / ".vivado.begin.rst"
    step_end = rdir / ".route_design.end.rst"
    end_f = rdir / ".vivado.end.rst"
    log_f = rdir / "runme.log"

    step_end.write_text("stale step end", encoding="utf-8")
    end_f.write_text("stale end", encoding="utf-8")
    os.utime(step_end, (100.0, 100.0))
    os.utime(end_f, (100.0, 100.0))

    begin_f.write_text("<ProcessHandle/>", encoding="utf-8")
    os.utime(begin_f, (200.0, 200.0))
    log_f.write_text("Phase 1 Starting\n", encoding="utf-8")
    os.utime(log_f, (201.0, 201.0))

    clock = _FakeClock(0.0)
    seen_pids: list[int] = []
    tick = {"n": 0}

    def fake_alive(pid: int) -> bool:
        seen_pids.append(pid)
        return pid == 54321

    async def fake_sleep(_sec):
        tick["n"] += 1
        clock.advance(1.0)
        if tick["n"] == 1:
            begin_f.write_text('<Process Pid="54321"/>', encoding="utf-8")
            os.utime(begin_f, (200.0, 200.0))
        elif tick["n"] == 2:
            step_end.write_text("fresh route end", encoding="utf-8")
            os.utime(step_end, (205.0, 205.0))

    mem_ok = {"ram_total_gb": 64.0, "ram_pct": 40.0, "ram_avail_gb": 38.0}
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", side_effect=fake_alive),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=mem_ok),
        patch("vivado_mcp.watch_engine.time.monotonic", clock),
        patch("vivado_mcp.watch_engine.asyncio.sleep", side_effect=fake_sleep),
    ):
        res = await watch_target_async(
            run_dir=rdir, until="route_design", stall_seconds=0, timeout_seconds=60, poll_seconds=1
        )
    assert res.event == "reached" and res.exit_code == 0
    assert tick["n"] >= 2
    assert 54321 in seen_pids


def test_p1_4_sample_watched_cpu_checks_all_descendants():
    cpu_by_pid = {100: 0.2, 1001: 0.1, 1002: 87.5}
    raw = ({1001: 100, 1002: 100}, [{"pid": 1001}, {"pid": 1002}])
    with (
        patch.object(watch_engine.sys, "platform", "win32"),
        patch("vivado_mcp.system_telemetry._raw_eda_process_list", return_value=raw),
        patch(
            "vivado_mcp.watch_engine.sample_process_cpu_active",
            side_effect=lambda p: cpu_by_pid.get(p),
        ),
    ):
        assert watch_engine._sample_watched_cpu_pct(100) == 87.5


async def test_p1_5_job_mode_dead_pid_waits_for_live_owner_json_writeback(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    log_f = jobs_dir / "j1.log"
    log_f.write_text("Phase 4 Routing\nExiting Vivado...\n", encoding="utf-8")
    jfile = jobs_dir / "j1.json"
    jfile.write_text(
        json.dumps({
            "job_id": "j1",
            "status": "running",
            "pid": 44444,
            "owner_pid": 99999,
            "log_path": str(log_f),
            "started_at": "2026-11-24T00:00:00Z",
        }),
        encoding="utf-8",
    )

    clock = _FakeClock(0.0)
    polls = {"n": 0}

    async def fake_sleep(sec):
        polls["n"] += 1
        clock.advance(max(sec, 1.0))
        if polls["n"] >= 3:
            jfile.write_text(
                json.dumps({
                    "job_id": "j1",
                    "status": "completed",
                    "returncode": 0,
                    "pid": 44444,
                    "owner_pid": 99999,
                    "log_path": str(log_f),
                }),
                encoding="utf-8",
            )

    mem_ok = {"ram_total_gb": 64.0, "ram_pct": 40.0, "ram_avail_gb": 38.0}
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", side_effect=lambda p: p == 99999),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=mem_ok),
        patch("vivado_mcp.watch_engine.time.monotonic", clock),
        patch("vivado_mcp.watch_engine.asyncio.sleep", side_effect=fake_sleep),
    ):
        res = await watch_target_async(
            job_id="j1", jobs_dir=jobs_dir, stall_seconds=0, timeout_seconds=30, poll_seconds=1
        )
    assert res.event == "completed" and res.exit_code == 0

    # Owner dead + Vivado dead -> silent_exit after >= 1.5s
    jfile.write_text(
        json.dumps({
            "job_id": "j1",
            "status": "running",
            "pid": 44444,
            "owner_pid": 99999,
            "log_path": str(log_f),
        }),
        encoding="utf-8",
    )
    clock2 = _FakeClock(0.0)
    with (
        patch("vivado_mcp.watch_engine.is_pid_alive", return_value=False),
        patch("vivado_mcp.watch_engine.get_host_memory", return_value=mem_ok),
        patch("vivado_mcp.watch_engine.time.monotonic", clock2),
        patch("vivado_mcp.watch_engine.asyncio.sleep", side_effect=lambda s: clock2.advance(1.0)),
    ):
        res2 = await watch_target_async(
            job_id="j1", jobs_dir=jobs_dir, stall_seconds=0, timeout_seconds=30, poll_seconds=1
        )
    assert res2.event == "silent_exit" and res2.exit_code == 2


def test_p2_watch_cli_timeout_zero_rejected(tmp_path):
    rdir = tmp_path / "p2.runs" / "impl_1"
    rdir.mkdir(parents=True)
    assert watch_main(["--run-dir", str(rdir), "--timeout", "0"]) == 1


async def test_p2_wait_run_elapsed_and_batch_tail(tmp_path, monkeypatch):
    rdir = tmp_path / "p2.runs" / "impl_1"
    rdir.mkdir(parents=True)

    # wait_run terminal heartbeat reports elapsed > 0
    progress_msgs: list[str] = []

    session = AsyncMock()
    session.session_id = "default"
    session.state = SessionState.READY

    async def execute(cmd, timeout=None):
        if "VMCP_WAIT_META" in cmd:
            return _result(f"VMCP_WAIT_META:dir={rdir}\nVMCP_WAIT_META:part=xc7a35t")
        if "VMCP_POLL" in cmd:
            return _result("VMCP_POLL|route_design Complete!|100%|00:00:14")
        if "VMCP_RUN_DONE" in cmd:
            return _result(
                "VMCP_RUN:status=route_design Complete!\nVMCP_RUN:progress=100%\n"
                f"VMCP_RUN:dir={rdir}\nVMCP_RUN:log_exists=0\nVMCP_RUN:log_size=0\n"
                "VMCP_RUN:log_mtime=0\nVMCP_RUN:log_offset=0\nVMCP_RUN:total_lines=0\n"
                "VMCP_RUN_DONE"
            )
        if "VMCP_HOOK" in cmd:
            return _result("VMCP_HOOK_DONE")
        return _result("")

    session.execute.side_effect = execute
    ctx = MagicMock()
    clock = _FakeClock(100.0)

    async def fake_report(_ctx, *, progress, total=100, message=None):
        if message:
            progress_msgs.append(message)

    mono_calls = {"n": 0}

    def step_clock() -> float:
        mono_calls["n"] += 1
        return 100.0 if mono_calls["n"] == 1 else 114.2

    with (
        patch("vivado_mcp.tools.flow_tools._require_session", return_value=session),
        patch("vivado_mcp.tools.flow_tools.report_mcp_progress", side_effect=fake_report),
        patch("vivado_mcp.tools.flow_tools.time.monotonic", side_effect=step_clock),
        patch(
            "vivado_mcp.watch_engine.get_host_memory",
            return_value={"ram_total_gb": 64.0, "ram_pct": 40.0, "ram_avail_gb": 38.0},
        ),
    ):
        out = await wait_run("impl_1", until="route_design", timeout_seconds=60, ctx=ctx)
    assert "route_design Complete!" in out
    assert progress_msgs and "14s" in progress_msgs[-1]

    # wait_job_ex refreshes log_tail before returning early_error
    reg = batch_jobs.BatchJobRegistry(jobs_dir=tmp_path / "bjobs")
    log_f = tmp_path / "bjobs" / "job_tail.log"
    log_f.parent.mkdir(parents=True, exist_ok=True)
    log_f.write_text("ERROR: [Synth 8-99] first line\n", encoding="utf-8")
    job = batch_jobs.BatchJob(
        job_id="job_tail",
        kind="batch_tcl",
        status="running",
        started_at=time.time(),
        pid=os.getpid(),
        log_path=str(log_f),
        tcl_path=str(tmp_path / "a.tcl"),
    )
    (tmp_path / "bjobs" / "job_tail.json").write_text(
        json.dumps(job.to_dict()), encoding="utf-8"
    )
    reg._ensure_loaded()
    reg._jobs["job_tail"] = job
    reg._processes["job_tail"] = MagicMock()
    ev = asyncio.Event()
    reg._events["job_tail"] = ev

    async def fake_wait_for(awaitable, timeout=0.0):
        log_f.write_text(
            "ERROR: [Synth 8-99] first line\nERROR: [Synth 8-100] settled tail\n",
            encoding="utf-8",
        )
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(batch_jobs.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(batch_jobs.time, "monotonic", clock)
    res_job, timed_out, alert = await reg.wait_job_ex(
        "job_tail", timeout_seconds=30, stall_seconds=0
    )
    assert not timed_out and alert["event"] == "early_error"
    assert any(
        "settled tail" in (ln.get("text", "") if isinstance(ln, dict) else str(ln))
        for ln in res_job.log_tail
    )
    await reg.close()
