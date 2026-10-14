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

from core.file_adapter import FileAdapter, FileAdapterError, FileLimits
from core.tool_adapter import (
    AdaptedTool,
    ToolConfigError,
    ToolValidationError,
    adapt_tools,
    format_invoke_content,
    prepare_invoke_parameters,
    to_mcp_tool,
)
from core.tool_metadata import ToolMetadataResolver

logger = logging.getLogger(__name__)

# (tool, validated-parameters) -> ToolInvokeMessage stream
InvokeTool = Callable[[AdaptedTool, dict[str, Any]], Iterable[ToolInvokeMessage]]

# upload(filename, content, mimetype) -> UploadFileResponse-like object.
# Injected by the endpoints (``self.session.file.upload``) so the handler and
# the file adapter stay testable without a live session.
UploadFile = Callable[[str, bytes, str], Any]


def rpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_call_result(message: str, *, is_error: bool) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": is_error,
    }


def _tool_call_blocks(content: list[dict[str, Any]], *, is_error: bool) -> dict[str, Any]:
    return {"content": content, "isError": is_error}


def dispatch(
    method: Any,
    req_id: Any,
    params: dict[str, Any],
    settings: Mapping,
    invoke_tool: InvokeTool,
    upload_file: UploadFile | None = None,
) -> dict[str, Any]:
    if method == "tools/list":
        return _tools_list(req_id, settings)
    if method == "tools/call":
        return _tools_call(req_id, params, settings, invoke_tool, upload_file)
    return rpc_error(req_id, -32601, f"Unsupported method: {method}")


def _resolver(settings: Mapping) -> ToolMetadataResolver:
    # The provider-level cache lives in core.tool_metadata and survives across
    # requests, so building this per-call is cheap.
    return ToolMetadataResolver(settings)


def _tools_list(req_id: Any, settings: Mapping) -> dict[str, Any]:
    tools = adapt_tools(settings.get("tools"), _resolver(settings))
    return rpc_result(req_id, {"tools": [to_mcp_tool(t) for t in tools]})


def _tools_call(
    req_id: Any,
    params: dict[str, Any],
    settings: Mapping,
    invoke_tool: InvokeTool,
    upload_file: UploadFile | None = None,
) -> dict[str, Any]:
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}

    tools = {t.name: t for t in adapt_tools(settings.get("tools"), _resolver(settings))}
    tool = tools.get(tool_name)
    if tool is None:
        # Per MCP semantics a failed tool call is a tool result with
        # isError=true, not a JSON-RPC protocol error.
        return rpc_result(req_id, _tool_call_result(f"Unknown tool: {tool_name}", is_error=True))

    # Built per call so the total-bytes budget bounds one request, and the
    # upload-reuse cache cannot outlive the signed URL's validity.
    file_adapter = FileAdapter(upload_file, FileLimits.from_settings(settings)) if upload_file else None

    try:
        prepared = prepare_invoke_parameters(tool, arguments, file_adapter=file_adapter)
    except (ToolValidationError, ToolConfigError, FileAdapterError) as exc:
        return rpc_result(req_id, _tool_call_result(str(exc), is_error=True))

    try:
        messages = invoke_tool(tool, prepared)
        content = format_invoke_content(messages)
    except Exception:
        # Keep the full traceback server-side; the Dify tool runtime may
        # include provider details that must not travel back to the client.
        logger.exception("Tool '%s' failed during execution", tool_name)
        return rpc_result(
            req_id,
            _tool_call_result(f"Tool '{tool_name}' failed during execution", is_error=True),
        )

    # Text-only clients ignore image blocks; if the tool returned nothing
    # textual, add a description so the result is not silently empty for them.
    if not any(b["type"] == "text" for b in content):
        summary = ", ".join(
            f"{b['type']} ({b.get('mimeType', 'unknown')})" for b in content
        )
        content = content + [{"type": "text", "text": f"[returned {len(content)} non-text block(s): {summary}]"}]

    return rpc_result(req_id, _tool_call_blocks(content, is_error=False))
