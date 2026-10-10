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
from core.tool_metadata import (
    ToolMetadataResolver,
    _slim_parameter,
    clear_cache,
    request_headers,
)

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
        {
            "dify_console_api_base": "http://dify.local",
            "dify_console_api_key": "k",
            "dify_workspace_id": "ws-1",
        },
        fetcher=fetcher,
    )


BUILTIN_PROVIDER_LIST = [
    {
        "name": "langgenius/tavily/tavily",
        "label": {"en_US": "Tavily", "zh_Hans": "Tavily"},
        "icon": "http://dify.local/assets/icon.svg",
        "icon_dark": "http://dify.local/assets/icon-dark.svg",
    },
    {
        "name": "langgenius/time/time",
        "label": {"en_US": "Time"},
        "icon": "http://dify.local/assets/time.svg",
        "icon_dark": "",
    },
]


def _icon_http_getter(path):
    assert path == "/console/api/workspaces/current/tools/builtin"
    return BUILTIN_PROVIDER_LIST


def _resolver_with_icons(fetcher):
    return ToolMetadataResolver(
        {
            "dify_console_api_base": "http://dify.local",
            "dify_console_api_key": "k",
            "dify_workspace_id": "ws-1",
        },
        fetcher=fetcher,
        http_getter=_icon_http_getter,
    )


def test_mcp_tool_carries_title_and_icons_from_provider_list():
    resolver = _resolver_with_icons(lambda base, provider: TAVILY_CONSOLE_TOOLS)
    tool = adapt_tool_entry(real_selector_tavily(), resolver)
    mcp = to_mcp_tool(tool)
    assert mcp["title"] == "Tavily Search"  # tool's own label wins
    assert mcp["icons"] == [
        {"src": "http://dify.local/assets/icon.svg", "theme": "light"},
        {"src": "http://dify.local/assets/icon-dark.svg", "theme": "dark"},
    ]


def test_icon_failure_does_not_hide_tool():
    def failing_http(path):
        raise RuntimeError("boom")

    resolver = ToolMetadataResolver(
        {
            "dify_console_api_base": "http://dify.local",
            "dify_console_api_key": "k",
            "dify_workspace_id": "ws-1",
        },
        fetcher=lambda base, provider: TAVILY_CONSOLE_TOOLS,
        http_getter=failing_http,
    )
    tool = adapt_tool_entry(real_selector_tavily(), resolver)
    assert tool is not None
    mcp = to_mcp_tool(tool)
    assert "icons" not in mcp
    assert mcp["title"] == "Tavily Search"


def test_icon_meta_missing_provider_gives_empty_meta():
    resolver = _resolver_with_icons(lambda base, provider: TAVILY_CONSOLE_TOOLS)
    meta = resolver.get_icon_meta("some/unknown/provider")
    assert meta.title == "" and meta.icon == "" and meta.icon_dark == ""


def test_title_falls_back_to_provider_title_when_label_is_raw_id():
    entry = real_selector_tavily()
    entry["tool_label"] = "tavily_search"  # selector persisted the raw id
    resolver = _resolver_with_icons(lambda base, provider: TAVILY_CONSOLE_TOOLS)
    mcp = to_mcp_tool(adapt_tool_entry(entry, resolver))
    assert mcp["title"] == "Tavily"


def test_no_icons_field_when_resolver_has_no_icon_support():
    # resolver without http_getter (fetcher-only tests) must not gain icons
    resolver = _resolver(lambda base, provider: TAVILY_CONSOLE_TOOLS)
    mcp = to_mcp_tool(adapt_tool_entry(real_selector_tavily(), resolver))
    assert "icons" not in mcp
    assert mcp["title"] == "Tavily Search"


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
    # dispatch builds its own ToolMetadataResolver from settings; simulate a
    # warm provider cache (keyed by workspace+provider, slimmed parameters) so
    # no real HTTP happens.
    from core import tool_metadata

    tool_metadata._CACHE[("ws-1", "langgenius/tavily/tavily")] = (
        tool_metadata.time.monotonic(),
        {"tavily_search": [
            {"name": "query", "type": "string", "form": "llm", "required": True},
        ]},
    )
    settings = {
        "tools": [real_selector_tavily()],
        "dify_console_api_base": "http://dify.local",
        "dify_console_api_key": "k",
        "dify_workspace_id": "ws-1",
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


# --- console fetch shape: auth headers and slimmed parameters ---

def test_console_auth_headers_match_verified_curl():
    assert request_headers("secret-key", "ws-abc") == {
        "Authorization": "Bearer secret-key",
        "X-WORKSPACE-ID": "ws-abc",
        "Accept": "application/json",
    }


def test_console_url_keeps_provider_slashes_unescaped():
    # Matches the verified-working path: builtin/{provider with slashes}/tools
    import urllib.parse

    provider = "langgenius/tavily/tavily"
    quoted = urllib.parse.quote(provider, safe="/")
    assert quoted == "langgenius/tavily/tavily"
    url = (
        f"http://api:5001/console/api/workspaces/current/tool-provider/"
        f"builtin/{quoted}/tools"
    )
    assert url.endswith("/tool-provider/builtin/langgenius/tavily/tavily/tools")
    assert "%2F" not in url


def test_resolver_requires_workspace_id():
    resolver = ToolMetadataResolver(
        {"dify_console_api_base": "http://dify.local", "dify_console_api_key": "k"},
        fetcher=lambda base, provider: [],
    )
    assert resolver.configured is False
    assert adapt_tool_entry(real_selector_tavily(), resolver) is None


def test_slim_parameter_keeps_only_mcp_relevant_fields():
    real_console_param = {
        "name": "search_depth",
        "label": {"en_US": "Search Depth", "zh_Hans": "搜索深度"},
        "placeholder": None, "scope": None, "auto_generate": None,
        "template": None, "required": False, "default": "basic",
        "min": None, "max": None, "precision": None,
        "options": [
            {"value": "basic", "label": {"en_US": "Basic"}, "icon": ""},
            {"value": "advanced", "label": {"en_US": "Advanced"}, "icon": ""},
        ],
        "type": "select", "multiple": False,
        "human_description": {"en_US": "The depth of the search."},
        "form": "llm",
        "llm_description": "The depth of the search.",
        "input_schema": None,
    }
    slim = _slim_parameter(real_console_param)
    assert slim == {
        "name": "search_depth",
        "type": "select",
        "form": "llm",
        "required": False,
        "multiple": False,
        "llm_description": "The depth of the search.",
        "options": [{"value": "basic"}, {"value": "advanced"}],
    }
    # and the slimmed param still produces the correct MCP property schema
    schema = to_mcp_tool(adapt_tool_entry(
        {
            "provider_name": "p", "tool_name": "t", "enabled": True,
            "settings": {}, "parameters": {},
        },
        _resolver(lambda base, provider: [
            {"name": "t", "parameters": [real_console_param]},
        ]),
    ))["inputSchema"]
    assert schema["properties"]["search_depth"] == {
        "type": "string",
        "enum": ["basic", "advanced"],
        "description": "The depth of the search.",
    }
