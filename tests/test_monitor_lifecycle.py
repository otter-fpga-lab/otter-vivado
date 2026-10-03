"""报告后台读取与观察器释放回归，不启动真实 EDA 或改变设备。"""

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.run_monitor import MonitorRegistry, RunMonitor, read_reports
from vivado_mcp.tools.monitor_tools import close_run_monitor
from vivado_mcp.vivado.base_session import SessionState
from vivado_mcp.vivado.session import SubprocessSession
from vivado_mcp.vivado.tcl_utils import TclResult


def _result(progress=25, started=100):
    return TclResult(
        f"VMCP_RUN:status=route_design Running\nVMCP_RUN:progress={progress}%\n"
        f"VMCP_RUN:run_started={started}\nVMCP_RUN:dir=/example/impl_1\nVMCP_RUN_DONE",
        0, False,
    )


class _Session:
    def __init__(self, session_id="test"):
        self.session_id = session_id
        self.mode = "gui"
        self.is_alive = True
        self.state = "ready"
        self.execute = AsyncMock(return_value=_result())
        self.stop = AsyncMock()


@pytest.fixture
def slow_reports(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read(run):
        calls.append(run)
        entered.set()
        release.wait(3)
        return [{"name": f"report-{run['run_started']}", "freshness": "unverified"}]

    monkeypatch.setattr("vivado_mcp.run_monitor.read_reports", read)
    yield entered, release, calls
    release.set()


async def test_slow_reports_do_not_block_status_and_only_one_reader_is_pending(slow_reports):
    entered, release, calls = slow_reports
    source = _Session()
    monitor = RunMonitor(source, "impl_1", "route_design")
    try:
        await monitor.sample()
        assert monitor.snapshot()["run"]["progress_percent"] == 25
        assert monitor.snapshot()["connection"] == "connected"
        assert await asyncio.to_thread(entered.wait, 1)
        reader = monitor._report_task
        source.execute.return_value = _result(progress=50)
        await monitor.sample()
        source.execute.return_value = _result(progress=75)
        await monitor.sample()
        await monitor.wait_for_reports(timeout=0.001)
        snapshot = monitor.snapshot()
        assert snapshot["run"]["progress_percent"] == 75
        assert snapshot["reports_status"] == "loading"
        assert snapshot["reports_observed_at"] is None
        assert len(calls) == 1
        assert monitor._report_task is reader and not reader.done()
        release.set()
        await monitor.wait_for_reports()
        assert monitor.snapshot()["reports_status"] == "ready"
        assert monitor.snapshot()["reports_observed_at"] is not None
        assert monitor.snapshot()["reports_source"]["run_started"] == 100
        assert monitor.snapshot()["run"]["progress_percent"] == 75
    finally:
        release.set()
        await monitor.close()
    source.stop.assert_not_called()


async def test_late_report_for_previous_run_is_discarded(slow_reports):
    entered, release, _ = slow_reports
    source = _Session()
    monitor = RunMonitor(source, "impl_1", "route_design")
    try:
        await monitor.sample()
        assert await asyncio.to_thread(entered.wait, 1)
        source.execute.return_value = _result(started=200)
        await monitor.sample()
        release.set()
        await monitor.wait_for_reports()
        assert monitor.snapshot()["reports"] == []
        assert monitor.snapshot()["reports_status"] == "stale"
        assert monitor.snapshot()["reports_observed_at"] is None
        await monitor.sample()
        await monitor.wait_for_reports()
        assert monitor.snapshot()["reports"] == [
            {"name": "report-200", "freshness": "unverified"}
        ]
        assert monitor.snapshot()["reports_source"]["run_started"] == 200
    finally:
        release.set()
        await monitor.close()


async def test_report_failure_preserves_previous_report_but_updates_real_status(monkeypatch):
    source = _Session()
    monitor = RunMonitor(source, "impl_1", "route_design")
    monkeypatch.setattr("vivado_mcp.run_monitor.read_reports", lambda run: [{"name": "old"}])
    try:
        await monitor.sample()
        await monitor.wait_for_reports()
        before = monitor.snapshot()

        def failure(run):
            raise PermissionError("report is locked")

        monkeypatch.setattr("vivado_mcp.run_monitor.read_reports", failure)
        source.execute.return_value = _result(progress=50)
        await monitor.sample()
        await monitor.wait_for_reports()
        after = monitor.snapshot()
        assert after["connection"] == "connected"
        assert after["run"]["progress_percent"] == 50
        assert after["reports"] == before["reports"]
        assert after["reports_observed_at"] == before["reports_observed_at"]
        assert after["reports_status"] == "error"
        assert after["reports_error"] == "report is locked"
    finally:
        await monitor.close()


async def test_report_completion_during_status_query_is_not_overwritten(slow_reports):
    entered, release, _ = slow_reports
    source = _Session()
    monitor = RunMonitor(source, "impl_1", "route_design")
    query_entered, query_release = asyncio.Event(), asyncio.Event()

    async def delayed_query(*args, **kwargs):
        query_entered.set()
        await query_release.wait()
        return _result(progress=50)

    try:
        await monitor.sample()
        assert await asyncio.to_thread(entered.wait, 1)
        source.execute.side_effect = delayed_query
        query = asyncio.create_task(monitor.sample())
        await query_entered.wait()
        release.set()
        await monitor.wait_for_reports()
        reports = monitor.snapshot()["reports"]
        assert reports
        query_release.set()
        await query
        assert monitor.snapshot()["reports"] == reports
        assert monitor.snapshot()["run"]["progress_percent"] == 50
    finally:
        query_release.set()
        release.set()
        await monitor.close()


async def test_disconnected_observer_stops_polling_but_retains_url_and_evidence(monkeypatch):
    monkeypatch.setattr("vivado_mcp.run_monitor.read_reports", lambda run: [])
    source = _Session()
    monitor = RunMonitor(source, "impl_1", "route_design")
    try:
        await monitor.sample()
        await monitor.wait_for_reports()
        before = monitor.snapshot()
        url = monitor.open_view()
        source.is_alive = False
        monitor.start()
        await asyncio.wait_for(monitor._task, 1)
        assert monitor.snapshot()["connection"] == "disconnected"
        assert monitor.snapshot()["run"] == before["run"]
        assert monitor.snapshot()["reports_observed_at"] == before["reports_observed_at"]
        assert monitor.open_view() == url
        assert monitor._http._thread.is_alive()
        source.execute.assert_awaited_once()
    finally:
        await monitor.close()
    source.stop.assert_not_called()


async def test_close_monitor_releases_old_and_current_matches_without_stopping_sessions():
    registry = MonitorRegistry()
    old_source, current_source = _Session(), _Session()
    old_source.is_alive = current_source.is_alive = False
    old = registry.get(old_source, "impl_1", "route_design")
    current = registry.get(current_source, "impl_1", "route_design")
    other = registry.get(current_source, "impl_2", "route_design")
    old.open_view()
    current.open_view()
    ctx = SimpleNamespace(request_context=SimpleNamespace(
        lifespan_context=SimpleNamespace(run_monitors=registry)))
    try:
        with patch("vivado_mcp.tools.monitor_tools._require_session", side_effect=AssertionError):
            response = json.loads(await close_run_monitor(session_id="test", ctx=ctx))
        assert response["status"] == "closed" and response["closed"] == 2
        assert old._closed and current._closed
        assert old._http is current._http is None
        assert not other._closed
        assert len(registry._monitors) == 1
        response = json.loads(await close_run_monitor(session_id="test", ctx=ctx))
        assert response["status"] == "not_found" and response["closed"] == 0
        assert len(registry._monitors) == 1
        old_source.execute.assert_not_awaited()
        current_source.execute.assert_not_awaited()
    finally:
        await registry.close()
    old_source.stop.assert_not_called()
    current_source.stop.assert_not_called()


def test_unreadable_report_is_an_explicit_report_error(tmp_path, monkeypatch):
    report = tmp_path / "locked.rpt"
    report.write_text("report", encoding="utf-8")
    original_open = Path.open

    def locked_open(path, *args, **kwargs):
        if path == report:
            raise PermissionError("sharing violation")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked_open)
    with pytest.raises(OSError, match="sharing violation"):
        read_reports({"directory": str(tmp_path)})


async def test_closing_observer_preserves_real_session_inflight_reader():
    source = SubprocessSession("/fake/vivado", "running")
    source._process = MagicMock(returncode=None)
    source._state = SessionState.READY
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute_impl(command):
        entered.set()
        await release.wait()
        return _result()

    source._execute_impl = execute_impl
    monitor = RunMonitor(source, "impl_1", "route_design")
    monitor.start()
    await entered.wait()
    reader = source._inflight_task
    try:
        await monitor.close()
        assert source.is_alive
        assert reader is not None and not reader.done()
        assert source.state == SessionState.BUSY
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(reader), 1)


def test_slow_report_thread_does_not_block_asyncio_run_exit():
    script = """
import asyncio
import threading
from vivado_mcp.run_monitor import RunMonitor
import vivado_mcp.run_monitor as module
class Session:
    session_id = 'test'
    mode = 'gui'
module.read_reports = lambda run: threading.Event().wait(60)
async def main():
    monitor = RunMonitor(Session(), 'impl_1', 'route_design')
    monitor._schedule_reports({})
    await monitor.wait_for_reports(timeout=0.01)
    assert monitor.snapshot()['reports_status'] == 'loading'
    await monitor.close()
asyncio.run(main())
print('exited without waiting for report IO')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=5, check=True,
    )
    assert "exited without waiting for report IO" in result.stdout
