"""从明确的实现检查点导出消费者交付包，保留来源和部分失败现场。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from vivado_mcp.analysis.debug_artifacts import check_debug_artifacts
from vivado_mcp.tcl_scripts import DEBUG_BUNDLE_EXPORT

_FILES = {
    "checkpoint": "source.dcp", "bit": "design.bit", "ltx": "design.ltx",
    "timing": "timing.rpt", "utilization": "utilization.rpt", "drc": "drc.rpt",
}


def _identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def fingerprint(path: Path) -> dict:
    """稳定读取普通文件，不以名称或时间替代内容摘要。"""
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"产物必须是普通文件，不能是符号链接：{path}")
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(source.fileno())
    if _identity(before) != _identity(after) or _identity(after) != _identity(path.stat()):
        raise ValueError(f"读取期间文件改变：{path}")
    if not size:
        raise ValueError(f"产物为空：{path}")
    return {"filename": path.name, "size": size, "sha256": digest.hexdigest()}


def _snapshot(source: Path, destination: Path) -> dict:
    """复制明确的检查点，复制时检测变化；失败保留已创建的文件。"""
    if not source.is_file():
        raise ValueError("checkpoint_path 必须是已完成实现的普通 .dcp 文件")
    before_path = source.stat()
    with source.open("rb") as reader, destination.open("xb") as writer:
        before = os.fstat(reader.fileno())
        digest = hashlib.sha256()
        size = 0
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(reader.fileno())
    if (not size or _identity(before_path) != _identity(before)
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(source.stat())):
        raise ValueError("检查点为空或复制期间发生改变，请等待构建完成")
    result = fingerprint(destination)
    if result["size"] != size or result["sha256"] != digest.hexdigest():
        raise ValueError("检查点副本与读取内容不一致")
    return result


def _save_new(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _receipt(path, result):
    """结果记录失败也返回真实操作状态，不误报成从未导出。"""
    try:
        _save_new(path, result)
    except OSError as exc:
        result["receipt_error"] = str(exc)


def _parse_response(output, attempt_id):
    fields = {}
    done = 0
    for line in output.splitlines():
        if line == "VMCP_BUNDLE_DONE:1":
            done += 1
        elif line.startswith("VMCP_BUNDLE_FIELD:"):
            key, raw = line.split(":", 1)[1].split("|", 1)
            if key in fields:
                raise ValueError("导出回执含重复字段")
            fields[key] = bytes.fromhex(raw).decode("utf-8")
    if (done != 1 or fields.get("attempt_id") != attempt_id
            or fields.get("status") not in {"exported", "partial", "blocked"}):
        raise ValueError("导出回执不完整或不属于本次尝试")
    if fields["status"] == "exported" and (
        fields.get("stage") != "complete"
        or any(not fields.get(k) for k in ("part", "design", "vivado_version"))
    ):
        raise ValueError("导出成功回执缺少设计/版本证据")
    return fields


async def export_debug_bundle(
    session, checkpoint_path: str, output_dir: str, expected_part: str,
    source_revision: str | None = None, timeout_seconds: int = 1800,
) -> dict:
    """在专用空会话中生成 bit/ltx/报告；未知回执不重试、不删除现场。"""
    if not isinstance(expected_part, str) or not re.fullmatch(r"[A-Za-z0-9-]+", expected_part):
        raise ValueError("expected_part 必须是检查点的完整器件名称（含速度等级）")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 7200:
        raise ValueError("timeout_seconds 必须是 1~7200 的整数")
    if source_revision is not None and (
        not isinstance(source_revision, str) or not 1 <= len(source_revision) <= 256
        or "\x00" in source_revision
    ):
        raise ValueError("source_revision 必须是 1~256 字符的来源标签")
    source = Path(checkpoint_path).expanduser().absolute()
    if source.suffix.lower() != ".dcp" or not source.is_file():
        raise ValueError("checkpoint_path 必须是实际存在的 .dcp 文件")
    destination = Path(output_dir).expanduser().absolute()
    # mkdir 的排他语义拒绝已有目录/文件/链接；即使上次失败也使用新目录。
    destination.mkdir(parents=False, exist_ok=False)
    destination = destination.resolve()
    paths = {key: destination / name for key, name in _FILES.items()}
    attempt_id = uuid.uuid4().hex
    result = {
        "schema_version": 1, "attempt_id": attempt_id, "status": "partial",
        "bundle_directory": str(destination), "pairing": "unverified",
        "hardware_verified": False, "timing_signoff": "unverified",
        "source_checkpoint": str(source), "declared_source_revision": source_revision,
        "expected_part": expected_part, "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": getattr(session, "session_id", "unknown"),
        "method": "single_checkpoint_single_execute",
    }
    _save_new(destination / "attempt.json", {**result, "status": "pending"})
    attempted = False
    try:
        snapshot = await asyncio.to_thread(_snapshot, source, paths["checkpoint"])
        values = {key: str(path).replace("\\", "/") for key, path in paths.items()}
        values.update(expected_part=expected_part, attempt_id=attempt_id)
        declarations = "\n".join(
            f"set __{key} [encoding convertfrom utf-8 [binary format H* {value.encode().hex()}]]"
            for key, value in values.items()
        )
        command = DEBUG_BUNDLE_EXPORT.replace("__DECLARATIONS__", declarations)
        attempted = True
        response = await session.execute(command, timeout=float(timeout_seconds))
        if response.is_error:
            raise RuntimeError("会话返回错误，导出结果未知：" + response.output[-2000:])
        evidence = _parse_response(response.output, attempt_id)
        result["vivado"] = evidence
        result["status"] = evidence["status"]
        if evidence["status"] == "exported":
            if evidence["part"] != expected_part:
                raise ValueError("导出回执的器件与预期不符")
            # 命令已返回完整终态，此后的文件验证失败属于本地部分完成。
            attempted = False
            artifacts = {}
            for key, path in paths.items():
                artifacts[key] = await asyncio.to_thread(fingerprint, path)
            if artifacts["checkpoint"] != snapshot:
                raise ValueError("导出期间检查点副本发生改变")
            check = await asyncio.to_thread(
                check_debug_artifacts, str(paths["bit"]), str(paths["ltx"]),
                {"part": expected_part},
            )
            if (check["status"] == "blocked"
                    or check["bit"]["sha256"] != artifacts["bit"]["sha256"]
                    or check["ltx"]["sha256"] != artifacts["ltx"]["sha256"]):
                raise ValueError("导出产物离线核对失败或内容在核对期间变化")
            result["artifacts"] = artifacts
            result["offline_check"] = {
                "status": check["status"], "pairing": check["pairing"],
                "bit_payload_complete": check["bit"]["payload_complete"],
                "debug_core_count": len(check["ltx"]["cores"]),
            }
            result["provenance"] = "recorded_same_checkpoint_export"
            result["notes"] = [
                "导出命令未切换设计；这是工具记录的来源，不是不可伪造的硬件配对证明。",
                "source_revision 为调用方声明，未验证它与检查点的源码关系。",
                "报告已生成；未据此判定 DRC/时序通过。专用会话保留已打开的检查点。",
            ]
            _save_new(destination / "manifest.json", result)
            result["manifest_path"] = str(destination / "manifest.json")
    except asyncio.CancelledError:
        result.update(status="unknown", error="调用被取消；Vivado 可能仍在执行，不能重放")
        _receipt(destination / "result.json", result)
        raise
    except Exception as exc:
        result.update(status="unknown" if attempted else "partial", error=str(exc))
    _receipt(destination / "result.json", result)
    return result


def verify_debug_bundle(manifest_path: str, bit_sha: str, ltx_sha: str) -> dict:
    """验证本地来源记录与完整交付包内容；不把用户可编辑的记录当作认证。"""
    path = Path(manifest_path).expanduser().absolute()
    if path.stat().st_size > 64 * 1024:
        raise ValueError("交付清单不能超过 64 KiB")
    before = fingerprint(path)
    with path.open("rb") as source:
        raw = source.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024 or hashlib.sha256(raw).hexdigest() != before["sha256"]:
        raise ValueError("交付清单读取期间改变")
    manifest = json.loads(raw.decode("utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("status") != "exported"
            or manifest.get("method") != "single_checkpoint_single_execute"
            or manifest.get("provenance") != "recorded_same_checkpoint_export"):
        raise ValueError("不是已完成的检查点导出清单")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(_FILES):
        raise ValueError("交付清单缺少完整产物")
    observed = {}
    for kind, name in _FILES.items():
        # 使用固定文件名，绝不根据清单里的任意路径读取文件。
        record = artifacts[kind]
        if (not isinstance(record, dict) or record.get("filename") != name
                or not isinstance(record.get("sha256"), str)):
            raise ValueError("交付清单的产物字段无效")
        observed[kind] = fingerprint(path.parent / name)
        if observed[kind] != record:
            raise ValueError(f"交付产物指纹不符：{name}")
    if observed["bit"]["sha256"] != bit_sha or observed["ltx"]["sha256"] != ltx_sha:
        raise ValueError("传入的 bit/ltx 不属于所提供的交付清单")
    if fingerprint(path) != before:
        raise ValueError("交付清单在核对期间改变")
    return {
        "status": "record_matches", "manifest_path": str(path),
        "attempt_id": manifest.get("attempt_id"), "vivado": manifest.get("vivado"),
        "declared_source_revision": manifest.get("declared_source_revision"),
        "note": "清单及文件指纹一致；本地记录可编辑，不是硬件配对或源码来源认证。",
    }
