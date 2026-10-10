import json
import uuid
from collections.abc import Mapping

from dify_plugin import Endpoint
from werkzeug import Request, Response

from core.global_config import resolve_settings
from core.mcp_handler import dispatch
from core.server_info import SERVER_NAME, SERVER_VERSION
from core.tool_adapter import AdaptedTool


class McpPostEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        """MCP Streamable HTTP transport.

        Not validated (kept from the original implementation, see README
        security notes): Origin header, authentication, session ids, and
        SSE streaming on this endpoint.
        """

        data = r.json or {}
        method = data.get("method")
        req_id = data.get("id")

        if method == "initialize":
            session_id = str(uuid.uuid4()).replace("-", "")
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {},
                    },
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION
                    },
                },
            }
            headers = {"mcp-session-id": session_id}
            return Response(
                json.dumps(response),
                status=200,
                content_type="application/json",
                headers=headers,
            )

        if method == "notifications/initialized":
            return Response("", status=202, content_type="application/json")

        response = dispatch(
            method,
            req_id,
            data.get("params") or {},
            resolve_settings(settings, self.session.storage),
            self._invoke_tool,
        )
        return Response(
            json.dumps(response), status=200, content_type="application/json"
        )

    def _invoke_tool(self, tool: AdaptedTool, parameters: dict):
        return self.session.tool.invoke(
            provider_type=tool.provider_type,
            provider=tool.provider,
            tool_name=tool.name,
            parameters=parameters,
        )
