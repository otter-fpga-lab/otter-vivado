"""构建反馈：静默退出说明、钩子告警、wait_run 与异步比特流；全部 mock，不启动 Vivado。"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.run_feedback import classify_hooks, evaluate_wait, status_step
from vivado_mcp.tools.flow_tools import generate_bitstream, wait_run
from vivado_mcp.tools.report_tools import get_run_progress
from vivado_mcp.vivado.base_session import SessionState
from vivado_mcp.vivado.session import SubprocessSession
from vivado_mcp.vivado.tcl_utils import TclResult


def _result(output: str, error: bool = False) -> TclResult:
    return TclResult(output=output, return_code=int(error), is_error=error)


def _progress_output(status: str, tail: list[str]) -> str:
    lines = [
        f"VMCP_RUN:status={status}",
        "VMCP_RUN:progress=100%",
        "VMCP_RUN:dir=C:/proj/demo.runs/impl_1",
        "VMCP_RUN:log_exists=1",
        "VMCP_RUN:log_size=2048",
        f"VMCP_RUN:log_mtime={int(time.time())}",
        "VMCP_RUN:log_offset=0",
        f"VMCP_RUN:total_lines={len(tail) + 1}",
        "VMCP_RUN_PHASE:1|Starting Routing Task",
    ]
    lines += [f"VMCP_RUN_TAIL:{i + 2}|{text}" for i, text in enumerate(tail)]
    lines.append("VMCP_RUN_DONE")
    return "\n".join(lines)


_SILENT_TAIL = [
    "route_design completed successfully",
    "INFO: [runtcl-4] Executing : report_power -file demo_power_routed.rpt",
]
_HOOKS_CLEAN = "VMCP_HOOK_PROJDIR:C:/proj\nVMCP_HOOK_DONE"
_HOOKS_BAD = "\n".join([
    "VMCP_HOOK_PROJDIR:C:/proj",
    "VMCP_HOOK_TEMP:C:/Users/me/AppData/Local/Temp",
    "VMCP_HOOK:STEPS.ROUTE_DESIGN.TCL.PRE|0|C:/Users/me/AppData/Local/Temp/pre_route.tcl",
    "VMCP_HOOK:STEPS.WRITE_BITSTREAM.TCL.POST|1|C:/proj/scripts/post.tcl",
    "VMCP_HOOK:STEPS.OPT_DESIGN.TCL.PRE|1|D:/other/opt.tcl",
    "VMCP_HOOK_DONE",
])


def _session(
    *, status="route_design ERROR", tail=None, diag="errors=0,critical_warnings=2,warnings=9",
    hooks=_HOOKS_CLEAN, polls=None,
):
    """按命令内容分派的会话替身。"""
    tail = _SILENT_TAIL if tail is None else tail
    poll_iter = iter(polls or [])
    session = AsyncMock()
    session.session_id = "default"
    session.state = SessionState.READY

    async def execute(command, timeout=None):
        if "VMCP_POLL" in command:
            return _result(f"VMCP_POLL|{next(poll_iter)}|50%|00:01:00")
        if "VMCP_RUN_DONE" in command:
            return _result(_progress_output(status, tail))
        if "VMCP_HOOK" in command:
            return _result(hooks)
        if "VMCP_DIAG" in command:
            return _result(f"VMCP_DIAG:{diag}")
        if "VMCP_TAIL_DONE" in command:
            body = "\n".join(f"VMCP_TAIL_LINE:{i + 1}|{t}" for i, t in enumerate(tail))
            return _result(f"VMCP_TAIL:status={status}\nVMCP_TAIL:total={len(tail)}\n"
                           f"{body}\nVMCP_TAIL_DONE")
        return _result("")

    session.execute.side_effect = execute
    return session


def _ctx():
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx


# --------------------------------------------------------------------------- #
#  get_run_progress：静默退出与钩子
# --------------------------------------------------------------------------- #


async def test_silent_child_exit_is_named_with_inline_action():
    session = _session()
    with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
        result = await get_run_progress(ctx=_ctx())
    headline = result.splitlines()[0]
    assert "子进程静默退出" in headline
    assert "route_design completed successfully" in headline
    assert headline != "=== impl_1 运行进度: 失败 ==="
    assert "runme.log ERROR=0 / CRITICAL WARNING=2" in result
    assert "run_implementation(run_name='impl_1', session_id='default', wait=False)" in result
    assert "reset_run -prev_step" in result
    assert "wait_run(run_name='impl_1', until='route_design'" in result


async def test_real_errors_keep_default_advice_without_tail_scan():
    session = _session(diag="errors=3,critical_warnings=0,warnings=1")
    with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
        result = await get_run_progress(ctx=_ctx())
    assert "子进程静默退出" not in result
    assert "get_critical_warnings(run_name='impl_1')" in result
    assert not any("VMCP_TAIL_DONE" in c.args[0] for c in session.execute.await_args_list)


async def test_running_run_skips_failure_diagnosis():
    session = _session(status="route_design Running...", tail=["Phase 4 Rip-up"])
    with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
        result = await get_run_progress(ctx=_ctx())
    assert "运行进度: 运行中 ===" in result
    assert not any("VMCP_DIAG" in c.args[0] for c in session.execute.await_args_list)


async def test_hook_warnings_suggest_clearing_without_modifying():
    session = _session(status="route_design Complete!", tail=["done"], hooks=_HOOKS_BAD)
    with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
        result = await get_run_progress(ctx=_ctx())
    assert "钩子告警" in result
    assert "STEPS.ROUTE_DESIGN.TCL.PRE" in result
    assert "set_property STEPS.ROUTE_DESIGN.TCL.PRE {} [get_runs impl_1]" in result
    assert "set_property STEPS.OPT_DESIGN.TCL.PRE {} [get_runs impl_1]" in result
    assert "STEPS.WRITE_BITSTREAM.TCL.POST" not in result
    for call in session.execute.await_args_list:
        assert "set_property" not in call.args[0]
        assert "reset_run" not in call.args[0]


def test_classify_hooks_reasons():
    issues = {i.prop: i.reasons for i in classify_hooks(_HOOKS_BAD)}
    assert set(issues) == {"STEPS.ROUTE_DESIGN.TCL.PRE", "STEPS.OPT_DESIGN.TCL.PRE"}
    assert "文件不存在" in issues["STEPS.ROUTE_DESIGN.TCL.PRE"]
    assert "位于 %TEMP% 临时目录" in issues["STEPS.ROUTE_DESIGN.TCL.PRE"]
    assert issues["STEPS.OPT_DESIGN.TCL.PRE"][0].startswith("位于工程目录外")


async def test_hook_query_failure_degrades_visibly():
    session = _session(status="route_design Complete!", tail=["done"],
                       hooks="VMCP_HOOK_ERROR:invalid command name list_property")
    with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
        result = await get_run_progress(ctx=_ctx())
    assert "[DEGRADED] 未能检查 impl_1 的 TCL.PRE/POST 钩子" in result


# --------------------------------------------------------------------------- #
#  wait_run 判定
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status,until,verdict", [
    ("route_design Complete!", "route_design", "reached"),
    ("Running write_bitstream...", "route_design", "reached"),
    ("post_route_phys_opt_design Running", "route_design", "reached"),
    ("write_bitstream ERROR", "route_design", "reached"),
    ("route_design Running...", "route_design", "running"),
    ("place_design Complete!", "route_design", "settled"),
    ("route_design ERROR", "route_design", "failed"),
    ("route_design ERROR", "write_bitstream", "failed"),
    ("synth_design Complete!", "complete", "reached"),
    ("write_bitstream Complete!", "complete", "reached"),
    ("route_design Complete!", "complete", "settled"),
    ("Queued...", "complete", "running"),
    ("Not started", "complete", "idle"),
    ("write_bitstream Cancelled", "complete", "cancelled"),
    ("post_place_power_opt_design Complete!", "power_opt_design", "reached"),
    ("phys_opt_design Running", "post_place_power_opt_design", "reached"),
])
def test_evaluate_wait(status, until, verdict):
    assert evaluate_wait(status, until) == verdict


def test_step_names_do_not_match_inside_longer_steps():
    assert status_step("post_place_power_opt_design Complete!") == "post_place_power_opt_design"
    assert status_step("post_route_phys_opt_design Running") == "post_route_phys_opt_design"


# --------------------------------------------------------------------------- #
#  wait_run 工具
# --------------------------------------------------------------------------- #


async def _call_wait(session, **kwargs):
    sleep = AsyncMock()
    with (
        patch("vivado_mcp.tools.flow_tools._require_session", return_value=session),
        patch("vivado_mcp.tools.flow_tools.asyncio.sleep", sleep),
    ):
        result = await wait_run(ctx=_ctx(), **kwargs)
    return result, sleep


async def test_wait_run_returns_when_target_step_reached():
    session = _session(status="route_design Complete!", tail=["done"], polls=[
        "place_design Running...", "route_design Running...", "route_design Complete!",
    ])
    result, sleep = await _call_wait(session, until="route_design", poll_seconds=15)
    assert result.startswith("=== wait_run: impl_1 until=route_design → 已到达 route_design ===")
    assert "查询 3 次" in result
    assert "=== impl_1 运行进度: 已完成 ===" in result
    assert [c.args[0] for c in sleep.await_args_list] == [15, 15]
    for call in session.execute.await_args_list:
        assert "launch_runs" not in call.args[0]
        assert "reset_run" not in call.args[0]


async def test_wait_run_confirms_stopped_state_before_returning():
    session = _session(status="route_design Complete!", tail=["done"], polls=[
        "route_design Complete!", "route_design Complete!",
    ])
    result, sleep = await _call_wait(session, until="write_bitstream")
    assert "→ 已停止，未到达目标" in result
    assert "未到达 write_bitstream" in result
    assert [c.args[0] for c in sleep.await_args_list] == [5.0]


async def test_wait_run_complete_after_route_confirmation():
    session = _session(status="route_design Complete!", tail=["done"], polls=[
        "route_design Complete!", "route_design Complete!",
    ])
    result, _ = await _call_wait(session)
    assert "until=complete → 已完成" in result


async def test_wait_run_failure_includes_silent_exit_report():
    session = _session(polls=["route_design ERROR"])
    result, sleep = await _call_wait(session)
    assert "→ 失败 ===" in result
    assert "子进程静默退出" in result
    sleep.assert_not_awaited()


async def test_wait_run_timeout_does_not_cancel_run():
    session = _session(status="route_design Running...", tail=["Phase 4"], polls=[
        "route_design Running...",
    ])
    with patch("vivado_mcp.tools.flow_tools.time.monotonic", side_effect=[0, 61, 61]):
        result, _ = await _call_wait(session, timeout_seconds=60)
    assert "等待超时（run 未取消）" in result
    assert "可再次 wait_run(run_name='impl_1', until='complete'" in result
    for call in session.execute.await_args_list:
        assert "stop_run" not in call.args[0]


@pytest.mark.parametrize("kwargs,message", [
    ({"until": "bitstream"}, "until 必须是"),
    ({"timeout_seconds": 1801}, "timeout_seconds 必须在 1~1800"),
    ({"timeout_seconds": 0}, "timeout_seconds 必须在 1~1800"),
    ({"poll_seconds": 1}, "poll_seconds 必须在 2~300"),
    ({"run_name": "impl 1"}, "[ERROR]"),
])
async def test_wait_run_rejects_invalid_arguments(kwargs, message):
    session = _session()
    result, _ = await _call_wait(session, **kwargs)
    assert result.startswith("[ERROR]")
    assert message in result
    session.execute.assert_not_awaited()


async def test_wait_run_does_not_hold_session_lock_between_polls(monkeypatch):
    """两次查询之间的 sleep 期间，其他命令能在同一会话上执行。"""
    session = SubprocessSession("/fake/vivado", "waiter")
    session._process = MagicMock(returncode=None)
    session._state = SessionState.READY
    statuses = iter(["route_design Running...", "route_design Complete!"])
    executed = []

    async def execute_impl(command):
        executed.append(command)
        if "VMCP_POLL" in command:
            return _result(f"VMCP_POLL|{next(statuses)}|90%|")
        if "VMCP_RUN_DONE" in command:
            return _result(_progress_output("route_design Complete!", ["done"]))
        if "VMCP_HOOK" in command:
            return _result(_HOOKS_CLEAN)
        return _result("other ok")

    session._execute_impl = execute_impl
    interleaved = []

    async def sleep(_seconds):
        result = await asyncio.wait_for(session.execute("puts other", timeout=1), timeout=1)
        interleaved.append(result.output)

    with (
        patch("vivado_mcp.tools.flow_tools._require_session", return_value=session),
        patch("vivado_mcp.tools.flow_tools.asyncio.sleep", sleep),
    ):
        result = await wait_run(until="route_design", ctx=_ctx())
    assert interleaved == ["other ok"]
    assert "已到达 route_design" in result


# --------------------------------------------------------------------------- #
#  generate_bitstream(wait=False)
# --------------------------------------------------------------------------- #


async def test_generate_bitstream_async_returns_after_launch():
    session = AsyncMock()
    session.session_id = "default"
    session.execute.side_effect = [_result("")]
    with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
        result = await generate_bitstream(force=True, wait=False, ctx=_ctx())
    assert "比特流生成已异步启动" in result
    assert "job_id: default:impl_1" in result
    assert "wait_run(run_name='impl_1', until='write_bitstream', session_id='default')" in result
    assert session.execute.await_count == 1
    assert "launch_runs impl_1 -to_step write_bitstream" in session.execute.await_args.args[0]


async def test_generate_bitstream_async_keeps_cw_gate():
    session = AsyncMock()
    session.session_id = "default"
    session.execute.side_effect = [
        _result(
            "VMCP_PRE_BIT:status=route_design Complete!,critical_warnings=2\n"
            "VMCP_PRE_BIT_CW:CRITICAL WARNING: x"
        ),
    ]
    with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
        result = await generate_bitstream(wait=False, ctx=_ctx())
    assert "安全检查未通过" in result
    assert session.execute.await_count == 1
