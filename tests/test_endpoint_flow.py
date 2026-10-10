"""End-to-end flow through the real endpoint classes with a fake session.

Validates that the rewritten Streamable HTTP endpoint and the SSE message
endpoint wire the shared dispatcher correctly: initialize handshake, tools/list
schema, and tools/call reaching the (mocked) Dify tool runtime.
"""

import json
import types

from endpoints.mcp_post import McpPostEndpoint
from endpoints.messages import MessageEndpoint


class FakeStorage:
    def __init__(self):
        self.store = {}

    def set(self, k, v):
        self.store[k] = v

    def get(self, k):
        return self.store[k]

    def exist(self, k):
        return k in self.store

    def delete(self, k):
        del self.store[k]


class FakeToolInvocation:
    def __init__(self):
        self.calls = []

    def invoke(self, *, provider_type, provider, tool_name, parameters):
        self.calls.append(
            {
                "provider_type": provider_type,
                "provider": provider,
                "tool_name": tool_name,
                "parameters": parameters,
            }
        )
        from dify_plugin.entities.tool import ToolInvokeMessage
        return [
            ToolInvokeMessage(
                type=ToolInvokeMessage.MessageType.TEXT,
                message=ToolInvokeMessage.TextMessage(text=f"results for {parameters['query']}"),
            )
        ]


class FakeSession:
    def __init__(self):
        self.tool = FakeToolInvocation()
        self.storage = FakeStorage()


def _make_request(body: dict):
    # Only .json is read by the endpoints.
    return types.SimpleNamespace(json=body, args={})


def _endpoint_with_session(cls):
    ep = cls.__new__(cls)
    ep.session = FakeSession()
    return ep


TAVILY = {
    "enabled": True,
    "type": "builtin",
    "provider_name": "langgenius/tavily/tavily",
    "tool_name": "tavily_search",
    "tool_label": "Tavily",
    "tool_description": "search",
    "tool_parameters": {
        "query": {"name": "query", "type": "string", "required": True,
                   "description": "query"},
        "include_answer": {"name": "include_answer", "type": "select",
                            "required": True, "description": "a",
                            "options": [{"value": "false"}, {"value": "advanced"}]},
    },
    "tool_configuration": {"include_answer": {"type": "constant", "value": "false"}},
}
SETTINGS = {"tools": [TAVILY]}


# --- Streamable HTTP ---

def test_streamable_initialize_returns_session_header():
    ep = _endpoint_with_session(McpPostEndpoint)
    resp = ep._invoke(_make_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                      {}, SETTINGS)
    assert resp.status_code == 200
    assert "mcp-session-id" in resp.headers
    payload = json.loads(resp.get_data())
    assert payload["result"]["serverInfo"]["name"] == "MCP Compatible Dify Tools"


def test_streamable_tools_list():
    ep = _endpoint_with_session(McpPostEndpoint)
    resp = ep._invoke(_make_request({"id": 2, "method": "tools/list"}), {}, SETTINGS)
    tools = json.loads(resp.get_data())["result"]["tools"]
    assert tools[0]["name"] == "tavily_search"
    assert tools[0]["inputSchema"]["properties"]["query"]["type"] == "string"
    assert tools[0]["inputSchema"]["required"] == ["query"]
    assert "include_answer" not in tools[0]["inputSchema"]["properties"]


def test_streamable_tools_call_full_flow():
    ep = _endpoint_with_session(McpPostEndpoint)
    resp = ep._invoke(
        _make_request({"id": 3, "method": "tools/call",
                        "params": {"name": "tavily_search",
                                    "arguments": {"query": "python 3.12"}}}),
        {}, SETTINGS,
    )
    payload = json.loads(resp.get_data())
    assert payload["result"]["isError"] is False
    assert "python 3.12" in payload["result"]["content"][0]["text"]
    call = ep.session.tool.calls[0]
    assert call["parameters"] == {"query": "python 3.12", "include_answer": "false"}
    assert call["provider"] == "langgenius/tavily/tavily"
    assert call["provider_type"].value == "builtin"


# --- Legacy SSE message endpoint (same core, delivered via storage) ---

def test_sse_message_tools_call_via_storage():
    ep = _endpoint_with_session(MessageEndpoint)
    req = types.SimpleNamespace(json={"id": 4, "method": "tools/call",
                                       "params": {"name": "tavily_search",
                                                   "arguments": {"query": "hi"}}},
                                 args={"session_id": "abc"})
    resp = ep._invoke(req, {}, SETTINGS)
    assert resp.status_code == 202
    stored = json.loads(ep.session.storage.get("abc"))
    assert stored["result"]["isError"] is False
    assert ep.session.tool.calls[0]["parameters"] == {
        "query": "hi", "include_answer": "false",
    }


def test_sse_message_initialize_via_storage():
    ep = _endpoint_with_session(MessageEndpoint)
    req = types.SimpleNamespace(json={"id": 5, "method": "initialize"},
                                 args={"session_id": "sess"})
    resp = ep._invoke(req, {}, SETTINGS)
    assert resp.status_code == 202
    stored = json.loads(ep.session.storage.get("sess"))
    assert stored["result"]["capabilities"]["tools"] == {"listChanged": False}
