"""Run 进度解析器:把 QUERY_RUN_PROGRESS 的 Tcl 输出翻成结构化数据。

用途:``get_run_progress`` 工具。用户起了 run_synthesis / run_implementation
但 10-30 分钟的黑盒等待里想知道"当前走到第几步"。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

_META_RE = re.compile(r"VMCP_RUN:(\w+)=(.*)")
_PHASE_RE = re.compile(r"VMCP_RUN_PHASE:(\d+)\|(.*)")
_TAIL_RE = re.compile(r"VMCP_RUN_TAIL:(\d+)\|(.*)")
_ERR_RE = re.compile(r"VMCP_RUN_ERROR:(.*)")
_COMPLETE_RE = re.compile(r"(?:(\w+)\s+)?complete!?", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(?:\d+(?:\.\d+)?|\.\d+)\s*%")


def run_state(status: str, target_step: str | None = None) -> str:
    """按真实 STATUS 判定状态；阶段完成不等于请求的目标已经完成。

    未指定目标时，只把综合、布线、比特流这三个流程终点标为完成。
    错误和取消优先于完成；未知文本保持 unknown，不能猜测成功。
    """
    value = status.strip().lower()
    if re.search(r"\b(?:error|failed|failure)\b", value):
        return "failed"
    if re.search(r"\b(?:cancelled|canceled|aborted)\b", value):
        return "cancelled"
    if re.search(r"\b(?:running|queued)\b", value):
        return "running"
    if value in {"not started", "reset", "idle"}:
        return "idle"
    complete = _COMPLETE_RE.fullmatch(value)
    if complete:
        step = complete.group(1)
        if target_step is None:
            reached = step in {"synth_design", "route_design", "write_bitstream"}
        else:
            target = target_step.strip().lower()
            reached = step == target or (target == "route_design" and step == "write_bitstream")
        return "completed" if reached else "stage_complete"
    return "unknown"


def progress_percent(raw: str) -> float | None:
    """只接受工具实际返回的 0..100 百分比；空值和无效值保持未知。"""
    value = raw.strip()
    if not _PERCENT_RE.fullmatch(value):
        return None
    percent = float(value.rstrip("%").strip())
    return percent if 0 <= percent <= 100 else None


@dataclass(frozen=True)
class PhaseLine:
    """runme.log 中一条 Phase/Starting/Finished 关键阶段行。"""
    lineno: int
    text: str


@dataclass
class RunProgress:
    """单个 run(synth_1 / impl_1)的运行快照。"""
    run_name: str = ""
    found: bool = False
    error: str = ""

    # Vivado run 属性
    status: str = ""          # 如 "route_design Running" / "synth_design Complete!"
    progress: str = ""        # 如 "50%"

    # log 元信息
    log_path: str = ""
    log_exists: bool = False
    log_size: int = 0
    log_mtime: int = 0        # Unix epoch(Tcl file mtime)
    log_offset: int = 0       # 有界采样窗口的起始字节；非零时行号是窗口相对值
    total_lines: int = 0

    # 结构化阶段 + 尾部
    phases: list[PhaseLine] = field(default_factory=list)
    tail: list[PhaseLine] = field(default_factory=list)

    def state(self, target_step: str | None = None) -> str:
        """查询错误优先，未找到有效状态时不能宣告完成。"""
        if self.error:
            return "failed"
        if not self.found:
            return "unknown"
        return run_state(self.status, target_step)

    def is_running(self) -> bool:
        return self.state() == "running"

    def is_complete(self, target_step: str | None = None) -> bool:
        return self.state(target_step) == "completed"

    def is_error(self) -> bool:
        return self.state() == "failed"

    def current_phase(self) -> str:
        """最近观测到的阶段日志，不保证代表 Vivado 此刻正在执行的步骤。"""
        if not self.phases:
            return ""
        return self.phases[-1].text

    def elapsed_since_last_update(self) -> int:
        """日志最后修改距现在的秒数(判断 run 是否还在活跃)。"""
        if self.log_mtime == 0:
            return -1
        return int(time.time()) - self.log_mtime


def parse_run_progress(raw: str, run_name: str = "") -> RunProgress:
    """解析 QUERY_RUN_PROGRESS 的 Tcl 输出。"""
    rp = RunProgress(run_name=run_name)
    done = False

    for line in raw.splitlines():
        line = line.rstrip()

        if line == "VMCP_RUN_DONE":
            done = True
            continue

        m_err = _ERR_RE.match(line)
        if m_err is not None:
            rp.error = m_err.group(1).strip()
            rp.found = False
            continue

        m_meta = _META_RE.match(line)
        if m_meta is not None:
            key, value = m_meta.group(1), m_meta.group(2).strip()
            if key == "status":
                rp.status = value
            elif key == "progress":
                rp.progress = value
            elif key == "dir":
                rp.log_path = f"{value}/runme.log" if value else ""
            elif key == "log_exists":
                rp.log_exists = value == "1"
            elif key == "log_size":
                try:
                    rp.log_size = int(value)
                except ValueError:
                    pass
            elif key == "log_mtime":
                try:
                    rp.log_mtime = int(value)
                except ValueError:
                    pass
            elif key == "log_offset":
                try:
                    rp.log_offset = max(0, int(value))
                except ValueError:
                    pass
            elif key == "total_lines":
                try:
                    rp.total_lines = int(value)
                except ValueError:
                    pass
            continue

        m_phase = _PHASE_RE.match(line)
        if m_phase is not None:
            rp.phases.append(PhaseLine(
                lineno=int(m_phase.group(1)),
                text=m_phase.group(2).strip(),
            ))
            continue

        m_tail = _TAIL_RE.match(line)
        if m_tail is not None:
            rp.tail.append(PhaseLine(
                lineno=int(m_tail.group(1)),
                text=m_tail.group(2).rstrip(),
            ))
            continue

    rp.found = bool(rp.status) and done and not rp.error
    if not rp.error and (rp.status or done) and not rp.found:
        rp.error = "运行快照不完整: 缺少 STATUS 或 VMCP_RUN_DONE 标记。"
    return rp


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _fmt_age(seconds: int) -> str:
    if seconds < 0:
        return "未知"
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分前"


def format_run_progress(rp: RunProgress, phase_window: int = 5) -> str:
    """人类可读报告。phase_window = 显示最近几条 Phase 行。"""
    if rp.error:
        return f"[ERROR] {rp.error}"
    if not rp.found:
        return f"[ERROR] Run '{rp.run_name}' 未找到或无状态数据。"

    # 状态标签
    label = {
        "failed": "失败",
        "cancelled": "已取消",
        "completed": "已完成",
        "stage_complete": "阶段已完成",
        "running": "运行中",
        "idle": "未启动",
        "unknown": "未知",
    }[rp.state()]

    out: list[str] = [f"=== {rp.run_name} 运行进度: {label} ==="]
    out.append(f"状态:   {rp.status or '(无)'}")
    if rp.progress:
        out.append(f"进度:   {rp.progress}")

    if not rp.log_exists:
        out.append("")
        out.append("日志:   runme.log 不存在(run 可能未启动)")
        return "\n".join(out)

    age = rp.elapsed_since_last_update()
    line_count = f"窗口内 {rp.total_lines} 行" if rp.log_offset else f"{rp.total_lines} 行"
    out.append(
        f"日志:   {rp.log_path}"
        f"  ({_fmt_size(rp.log_size)}, "
        f"{line_count}, 最近 {_fmt_age(age)}更新)"
    )
    if rp.log_offset:
        out.append(
            f"采样范围: 仅末尾 64 KiB（起始字节 {rp.log_offset}），"
            "以下行号均为窗口内相对行号，不代表全日志行号。"
        )

    if rp.phases:
        recent = rp.phases[-phase_window:]
        out.append("")
        out.append(f"最近观测阶段(最近 {len(recent)} 条，不保证是当前步骤):")
        for i, p in enumerate(recent):
            arrow = " ← 最近观测" if i == len(recent) - 1 else ""
            out.append(f"  L{p.lineno}: {p.text}{arrow}")

    if rp.tail:
        out.append("")
        out.append(f"日志尾部(最后 {len(rp.tail)} 行):")
        for t in rp.tail:
            out.append(f"  {t.text}")

    # 建议
    out.append("")
    if rp.is_error():
        out.append(f"建议: 运行 get_critical_warnings(run_name='{rp.run_name}') 查看 ERROR 详情。")
    elif rp.is_running():
        out.append(f"建议: run 仍在进行,稍后再查。运行 get_run_progress('{rp.run_name}') 刷新。")
        if age > 120:
            out.append(
                f"[!] 注意: log 最近 {_fmt_age(age)}才更新,"
                "若长时间无变化,可能 Vivado 卡住或进程已退。"
            )
    elif rp.is_complete():
        out.append(
            "建议: 运行 get_next_suggestion 看下一步,"
            "或 get_timing_report / get_utilization_report。"
        )

    return "\n".join(out)
