"""长任务轮询回归：真实进度、目标步骤和只读观察边界。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.tools.flow_tools import (
    _poll_run_until_done,
    generate_bitstream,
    run_implementation,
    run_synthesis,
)
from vivado_mcp.vivado.base_session import SessionState
from vivado_mcp.vivado.session import SubprocessSession
from vivado_mcp.vivado.tcl_utils import TclResult


def _result(output: str, error: bool = False) -> TclResult:
    return TclResult(output=output, return_code=int(error), is_error=error)


def _context():
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx


@pytest.mark.parametrize("status", ["route_design ERROR", "route_design Cancelled"])
async def test_terminal_failure_keeps_reported_progress(status):
    session = AsyncMock()
    session.execute.return_value = _result(f"VMCP_POLL|{status}|37.5%|00:01:00")
    ctx = _context()

    result = await _poll_run_until_done(session, "impl_1", 10, ctx, "route_design")

    assert result == ("done", status, "37.5%", "00:01:00")
    ctx.report_progress.assert_awaited_once_with(progress=37.5, total=100)


@pytest.mark.parametrize("progress", ["", "unknown", "120%", "NaN%", "100"])
async def test_completed_status_does_not_invent_unknown_percentage(progress):
    session = AsyncMock()
    session.execute.return_value = _result(f"VMCP_POLL|synth_design Complete!|{progress}|")
    ctx = _context()

    result = await _poll_run_until_done(session, "synth_1", 10, ctx, "synth_design")

    assert result[0] == "done"
    assert result[2] == progress
    ctx.report_progress.assert_not_awaited()


@pytest.mark.parametrize("earlier_step", ["place_design", "route_design", "synth_design"])
async def test_waits_for_bitstream_target_after_other_stage_completes(earlier_step):
    session = AsyncMock()
    session.execute.side_effect = [
        _result(f"VMCP_POLL|{earlier_step} Complete!|75%|00:02:00"),
        _result("VMCP_POLL|write_bitstream Complete!|100%|00:03:00"),
    ]
    ctx = _context()

    with patch("vivado_mcp.tools.flow_tools.asyncio.sleep", new_callable=AsyncMock):
        result = await _poll_run_until_done(session, "impl_1", 10, ctx, "write_bitstream")

    assert result[1] == "write_bitstream Complete!"
    assert session.execute.await_count == 2
    assert [c.kwargs["progress"] for c in ctx.report_progress.await_args_list] == [75, 100]
    for call in session.execute.await_args_list:
        assert "reset_run" not in call.args[0]
        assert "stop_run" not in call.args[0]
        assert "launch_runs" not in call.args[0]


@pytest.mark.parametrize(
    "response",
    [
        _result("VMCP_POLL|write_bitstream Complete!|100%|", error=True),
        _result("INFO: run query returned no marker"),
        _result("VMCP_POLL|write_bitstream Complete!"),
        _result("VMCP_POLL||100%|"),
    ],
)
async def test_query_error_or_missing_fields_never_produce_success(response):
    session = AsyncMock()
    session.execute.return_value = response
    ctx = _context()

    with pytest.raises(RuntimeError):
        await _poll_run_until_done(session, "impl_1", 10, ctx, "write_bitstream")

    ctx.report_progress.assert_not_awaited()
    assert session.execute.await_count == 1


async def test_timeout_keeps_unknown_progress_and_does_not_cancel_run():
    session = AsyncMock()
    session.execute.return_value = _result("VMCP_POLL|route_design Running||")
    ctx = _context()

    with (
        patch("vivado_mcp.tools.flow_tools.time.monotonic", side_effect=[0, 0, 11]),
        patch("vivado_mcp.tools.flow_tools.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await _poll_run_until_done(session, "impl_1", 10, ctx, "route_design")

    assert result == ("timeout", "route_design Running", "", "")
    ctx.report_progress.assert_not_awaited()
    assert session.execute.await_count == 1
    assert "stop_run" not in session.execute.await_args.args[0]


@pytest.mark.parametrize(
    ("tool", "target"), [(run_synthesis, "synth_design"), (run_implementation, "route_design")]
)
async def test_flow_entry_points_pass_explicit_target(tool, target):
    with (
        patch("vivado_mcp.tools.flow_tools._require_session", return_value=AsyncMock()),
        patch("vivado_mcp.tools.flow_tools._launch_and_wait", new_callable=AsyncMock) as launch,
    ):
        await tool(ctx=_context())

    assert launch.await_args.kwargs["target_step"] == target


async def test_bitstream_entry_point_waits_past_route_completion():
    session = AsyncMock()
    session.execute.side_effect = [
        _result(""),
        _result("VMCP_POLL|route_design Complete!|100%|00:02:00"),
        _result("VMCP_POLL|write_bitstream Complete!|100%|00:03:00"),
        _result("VMCP_BITDIR:/tmp/example.runs/impl_1"),
    ]

    with (
        patch("vivado_mcp.tools.flow_tools._require_session", return_value=session),
        patch("vivado_mcp.tools.flow_tools.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await generate_bitstream(force=True, ctx=_context())

    assert "状态: write_bitstream Complete!" in result
    assert "比特流目录: /tmp/example.runs/impl_1" in result
    assert session.execute.await_count == 4


async def test_bitstream_cancellation_does_not_fetch_success_artifacts():
    session = AsyncMock()
    session.execute.side_effect = [
        _result(""),
        _result("VMCP_POLL|write_bitstream Cancelled|12%|00:00:03"),
    ]

    with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
        result = await generate_bitstream(force=True, ctx=_context())

    assert result.startswith("[ERROR]")
    assert "比特流目录" not in result
    assert session.execute.await_count == 2


@pytest.mark.parametrize("release_before_deadline", [True, False])
async def test_real_session_inflight_observation_does_not_interrupt_flow_wait(
    monkeypatch, release_before_deadline,
):
    """复用真实 session 的锁与 shield，模拟观察查询超时后迟到的响应。"""
    session = SubprocessSession("/fake/vivado", "observed")
    session._process = MagicMock(returncode=None)
    session._state = SessionState.READY
    release_observer = asyncio.Event()
    commands = []

    async def execute_impl(command):
        commands.append(command)
        if command == "observer query":
            await release_observer.wait()
            return _result("VMCP_RUN:status=route_design Running\nVMCP_RUN_DONE")
        return _result("VMCP_POLL|route_design Complete!|100%|00:10:00")

    session._execute_impl = execute_impl
    monkeypatch.setattr("vivado_mcp.tools.flow_tools._POLL_INTERVAL_SEC", 0.001)
    with pytest.raises(asyncio.TimeoutError):
        await session.execute("observer query", timeout=0.001)
    inflight = session._inflight_task
    assert inflight is not None
    assert session.state == SessionState.BUSY
    ctx = _context()
    poll = asyncio.create_task(
        _poll_run_until_done(
            session, "impl_1", 1 if release_before_deadline else 0.01, ctx, "route_design"
        )
    )
    try:
        await asyncio.sleep(0.005)
        assert commands == ["observer query"]
        assert not inflight.cancelled()
        if release_before_deadline:
            assert not poll.done()
            release_observer.set()
        result = await asyncio.wait_for(poll, timeout=1)
        if release_before_deadline:
            assert result == ("done", "route_design Complete!", "100%", "00:10:00")
            assert len(commands) == 2
            ctx.report_progress.assert_awaited_once_with(progress=100, total=100)
        else:
            assert result == ("timeout", "UNKNOWN", "", "")
            assert commands == ["observer query"]
            assert not inflight.done()
            ctx.report_progress.assert_not_awaited()
    finally:
        release_observer.set()
        await asyncio.wait_for(asyncio.shield(inflight), timeout=1)
        if not poll.done():
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)


async def test_busy_race_after_status_check_is_retried(monkeypatch):
    """抢锁间隙进入 BUSY 时允许重试，而不是结束原构建的观察。"""
    session = AsyncMock()
    session.is_alive = True
    session.state = SessionState.READY

    async def execute(*args, **kwargs):
        if session.execute.await_count == 1:
            session.state = SessionState.BUSY
            asyncio.get_running_loop().call_soon(setattr, session, "state", SessionState.READY)
            raise RuntimeError("上一条命令仍在执行")
        return _result("VMCP_POLL|synth_design Complete!|100%|")

    session.execute.side_effect = execute
    monkeypatch.setattr("vivado_mcp.tools.flow_tools._POLL_INTERVAL_SEC", 0.001)
    result = await _poll_run_until_done(session, "synth_1", 1, _context(), "synth_design")
    assert result[0] == "done"
    assert session.execute.await_count == 2


@pytest.mark.parametrize("exception", [RuntimeError("Tcl failed"), ConnectionError("disconnected")])
async def test_real_query_failures_are_not_hidden_by_busy_retry(exception):
    session = AsyncMock()
    session.state = SessionState.READY
    session.execute.side_effect = exception
    with pytest.raises(type(exception), match=str(exception)):
        await _poll_run_until_done(session, "impl_1", 1, _context(), "route_design")
    assert session.execute.await_count == 1


async def test_dead_session_with_busy_state_is_reported_as_error():
    session = SubprocessSession("/fake/vivado", "dead")
    session._process = MagicMock(returncode=1)
    session._state = SessionState.BUSY
    with pytest.raises(RuntimeError, match="未运行"):
        await _poll_run_until_done(session, "impl_1", 1, _context(), "route_design")
