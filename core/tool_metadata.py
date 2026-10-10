"""Fetch original tool parameter schemas from the Dify Console API.

The Dify Endpoint tool selector persists only values (`parameters`/`settings`),
never the parameter schema (type/required/description) — verified against the
Dify frontend `getToolValue()` and the backend/daemon forwarding chain, which
pass settings through without enrichment. The plugin SDK and daemon expose no
backwards-invocation to query another plugin's tool declaration.

So the schema is read — never invoked — from the Console API:

    GET {base}/console/api/workspaces/current/tool-provider/builtin/{provider}/tools

which returns ``ToolApiEntity`` objects whose ``parameters`` are full
``ToolParameter`` serializations (name/type/form/required/llm_description/
options). The endpoint settings must provide ``dify_console_api_base`` and
``dify_console_api_key``; declaring the key as ``secret-input`` makes the
daemon encrypt it at rest and decrypt it before invoking this endpoint.

This module performs read-only GETs. It never calls the tools themselves.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

# provider -> (fetched_at_monotonic, tools-by-name or None-on-error)
_CACHE: dict[str, tuple[float, dict[str, list[dict[str, Any]]] | None]] = {}

Fetcher = Callable[[str, str], list[dict[str, Any]]]


def _console_request_json(url: str, api_key: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class ToolMetadataResolver:
    """Resolve original tool parameter schemas, with a per-provider cache."""

    def __init__(self, settings: Any, cache_ttl: float = 300.0,
                 fetcher: Fetcher | None = None) -> None:
        base = ""
        api_key = ""
        try:
            base = str(settings.get("dify_console_api_base") or "").rstrip("/")
            api_key = str(settings.get("dify_console_api_key") or "")
        except AttributeError:
            pass
        self._base = base
        self._api_key = api_key
        self._cache_ttl = cache_ttl
        self._fetcher = fetcher

    @property
    def configured(self) -> bool:
        return bool(self._base and self._api_key)

    def _missing_config_reason(self) -> str:
        return (
            "endpoint settings are missing 'dify_console_api_base' or "
            "'dify_console_api_key'; original tool schemas cannot be fetched"
        )

    def _fetch_provider(self, provider: str) -> dict[str, list[dict[str, Any]]] | None:
        """Return {tool_name: parameters-list} for one provider, or None."""

        now = time.monotonic()
        cached = _CACHE.get(provider)
        if cached and now - cached[0] < self._cache_ttl:
            return cached[1]

        tools: dict[str, list[dict[str, Any]]] | None = None
        try:
            if self._fetcher is not None:
                data = self._fetcher(self._base, provider)
            else:
                if not self.configured:
                    raise RuntimeError(self._missing_config_reason())
                url = (
                    f"{self._base}/console/api/workspaces/current/"
                    f"tool-provider/builtin/{urllib.parse.quote(provider, safe='')}/tools"
                )
                payload = _console_request_json(url, self._api_key, timeout=10)
                data = payload if isinstance(payload, list) else payload.get("data", [])

            tools = {}
            for tool in data or []:
                if isinstance(tool, dict) and tool.get("name"):
                    tools[tool["name"]] = tool.get("parameters") or []
        except Exception as exc:
            # Log type + HTTP status only; response bodies may be large and
            # provider errors can echo request details.
            status = getattr(exc, "status", None) or getattr(exc, "code", None)
            logger.warning(
                "Tool metadata fetch failed provider=%s error=%s%s",
                provider, type(exc).__name__, f" status={status}" if status else "",
            )
            tools = None

        _CACHE[provider] = (now, tools)
        return tools

    def get_parameters(
        self, provider: str, tool_name: str
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Return (parameters, None) on success, (None, reason) on failure.

        An empty list is a valid result: the tool truly has no parameters.
        """

        if not self.configured and self._fetcher is None:
            return None, self._missing_config_reason()
        if not provider:
            return None, "endpoint tool entry has no provider_name to query"

        tools = self._fetch_provider(provider)
        if tools is None:
            return None, (
                f"failed to fetch tool metadata from the Dify console API "
                f"(provider '{provider}')"
            )
        if tool_name not in tools:
            return None, f"tool '{tool_name}' not found under provider '{provider}'"
        return tools[tool_name], None


def clear_cache() -> None:
    _CACHE.clear()
