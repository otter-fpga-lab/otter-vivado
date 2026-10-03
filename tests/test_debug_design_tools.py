"""经真实 MCP 注册、Context 与后端验证工程准备入口，不启动 Vivado。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import Context

# 复用已独立覆盖语法和副作用的真实 tclsh 夹具，避免再建一层业务 mock。
from tests.test_debug_ip import ILA, PROJECT, TclMockSession
from vivado_mcp.server import AppContext, mcp
from vivado_mcp.vivado.session_manager import SessionManager
from vivado_mcp.vivado.tcl_utils import TclResult


def context(session: TclMockSession | None = None, session_id: str = "board") -> Context:
    manager = SessionManager(vivado_path="unused-test-installation")
    if session is not None:
        session.is_alive = True
        manager._sessions[session_id] = session
    request = SimpleNamespace(lifespan_context=AppContext(session_manager=manager))
    return Context(request_context=request, mcp_server=mcp)


async def call(name: str, arguments: dict, ctx: Context | None = None) -> dict:
    result = await mcp.call_tool(name, arguments, context=ctx)
    assert not result.is_error
    assert len(result.content) == 1 and result.content[0].type == "text"
    return json.loads(result.content[0].text)


async def test_registered_tools_expose_input_contracts_without_context():
    registered = await mcp.list_tools()
    assert len(registered) == 49
    tools = {tool.name: tool for tool in registered}
    contracts = {
        "plan_debug_design": ({"spec"}, {"spec"}),
        "inspect_debug_design": ({"spec", "session_id"}, {"spec"}),
        "prepare_debug_design": (
            {"spec", "expected_project", "session_id"},
            {"spec", "expected_project"},
        ),
    }
    for name, (fields, required) in contracts.items():
        schema = tools[name].input_schema
        assert set(schema["properties"]) == fields
        assert set(schema["required"]) == required
        assert schema["properties"]["spec"]["type"] == "object"
        if "session_id" in fields:
            assert schema["properties"]["session_id"]["type"] == "string"
            assert schema["properties"]["session_id"]["default"] == "default"
        if "expected_project" in fields:
            assert schema["properties"]["expected_project"]["type"] == "object"


@pytest.mark.parametrize(
    "spec",
    [
        ILA,
        {
            "kind": "vio_ip",
            "name": "vio_controls",
            "clock": "clk",
            "outputs": [{"name": "gain", "width": 8, "initial": "0x80"}],
        },
        {"kind": "mark_debug", "name": "mark_pixels", "nets": ["top/pixel[0]"]},
        {
            "kind": "ila_netlist",
            "name": "ila_pixels",
            "clock": "top/pixel_clk",
            "probes": [{"nets": ["top/pixel[0]", "top/pixel[1]"]}],
        },
    ],
)
async def test_offline_plan_runs_through_mcp_without_request_context_or_session(spec):
    plan = await call("plan_debug_design", {"spec": spec})
    assert "error" not in plan
    assert plan["spec"]["kind"] == spec["kind"]
    assert plan["artifacts"]
    assert all(artifact["filename"] and artifact["content"] for artifact in plan["artifacts"])


async def test_schema_required_project_identity_is_enforced_before_tool_execution():
    from mcp.server.mcpserver.exceptions import ToolError

    session = TclMockSession()
    with pytest.raises(ToolError, match="expected_project"):
        await mcp.call_tool(
            "prepare_debug_design", {"spec": ILA, "session_id": "board"}, context=context(session)
        )
    assert not session.command


async def test_inspect_identity_and_named_session_flow_through_prepare():
    session = TclMockSession()
    ctx = context(session)
    inspected = await call("inspect_debug_design", {"spec": ILA, "session_id": "board"}, ctx)
    assert inspected["status"] == "ready"
    assert inspected["project"] == PROJECT
    assert "MOCK_CALL:" not in session.output
    prepared = await call(
        "prepare_debug_design",
        {
            "spec": ILA,
            "expected_project": inspected["project"],
            "session_id": "board",
        },
        ctx,
    )
    assert prepared["status"] == "created"
    assert prepared["project"] == inspected["project"]
    assert prepared["readback"]["CONFIG.C_DATA_DEPTH"] == "4096"
    assert "MOCK_CALL:generate_target all new_ip" in session.output


async def test_prepare_does_not_replace_stale_expected_identity_with_current_identity():
    session = TclMockSession()
    prepared = await call(
        "prepare_debug_design",
        {
            "spec": ILA,
            "expected_project": {**PROJECT, "part": "xc7z020clg400-1"},
            "session_id": "board",
        },
        context(session),
    )
    assert prepared["status"] == "blocked"
    assert "Current project changed" in prepared["error"]
    assert "MOCK_CALL:" not in session.output


@pytest.mark.parametrize("name", ["inspect_debug_design", "prepare_debug_design"])
async def test_missing_named_session_returns_error_without_using_another_session(name):
    session = TclMockSession()
    arguments = {"spec": ILA, "session_id": "missing"}
    if name == "prepare_debug_design":
        arguments["expected_project"] = PROJECT
    result = await call(name, arguments, context(session))
    assert "missing" in result["error"] and "start_session" in result["error"]
    assert not session.command


async def test_validation_errors_are_json_and_do_not_reach_session():
    session = TclMockSession()
    ctx = context(session)
    planned = await call("plan_debug_design", {"spec": {"kind": "unknown"}})
    inspected = await call(
        "inspect_debug_design",
        {
            "spec": {"kind": "unknown"},
            "session_id": "board",
        },
        ctx,
    )
    prepared = await call(
        "prepare_debug_design",
        {
            "spec": ILA,
            "expected_project": {},
            "session_id": "board",
        },
        ctx,
    )
    assert all(set(value) == {"error"} for value in (planned, inspected, prepared))
    assert "expected_project" in prepared["error"]
    assert not session.command


async def test_inspect_protocol_error_and_prepare_partial_state_are_preserved():
    class IncompleteSession(TclMockSession):
        async def execute(self, command, timeout):
            return TclResult("incomplete response", 0, False)

    inspected = await call(
        "inspect_debug_design", {"spec": ILA, "session_id": "board"}, context(IncompleteSession())
    )
    assert set(inspected) == {"error"}
    assert "响应不完整" in inspected["error"]
    session = TclMockSession("set __fail generate")
    prepared = await call(
        "prepare_debug_design",
        {
            "spec": ILA,
            "expected_project": PROJECT,
            "session_id": "board",
        },
        context(session),
    )
    assert prepared["status"] == "partial"
    assert prepared["created"] is True and prepared["generated"] is False
    assert prepared["configuration_validation"] == "verified"
    assert prepared["error"] == "Output generation failed"
    assert prepared["recovery"]
