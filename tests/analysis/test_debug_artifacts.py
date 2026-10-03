"""合成调试产物的边界核对；不是商业 Vivado/板卡验证。"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tests.analysis.test_bit_header_parser import _build_bit
from vivado_mcp.analysis.bit_header_parser import parse_bit
from vivado_mcp.analysis.debug_artifacts import check_debug_artifacts
from vivado_mcp.analysis.ltx_parser import parse_ltx

FIXTURES = Path(__file__).parents[1] / "fixtures"
EXPECTED = {
    "part": "xc7k325tffg900-2",
    "cores": [{
        "name": "u_top/ila_sample", "type": "ILA_V3",
        "probes": [{"name": "probe0", "width": 64, "direction": "IN", "port_index": 0}],
    }],
}


@pytest.fixture
def pair(tmp_path):
    bit = tmp_path / "设计 with spaces.bit"
    bit.write_bytes(_build_bit())
    ltx = tmp_path / "设计 with spaces.ltx"
    ltx.write_bytes((FIXTURES / "sample_probes.ltx").read_bytes())
    return bit, ltx


def check(pair, expected=EXPECTED):
    return check_debug_artifacts(*(str(p) for p in pair), expected)


def change_ltx(pair, edit):
    data = json.loads(pair[1].read_text())
    edit(data["ltx_root"]["ltx_data"][0]["debug_cores"])
    pair[1].write_text(json.dumps(data))


def test_consistent_is_never_pairing_or_hardware_pass(pair):
    result = check(pair)
    assert result["status"] == "consistent"
    assert result["pairing"] == "unverified"
    assert result["hardware_verified"] is False
    for kind, path in zip(("bit", "ltx"), pair):
        assert result[kind]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    core = result["ltx"]["cores"][1]
    assert core["uuid"] == "00112233445566778899AABBCCDDEEFF"
    assert core["probes"][0]["subnets"] == ["u_top/data_bus_a[63]", "u_top/data_bus_a[0]"]


def test_inventory_without_expectations_is_incomplete(pair):
    assert check(pair, None)["status"] == "incomplete"


@pytest.mark.parametrize("edit", [
    lambda raw: raw[:-1],
    lambda raw: raw + b"extra",
    lambda raw: _build_bit(include_e=False),
    lambda raw: _build_bit(part=""),
    lambda raw: _build_bit(design=""),
])
def test_incomplete_or_excess_bit_payload_blocks(pair, edit):
    pair[0].write_bytes(edit(pair[0].read_bytes()))
    assert check(pair)["status"] == "blocked"


def test_large_bit_streams_hash_with_bounded_reads(pair, monkeypatch):
    import builtins

    import vivado_mcp.analysis.bit_header_parser as module

    raw = _build_bit(payload_len=12 * 1024 * 1024)
    pair[0].write_bytes(raw)
    real_open = builtins.open
    reads = []

    class Reader:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def fileno(self):
            return self.source.fileno()

        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            reads.append(size)
            return self.source.read(size)

    monkeypatch.setattr(module, "open", lambda *a, **k: Reader(real_open(*a, **k)), raising=False)
    header = parse_bit(str(pair[0]))
    assert header.payload_complete
    assert header.sha256 == hashlib.sha256(raw).hexdigest()
    assert len(reads) > 10


@pytest.mark.parametrize("actual,wanted,status", [
    ("7k325tffg900", "xc7k325tffg900-2", "consistent"),
    ("7k325tffg900", "xc7k325tffg676-2", "blocked"),
    ("zu7evffvc1156", "xczu7ev-ffvc1156-2-e", "consistent"),
    ("zu7evffvc1156", "xczu7ev-ffvc900-2-e", "blocked"),
    ("xq7k325tffg900", "xq7k325tffg900-2", "consistent"),
    ("7k325tffg900", "xa7k325tffg900-2", "blocked"),
])
def test_part_keeps_package_and_explicit_family(pair, actual, wanted, status):
    pair[0].write_bytes(_build_bit(part=actual))
    expected = copy.deepcopy(EXPECTED)
    expected["part"] = wanted
    assert check(pair, expected)["status"] == status


@pytest.mark.parametrize("edit,status", [
    (lambda cs: cs.append(copy.deepcopy(cs[1])), "blocked"),
    (lambda cs: cs[1].update(name="another/ila"), "blocked"),
    (lambda cs: cs[1].update(uuid=""), "incomplete"),
    (lambda cs: cs[1].update(uuid="wrong"), "incomplete"),
    (lambda cs: cs[1]["pins"].append(copy.deepcopy(cs[1]["pins"][0])), "blocked"),
    (lambda cs: cs[1]["pins"][0].update(rightIndex=31), "blocked"),
    (lambda cs: cs[1]["pins"][0].update(direction="OUT"), "blocked"),
    (lambda cs: cs[1]["pins"][0].update(portIndex=7), "blocked"),
    (lambda cs: cs[1]["pins"][0].pop("leftIndex"), "incomplete"),
    (lambda cs: cs[1]["pins"][0].pop("portIndex"), "incomplete"),
    (lambda cs: cs[1]["pins"][0].update(isVector=False), "blocked"),
    (lambda cs: cs[1]["pins"][0].update(leftIndex="nonsense"), "blocked"),
    (lambda cs: cs[1].update(pins=None), "blocked"),
    (lambda cs: cs[1]["pins"][0].update(nets=[None]), "blocked"),
])
def test_ltx_failures_are_not_silent_success(pair, edit, status):
    change_ltx(pair, edit)
    assert check(pair)["status"] == status


def test_descending_indices_preserve_width(pair):
    change_ltx(pair, lambda cs: cs[1]["pins"][0].update(leftIndex=63, rightIndex=0))
    assert check(pair)["status"] == "consistent"


def test_vio_directions_and_widths(pair):
    change_ltx(pair, lambda cs: cs[1].update(type="VIO_V2"))
    expected = copy.deepcopy(EXPECTED)
    expected["cores"][0]["type"] = "VIO_V2"
    result = check(pair, expected)
    assert result["status"] == "consistent"
    assert result["ltx"]["cores"][1]["is_vio"]
    assert len(parse_ltx(str(pair[1])).vio_cores) == 1


@pytest.mark.parametrize("uuid,status", [
    ("{00112233-4455-6677-8899-AABBCCDDEEFF}", "consistent"),
    ("11112233445566778899AABBCCDDEEFF", "blocked"),
])
def test_expected_uuid_is_only_ltx_evidence(pair, uuid, status):
    expected = copy.deepcopy(EXPECTED)
    expected["cores"][0]["uuid"] = uuid
    result = check(pair, expected)
    assert result["status"] == status
    assert result["pairing"] == "unverified"


@pytest.mark.parametrize("raw", [
    b'{"ltx_root":{"ltx_data":null}}',
    b'{"ltx_root":{"ltx_data":[{"debug_cores":{}}]}}',
    b'{"ltx_root":{},"ltx_root":{}}',
    b'{"ltx_root":{"bad":"\xff"}}',
    b'<LTX/>',
])
def test_bad_ltx_has_structured_failure(pair, raw):
    pair[1].write_bytes(raw)
    result = check(pair)
    assert result["status"] == "blocked"
    assert any(c["code"] == "ltx.parse" for c in result["checks"])


def test_bom_is_supported(pair):
    pair[1].write_bytes(b"\xef\xbb\xbf" + pair[1].read_bytes())
    assert check(pair)["status"] == "consistent"


@pytest.mark.parametrize("expected", [
    [], {"unknown": 1}, {"part": ""}, {"cores": []}, {"cores": None},
    {"cores": [{"name": "ila", "type": "ILA_V3", "probes": [{"name": "p", "width": True}]}]},
])
def test_bad_expectations_rejected(pair, expected):
    with pytest.raises(ValueError):
        check(pair, expected)


def test_file_changes_while_other_artifact_is_parsed(pair, monkeypatch):
    import vivado_mcp.analysis.debug_artifacts as module

    original = module.parse_ltx

    def read_and_change(path):
        result = original(path)
        pair[0].write_bytes(_build_bit(design="changed"))
        return result

    monkeypatch.setattr(module, "parse_ltx", read_and_change)
    result = check(pair)
    assert result["status"] == "blocked"
    assert any(c["code"] == "bit.changed" for c in result["checks"])


def test_missing_file_is_reported_and_other_file_still_inspected(pair):
    pair[0].unlink()
    result = check(pair)
    assert result["status"] == "blocked"
    assert result["ltx"]["cores"]


@pytest.mark.parametrize("field,value", [("type", []), ("uuid", 1), ("probes", {})])
def test_invalid_core_field_types_raise_value_error(pair, field, value):
    expected = copy.deepcopy(EXPECTED)
    expected["cores"][0][field] = value
    with pytest.raises(ValueError):
        check(pair, expected)


@pytest.mark.parametrize("field,value", [
    ("direction", []), ("port_index", True), ("width", 0), ("name", ""),
])
def test_invalid_probe_field_types_raise_value_error(pair, field, value):
    expected = copy.deepcopy(EXPECTED)
    expected["cores"][0]["probes"][0][field] = value
    with pytest.raises(ValueError):
        check(pair, expected)


def test_duplicate_uuid_even_with_distinct_names_blocks(pair):
    def edit(cores):
        another = copy.deepcopy(cores[1])
        another["name"] = "another/ila"
        cores.append(another)

    change_ltx(pair, edit)
    result = check(pair)
    assert result["status"] == "blocked"
    assert any(c["code"] == "ltx.uuids" for c in result["checks"])


@pytest.mark.parametrize("kind", ["bit", "ltx"])
def test_parser_detects_replacement_during_read(pair, monkeypatch, kind):
    import builtins

    import vivado_mcp.analysis.bit_header_parser as bits
    import vivado_mcp.analysis.ltx_parser as ltxs

    module = bits if kind == "bit" else ltxs
    target = pair[0 if kind == "bit" else 1]
    replacement = target.with_suffix(".replacement")
    replacement.write_bytes(target.read_bytes())
    original = builtins.open

    class Reader:
        def __init__(self, source):
            self.source = source
            self.changed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def fileno(self):
            return self.source.fileno()

        def read(self, size=-1):
            data = self.source.read(size)
            if not self.changed:
                replacement.replace(target)
                self.changed = True
            return data

    monkeypatch.setattr(module, "open", lambda *a, **kw: Reader(original(*a, **kw)), raising=False)
    parser = parse_bit if kind == "bit" else parse_ltx
    with pytest.raises(ValueError, match="发生变化"):
        parser(str(target))
