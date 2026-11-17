"""跨版本文件变更指纹与 ctime 差异回归测试。"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.analysis.test_ila_waveform import VCD
from vivado_mcp.analysis.bit_header_parser import parse_bit
from vivado_mcp.analysis.debug_artifacts import check_debug_artifacts
from vivado_mcp.analysis.file_identity import same_stat_identity, stat_identity
from vivado_mcp.analysis.ila_waveform import read_ila_waveform
from vivado_mcp.analysis.ltx_parser import parse_ltx
from vivado_mcp.debug_bundle import fingerprint

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _stat(**overrides):
    base = {
        "st_dev": 100,
        "st_ino": 200,
        "st_size": 64,
        "st_mtime_ns": 1_700_000_000_000_000_000,
        "st_ctime_ns": 1_700_000_000_000_000_000,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _with_ctime(stat_obj: os.stat_result, ctime_ns: int):
    return SimpleNamespace(
        st_mode=stat_obj.st_mode,
        st_dev=stat_obj.st_dev,
        st_ino=stat_obj.st_ino,
        st_size=stat_obj.st_size,
        st_mtime_ns=stat_obj.st_mtime_ns,
        st_ctime_ns=ctime_ns,
    )


class TestStatIdentity:
    def test_ignores_ctime_mismatch(self):
        a = _stat(st_ctime_ns=10)
        b = _stat(st_ctime_ns=999)
        assert stat_identity(a) == stat_identity(b)
        assert same_stat_identity(a, b) is True

    @pytest.mark.parametrize(
        "changed",
        [
            {"st_size": 65},
            {"st_mtime_ns": 1_700_000_000_000_000_001},
            {"st_ino": 201},
            {"st_dev": 101},
        ],
    )
    def test_detects_size_mtime_dev_and_ino_changes(self, changed):
        assert same_stat_identity(_stat(), _stat(**changed)) is False

    def test_handles_unavailable_dev_and_ino(self):
        with_ids = _stat(st_dev=100, st_ino=200)
        zero_ids = _stat(st_dev=0, st_ino=0)
        assert same_stat_identity(with_ids, zero_ids) is True
        assert same_stat_identity(zero_ids, _stat(st_dev=0, st_ino=0, st_size=65)) is False
        assert same_stat_identity(zero_ids, _stat(st_dev=0, st_ino=0, st_mtime_ns=1)) is False


def test_parsers_and_fingerprint_tolerate_fstat_stat_ctime_mismatch(tmp_path, monkeypatch):
    bit = tmp_path / "design.bit"
    ltx = tmp_path / "design.ltx"
    vcd = tmp_path / "wave.vcd"
    bit.write_bytes((FIXTURES / "sample_header.bit").read_bytes())
    ltx.write_bytes((FIXTURES / "sample_probes.ltx").read_bytes())
    vcd.write_text(VCD, encoding="utf-8")

    real_fstat = os.fstat
    real_stat = os.stat
    monkeypatch.setattr(os, "fstat", lambda fd: _with_ctime(real_fstat(fd), 111))
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: _with_ctime(real_stat(p, *a, **k), 222))

    assert parse_bit(str(bit)).part_normalized == "xc7k325tffg900"
    assert parse_ltx(str(ltx)).cores
    assert read_ila_waveform(str(vcd))["signals"]
    assert fingerprint(bit)["size"] == bit.stat().st_size
    assert check_debug_artifacts(str(bit), str(ltx))["status"] == "incomplete"


def test_debug_artifacts_still_detects_mtime_change_when_size_unchanged(tmp_path, monkeypatch):
    import vivado_mcp.analysis.debug_artifacts as module

    bit = tmp_path / "design.bit"
    ltx = tmp_path / "design.ltx"
    bit.write_bytes((FIXTURES / "sample_header.bit").read_bytes())
    ltx.write_bytes((FIXTURES / "sample_probes.ltx").read_bytes())

    real_parse_bit = module.parse_bit

    def touch_after_parse(path: str):
        parsed = real_parse_bit(path)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        return parsed

    monkeypatch.setattr(module, "parse_bit", touch_after_parse)
    result = check_debug_artifacts(str(bit), str(ltx))
    assert result["status"] == "blocked"
    assert any(c["code"] == "bit.changed" and c["status"] == "blocked" for c in result["checks"])
