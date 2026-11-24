"""托管 Batch Tcl、独立 OOC 综合与离线目录/工程摘要测试（全模拟，不启动 Vivado）。"""

from __future__ import annotations

import asyncio
import collections
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver import Context

from vivado_mcp import batch_jobs
from vivado_mcp.server import AppContext, mcp
from vivado_mcp.vivado.session_manager import SessionManager

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeBatchProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        delay: float = 0.0,
        pid: int = 43210,
        on_start=None,
        stdout_lines: list[bytes] | None = None,
        stderr_lines: list[bytes] | None = None,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._target_rc = returncode
        self._delay = delay
        self._on_start = on_start
        self._cancelled = asyncio.Event()
        self.stdout = _FakeStream(stdout_lines or [])
        self.stderr = _FakeStream(stderr_lines or [])
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        if self._on_start is not None:
            self._on_start()
        if self._delay > 0:
            try:
                await asyncio.wait_for(self._cancelled.wait(), timeout=self._delay)
            except asyncio.TimeoutError:
                pass
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._target_rc
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._cancelled.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._cancelled.set()


def _make_context(monkeypatch, tmp_path: Path) -> tuple[Context, AppContext]:
    jobs_dir = tmp_path / "jobs"
    manager = SessionManager(vivado_path="C:/Xilinx/Vivado/2024.2/bin/vivado.bat")
    app_ctx = AppContext(
        session_manager=manager,
        batch_jobs=batch_jobs.BatchJobRegistry(jobs_dir=jobs_dir),
    )
    req = SimpleNamespace(lifespan_context=app_ctx)
    ctx = Context(request_context=req, mcp_server=mcp)
    return ctx, app_ctx


async def _call_tool_json(name: str, args: dict, ctx: Context) -> dict:
    res = await mcp.call_tool(name, args, context=ctx)
    assert not res.is_error
    return json.loads(res.content[0].text)


def test_build_ooc_synth_tcl_deterministic_and_validated(tmp_path):
    sv = tmp_path / "top.sv"
    v = tmp_path / "sub.v"
    vhd = tmp_path / "pkg.vhd"
    xci = tmp_path / "fifo.xci"
    inc = tmp_path / "inc"
    inc.mkdir()
    for f in (sv, v, vhd, xci):
        f.write_text("// stub\n", encoding="utf-8")

    out_dir = tmp_path / "out"
    job_dir = tmp_path / "job"
    tcl_text, xdc_text, resolved = batch_jobs.build_ooc_synth_tcl(
        top="my_top",
        part="xc7a35tcpg236-1",
        files=[str(sv), str(v), str(vhd), str(xci)],
        include_dirs=[str(inc)],
        defines={"WIDTH": 16, "SYNTH": None},
        generics={"DEPTH": 64},
        clock_port="clk",
        clock_period_ns=10.0,
        synth_args=["-flatten_hierarchy", "none"],
        output_dir=out_dir,
        job_dir=job_dir,
    )
    assert len(resolved) == 4
    assert xdc_text == "create_clock -name ooc_clk -period 10 [get_ports {clk}]\n"
    assert "read_verilog -sv " in tcl_text
    assert "read_verilog " in tcl_text
    assert "read_vhdl " in tcl_text
    assert "read_ip " in tcl_text
    assert "ooc_clock.xdc" in tcl_text
    assert (
        "synth_design -top my_top -part xc7a35tcpg236-1 -mode out_of_context"
        in tcl_text
    )
    assert '-verilog_define "WIDTH=16"' in tcl_text
    assert '-verilog_define "SYNTH"' in tcl_text
    assert '-generic "DEPTH=64"' in tcl_text
    assert "-flatten_hierarchy none" in tcl_text
    assert "report_timing_summary -file " in tcl_text
    assert "report_utilization -file " in tcl_text

    # 确定性校验：同样参数生成完全相同文本
    tcl2, xdc2, _ = batch_jobs.build_ooc_synth_tcl(
        top="my_top",
        part="xc7a35tcpg236-1",
        files=[str(sv), str(v), str(vhd), str(xci)],
        include_dirs=[str(inc)],
        defines={"WIDTH": 16, "SYNTH": None},
        generics={"DEPTH": 64},
        clock_port="clk",
        clock_period_ns=10.0,
        synth_args=["-flatten_hierarchy", "none"],
        output_dir=out_dir,
        job_dir=job_dir,
    )
    assert tcl_text == tcl2 and xdc_text == xdc2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top": "bad;puts hi", "part": "xc7a35t", "files": ["a.sv"]},
        {"top": "ok", "part": "bad part", "files": ["a.sv"]},
        {"top": "ok", "part": "xc7a35t", "files": []},
        {"top": "ok", "part": "xc7a35t", "files": ["a.sv"], "clock_port": "clk"},
        {
            "top": "ok",
            "part": "xc7a35t",
            "files": ["a.sv"],
            "clock_port": "clk;rm",
            "clock_period_ns": 5.0,
        },
        {
            "top": "ok",
            "part": "xc7a35t",
            "files": ["a.sv"],
            "synth_args": ["-directive", "Explore;exit"],
        },
    ],
)
def test_build_ooc_synth_rejects_invalid_inputs(tmp_path, kwargs):
    sv = tmp_path / "a.sv"
    sv.write_text("module a; endmodule\n", encoding="utf-8")
    if kwargs.get("files") == ["a.sv"]:
        kwargs = {**kwargs, "files": [str(sv)]}
    with pytest.raises(ValueError):
        batch_jobs.build_ooc_synth_tcl(output_dir=tmp_path, job_dir=tmp_path, **kwargs)


async def test_run_batch_tcl_validates_xor_and_non_ascii(monkeypatch, tmp_path):
    ctx, app_ctx = _make_context(monkeypatch, tmp_path)
    monkeypatch.setattr(batch_jobs, "find_vivado", lambda p=None: "C:/Vivado/bin/vivado.bat")

    both = await _call_tool_json(
        "run_batch_tcl", {"tcl_file": "a.tcl", "tcl_content": "puts 1"}, ctx
    )
    assert "error" in both

    neither = await _call_tool_json("run_batch_tcl", {}, ctx)
    assert "error" in neither

    bad_dir = tmp_path / "中文目录"
    bad_dir.mkdir()
    bad_tcl = bad_dir / "test.tcl"
    bad_tcl.write_text("puts hi\n", encoding="utf-8")

    spawn_spy = AsyncMock(side_effect=AssertionError("绝不能在非 ASCII 路径启动 Vivado"))
    monkeypatch.setattr(app_ctx.batch_jobs, "_spawn_process", spawn_spy)

    non_ascii = await _call_tool_json(
        "run_batch_tcl",
        {"tcl_file": str(bad_tcl), "output_dir": str(tmp_path / "ascii_out")},
        ctx,
    )
    assert "ASCII" in non_ascii["error"]
    spawn_spy.assert_not_called()
    await app_ctx.batch_jobs.close()


async def test_run_ooc_synth_mocked_execution_and_compact_output(monkeypatch, tmp_path):
    ctx, app_ctx = _make_context(monkeypatch, tmp_path)
    rtl = tmp_path / "alu.sv"
    rtl.write_text("module alu(input clk); endmodule\n", encoding="utf-8")

    monkeypatch.setattr(
        batch_jobs, "find_vivado", lambda p=None: "C:/Xilinx/Vivado/2024.2/bin/vivado.bat"
    )

    timing_sample = "| Design State : Synthesized\n" + (
        FIXTURES / "sample_report_timing.txt"
    ).read_text(encoding="utf-8")
    util_sample = (FIXTURES / "sample_report_utilization.txt").read_text(encoding="utf-8")

    recorded_cmds: list[list[str]] = []

    async def fake_spawn(command, cwd, env):
        recorded_cmds.append(command)
        log_idx = command.index("-log") + 1
        log_file = Path(command[log_idx])
        out_dir = Path(cwd)
        # 构造模拟 Vivado 输出：5 条重复 CW + 2 条不同 CW + Phase + 报告
        (out_dir / "timing_summary.rpt").write_text(timing_sample, encoding="utf-8")
        (out_dir / "utilization.rpt").write_text(util_sample, encoding="utf-8")
        log_lines = [
            "****** Vivado v2024.2 (64-bit)",
            "Starting synth_design",
            "Phase 1 RTL Elaboration",
            *[
                f"CRITICAL WARNING: [Synth 8-3848] Net p_{i} in module alu does not have driver."
                for i in range(5)
            ],
            "CRITICAL WARNING: [Timing 38-282] The design failed to meet the timing requirements.",
            "WARNING: [Synth 8-3331] design alu has unconnected port dbg",
            "synth_design completed successfully",
            "Exiting Vivado...",
        ]
        log_file.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return _FakeBatchProcess(returncode=0)

    monkeypatch.setattr(app_ctx.batch_jobs, "_spawn_process", fake_spawn)

    raw_res = await mcp.call_tool(
        "run_ooc_synth",
        {
            "top": "alu",
            "part": "xc7a35tcpg236-1",
            "files": [str(rtl)],
            "clock_port": "clk",
            "clock_period_ns": 10.0,
            "wait": True,
        },
        context=ctx,
    )
    assert not raw_res.is_error
    payload_text = raw_res.content[0].text
    assert len(payload_text.encode("utf-8")) < 2048

    data = json.loads(payload_text)
    assert data["status"] == "completed"
    assert data["exit_code"] == 0
    assert data["kind"] == "ooc_synth"
    assert data["top"] == "alu"
    assert data["part"] == "xc7a35tcpg236-1"
    assert data["timing"]["wns"] == 0.234
    assert data["timing"]["whs"] == 0.045
    assert data["timing"]["met"] is True
    assert data["timing"]["stage"] == "post-synth"
    assert data["utilization"]["lut"] == {"used": 1440, "avail": 20800, "pct": 6.92}
    assert data["utilization"]["ff"] == {"used": 2200, "avail": 41600, "pct": 5.29}
    assert data["utilization"]["bram"] == {"used": 12, "avail": 50, "pct": 24.0}
    assert data["utilization"]["dsp"] == {"used": 8, "avail": 90, "pct": 8.89}
    assert data["diagnostics"]["errors"] == 0
    assert data["diagnostics"]["critical_warnings"] == 6
    assert data["diagnostics"]["warnings"] == 1
    assert len(data["diagnostics"]["top_issues"]) == 2
    assert data["diagnostics"]["top_issues"][0]["id"] == "Synth 8-3848"
    assert data["diagnostics"]["top_issues"][0]["count"] == 5

    # 报告清单只保留名称和路径，不含报告原文
    report_names = {r["name"] for r in data["reports"]}
    assert report_names >= {"timing_summary.rpt", "utilization.rpt", "vivado.log"}
    assert all(set(r) == {"name", "path"} for r in data["reports"])
    assert "Intra Clock Table" not in payload_text

    # 验证命令标志与持久化 JSON 位于 jobs 目录
    cmd = recorded_cmds[0]
    assert cmd[1:5] == ["-mode", "batch", "-nojournal", "-notrace"]
    job_dir = Path(data["output_dir"])
    assert job_dir.parent == app_ctx.batch_jobs.jobs_dir
    assert (job_dir / "ooc_synth.tcl").is_file()
    assert (job_dir / "ooc_clock.xdc").is_file()
    assert (app_ctx.batch_jobs.jobs_dir / f"{data['job_id']}.json").is_file()

    # 无时序约束 NA 摘要不伪造 0
    na_dir = tmp_path / "na_case"
    na_dir.mkdir()
    (na_dir / "timing.rpt").write_text(
        "WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints "
        "WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints\n"
        "------- ------- --------------------- ------------------- "
        "------- ------- --------------------- -------------------\n"
        "NA NA 0 0 NA NA 0 0\n",
        encoding="utf-8",
    )
    na_info = batch_jobs.inspect_offline_dir(na_dir)
    assert na_info["timing"]["parse_status"] == "no_timing_data"
    assert na_info["timing"]["wns"] is None
    assert na_info["timing"]["met"] is None

    await app_ctx.batch_jobs.close()


async def test_timeout_does_not_kill_vivado_and_resumes_or_cancels(monkeypatch, tmp_path):
    progress_calls = []

    async def fake_report_progress(progress, total=None, message=None):
        progress_calls.append((progress, total, message))

    app_ctx = AppContext(
        session_manager=SessionManager(vivado_path="C:/Xilinx/Vivado/2024.2/bin/vivado.bat"),
        batch_jobs=batch_jobs.BatchJobRegistry(jobs_dir=tmp_path / "jobs"),
    )
    req = SimpleNamespace(
        lifespan_context=app_ctx,
        session=SimpleNamespace(report_progress=fake_report_progress),
    )
    ctx = Context(request_context=req, mcp_server=mcp)

    monkeypatch.setattr(
        batch_jobs, "find_vivado", lambda p=None: "C:/Xilinx/Vivado/2024.2/bin/vivado.bat"
    )

    slow_proc = _FakeBatchProcess(returncode=0, delay=1.6)
    cancel_proc = _FakeBatchProcess(returncode=0, delay=30.0)
    procs = [slow_proc, cancel_proc]

    async def fake_spawn(command, cwd, env):
        p = procs.pop(0)
        log_file = Path(command[command.index("-log") + 1])
        log_file.write_text("Phase 1 Starting\n", encoding="utf-8")
        return p

    monkeypatch.setattr(app_ctx.batch_jobs, "_spawn_process", fake_spawn)

    # 1. 超时不杀进程，返回 running + wait_timed_out=True
    timed = await _call_tool_json(
        "run_batch_tcl",
        {"tcl_content": "puts running", "timeout_seconds": 1, "wait": True},
        ctx,
    )
    assert timed["status"] == "running"
    assert timed["wait_timed_out"] is True
    assert slow_proc.returncode is None
    assert not slow_proc.terminated
    assert progress_calls  # 期间有进度上报

    # 2. 用 get_batch_job(wait_seconds=5) 继续在服务端等待完成
    finished = await _call_tool_json(
        "get_batch_job",
        {"job_id": timed["job_id"], "wait_seconds": 5, "log_tail_lines": 10},
        ctx,
    )
    assert finished["status"] == "completed"
    assert finished["exit_code"] == 0
    assert isinstance(finished["log_tail"], list)

    # 3. 列表模式 & cancel=True 终止运行中任务
    started2 = await _call_tool_json(
        "run_batch_tcl", {"tcl_content": "after 30000", "wait": False}, ctx
    )
    assert started2["status"] == "running"

    listed = await _call_tool_json("get_batch_job", {}, ctx)
    assert listed["count"] == 2
    assert {j["job_id"] for j in listed["jobs"]} == {timed["job_id"], started2["job_id"]}

    monkeypatch.setattr(
        batch_jobs,
        "_terminate_process_tree",
        AsyncMock(side_effect=lambda p: p.terminate()),
    )
    cancelled = await _call_tool_json(
        "get_batch_job", {"job_id": started2["job_id"], "cancel": True}, ctx
    )
    assert cancelled["status"] == "cancelled"
    assert cancel_proc.terminated is True

    await app_ctx.batch_jobs.close()


async def test_failed_job_triggers_silent_exit_forensics(monkeypatch, tmp_path):
    ctx, app_ctx = _make_context(monkeypatch, tmp_path)
    monkeypatch.setattr(
        batch_jobs, "find_vivado", lambda p=None: "C:/Xilinx/Vivado/2024.2/bin/vivado.bat"
    )
    monkeypatch.setattr(
        batch_jobs, "query_windows_crash_events", lambda *a, **kw: ("ok", "", "recent", [])
    )

    async def fake_spawn(command, cwd, env):
        log_file = Path(command[command.index("-log") + 1])
        log_file.write_text("synth_design completed successfully\n", encoding="utf-8")
        return _FakeBatchProcess(returncode=1)

    monkeypatch.setattr(app_ctx.batch_jobs, "_spawn_process", fake_spawn)
    res = await _call_tool_json("run_batch_tcl", {"tcl_content": "synth_design", "wait": True}, ctx)
    assert res["status"] == "failed"
    assert res["exit_code"] == 1
    assert res["forensics"]["silent_exit_suspected"] is True
    await app_ctx.batch_jobs.close()


def test_inspect_offline_dir_xpr_and_runs_precedence(tmp_path):
    proj_dir = tmp_path / "demo_proj"
    proj_dir.mkdir()
    xpr = proj_dir / "demo_proj.xpr"
    xpr.write_text(
        '<Project Version="7" Minor="60">'
        '<Configuration><Option Name="Part" Val="xczu7ev-ffvc1156-2-e"/></Configuration>'
        '<FileSets><FileSet Name="sources_1" Type="DesignSrcs">'
        '<Config><Option Name="TopModule" Val="isp_top"/></Config></FileSet></FileSets>'
        '<Runs><Run Id="synth_1" Type="Ft3:Synth"/>'
        '<Run Id="impl_1" Type="Ft2:EntireDesign" SynthRun="synth_1"/></Runs>'
        "</Project>",
        encoding="utf-8",
    )
    runs_dir = proj_dir / "demo_proj.runs"
    synth_dir = runs_dir / "synth_1"
    impl_dir = runs_dir / "impl_1"
    synth_dir.mkdir(parents=True)
    impl_dir.mkdir(parents=True)

    timing_base = (FIXTURES / "sample_report_timing.txt").read_text(encoding="utf-8")
    (synth_dir / "timing_synth.rpt").write_text(
        "| Design State : Synthesized\n" + timing_base.replace("0.234", "1.500"),
        encoding="utf-8",
    )
    (impl_dir / "timing_routed.rpt").write_text(
        "| Design State : Routed\n" + timing_base.replace("0.234", "-0.412"),
        encoding="utf-8",
    )
    (impl_dir / "utilization_placed.rpt").write_text(
        (FIXTURES / "sample_report_utilization.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (impl_dir / ".vivado.begin.rst").write_text("", encoding="utf-8")
    (impl_dir / ".vivado.end.rst").write_text("", encoding="utf-8")
    (impl_dir / "runme.log").write_text(
        "CRITICAL WARNING: [Timing 38-282] setup failed\nExiting Vivado...\n",
        encoding="utf-8",
    )

    from_xpr = batch_jobs.inspect_offline_dir(xpr)
    assert from_xpr["kind"] == "xpr_project"
    assert from_xpr["project"] == "demo_proj"
    assert from_xpr["top"] == "isp_top"
    assert from_xpr["part"] == "xczu7ev-ffvc1156-2-e"
    assert from_xpr["status"] == "completed"
    # post-route 结果优先于 post-synth 估算
    assert from_xpr["timing"]["stage"] == "post-route"
    assert from_xpr["timing"]["wns"] == -0.412
    assert from_xpr["timing"]["met"] is False
    assert from_xpr["utilization"]["lut"]["used"] == 1440
    assert from_xpr["diagnostics"]["critical_warnings"] == 1

    from_runs = batch_jobs.inspect_offline_dir(runs_dir)
    assert from_runs["kind"] == "runs_dir"
    assert from_runs["timing"]["wns"] == -0.412
    assert from_runs["timing"]["stage"] == "post-route"


async def test_registry_persistence_reload_and_shutdown_cleanup(monkeypatch, tmp_path):
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr(
        batch_jobs, "find_vivado", lambda p=None: "C:/Xilinx/Vivado/2024.2/bin/vivado.bat"
    )
    monkeypatch.setattr(
        batch_jobs,
        "_terminate_process_tree",
        AsyncMock(side_effect=lambda p: p.terminate()),
    )

    reg1 = batch_jobs.BatchJobRegistry(jobs_dir=jobs_dir)
    done_proc = _FakeBatchProcess(returncode=0)
    run_proc = _FakeBatchProcess(returncode=0, delay=30.0)
    procs = [done_proc, run_proc]

    async def fake_spawn(command, cwd, env):
        return procs.pop(0)

    monkeypatch.setattr(reg1, "_spawn_process", fake_spawn)
    j1 = await reg1.start_batch_tcl(tcl_content="puts ok")
    await reg1.wait_job(j1.job_id, 5)
    assert j1.status == "completed"

    j2 = await reg1.start_batch_tcl(tcl_content="after 30000")
    assert j2.status == "running"

    # 模拟遗留一个非本进程运行的持久化 running JSON
    orphan_meta = jobs_dir / "orphan_1.json"
    orphan_meta.write_text(
        json.dumps(
            {
                "job_id": "orphan_1",
                "kind": "batch_tcl",
                "status": "running",
                "started_at": 100.0,
            }
        ),
        encoding="utf-8",
    )

    # shutdown 应优雅终止运行中任务
    await reg1.close()
    assert run_proc.terminated is True
    assert j2.status == "cancelled"

    # 新实例加载持久化记录：completed 保留，孤儿 running 转 failed
    reg2 = batch_jobs.BatchJobRegistry(jobs_dir=jobs_dir)
    reloaded = {j["job_id"]: j for j in reg2.list_jobs()}
    assert reloaded[j1.job_id]["status"] == "completed"
    assert reloaded[j2.job_id]["status"] == "cancelled"
    assert reloaded["orphan_1"]["status"] == "failed"


async def test_start_ooc_synth_creates_nonexistent_output_dir(monkeypatch, tmp_path):
    ctx, app_ctx = _make_context(monkeypatch, tmp_path)
    src = tmp_path / "rtl" / "mac.sv"
    src.parent.mkdir()
    src.write_text("module mac; endmodule\n", encoding="utf-8")
    nested_out = tmp_path / "nested" / "ooc_out"
    assert not nested_out.exists()

    monkeypatch.setattr(
        batch_jobs, "find_vivado", lambda p=None: "C:/Xilinx/Vivado/2024.2/bin/vivado.bat"
    )

    async def fake_spawn(command, cwd, env):
        assert Path(cwd) == nested_out.resolve()
        assert nested_out.is_dir()
        return _FakeBatchProcess(returncode=0)

    monkeypatch.setattr(app_ctx.batch_jobs, "_spawn_process", fake_spawn)
    res = await _call_tool_json(
        "run_ooc_synth",
        {
            "top": "mac",
            "part": "xc7a35tcsg324-1",
            "files": [str(src)],
            "output_dir": str(nested_out),
            "wait": True,
        },
        ctx,
    )
    assert res["status"] == "completed"
    assert nested_out.is_dir()
    await app_ctx.batch_jobs.close()


def test_build_ooc_synth_tcl_rejects_non_ascii_source_and_include(tmp_path):
    bad_rtl = tmp_path / "中文模块.sv"
    bad_rtl.write_text("module top; endmodule\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ASCII"):
        batch_jobs.build_ooc_synth_tcl(
            top="top",
            part="xc7a35tcsg324-1",
            files=[str(bad_rtl)],
            output_dir=tmp_path / "out",
            job_dir=tmp_path / "job",
        )

    good_rtl = tmp_path / "top.sv"
    good_rtl.write_text("module top; endmodule\n", encoding="utf-8")
    bad_inc = tmp_path / "头文件目录"
    bad_inc.mkdir()
    with pytest.raises(ValueError, match="ASCII"):
        batch_jobs.build_ooc_synth_tcl(
            top="top",
            part="xc7a35tcsg324-1",
            files=[str(good_rtl)],
            include_dirs=[str(bad_inc)],
            output_dir=tmp_path / "out",
            job_dir=tmp_path / "job",
        )


async def test_drain_stream_handles_oversized_line_without_deadlock(tmp_path):
    reg = batch_jobs.BatchJobRegistry(jobs_dir=tmp_path / "jobs")
    reader = asyncio.StreamReader(limit=65536)
    reader.feed_data(b"A" * 100000 + b"\nDONE_LINE\n")
    reader.feed_eof()

    ring: collections.deque[str] = collections.deque(maxlen=20)
    await asyncio.wait_for(reg._drain_stream(reader, ring), timeout=3.0)
    joined = "\n".join(ring)
    assert "[line truncated]" in joined
    assert "DONE_LINE" in joined


def test_utf8_tail_seek_skips_continuation_bytes(tmp_path):
    log_path = tmp_path / "utf8_tail.log"
    chunk = "阶段状态：综合完成，无时序违例。\n".encode("utf-8")
    # 写入大于 64 KiB 的 UTF-8 日志并人为让尾部偏移落在多字节续字节上
    payload = b"X" * 2 + chunk * 2000
    log_path.write_bytes(payload)

    text, truncated = batch_jobs._read_text_bounded(log_path, 4096)
    assert truncated is True
    assert "阶段状态" in text
    assert "\ufffd" not in text

    phases, tail = batch_jobs._read_log_tail(log_path, max_lines=5)
    assert len(tail) == 5
    assert "综合完成" in tail[-1]["text"]


def test_inspect_offline_dir_prioritizes_impl_dedups_vivado_log_and_detects_crash(tmp_path):
    runs_dir = tmp_path / "demo.runs"
    ip_dir = runs_dir / "aaa_clk_wiz_0_synth_1"
    synth_dir = runs_dir / "synth_1"
    impl_dir = runs_dir / "impl_1"
    for d in (ip_dir, synth_dir, impl_dir):
        d.mkdir(parents=True)

    (ip_dir / "runme.log").write_text("IP synth complete\n", encoding="utf-8")
    (synth_dir / "runme.log").write_text("Synth complete\n", encoding="utf-8")
    # impl_1 同时存在 runme.log 与重复 vivado.log，且无 .vivado.error.rst 但含崩溃关键字
    crash_log = (
        "CRITICAL WARNING: [Timing 38-282] setup violation on clk\n"
        "Abnormal program termination: Vivado access violation\n"
    )
    (impl_dir / "runme.log").write_text(crash_log, encoding="utf-8")
    (impl_dir / "vivado.log").write_text(crash_log, encoding="utf-8")

    artifacts = batch_jobs._collect_artifact_files(runs_dir)
    rels = [rel for _, rel in artifacts]
    assert rels.index("impl_1/runme.log") < rels.index("synth_1/runme.log")
    assert rels.index("synth_1/runme.log") < rels.index("aaa_clk_wiz_0_synth_1/runme.log")

    info = batch_jobs.inspect_offline_dir(runs_dir)
    # vivado.log 与 runme.log 同目录时不双计关键告警
    assert info["diagnostics"]["critical_warnings"] == 1
    assert info["status"] == "failed"
    assert any(it["category"] == "NONSTANDARD_ERROR" for it in info["diagnostics"]["top_issues"])


def test_registry_list_inspected_threadsafe_snapshot_and_removal(tmp_path):
    target_a = tmp_path / "dir_a"
    target_b = tmp_path / "dir_b"
    target_a.mkdir()
    target_b.mkdir()
    reg = batch_jobs.BatchJobRegistry(jobs_dir=tmp_path / "jobs")
    reg.inspect_and_remember(str(target_a))
    reg.inspect_and_remember(str(target_b))
    assert reg.remove_inspected(str(target_a)) is True
    listed = reg.list_inspected()
    assert len(listed) == 1
    assert Path(listed[0]["target_path"]) == target_b.resolve()


def test_batch_registry_hot_syncs_remote_jobs_and_tracks_local_owner(monkeypatch, tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True)
    reg = batch_jobs.BatchJobRegistry(jobs_dir=jobs_dir)
    # 首次加载空目录
    assert reg.list_jobs() == []

    remote_log = tmp_path / "remote_run" / "vivado.log"
    remote_log.parent.mkdir()
    remote_log.write_text(
        "Vivado v2024.2\nPhase 1.1 Placer Initialization\nPhase 2.1 Global Placement\n",
        encoding="utf-8",
    )

    remote_owner_pid = 777111
    remote_viv_pid = 777222
    monkeypatch.setattr(
        batch_jobs,
        "is_pid_alive",
        lambda pid: pid in {remote_owner_pid, remote_viv_pid},
    )

    remote_job_file = jobs_dir / "tcl_remote_1.json"
    remote_job_file.write_text(
        json.dumps(
            {
                "job_id": "tcl_remote_1",
                "kind": "batch_tcl",
                "status": "running",
                "started_at": 100.0,
                "pid": remote_viv_pid,
                "owner_pid": remote_owner_pid,
                "client": "Codex",
                "top": "remote_top",
                "part": "xc7a35tcsg324-1",
                "log_path": str(remote_log),
                "output_dir": str(remote_log.parent),
            }
        ),
        encoding="utf-8",
    )

    listed = reg.list_jobs()
    assert len(listed) == 1
    rj = listed[0]
    assert rj["job_id"] == "tcl_remote_1"
    assert rj["owner_pid"] == remote_owner_pid
    assert rj["client"] == "Codex"
    assert rj["is_local_owner"] is False
    assert rj["status"] == "running"
    assert "Global Placement" in rj["current_phase"]
    assert rj["elapsed_sec"] is not None and rj["elapsed_sec"] > 0
    assert len(rj["log_tail"]) == 3
    # 跨进程作业输出目录自动进入白名单根目录
    assert remote_log.parent.resolve() in reg.allowed_report_roots()

    # 未变更的文件不再重复解析 JSON（即使文件仅读 cache）
    orig_loads = json.loads
    load_calls = 0

    def counting_loads(text, *args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        return orig_loads(text, *args, **kwargs)

    monkeypatch.setattr(batch_jobs.json, "loads", counting_loads)
    listed2 = reg.list_jobs()
    assert len(listed2) == 1
    assert load_calls == 0

    # 远程属主和 Vivado 均退出且 JSON 未刷新时，自动降级为 failed
    monkeypatch.setattr(batch_jobs, "is_pid_alive", lambda pid: False)
    listed3 = reg.list_jobs()
    assert listed3[0]["status"] == "failed"


async def test_get_build_summary_xpr_signoff_and_cross_agent_overview(monkeypatch, tmp_path):
    ctx, app_ctx = _make_context(monkeypatch, tmp_path)

    proj_dir = tmp_path / "fpga_proj"
    synth_dir = proj_dir / "fpga.runs" / "synth_1"
    impl_dir = proj_dir / "fpga.runs" / "impl_1"
    synth_dir.mkdir(parents=True)
    impl_dir.mkdir(parents=True)

    xpr_path = proj_dir / "fpga.xpr"
    xpr_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Project Version="7" Minor="39">\n'
        '  <DefaultLaunch Dir="$PRUNDIR"/>\n'
        '  <Configuration><Option Name="Part" Val="xc7k325tffg900-2"/></Configuration>\n'
        '  <FileSets><FileSet Name="sources_1" Type="DesignSrcs">\n'
        '    <Config><Option Name="TopModule" Val="signoff_top"/></Config>\n'
        '  </FileSet></FileSets>\n'
        '  <Runs>\n'
        '    <Run Id="synth_1" Type="Ft3:Synth" SrcSet="sources_1"/>\n'
        '    <Run Id="impl_1" Type="Ft2:EntireDesign" SrcSet="sources_1" SynthRun="synth_1"/>\n'
        '  </Runs>\n'
        '</Project>\n',
        encoding="utf-8",
    )

    (synth_dir / "runme.log").write_text(
        "WARNING: [Synth 8-3848] Net foo_undriven in module/entity top does not have driver.\n"
        "WARNING: [Synth 8-3848] Net bar_undriven in module/entity top does not have driver.\n",
        encoding="utf-8",
    )
    (impl_dir / ".vivado.begin.rst").write_text("b", encoding="utf-8")
    (impl_dir / ".vivado.end.rst").write_text("e", encoding="utf-8")
    (impl_dir / "runme.log").write_text(
        "Vivado v2024.2\n"
        "CRITICAL WARNING: [Constraints 18-5210] Minor constraint note.\n"
        "Phase 4.1 Global Routing\n",
        encoding="utf-8",
    )
    (impl_dir / "route_timing_summary.rpt").write_text(
        "Design State : Routed\n"
        "-----------------------------------------------------------------------------------\n"
        "| Design Timing Summary\n"
        "| ---------------------\n"
        "-----------------------------------------------------------------------------------\n\n"
        "    WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints"
        "  WHS(ns)  THS(ns)  THS Failing Endpoints  THS Total Endpoints"
        "  WPWS(ns)  TPWS(ns)  TPWS Failing Endpoints  TPWS Total Endpoints\n"
        "    -------  -------  ---------------------  -------------------"
        "  -------  -------  ---------------------  -------------------"
        "  --------  --------  ----------------------  --------------------\n"
        "      0.415    0.000                      0                 4200"
        "    0.088    0.000                      0                 4200"
        "     1.750     0.000                       0                  1500\n",
        encoding="utf-8",
    )
    (impl_dir / "route_utilization.rpt").write_text(
        "+-------------------------+------+-------+-----------+-------+\n"
        "|        Site Type        | Used | Fixed | Available | Util% |\n"
        "+-------------------------+------+-------+-----------+-------+\n"
        "| Slice LUTs              | 4500 |     0 |    203800 |  2.21 |\n"
        "| Slice Registers         | 6200 |     0 |    407600 |  1.52 |\n"
        "| Block RAM Tile          |   12 |     0 |       445 |  2.70 |\n"
        "| URAM                    |    2 |     0 |        16 | 12.50 |\n"
        "| DSPs                    |    8 |     0 |       840 |  0.95 |\n"
        "+-------------------------+------+-------+-----------+-------+\n",
        encoding="utf-8",
    )
    (impl_dir / "route_power.rpt").write_text(
        "| Total On-Chip Power (W)  | 1.842 |\n",
        encoding="utf-8",
    )
    (impl_dir / "route_drc.rpt").write_text(
        "2. REPORT DETAILS\nERROR: [DRC NSTD-1] Unspecified I/O Standard\n",
        encoding="utf-8",
    )
    (impl_dir / "signoff_top.bit").write_bytes(b"BIT")
    (impl_dir / "debug_nets.ltx").write_text("{}", encoding="utf-8")
    (proj_dir / "signoff_top.xsa").write_bytes(b"XSA")

    res = await mcp.call_tool(
        "get_build_summary",
        {"target": str(xpr_path), "run_name": "impl_1"},
        context=ctx,
    )
    raw_json = res.content[0].text
    assert len(raw_json.encode("utf-8")) < 700
    data = json.loads(raw_json)
    assert data["status"] == "completed"  # .vivado.end.rst + 日志零 ERROR
    assert data["top"] == "signoff_top"
    assert data["part"] == "xc7k325tffg900-2"
    assert data["timing"]["wns"] == 0.415
    assert data["timing"]["wpws"] == 1.75
    assert data["timing"]["met"] is True
    assert data["utilization"]["lut"]["used"] == 4500
    assert data["utilization"]["uram"]["used"] == 2
    assert data["diagnostics"]["cw"] == 1
    assert data["diagnostics"]["synth_8_3848"] == 2
    assert data["diagnostics"]["drc_errors"] == 1
    assert data["power_w"] == 1.842
    assert data["artifacts"]["bit"] is not None and data["artifacts"]["bit"].endswith(".bit")
    assert data["artifacts"]["ltx"] is not None and data["artifacts"]["ltx"].endswith(".ltx")
    assert data["artifacts"]["xsa"] is not None and data["artifacts"]["xsa"].endswith(".xsa")

    # 缺失指标的空目录绝不伪造 0
    empty_dir = tmp_path / "empty_ooc"
    empty_dir.mkdir()
    res_empty = await mcp.call_tool(
        "get_build_summary", {"target": str(empty_dir)}, context=ctx
    )
    empty_data = json.loads(res_empty.content[0].text)
    assert empty_data["timing"]["wns"] is None
    assert empty_data["timing"]["wpws"] is None
    assert empty_data["utilization"]["uram"] is None
    assert empty_data["diagnostics"]["errors"] is None
    assert empty_data["diagnostics"]["synth_8_3848"] is None
    assert empty_data["diagnostics"]["drc_errors"] is None
    assert empty_data["power_w"] is None
    assert empty_data["artifacts"] == {"bit": None, "ltx": None, "xsa": None}

    # 无 target 返回 < 1KB 跨 Agent 总览
    app_ctx.shared_registry.upsert_session(
        "default",
        mode="tcl",
        project_name="signoff_top",
        run_name="impl_1",
        status="running",
        progress=65.0,
    )
    overview_res = await mcp.call_tool("get_build_summary", {}, context=ctx)
    overview_raw = overview_res.content[0].text
    assert len(overview_raw.encode("utf-8")) < 1024
    overview = json.loads(overview_raw)
    assert "ram" in overview and overview["ram"]["pct"] is not None
    assert "vivado" in overview
    assert len(overview["runs"]) >= 1
    assert overview["runs"][0]["proj"] == "signoff_top"

    # get_build_summary() 传入已知 session/job PID，不将活着的受管 Vivado 误判为 orphan
    import os

    from vivado_mcp import system_telemetry

    fake_raw = [
        {
            "pid": 90001,
            "parent_pid": os.getpid(),
            "name": "vivado.exe",
            "rss_mb": 1024.0,
            "uptime_sec": 30.0,
            "command_hint": "vivado.exe -mode tcl",
            "killable": True,
        }
    ]
    monkeypatch.setattr(
        system_telemetry,
        "_raw_eda_process_list",
        lambda: ({90001: os.getpid()}, fake_raw),
    )
    app_ctx.session_manager.list_sessions = AsyncMock(
        return_value=[
            {
                "session_id": "s_tcl",
                "mode": "tcl",
                "state": "ready",
                "is_alive": True,
                "pid": 90001,
            }
        ]
    )
    overview_no_orphan = json.loads(
        (await mcp.call_tool("get_build_summary", {}, context=ctx)).content[0].text
    )
    assert overview_no_orphan["vivado"] == 1
    assert overview_no_orphan["orphans"] == 0
