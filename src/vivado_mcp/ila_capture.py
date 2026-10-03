"""完整 ILA 单窗口采集落盘；失败保留现场，不重放采集或上传。"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from vivado_mcp import tcl_scripts as scripts
from vivado_mcp.debug_bundle import _receipt, _save_new, fingerprint


async def export_capture(backend, target, device, core, output_dir, expected_uuid=None):
    """共享服务持有操作锁时调用；写入新目录，记录本次返回的数据对象。"""
    backend._selection(target, device, core, expected_uuid)
    if not isinstance(output_dir, str) or not output_dir or "\x00" in output_dir:
        raise ValueError("output_dir 必须是消费者工程内的新目录路径")
    directory = Path(output_dir).expanduser().absolute()
    directory.mkdir(exist_ok=False)
    directory = directory.resolve()
    record = {
        "schema": "otter.ila-capture.v1", "source": "live",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": target, "device": device, "core": core,
        "expected_uuid": expected_uuid, "output_dir": str(directory),
        "status": "pending", "hardware_pairing": "unverified",
    }
    _save_new(directory / "attempt.json", record)
    try:
        fields = await backend._mutate(
            "export", scripts.DEBUG_SELECT_ILA + scripts.DEBUG_CHECK_UUID
            + scripts.DEBUG_UPLOAD_ILA_DATA + scripts.DEBUG_EXPORT_ILA,
            target, device, core, expected_uuid,
            capture_path=(directory / "capture.vcd").as_posix(),
        )
        if len(fields) != 6 or fields[0] != core:
            raise RuntimeError("ILA 导出响应结构损坏")
        waveform = await asyncio.to_thread(fingerprint, directory / "capture.vcd")
        record.update(
            status="succeeded", finished_at=datetime.now(timezone.utc).isoformat(),
            data_object=fields[1], actual_uuid=fields[2],
            sample_count=fields[3], trigger_position=fields[4], vivado_version=fields[5],
            capture_complete=True, waveform=waveform,
            time_interpretation="vcd_ticks_only", sample_period_seconds=None,
        )
        _save_new(directory / "manifest.json", record)
    except (Exception, asyncio.CancelledError) as exc:
        record.update(status="unknown", error=str(exc) or type(exc).__name__)
        _receipt(directory / "result.json", record)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError(
            f"ILA 导出未完整确认；保留 {directory}，请核对文件与设备，不自动重试：{exc}"
        ) from exc
    _receipt(directory / "result.json", record)
    return record
