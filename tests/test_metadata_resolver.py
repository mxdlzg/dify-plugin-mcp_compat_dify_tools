"""Regression tests against the REAL shape Dify's endpoint tool selector
persists (getToolValue/getStructureValue in the Dify web source):

    {provider_name, tool_name, tool_label, tool_description,
     settings:   {name: {value: {type: constant, value: X}}}   # double wrapper
     parameters: {name: {auto: 0|1, value: {type, value}}}
     enabled, type, extra}

No type/required/description anywhere — the schema must come from the
metadata resolver, and tools whose schema cannot be resolved must not be
exposed.
"""

import pytest

from core.mcp_handler import dispatch
from core.tool_adapter import adapt_tool_entry, adapt_tools, prepare_invoke_parameters, to_mcp_tool
from core.tool_metadata import ToolMetadataResolver, clear_cache

from fixtures import FakeToolInvocation


TAVILY_CONSOLE_TOOLS = [
    {
        "name": "tavily_search",
        "label": {"en_US": "Tavily Search"},
        "description": {"en_US": "search"},
        "parameters": [
            {
                "name": "query",
                "type": "string",
                "form": "llm",
                "required": True,
                "llm_description": "Query string for search",
                "label": {"en_US": "Query"},
                "human_description": {"en_US": "Query"},
            },
            {
                "name": "include_answer",
                "type": "select",
                "form": "llm",
                "required": False,
                "llm_description": "Answer by the LLM",
                "options": [
                    {"value": "false", "label": {"en_US": "Not Included"}},
                    {"value": "advanced", "label": {"en_US": "Advanced"}},
                ],
                "label": {"en_US": "Include Answer"},
                "human_description": {"en_US": "Include Answer"},
            },
        ],
    },
]


def real_selector_tavily():
    # Exactly what the Dify web UI saves for the endpoint (per Q2 research).
    return {
        "provider_name": "langgenius/tavily/tavily",
        "tool_name": "tavily_search",
        "tool_label": "Tavily Search",
        "tool_description": "A search engine tailored to LLM agents",
        "settings": {
            "include_answer": {"value": {"type": "constant", "value": "false"}},
        },
        "parameters": {
            "query": {"auto": 1, "value": {"type": "constant", "value": ""}},
        },
        "enabled": True,
        "type": "builtin",
        "extra": {"description": "A search engine tailored to LLM agents"},
    }


def zero_param_tool():
    return {
        "provider_name": "langgenius/time/time",
        "tool_name": "current_time",
        "tool_label": "Current Time",
        "settings": {},
        "parameters": {},
        "enabled": True,
        "type": "builtin",
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def _resolver(fetcher):
    return ToolMetadataResolver(
        {"dify_console_api_base": "http://dify.local", "dify_console_api_key": "k"},
        fetcher=fetcher,
    )


# --- real selector shape resolves its schema through the resolver ---

def test_real_selector_shape_gets_schema_from_console_api():
    resolver = _resolver(lambda base, provider: TAVILY_CONSOLE_TOOLS)
    tool = adapt_tool_entry(real_selector_tavily(), resolver)
    assert tool is not None
    schema = to_mcp_tool(tool)["inputSchema"]
    # query now visible and required (the end-to-end goal)
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["required"] == ["query"]
    # include_answer pinned in settings -> hidden and protected
    assert "include_answer" not in schema["properties"]


def test_real_selector_shape_call_merges_double_wrapped_config():
    resolver = _resolver(lambda base, provider: TAVILY_CONSOLE_TOOLS)
    tool = adapt_tool_entry(real_selector_tavily(), resolver)
    merged = prepare_invoke_parameters(tool, {"query": "python 3.12"})
    # double-wrapped constant unwrapped to the plain string
    assert merged == {"query": "python 3.12", "include_answer": "false"}


def test_auto_zero_parameter_is_pinned_and_hidden():
    entry = real_selector_tavily()
    entry["parameters"]["query"] = {"auto": 0, "value": {"type": "constant", "value": "fixed"}}
    resolver = _resolver(lambda base, provider: TAVILY_CONSOLE_TOOLS)
    tool = adapt_tool_entry(entry, resolver)
    schema = to_mcp_tool(tool)["inputSchema"]
    assert "query" not in schema["properties"]
    assert schema["required"] == []
    merged = prepare_invoke_parameters(tool, {})
    assert merged == {"query": "fixed", "include_answer": "false"}


def test_auto_one_stays_sendable_even_with_default_value():
    # auto=1 with an empty default: agent fills it at call time.
    resolver = _resolver(lambda base, provider: TAVILY_CONSOLE_TOOLS)
    tool = adapt_tool_entry(real_selector_tavily(), resolver)
    assert "query" in tool.sendable_param_names


# --- metadata-missing policy ---

def test_unresolvable_tool_is_not_exposed_without_console_config():
    # No resolver configured at all.
    assert adapt_tool_entry(real_selector_tavily(), None) is None


def test_unresolvable_tool_is_not_exposed_when_fetch_fails():
    def failing(base, provider):
        raise RuntimeError("connection refused")

    resolver = _resolver(failing)
    assert adapt_tool_entry(real_selector_tavily(), resolver) is None


def test_zero_parameter_tool_verified_empty_and_exposed():
    calls = []

    def fetcher(base, provider):
        calls.append(provider)
        return [{"name": "current_time", "parameters": []}]

    resolver = _resolver(fetcher)
    tool = adapt_tool_entry(zero_param_tool(), resolver)
    assert tool is not None
    assert to_mcp_tool(tool)["inputSchema"] == {
        "type": "object", "properties": {}, "required": [],
    }
    assert calls == ["langgenius/time/time"]


def test_tools_list_over_http_flow_uses_console_api():
    resolver_seen = []

    def fetcher(base, provider):
        resolver_seen.append((base, provider))
        return TAVILY_CONSOLE_TOOLS

    # dispatch builds its own ToolMetadataResolver from settings; inject via
    # monkeypatched module-level settings containing base+key and a stub
    # transport by patching the class-level cache directly.
    from core import tool_metadata

    tool_metadata._CACHE["langgenius/tavily/tavily"] = (
        tool_metadata.time.monotonic(),
        {"tavily_search": TAVILY_CONSOLE_TOOLS[0]["parameters"]},
    )
    settings = {
        "tools": [real_selector_tavily()],
        "dify_console_api_base": "http://dify.local",
        "dify_console_api_key": "k",
    }
    resp = dispatch("tools/list", 1, {}, settings, lambda t, p: [])
    tools = resp["result"]["tools"]
    assert tools[0]["inputSchema"]["properties"]["query"]["type"] == "string"

    inv = FakeToolInvocation()
    resp = dispatch(
        "tools/call", 2,
        {"name": "tavily_search", "arguments": {"query": "hello"}},
        settings,
        lambda t, p: inv(t, p),
    )
    assert resp["result"]["isError"] is False
    assert inv.calls[0]["parameters"] == {"query": "hello", "include_answer": "false"}
