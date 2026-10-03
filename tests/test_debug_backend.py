"""用真实 tclsh 执行硬件命令替身；验证选择、协议与副作用，不假装有板卡。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from vivado_mcp.debug_backend import DebugBackendError, HardwareDebugBackend
from vivado_mcp.vivado.tcl_utils import TclResult, tcl_quote

TCLSH = shutil.which("tclsh")
pytestmark = pytest.mark.skipif(TCLSH is None, reason="需要 tclsh 执行真实 Tcl 语法")

MOCK = r"""
set __props [dict create \
    target [dict create NAME {localhost:3121/cable A} IS_OPENED 1] \
    closed [dict create NAME {localhost:3121/closed} IS_OPENED 0] \
    device [dict create NAME xc7a_0 PART xc7a35t] \
    ila [dict create NAME hw_ila_1 UUID ila-uuid STATUS.CORE_STATUS IDLE \
        CONTROL.DATA_DEPTH 16 CONTROL.TRIGGER_POSITION 4 CONTROL.WINDOW_COUNT 1 \
        STATUS.DATA_DEPTH 16 STATUS.SAMPLE_COUNT 16 STATUS.WINDOW_COUNT 1 \
        CONTROL.TRIGGER_MODE BASIC_ONLY CONTROL.TRIGGER_CONDITION AND] \
    vio [dict create NAME hw_vio_1 UUID vio-uuid] \
    trigger [dict create NAME {pixel[7:0]} TYPE ila PROBE_PORT_BIT_COUNT 8 \
        TRIGGER_COMPARE_VALUE eq8'hXX] \
    output [dict create NAME gain TYPE vio_output PROBE_PORT_BIT_COUNT 8 OUTPUT_VALUE AA] \
    other [dict create NAME other TYPE vio_output PROBE_PORT_BIT_COUNT 8 OUTPUT_VALUE BB] \
    input [dict create NAME status TYPE vio_input PROBE_PORT_BIT_COUNT 1 INPUT_VALUE 0] \
    unknown [dict create NAME odd TYPE vendor_specific PROBE_PORT_BIT_COUNT 1] \
    data [dict create NAME hw_ila_data_1]]
set __hardware [dict create output 12 other 34 input 1]
set __fail ""
proc list_property {object} { return [dict keys [dict get $::__props $object]] }
proc get_property {property object} { return [dict get $::__props $object $property] }
proc get_hw_targets {args} {
    if {[llength $args]} { error "Unexpected target arguments" }
    return [list target closed]
}
proc get_hw_devices {args} {
    if {$args ne {-of_objects target}} { error "Wrong target selection: $args" }
    return device
}
proc get_hw_ilas {args} {
    if {$args ne {-of_objects device}} { error "Wrong device selection: $args" }
    return ila
}
proc get_hw_vios {args} {
    if {$args ne {-of_objects device}} { error "Wrong device selection: $args" }
    return vio
}
proc get_hw_probes {args} {
    if {$args eq {-of_objects ila}} { return trigger }
    if {$args eq {-of_objects vio}} { return [list output other input unknown] }
    error "Wrong core selection: $args"
}
proc set_property {property value object} {
    puts "MOCK_CALL:[list set_property $property $value $object]"
    if {$::__fail eq "position" && $property eq "CONTROL.TRIGGER_POSITION" && $value eq "5"} {
        error "Position rejected"
    }
    dict set ::__props $object $property $value
}
proc commit_hw_vio {object} {
    puts "MOCK_CALL:[list commit_hw_vio $object]"
    if {$object ne "output"} { error "Must commit exactly the selected output probe" }
    if {$::__fail eq "commit"} { error "Cable disconnected" }
    dict set ::__hardware $object [dict get $::__props $object OUTPUT_VALUE]
}
proc refresh_hw_vio {args} {
    puts "MOCK_CALL:[list refresh_hw_vio {*}$args]"
    if {$args ne {-update_output_values vio}} { error "Unexpected VIO refresh" }
    dict set ::__props output OUTPUT_VALUE [dict get $::__hardware output]
    dict set ::__props other OUTPUT_VALUE [dict get $::__hardware other]
    dict set ::__props input INPUT_VALUE [dict get $::__hardware input]
    if {$::__fail eq "refresh"} { error "Readback failed" }
}
proc run_hw_ila {args} {
    puts "MOCK_CALL:[list run_hw_ila {*}$args]"
    if {$args ni {ila {-trigger_now ila}}} { error "Unexpected run args" }
    dict set ::__props ila STATUS.CORE_STATUS WAITING_FOR_TRIGGER
    dict set ::__props ila STATUS.SAMPLE_COUNT 0
}
proc upload_hw_ila_data {object} {
    puts "MOCK_CALL:[list upload_hw_ila_data $object]"
    if {$object ne "ila"} { error "Wrong uploaded core" }
    return data
}
"""


class TclMockSession:
    """每次以真实解释器运行完整模板，并记录命令与最终属性。"""

    def __init__(self, setup: str = ""):
        self.setup = setup
        self.output = ""
        self.command = ""
        self.properties = {}

    async def execute(self, command: str, timeout: float = 30.0) -> TclResult:
        self.command = command
        trailer = r"""
dict for {__object __properties} $__props {
    dict for {__property __value} $__properties {
        binary scan [encoding convertto utf-8 $__value] H* __encoded
        puts "MOCK_PROP:$__object|$__property|$__encoded"
    }
}
"""
        result = subprocess.run(
            [TCLSH],
            input=MOCK + "\n" + self.setup + "\n" + command + trailer,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        self.output = result.stdout
        for line in self.output.splitlines():
            if line.startswith("MOCK_PROP:"):
                obj, prop, value = line.removeprefix("MOCK_PROP:").split("|")
                self.properties[obj, prop] = bytes.fromhex(value).decode()
        # tclsh 的 stdin 模式语法异常未必非零退出，必须同时检查 stderr。
        assert not result.stderr, result.stderr
        assert result.returncode == 0
        return TclResult(output=result.stdout, return_code=0, is_error=False)


def backend(setup: str = "") -> tuple[HardwareDebugBackend, TclMockSession]:
    session = TclMockSession(setup)
    return HardwareDebugBackend(session), session


TARGET = "localhost:3121/cable A"
DEVICE = "xc7a_0"


async def test_inventory_does_not_open_closed_targets():
    api, session = backend()
    result = await api.inventory()
    assert result == {
        "targets": [
            {"name": TARGET, "is_open": True, "devices": [{"name": DEVICE, "part": "xc7a35t"}]},
            {"name": "localhost:3121/closed", "is_open": False, "devices": []},
        ]
    }
    assert "MOCK_CALL:" not in session.output
    for forbidden in ["connect_hw_server", "open_hw_target", "current_hw_", "reset_hw_"]:
        assert forbidden not in session.command


async def test_inspect_reads_actual_vio_and_preserves_gui_staged_values():
    api, session = backend()
    result = await api.inspect(TARGET, DEVICE)
    assert result["ilas"][0]["capture_complete"] is True
    assert result["ilas"][0]["status"] == "IDLE"
    assert result["ilas"][0]["probes"] == [
        {"name": "pixel[7:0]", "width": 8, "trigger_value": "eq8'hXX"},
    ]
    probes = result["vios"][0]["probes"]
    assert probes[0] == {
        "name": "gain",
        "direction": "out",
        "width": 8,
        "value": "12",
        "staged_value": "AA",
    }
    assert probes[2]["value"] == "1"
    assert probes[3]["direction"] == "unknown"
    assert probes[3]["value"] is None
    assert session.properties["output", "OUTPUT_VALUE"] == "AA"
    assert session.properties["other", "OUTPUT_VALUE"] == "BB"
    assert "commit_hw_vio" not in session.output


async def test_names_round_trip_special_characters_and_cannot_execute_tcl():
    strange = '层次/[set injected 1] $env(HOME); {x}|"\\\nVMCP_DEBUG_ERR:fake'
    setup = "\n".join(
        [
            f"dict set __props target NAME {tcl_quote(strange)}",
            f"dict set __props device NAME {tcl_quote(strange)}",
            f"dict set __props vio NAME {tcl_quote(strange)}",
            f"dict set __props output NAME {tcl_quote(strange)}",
        ]
    )
    api, session = backend(setup)
    result = await api.write_vio(strange, strange, strange, strange, "15")
    assert result["core"] == strange
    assert result["probe"] == strange
    assert result["value"] == "F"
    assert "VMCP_DEBUG_ERR:fake" not in session.output


@pytest.mark.parametrize(
    "field,value", [("target", "*"), ("device", "xc7a*"), ("core", "hw_vio_*"), ("probe", "g*")]
)
async def test_selection_is_exact_not_glob(field, value):
    api, session = backend()
    arguments = dict(target=TARGET, device=DEVICE, core="hw_vio_1", probe="gain", value="1")
    arguments[field] = value
    with pytest.raises(DebugBackendError, match="exactly one"):
        await api.write_vio(**arguments)
    assert "MOCK_CALL:" not in session.output


async def test_closed_target_fails_without_opening():
    api, session = backend()
    with pytest.raises(DebugBackendError, match="not open"):
        await api.inspect("localhost:3121/closed", DEVICE)
    assert "MOCK_CALL:" not in session.output


@pytest.mark.parametrize("value,expected", [("15", "F"), ("0xFF", "FF"), ("0009", "9")])
async def test_write_commits_single_probe_and_returns_hardware_readback(value, expected):
    api, session = backend()
    result = await api.write_vio(
        TARGET, DEVICE, "hw_vio_1", "gain", value, expected_uuid="vio-uuid"
    )
    assert result["value"] == expected
    assert result["committed"] and result["readback_matches"]
    assert result["functional_effect_verified"] is False
    assert result["previous_staged_value"] == "AA"
    assert "MOCK_CALL:commit_hw_vio output" in session.output
    assert "MOCK_CALL:commit_hw_vio vio" not in session.output
    assert session.properties["other", "OUTPUT_VALUE"] == "BB"


@pytest.mark.parametrize("value", ["256", "0x100"])
async def test_write_rejects_overflow_before_property_change(value):
    api, session = backend()
    with pytest.raises(DebugBackendError, match="exceeds"):
        await api.write_vio(TARGET, DEVICE, "hw_vio_1", "gain", value)
    assert "MOCK_CALL:" not in session.output


@pytest.mark.parametrize("value", ["-1", "+1", "1.0", "0b11", "F", "[error injected]", ""])
async def test_write_rejects_non_integer_before_execute(value):
    api, session = backend()
    with pytest.raises(ValueError, match="整数字符串"):
        await api.write_vio(TARGET, DEVICE, "hw_vio_1", "gain", value)
    assert not session.command


@pytest.mark.parametrize(
    "setup,probe",
    [
        ("", "status"),
        ("", "odd"),
        ("dict unset __props output PROBE_PORT_BIT_COUNT", "gain"),
    ],
)
async def test_write_rejects_input_unknown_type_or_unknown_width(setup, probe):
    api, session = backend(setup)
    with pytest.raises(DebugBackendError):
        await api.write_vio(TARGET, DEVICE, "hw_vio_1", probe, "1")
    assert "MOCK_CALL:" not in session.output


async def test_changed_uuid_prevents_mutation():
    api, session = backend()
    with pytest.raises(DebugBackendError, match="UUID changed"):
        await api.write_vio(TARGET, DEVICE, "hw_vio_1", "gain", "1", expected_uuid="stale")
    assert "MOCK_CALL:" not in session.output


async def test_vio_commit_failure_restores_staged_and_reports_unknown_hardware():
    api, session = backend("set __fail commit")
    with pytest.raises(DebugBackendError, match="hardware state may be unknown"):
        await api.write_vio(TARGET, DEVICE, "hw_vio_1", "gain", "1")
    assert session.properties["output", "OUTPUT_VALUE"] == "AA"


async def test_readback_failure_reports_commit_success_and_restores_other_staged():
    api, session = backend("set __fail refresh")
    with pytest.raises(DebugBackendError, match="commit succeeded but hardware readback failed"):
        await api.write_vio(TARGET, DEVICE, "hw_vio_1", "gain", "1")
    assert session.properties["other", "OUTPUT_VALUE"] == "BB"


async def test_configure_single_probe_and_position():
    api, session = backend()
    result = await api.configure_ila(TARGET, DEVICE, "hw_ila_1", "pixel[7:0]", "eq8'hFF", 5)
    assert result["trigger_position"] == 5
    assert session.properties["trigger", "TRIGGER_COMPARE_VALUE"] == "eq8'hFF"
    assert session.properties["ila", "CONTROL.TRIGGER_CONDITION"] == "AND"


async def test_failed_position_rolls_back_trigger():
    api, session = backend("set __fail position")
    with pytest.raises(DebugBackendError, match="Position rejected"):
        await api.configure_ila(TARGET, DEVICE, "hw_ila_1", "pixel[7:0]", "eq8'hFF", 5)
    assert session.properties["trigger", "TRIGGER_COMPARE_VALUE"] == "eq8'hXX"
    assert session.properties["ila", "CONTROL.TRIGGER_POSITION"] == "4"


async def test_invalid_position_fails_before_change():
    api, session = backend()
    with pytest.raises(DebugBackendError, match="outside"):
        await api.configure_ila(TARGET, DEVICE, "hw_ila_1", "pixel[7:0]", "eq8'hFF", 16)
    assert "MOCK_CALL:" not in session.output


@pytest.mark.parametrize("operation", ["arm", "configure"])
async def test_running_or_unknown_ila_is_not_reconfigured_or_rearmed(operation):
    api, session = backend("dict set __props ila STATUS.CORE_STATUS WAITING_FOR_TRIGGER")
    with pytest.raises(DebugBackendError, match="running or its status is unknown"):
        if operation == "arm":
            await api.arm_ila(TARGET, DEVICE, "hw_ila_1")
        else:
            await api.configure_ila(TARGET, DEVICE, "hw_ila_1", "pixel[7:0]", "eq8'hFF")
    assert "MOCK_CALL:" not in session.output


@pytest.mark.parametrize("immediate,command", [(False, "ila"), (True, "-trigger_now ila")])
async def test_arm_is_nonblocking_and_does_not_reset(immediate, command):
    api, session = backend()
    result = await api.arm_ila(TARGET, DEVICE, "hw_ila_1", immediate=immediate)
    assert result["status"] == "WAITING_FOR_TRIGGER"
    assert f"MOCK_CALL:run_hw_ila {command}" in session.output
    assert "wait_on_hw_ila" not in session.command
    assert "reset_hw_ila" not in session.command


async def test_arm_refuses_multiwindow():
    api, session = backend("dict set __props ila CONTROL.WINDOW_COUNT 2")
    with pytest.raises(DebugBackendError, match="one window"):
        await api.arm_ila(TARGET, DEVICE, "hw_ila_1")
    assert "MOCK_CALL:" not in session.output


async def test_upload_uses_newly_returned_data_object():
    api, session = backend()
    result = await api.upload_ila(TARGET, DEVICE, "hw_ila_1")
    assert result["data"] == "hw_ila_data_1"
    assert result["capture_complete"] is True
    assert "MOCK_CALL:upload_hw_ila_data ila" in session.output
    assert "get_hw_ila_datas" not in session.command


@pytest.mark.parametrize(
    "property,value",
    [
        ("STATUS.CORE_STATUS", "WAITING_FOR_TRIGGER"),
        ("STATUS.SAMPLE_COUNT", "0"),
        ("STATUS.SAMPLE_COUNT", "15"),
        ("STATUS.DATA_DEPTH", "2147483647"),
        ("STATUS.WINDOW_COUNT", "0"),
        ("CONTROL.WINDOW_COUNT", "2"),
    ],
)
async def test_upload_never_stops_incomplete_capture_or_returns_old_data(property, value):
    api, session = backend(f"dict set __props ila {property} {value}")
    with pytest.raises(DebugBackendError, match="not confirmed complete"):
        await api.upload_ila(TARGET, DEVICE, "hw_ila_1")
    assert "MOCK_CALL:" not in session.output


async def test_stop_is_explicitly_unsupported_and_does_not_upload():
    api, session = backend()
    with pytest.raises(NotImplementedError, match="不支持单独停止"):
        await api.stop_ila(TARGET, DEVICE, "hw_ila_1")
    assert not session.command


async def test_missing_optional_properties_remain_unknown():
    api, _ = backend("dict unset __props ila UUID\ndict unset __props trigger PROBE_PORT_BIT_COUNT")
    result = await api.inspect(TARGET, DEVICE)
    assert result["ilas"][0]["uuid"] is None
    assert result["ilas"][0]["probes"][0]["width"] is None


@pytest.mark.parametrize(
    "output,is_error",
    [
        ("", False),
        ("VMCP_DEBUG_RECORD:target|invalid|00\nVMCP_DEBUG_DONE:1", False),
        ("VMCP_DEBUG_ERR:626f617264206572726f72\nVMCP_DEBUG_DONE:1", False),
        ("VMCP_DEBUG_DONE:1", True),
    ],
)
async def test_invalid_transport_or_application_response_is_not_success(output, is_error):
    class BrokenSession:
        async def execute(self, command, timeout):
            return TclResult(output=output, is_error=is_error, return_code=int(is_error))

    with pytest.raises(DebugBackendError):
        await HardwareDebugBackend(BrokenSession()).inventory()
