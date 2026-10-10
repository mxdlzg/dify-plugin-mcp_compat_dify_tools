import json
from collections.abc import Mapping

from dify_plugin import Endpoint
from werkzeug import Request, Response

from core.mcp_handler import dispatch
from core.server_info import SERVER_NAME, SERVER_VERSION
from core.tool_adapter import AdaptedTool


class MessageEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        """MCP message endpoint of the legacy HTTP-with-SSE transport.

        JSON-RPC responses are handed to the SSE endpoint (endpoints/sse.py)
        via session storage. Not validated (kept from the original
        implementation): session id and request origin.
        """

        session_id = r.args.get("session_id")
        data = r.json or {}
        method = data.get("method")

        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": data.get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "experimental": {},
                        "prompts": {"listChanged": False},
                        "resources": {
                            "subscribe": False,
                            "listChanged": False
                        },
                        "tools": {"listChanged": False}
                    },
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION
                    }
                },
            }
        elif method == "notifications/initialized":
            return Response("", status=202, content_type="application/json")
        else:
            response = dispatch(
                method,
                data.get("id"),
                data.get("params") or {},
                settings,
                self._invoke_tool,
            )

        if session_id:
            self.session.storage.set(session_id, json.dumps(response).encode())
        return Response("", status=202, content_type="application/json")

    def _invoke_tool(self, tool: AdaptedTool, parameters: dict):
        return self.session.tool.invoke(
            provider_type=tool.provider_type,
            provider=tool.provider,
            tool_name=tool.name,
            parameters=parameters,
        )
