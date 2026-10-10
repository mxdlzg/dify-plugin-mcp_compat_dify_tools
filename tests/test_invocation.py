"""Tests for argument merging, protection of server config, and validation."""

import pytest

from core.mcp_handler import dispatch, rpc_error
from core.tool_adapter import (
    ToolValidationError,
    adapt_tool_entry,
    adapt_tools,
    prepare_invoke_parameters,
    to_mcp_tool,
)

from fixtures import (
    no_llm_tool,
    number_bool_tool,
    object_array_tool,
    select_tool,
    string_only_tool,
    tavily_selector_shape,
)


def _tavily():
    return adapt_tool_entry(tavily_selector_shape())


# --- Test 4: call-parameter merging ---

def test_merge_client_arg_with_server_config():
    tool = _tavily()
    merged = prepare_invoke_parameters(tool, {"query": "Hello"})
    assert merged == {"query": "Hello", "include_answer": "false"}
    # include_answer came from Dify config and stayed a string.
    assert isinstance(merged["include_answer"], str)


def test_client_cannot_override_server_pinned_config():
    tool = _tavily()
    with pytest.raises(ToolValidationError, match="fixed server-side configuration"):
        prepare_invoke_parameters(tool, {"query": "Hello", "include_answer": "advanced"})


def test_client_cannot_send_unknown_param():
    tool = _tavily()
    with pytest.raises(ToolValidationError, match="unknown parameter"):
        prepare_invoke_parameters(tool, {"query": "x", "bogus": 1})


# --- Test 5: missing required parameter ---

def test_missing_required_raises_before_invocation():
    tool = _tavily()
    with pytest.raises(ToolValidationError, match="missing required parameter"):
        prepare_invoke_parameters(tool, {})
    # Tavily's config pins include_answer (required=True) yet it is server-side,
    # so only `query` is enforced on the client.
    with pytest.raises(ToolValidationError):
        prepare_invoke_parameters(tool, {"include_answer": "basic"})


# --- Test 6: generality across tool shapes ---

def test_string_only_tool():
    tool = adapt_tool_entry(string_only_tool())
    assert to_mcp_tool(tool)["inputSchema"]["properties"]["text"]["type"] == "string"
    assert prepare_invoke_parameters(tool, {"text": "hi"}) == {"text": "hi"}


def test_number_and_boolean_coercion():
    tool = adapt_tool_entry(number_bool_tool())
    schema = to_mcp_tool(tool)["inputSchema"]
    assert schema["properties"]["count"]["type"] == "number"
    assert schema["properties"]["flag"]["type"] == "boolean"
    assert prepare_invoke_parameters(tool, {"count": 3})["count"] == 3
    assert prepare_invoke_parameters(tool, {"count": "5"})["count"] == 5
    assert prepare_invoke_parameters(tool, {"count": 1, "flag": "true"})["flag"] is True
    with pytest.raises(ToolValidationError):
        prepare_invoke_parameters(tool, {"count": "abc"})


def test_object_and_array_types():
    tool = adapt_tool_entry(object_array_tool())
    schema = to_mcp_tool(tool)["inputSchema"]
    assert schema["properties"]["meta"]["type"] == "object"
    assert schema["properties"]["meta"]["properties"]["a"] == {"type": "string"}
    assert schema["properties"]["tags"]["type"] == "array"
    assert schema["properties"]["tags"]["items"] == {"type": "string"}
    # object/array values pass through untouched (no blind coercion)
    merged = prepare_invoke_parameters(tool, {"meta": {"a": "z", "b": 2}})
    assert merged["meta"] == {"a": "z", "b": 2}


def test_tool_without_llm_params_has_empty_schema():
    tool = adapt_tool_entry(no_llm_tool())
    schema = to_mcp_tool(tool)["inputSchema"]
    assert schema == {"type": "object", "properties": {}, "required": []}
    # No client args accepted; server config still injected.
    assert prepare_invoke_parameters(tool, {}) == {"region": "us"}
    with pytest.raises(ToolValidationError):
        prepare_invoke_parameters(tool, {"anything": 1})


def test_select_enum_validation():
    tool = adapt_tool_entry(select_tool())
    assert prepare_invoke_parameters(tool, {"mode": "advanced"})["mode"] == "advanced"
    with pytest.raises(ToolValidationError, match="must be one of"):
        prepare_invoke_parameters(tool, {"mode": "nope"})


# --- Test 7: protocol-level flow through the shared dispatcher ---

def test_unknown_tool_is_tool_error_not_protocol_error():
    settings = {"tools": [string_only_tool()]}
    resp = dispatch("tools/call", 1, {"name": "ghost", "arguments": {}}, settings,
                    lambda t, p: [])
    assert "error" not in resp
    assert resp["result"]["isError"] is True
    assert "Unknown tool" in resp["result"]["content"][0]["text"]


def test_tools_call_validation_failure_is_tool_error():
    settings = {"tools": [tavily_selector_shape()]}
    resp = dispatch("tools/call", 2, {"name": "tavily_search", "arguments": {}},
                    settings, lambda t, p: [])
    assert resp["result"]["isError"] is True
    assert "missing required" in resp["result"]["content"][0]["text"]


def test_tools_call_success_path(fake_invoke):
    settings = {"tools": [tavily_selector_shape()]}
    resp = dispatch(
        "tools/call", 3,
        {"name": "tavily_search", "arguments": {"query": "python 3.12"}},
        settings, fake_invoke,
    )
    assert resp["result"]["isError"] is False
    assert resp["result"]["content"][0]["text"] == "ran tavily_search"
    # The mock runtime received the merged parameters.
    assert fake_invoke.calls[0]["parameters"] == {
        "query": "python 3.12", "include_answer": "false",
    }


def test_runtime_exception_is_masked(fake_invoke):
    def boom(tool, params):
        raise RuntimeError("secret api key leaked in message")

    settings = {"tools": [string_only_tool()]}
    resp = dispatch("tools/call", 4, {"name": "echo", "arguments": {"text": "x"}},
                    settings, boom)
    assert resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert "secret api key" not in text  # internal detail not echoed to client
    assert "failed during execution" in text


def test_unsupported_method_returns_protocol_error():
    resp = dispatch("resources/list", 5, {}, {"tools": []}, lambda t, p: [])
    assert resp["error"]["code"] == -32601
