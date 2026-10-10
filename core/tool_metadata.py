"""Fetch original tool parameter schemas from the Dify Console API.

The Dify Endpoint tool selector persists only values (`parameters`/`settings`),
never the parameter schema (type/required/description) — verified against the
Dify frontend `getToolValue()` and the backend/daemon forwarding chain, which
pass settings through without enrichment. The plugin SDK and daemon expose no
backwards-invocation to query another plugin's tool declaration.

So the schema is read — never invoked — from the Console API:

    GET {base}/console/api/workspaces/current/tool-provider/builtin/{provider}/tools

authenticating with ``Authorization: Bearer <key>`` and
``X-WORKSPACE-ID: <workspace id>`` (verified against a live Dify instance).
The endpoint settings provide ``dify_console_api_base``,
``dify_console_api_key`` and ``dify_workspace_id``; declaring the key as
``secret-input`` makes the daemon encrypt it at rest and decrypt it before
invoking this endpoint.

Provider display metadata (label, and the ``icon``/``icon_dark`` **full URLs**
only served by the workspace provider list) comes from a second read:

    GET {base}/console/api/workspaces/current/tools/builtin

The response carries much more than the adapter needs (i18n labels,
human-facing descriptions, placeholder/min/max metadata), so every parameter
is slimmed to the fields the MCP schema actually uses before caching.

This module performs read-only GETs. It never calls the tools themselves.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from core.global_config import normalize_base

logger = logging.getLogger(__name__)

# (workspace_id, provider) -> (fetched_at, (tools-by-name, provider_meta) | None)
_CACHE: dict[
    tuple[str, str],
    tuple[float, tuple[dict[str, list[dict[str, Any]]], "ProviderMeta"] | None],
] = {}

Fetcher = Callable[[str, str], list[dict[str, Any]]]

# Fields an MCP inputSchema is built from; everything else in a Console API
# parameter (i18n label, human_description, placeholder, min/max, precision,
# default, scope, template, auto_generate, icon...) is dropped at fetch time.
_KEEP_PARAM_KEYS = ("name", "type", "form", "required", "multiple", "input_schema")


def request_headers(api_key: str, workspace_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "X-WORKSPACE-ID": workspace_id,
        "Accept": "application/json",
    }


def _pick_i18n(value: Any) -> str:
    """Flatten an I18n object or plain string to one string (prefer en_US)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("en_US", "zh_Hans"):
            if value.get(key):
                return str(value[key])
    return ""


def _slim_parameter(param: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the schema converter/adapters need."""

    slim = {k: param[k] for k in _KEEP_PARAM_KEYS if param.get(k) is not None}
    description = param.get("llm_description") or _pick_i18n(
        param.get("human_description")
    )
    if description:
        slim["llm_description"] = description
    options = param.get("options")
    if options:
        slim["options"] = [
            {"value": o.get("value")}
            for o in options
            if isinstance(o, dict) and o.get("value") is not None
        ]
    return slim


@dataclass
class ProviderMeta:
    """Provider display metadata for MCP tool listings (never affects
    exposure or invocation; an empty instance is a valid fallback)."""

    title: str = ""
    icon: str = ""        # full URL (light theme), from the provider list API
    icon_dark: str = ""   # full URL (dark theme), may be empty


# (base, workspace_id) -> (fetched_at, full builtin-provider list or None)
_PROVIDER_LIST_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]] | None]] = {}


class ToolMetadataResolver:
    """Resolve original tool parameter schemas, with a per-provider cache."""

    def __init__(self, settings: Any, cache_ttl: float = 300.0,
                 fetcher: Fetcher | None = None,
                 http_getter: Callable[[str], Any] | None = None) -> None:
        base = ""
        api_key = ""
        workspace_id = ""
        try:
            base = str(settings.get("dify_console_api_base") or "").strip()
            api_key = str(settings.get("dify_console_api_key") or "")
            workspace_id = str(settings.get("dify_workspace_id") or "")
        except AttributeError:
            pass
        self._base = normalize_base(base)
        self._api_key = api_key
        self._workspace_id = workspace_id
        self._cache_ttl = cache_ttl
        self._fetcher = fetcher
        self._http_getter = http_getter

    @property
    def configured(self) -> bool:
        return bool(self._base and self._api_key and self._workspace_id)

    def _missing_config_reason(self) -> str:
        return (
            "endpoint settings are missing 'dify_console_api_base', "
            "'dify_console_api_key' or 'dify_workspace_id'; original tool "
            "schemas cannot be fetched"
        )

    def _fetch_provider(self, provider: str) -> dict[str, list[dict[str, Any]]] | None:
        """Return {tool_name: slimmed-parameters-list} for one provider, or None."""

        cache_key = (self._workspace_id, provider)
        now = time.monotonic()
        cached = _CACHE.get(cache_key)
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
                    f"tool-provider/builtin/"
                    f"{urllib.parse.quote(provider, safe='/')}/tools"
                )
                request = urllib.request.Request(
                    url, headers=request_headers(self._api_key, self._workspace_id)
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                data = payload if isinstance(payload, list) else payload.get("data", [])

            tools = {}
            for tool in data or []:
                if isinstance(tool, dict) and tool.get("name"):
                    tools[tool["name"]] = [
                        _slim_parameter(p)
                        for p in (tool.get("parameters") or [])
                        if isinstance(p, dict)
                    ]
        except Exception as exc:
            # Log type + HTTP status only; response bodies may be large and
            # provider errors can echo request details.
            status = getattr(exc, "status", None) or getattr(exc, "code", None)
            logger.warning(
                "Tool metadata fetch failed provider=%s error=%s%s",
                provider, type(exc).__name__, f" status={status}" if status else "",
            )
            tools = None

        _CACHE[cache_key] = (now, tools)
        return tools

    def _http_get_json(self, path: str) -> Any:
        if self._http_getter is not None:
            return self._http_getter(path)
        url = f"{self._base}{path}"
        request = urllib.request.Request(
            url, headers=request_headers(self._api_key, self._workspace_id)
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get_provider_list(
        self, provider: str
    ) -> tuple[list[dict[str, Any]] | None, ProviderMeta]:
        """Fetch the builtin provider list (icons are full URLs only here).

        Returns (raw_list_or_None, ProviderMeta). A list-fetch failure or a
        missing entry degrades to empty metadata — the tool itself must stay
        exposed as long as its schema resolved.
        """

        now = time.monotonic()
        list_cache_key = (self._base, self._workspace_id)
        cached = _PROVIDER_LIST_CACHE.get(list_cache_key)
        if cached and now - cached[0] < self._cache_ttl:
            entries = cached[1]
        else:
            entries = None
            try:
                payload = self._http_get_json(
                    "/console/api/workspaces/current/tools/builtin"
                )
                entries = (
                    payload if isinstance(payload, list) else payload.get("data", [])
                )
            except Exception as exc:
                status = getattr(exc, "status", None) or getattr(exc, "code", None)
                logger.warning(
                    "Provider list fetch failed error=%s%s",
                    type(exc).__name__, f" status={status}" if status else "",
                )
            _PROVIDER_LIST_CACHE[list_cache_key] = (now, entries)

        if not entries:
            return entries, ProviderMeta()

        def matches(entry: dict[str, Any]) -> bool:
            name = str(entry.get("name") or "")
            return bool(name) and (
                name == provider
                or name.endswith("/" + provider)
                or provider.endswith("/" + name)
            )

        for entry in entries:
            if isinstance(entry, dict) and matches(entry):
                return entries, ProviderMeta(
                    title=_pick_i18n(entry.get("label")),
                    icon=str(entry.get("icon") or ""),
                    icon_dark=str(entry.get("icon_dark") or ""),
                )
        return entries, ProviderMeta()

    def get_icon_meta(self, provider: str) -> ProviderMeta:
        """Display metadata (title/icons) for a provider; never fails."""

        if not provider:
            return ProviderMeta()
        if self._http_getter is None:
            # No injected HTTP for the provider list: only hit the network in
            # the fully-configured production path. A fetcher-only resolver
            # (tests) must never trigger a real request here.
            if self._fetcher is not None or not self.configured:
                return ProviderMeta()
        try:
            _, meta = self._get_provider_list(provider)
        except Exception:
            logger.warning("Icon metadata unavailable provider=%s", provider)
            return ProviderMeta()
        return meta

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
    _PROVIDER_LIST_CACHE.clear()
