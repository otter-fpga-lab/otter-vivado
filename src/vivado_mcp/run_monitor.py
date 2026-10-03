"""共享的只读运行观察器：后台采样，MCP/HTTP 只读同一缓存。

复用已有 session 和 QUERY_RUN_PROGRESS，不启动/重置 run，不打开设计。
报告只从实际 run 目录读取；文件时间只提供候选证据，不等于签核通过。
"""

from __future__ import annotations

import asyncio
import codecs
import copy
import re
import time
from dataclasses import asdict
from pathlib import Path

from vivado_mcp.analysis.run_progress_parser import (
    parse_run_progress,
    progress_percent,
    run_state,
)
from vivado_mcp.analysis.timing_parser import parse_timing_summary
from vivado_mcp.analysis.util_parser import parse_utilization
from vivado_mcp.tcl_scripts import QUERY_RUN_PROGRESS
from vivado_mcp.vivado.tcl_utils import decode_vivado_output, validate_identifier

TARGET_STEPS = ("synth_design", "route_design", "write_bitstream")
_REPORT_LIMIT = 16
_REPORT_BYTES = 256 * 1024


def parse_snapshot(raw: str, run_name: str, target_step: str) -> dict:
    """解析同一 Tcl 查询结果；无完整结果标记时不能升级为有效快照。"""
    rp = parse_run_progress(raw, run_name)
    if rp.error or not rp.found or "VMCP_RUN_DONE" not in raw.splitlines():
        raise ValueError(rp.error or "缺少完整 run 状态标记")
    meta = dict(re.findall(r"^VMCP_RUN:(\w+)=(.*)$", raw, re.MULTILINE))
    if not rp.status:
        raise ValueError("缺少 run STATUS")
    tail = [asdict(line) for line in rp.tail]
    counts = {"errors": 0, "warnings": 0, "critical_warnings": 0}
    for line in tail:
        text = line["text"].lstrip()
        if text.startswith("ERROR:"):
            counts["errors"] += 1
        elif text.startswith("CRITICAL WARNING:"):
            counts["critical_warnings"] += 1
        elif text.startswith("WARNING:"):
            counts["warnings"] += 1
    return {
        **asdict(rp),
        "state": run_state(rp.status, target_step),
        "progress_percent": progress_percent(rp.progress),
        "current_phase": rp.current_phase(),
        "directory": meta.get("dir", ""),
        "project": meta.get("project", ""),
        "project_file": meta.get("project_file", ""),
        "project_mode": meta.get("project_mode", "unknown"),
        "version": meta.get("version", ""),
        "top": meta.get("top", ""),
        "elapsed": meta.get("elapsed", ""),
        "needs_refresh": meta.get("needs_refresh", "unknown"),
        "log_offset": int(meta.get("log_offset", "0")),
        "diagnostics": {**counts, "scope": "仅当前日志尾部；0 不代表全程无警告/错误"},
    }


def read_reports(run: dict) -> list[dict]:
    """有界读取已有报告，复用解析器；陈旧/不匹配报告不参与质量判定。"""
    directory = Path(run.get("directory", ""))
    if not directory.is_absolute() or not directory.is_dir():
        return []
    # Vivado launch_runs 生成的开始标记；未找到则明确缺少新鲜度证据。
    begin = directory / ".vivado.begin.rst"
    try:
        started = begin.stat().st_mtime if not begin.is_symlink() else None
    except OSError:
        started = None
    reports = []
    try:
        paths = sorted(directory.glob("*.rpt"))[:_REPORT_LIMIT]
    except OSError:
        return []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            stat = path.stat()
            with path.open("rb") as stream:
                data = stream.read(_REPORT_BYTES + 1)
            chunk = data[:_REPORT_BYTES]
            try:
                # 有界读取可能停在 UTF-8 多字节字符中间；未到 EOF 时保留完整前缀，
                # 避免仅因截断而把整份 UTF-8 报告错误判成 Windows ANSI。
                text = codecs.getincrementaldecoder("utf-8")().decode(
                    chunk, final=len(data) <= _REPORT_BYTES,
                )
            except UnicodeDecodeError:
                # 与会话输出共用 Windows 系统 ANSI 回退。
                text = decode_vivado_output(chunk)
            text = text.lstrip("\ufeff")
        except OSError:
            continue
        name = path.name.lower()
        stage = "unknown"
        for marker, label in (("routed", "post-route"), ("placed", "post-place"),
                              ("synth", "post-synth")):
            if marker in name:
                stage = label
                break
        # 文件名只作为阶段线索，不用当前已打开设计给旧报告冒认来源。
        freshness = "unverified"
        reason = "文件时间与阶段名不能证明本次目标已签核；不参与全局 PASS 判定"
        design = re.search(r"^\s*Design\s*:\s*(\S+)", text, re.MULTILINE)
        if started is not None and stat.st_mtime < started:
            freshness, reason = "stale", "报告早于本目录的 .vivado.begin.rst"
        elif str(run.get("needs_refresh", "")).lower() in ("1", "true", "yes"):
            freshness, reason = "stale", "Vivado 标记 NEEDS_REFRESH，不能作为当前结果"
        elif design and run.get("top") and design[1] != run["top"]:
            freshness, reason = "mismatched", "报告 Design 与当前 TOP 不同"
        elif started is not None and stage != "unknown":
            reason = "报告不早于 run 开始标记；仅为当前 run 候选，未证明目标/约束匹配"
        summary = None
        if len(data) > _REPORT_BYTES:
            reason += "；内容仅前 256 KiB，未解析摘要"
        elif "timing" in name:
            summary = {"kind": "timing", **parse_timing_summary(text).to_dict()}
        elif "utilization" in name:
            summary = {"kind": "utilization", **parse_utilization(text).to_dict()}
        reports.append({
            "name": path.name, "path": str(path), "stage": stage,
            "stage_source": "filename_hint", "freshness": freshness, "reason": reason,
            "mtime": stat.st_mtime, "size": stat.st_size, "summary": summary, "text": text,
        })
    return reports


class RunMonitor:
    """每个选定 session/run/目标一个观察器，不拥有底层 Vivado 生命周期。"""

    def __init__(self, session, run_name: str, target_step: str, interval: float = 5):
        self.session = session
        self.run_name = validate_identifier(run_name, "run_name")
        if target_step not in TARGET_STEPS:
            raise ValueError(f"target_step 必须为 {TARGET_STEPS}")
        if not 2 <= interval <= 60:
            raise ValueError("采样间隔须为 2~60 秒")
        self.target_step = target_step
        self.interval = interval
        self._task = None
        self._http = None
        self._closed = False
        self._value = {
            "source": "live", "session_id": session.session_id, "session_mode": session.mode,
            "run_name": run_name, "target_step": target_step, "connection": "waiting",
            "observed_at": None, "last_attempt_at": None, "error": "", "run": {},
            "reports": [], "quality": {"timing": "unknown", "resources": "unknown"},
        }

    def snapshot(self) -> dict:
        """只读取缓存，不查询、不启动或取消 Vivado。"""
        return copy.deepcopy(self._value)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    def open_view(self) -> str:
        from vivado_mcp.monitor_http import MonitorHTTP

        if self._http is None:
            self._http = MonitorHTTP(self.snapshot)
        return self._http.start()

    async def sample(self) -> None:
        """忙时不排队查询；超时保留底层 session 的在途响应所有权。"""
        now = time.time()
        current = {**self._value, "last_attempt_at": now}
        if not self.session.is_alive:
            self._value = {**current, "connection": "disconnected", "error": "会话已失联"}
            return
        state = getattr(self.session.state, "value", self.session.state)
        if state != "ready":
            self._value = {**current, "connection": "busy", "error": "主通道忙，保留最后采样"}
            return
        try:
            result = await self.session.execute(
                QUERY_RUN_PROGRESS.format(run_name=self.run_name, tail_n=30), timeout=3.0,
            )
            if result.is_error:
                raise ValueError(result.output)
            run = parse_snapshot(result.output, self.run_name, self.target_step)
            observed_at = time.time()
            reports = await asyncio.to_thread(read_reports, run)
            self._value = {
                **current, "connection": "connected", "error": "", "run": run,
                "observed_at": observed_at, "reports": reports,
            }
        except Exception as exc:
            connection = "error" if self.session.is_alive else "disconnected"
            if isinstance(exc, asyncio.TimeoutError):
                connection = "busy"
            self._value = {**current, "connection": connection, "error": str(exc)}

    async def _loop(self) -> None:
        while not self._closed:
            await self.sample()
            await asyncio.sleep(self.interval)

    async def close(self) -> None:
        """只停止采样/HTTP，不调用 session.stop/exit/reset。"""
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._http:
            await asyncio.to_thread(self._http.close)
            self._http = None


class MonitorRegistry:
    """与 MCP lifespan 绑定；同一目标重复打开复用观察器。"""

    def __init__(self):
        self._monitors: dict[tuple, RunMonitor] = {}

    def get(self, session, run_name: str, target_step: str) -> RunMonitor:
        key = (session, run_name, target_step)
        if key not in self._monitors:
            monitor = RunMonitor(session, run_name, target_step)
            self._monitors[key] = monitor
            monitor.start()
        return self._monitors[key]

    def last(self, session_id: str, run_name: str, target_step: str) -> RunMonitor | None:
        """会话失联后仍允许读取最后快照，与页面显示保持一致。"""
        for (session, name, target), monitor in reversed(list(self._monitors.items())):
            if (session.session_id, name, target) == (session_id, run_name, target_step):
                return monitor
        return None

    async def close(self) -> None:
        await asyncio.gather(*(monitor.close() for monitor in self._monitors.values()))
        self._monitors.clear()
