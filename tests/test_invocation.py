"""Tests for argument merging, protection of server config, and validation."""

import pytest

from core.mcp_handler import dispatch, rpc_error
from core.tool_adapter import (
    ToolConfigError,
    ToolValidationError,
    adapt_tool_entry,
    adapt_tools,
    prepare_invoke_parameters,
    to_mcp_tool,
)

from fixtures import (
    form_mixed_tool,
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


# --- Dify 1.17.1 `mixed` server-config values (Tavily project_id scenario) ---

def test_mixed_fixed_text_is_sent_as_string():
    tool = adapt_tool_entry(form_mixed_tool("proj-123"))
    merged = prepare_invoke_parameters(tool, {"query": "hi"})
    assert merged == {"query": "hi", "project_id": "proj-123"}


def test_mixed_empty_string_is_sent_unchanged():
    # "" is a real configured value distinct from null; it must be sent.
    tool = adapt_tool_entry(form_mixed_tool(""))
    merged = prepare_invoke_parameters(tool, {"query": "hi"})
    assert merged["project_id"] == ""


def test_mixed_null_optional_param_is_omitted():
    # Tavily project_id left empty arrives as mixed-null: omit the key so the
    # tool applies its own default instead of receiving a literal null.
    tool = adapt_tool_entry(form_mixed_tool(None))
    merged = prepare_invoke_parameters(tool, {"query": "hi"})
    assert merged == {"query": "hi"}


def test_mixed_variable_reference_raises_config_error():
    tool = adapt_tool_entry(form_mixed_tool("{{#sys.query#}}"))
    with pytest.raises(ToolConfigError, match="cannot be resolved"):
        prepare_invoke_parameters(tool, {"query": "hi"})


def test_required_form_param_with_null_config_raises():
    tool = adapt_tool_entry(form_mixed_tool(None, required=True))
    with pytest.raises(ToolConfigError, match="required parameter 'project_id'"):
        prepare_invoke_parameters(tool, {"query": "hi"})


def test_required_form_param_never_configured_raises():
    tool = adapt_tool_entry(form_mixed_tool(None, required=True, raw_settings={}))
    with pytest.raises(ToolConfigError, match="not configured"):
        prepare_invoke_parameters(tool, {"query": "hi"})


def test_falsy_server_values_survive_the_merge():
    # 0 / False / "" are real values: never treated as "not configured".
    settings = {
        "project_id": {"type": "mixed", "value": ""},
        "max_results": {"type": "constant", "value": 0},
        "notify": {"type": "constant", "value": False},
    }
    tool = adapt_tool_entry(form_mixed_tool(None, raw_settings=settings))
    merged = prepare_invoke_parameters(tool, {"query": "hi"})
    assert merged["project_id"] == ""
    assert merged["max_results"] == 0
    assert merged["notify"] is False


def test_client_still_cannot_override_mixed_form_param():
    # The None-omission path must not open a client override for form params.
    tool = adapt_tool_entry(form_mixed_tool(None))
    with pytest.raises(ToolValidationError, match="fixed server-side configuration"):
        prepare_invoke_parameters(tool, {"query": "hi", "project_id": "hack"})
