"""有界读取数字 VCD，向消费者提供无损字符串值和分页事件。"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_MAX_BYTES = 32 * 1024 * 1024
_MAX_SIGNALS = 4096
_MAX_WIDTH = 4096


def _identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _scan_ila_waveform(
    file_path: str, signal_ids: list[str] | None = None,
    start_tick: str = "0", end_tick: str | None = None,
    offset: int = 0, limit: int = 1000, expected_sha256: str | None = None,
    *, visitor=None,
) -> dict:
    """读取有限数字 VCD；时间和值用字符串，不推断实际采样周期或触发位置。"""
    for name, value in (("start_tick", start_tick), ("end_tick", end_tick)):
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,40}", value)
        ):
            raise ValueError(f"{name} 必须是最多 40 位非负十进制字符串")
    if start_tick is None:
        raise ValueError("start_tick 不能为 null")
    start, end = int(start_tick), int(end_tick) if end_tick is not None else None
    if end is not None and end < start:
        raise ValueError("end_tick 不能早于 start_tick")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("offset 必须非负；limit 必须在 1~10000")
    if signal_ids is not None and (
        not isinstance(signal_ids, list) or len(signal_ids) > _MAX_SIGNALS
        or any(not isinstance(v, str) or not v for v in signal_ids)
        or len(set(signal_ids)) != len(signal_ids)
    ):
        raise ValueError("signal_ids 必须是无重复的 VCD 标识符列表")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise ValueError("expected_sha256 必须为小写 SHA256")
    path = Path(file_path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("波形必须是普通文件，不能是符号链接")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > _MAX_BYTES:
            raise ValueError("VCD 超过 32 MiB 离线读取上限")
        raw = stream.read(_MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > _MAX_BYTES or _identity(before) != _identity(after) or (
        _identity(after) != _identity(path.stat())
    ):
        raise ValueError("VCD 超限或读取期间发生变化")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("VCD 指纹与预期不一致；不能拼接不同文件的分页")
    text = raw.decode("utf-8-sig")
    tokens = (m.group() for m in re.finditer(r"\S+", text))

    def take():
        try:
            value = next(tokens)
        except StopIteration as exc:
            raise ValueError("VCD 意外结束") from exc
        if len(value) > 8192:
            raise ValueError("VCD token 过长")
        return value

    def directive():
        values = []
        while (value := take()) != "$end":
            values.append(value)
            if len(values) > 8192:
                raise ValueError("VCD 指令过长")
        return values

    scopes, signals, widths = [], [], {}
    timescale, metadata_bytes = None, 0
    while True:
        token = take()
        values = directive()
        if token in {"$date", "$version", "$comment"}:
            continue
        if token == "$scope":
            if len(values) != 2 or len(scopes) >= 64:
                raise ValueError("VCD scope 无效或嵌套过深")
            scopes.append(values[1])
        elif token == "$upscope":
            if values or not scopes:
                raise ValueError("VCD upscope 不匹配")
            scopes.pop()
        elif token == "$timescale":
            match = re.fullmatch(r"(1|10|100)(s|ms|us|ns|ps|fs)", "".join(values))
            if timescale is not None or match is None:
                raise ValueError("VCD timescale 无效或重复")
            timescale = {"magnitude": int(match[1]), "unit": match[2]}
        elif token == "$var":
            if len(values) not in {4, 5} or values[0] not in {
                "wire", "reg", "logic", "integer", "parameter", "event",
                "supply0", "supply1", "tri", "tri0", "tri1", "triand", "trior", "wand", "wor",
            }:
                raise ValueError("仅支持数字 VCD 变量；实数/字符串及未知声明不受支持")
            if not re.fullmatch(r"[0-9]{1,4}", values[1]):
                raise ValueError("VCD width 无效")
            width, code = int(values[1]), values[2]
            if not 1 <= width <= _MAX_WIDTH or len(signals) >= _MAX_SIGNALS:
                raise ValueError("VCD 信号数量或位宽超限")
            if code in widths and widths[code] != width:
                raise ValueError("VCD 别名位宽不一致")
            metadata_bytes += sum(len(v.encode("utf-8")) for v in scopes + values)
            if metadata_bytes > 262144:
                raise ValueError("VCD 信号目录展开后超过 256 KiB")
            widths[code] = width
            signals.append({"id": code, "scope": list(scopes), "name": values[3],
                            "range": values[4] if len(values) == 5 else None,
                            "width": width, "type": values[0]})
        elif token == "$enddefinitions":
            if values or scopes or not signals:
                raise ValueError("VCD 声明未闭合或没有信号")
            break
        else:
            raise ValueError(f"不支持的 VCD 声明：{token}")
    selected = set(widths) if signal_ids is None else set(signal_ids)
    if selected - widths.keys():
        raise ValueError("signal_ids 包含未声明的 VCD 标识符")
    if sum(widths[code] for code in selected) > 262144:
        raise ValueError("所选信号总位宽超过 262144；请先取目录，再缩小 signal_ids")
    if visitor is not None:
        visitor.begin(signals, widths)
    initial = dict.fromkeys(sorted(selected))
    events, matched = [], 0
    page_bytes, page_full = 0, False
    tick, last_tick, dump = 0, 0, None
    for token in tokens:
        if len(token) > 8192:
            raise ValueError("VCD token 过长")
        if token == "$comment":
            directive()
            continue
        if token in {"$dumpvars", "$dumpall"}:
            if dump is not None:
                raise ValueError("VCD dump 嵌套")
            dump = token
            continue
        if token == "$end":
            if dump is None:
                raise ValueError("VCD 多余的 $end")
            dump = None
            continue
        if token.startswith("#"):
            if dump is not None or not re.fullmatch(r"#[0-9]{1,40}", token):
                raise ValueError("VCD 时间标记无效")
            tick = int(token[1:])
            if tick < last_tick:
                raise ValueError("VCD 时间倒退")
            last_tick = tick
            continue
        if token[0] in "bB":
            value, code = token[1:].lower(), take()
        elif token[0] in "01xXzZ":
            value, code = token[0].lower(), token[1:]
        else:
            raise ValueError(f"不支持的 VCD 值或指令：{token[:80]}")
        if code not in widths or not re.fullmatch(r"[01xz]+", value):
            raise ValueError("VCD 值无效或标识符未声明")
        width = widths[code]
        if len(value) > width:
            raise ValueError("VCD 值超过声明位宽")
        value = value.rjust(width, value[0] if value[0] in "xz" else "0")
        if code not in selected:
            continue
        if visitor is not None and (end is None or tick <= end):
            visitor.event(tick, code, value)
        if tick < start:
            initial[code] = value
        elif end is None or tick <= end:
            if visitor is None and matched >= offset and not page_full:
                page_full = len(events) >= limit or page_bytes + len(value) + len(code) > 262144
            if visitor is None and matched >= offset and not page_full:
                page_bytes += len(value) + len(code)
                events.append({"tick": str(tick), "id": code, "value": value})
            matched += 1
    if dump is not None:
        raise ValueError("VCD dump 未闭合")
    if visitor is not None:
        visitor.finish(last_tick)
    next_offset = (offset + len(events)
                   if visitor is None and offset + len(events) < matched else None)
    return {
        "schema": "otter.waveform.v1", "source": "file", "format": "vcd",
        "file": {"path": str(path.absolute()), "size": len(raw), "sha256": digest},
        "timescale": timescale, "time_interpretation": "vcd_ticks_only",
        "sample_period_seconds": None, "trigger_tick": None,
        "signals": signals, "selected_ids": sorted(selected),
        "start_tick": str(start), "end_tick": str(end) if end is not None else None,
        "last_tick": str(last_tick), "initial_values_before_start": initial,
        "events": events, "offset": offset, "next_offset": next_offset,
        "matching_events": matched, "truncated": next_offset is not None,
        "capture_completeness": "unverified",
    }


def read_ila_waveform(
    file_path: str, signal_ids: list[str] | None = None,
    start_tick: str = "0", end_tick: str | None = None,
    offset: int = 0, limit: int = 1000, expected_sha256: str | None = None,
) -> dict:
    """离线读取一页事件；内部扫描器也供分析使用，避免逐页重复读取文件。"""
    return _scan_ila_waveform(
        file_path, signal_ids, start_tick, end_tick, offset, limit, expected_sha256,
    )
