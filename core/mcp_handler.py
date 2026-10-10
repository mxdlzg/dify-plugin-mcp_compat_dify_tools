"""Shared JSON-RPC / MCP method dispatch for both endpoint transports.

``initialize`` stays in each endpoint (the two transports advertise different
capabilities and Streamable HTTP issues a session header), but every other
method is dispatched here so tools/list and tools/call behave identically
over Streamable HTTP and legacy SSE.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from core.tool_adapter import (
    AdaptedTool,
    ToolConfigError,
    ToolValidationError,
    adapt_tools,
    format_invoke_result,
    prepare_invoke_parameters,
    to_mcp_tool,
)

logger = logging.getLogger(__name__)

# (tool, validated-parameters) -> ToolInvokeMessage stream
InvokeTool = Callable[[AdaptedTool, dict[str, Any]], Iterable[ToolInvokeMessage]]


def rpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_call_result(message: str, *, is_error: bool) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": is_error,
    }


def dispatch(
    method: Any,
    req_id: Any,
    params: dict[str, Any],
    settings: Mapping,
    invoke_tool: InvokeTool,
) -> dict[str, Any]:
    if method == "tools/list":
        return _tools_list(req_id, settings)
    if method == "tools/call":
        return _tools_call(req_id, params, settings, invoke_tool)
    return rpc_error(req_id, -32601, f"Unsupported method: {method}")


def _tools_list(req_id: Any, settings: Mapping) -> dict[str, Any]:
    tools = adapt_tools(settings.get("tools"))
    return rpc_result(req_id, {"tools": [to_mcp_tool(t) for t in tools]})


def _tools_call(
    req_id: Any,
    params: dict[str, Any],
    settings: Mapping,
    invoke_tool: InvokeTool,
) -> dict[str, Any]:
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}

    tools = {t.name: t for t in adapt_tools(settings.get("tools"))}
    tool = tools.get(tool_name)
    if tool is None:
        # Per MCP semantics a failed tool call is a tool result with
        # isError=true, not a JSON-RPC protocol error.
        return rpc_result(req_id, _tool_call_result(f"Unknown tool: {tool_name}", is_error=True))

    try:
        prepared = prepare_invoke_parameters(tool, arguments)
    except (ToolValidationError, ToolConfigError) as exc:
        return rpc_result(req_id, _tool_call_result(str(exc), is_error=True))

    try:
        messages = invoke_tool(tool, prepared)
        text = format_invoke_result(messages)
    except Exception:
        # Keep the full traceback server-side; the Dify tool runtime may
        # include provider details that must not travel back to the client.
        logger.exception("Tool '%s' failed during execution", tool_name)
        return rpc_result(
            req_id,
            _tool_call_result(f"Tool '{tool_name}' failed during execution", is_error=True),
        )

    return rpc_result(req_id, _tool_call_result(text, is_error=False))
