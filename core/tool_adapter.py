"""Shared core for turning Dify endpoint tool settings into MCP tools.

Both the Streamable HTTP endpoint (``endpoints/mcp_post.py``) and the legacy
SSE message endpoint (``endpoints/messages.py``) build their tool list,
input schemas, and tool-call parameters through this module, so the two
transports can never drift apart again.

The adapter is pure logic over plain dicts — the endpoint keeps ownership of
``self.session.tool.invoke``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage, ToolProviderType

from core.parameter_resolver import (
    UnresolvedParameterError,
    resolve_config_value,
)
from core.schema_converter import build_input_schema

logger = logging.getLogger(__name__)


class ToolValidationError(ValueError):
    """Invalid tool call from the MCP client (missing/unknown/typed wrong)."""


class ToolConfigError(RuntimeError):
    """Server-side tool configuration cannot be used for this call."""


@dataclass
class AdaptedTool:
    name: str
    provider: str
    provider_type: ToolProviderType
    description: str
    input_schema: dict[str, Any]
    # names of llm-form parameters (the only ones an agent may send)
    llm_param_names: set[str] = field(default_factory=set)
    # llm params not pinned by server config = the sendable surface
    sendable_param_names: set[str] = field(default_factory=set)
    required_llm: list[str] = field(default_factory=list)
    # parameter name -> enum values, for select params
    enum_params: dict[str, list] = field(default_factory=dict)
    # parameter name -> dify type, for light coercion
    param_types: dict[str, str] = field(default_factory=dict)
    # raw server-side config (name -> expression), resolved lazily per call
    raw_config: dict[str, Any] = field(default_factory=dict)
    # config keys that belong to non-llm params: never overridable by client
    protected_config_keys: set[str] = field(default_factory=set)
    # non-llm parameter names that must not be advertised in the schema
    server_param_names: set[str] = field(default_factory=set)
    unsupported_params: list[str] = field(default_factory=list)


def _to_plain_text(value: Any) -> str:
    """Flatten a str or I18n dict to a single string (prefer en_US)."""

    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("en_US", "zh_Hans"):
            if value.get(key):
                return str(value[key])
        for v in value.values():
            if isinstance(v, str) and v:
                return v
    return ""


def _provider_type_from_selector(raw_type: Any) -> ToolProviderType:
    mapping = {
        "builtin": ToolProviderType.BUILT_IN,
        "built_in": ToolProviderType.BUILT_IN,
        "api": ToolProviderType.API,
        "workflow": ToolProviderType.WORKFLOW,
        "app": ToolProviderType.APP,
        "dataset-retrieval": ToolProviderType.DATASET_RETRIEVAL,
    }
    return mapping.get(str(raw_type), ToolProviderType.BUILT_IN)


def _llm_params_to_dicts(tool_parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a ToolSelector-style tool_parameters map to param dicts."""

    params: list[dict[str, Any]] = []
    for name, spec in (tool_parameters or {}).items():
        if not isinstance(spec, dict):
            continue
        params.append({
            "name": spec.get("name", name),
            "type": spec.get("type"),
            "form": "llm",
            "required": spec.get("required", False),
            "llm_description": spec.get("description") or "",
            "options": spec.get("options"),
            "multiple": spec.get("multiple", False),
            "input_schema": spec.get("input_schema"),
        })
    return params


def _collect_schema_meta(params: list[dict[str, Any]]) -> dict[str, Any]:
    enums: dict[str, list] = {}
    types: dict[str, str] = {}
    llm_names: set[str] = set()
    server_names: set[str] = set()
    for p in params:
        name = p.get("name")
        if not name:
            continue
        if p.get("form", "llm") == "llm":
            llm_names.add(name)
            types[name] = p.get("type") or "string"
            if p.get("options"):
                enums[name] = [
                    o.get("value") for o in p["options"] if isinstance(o, dict) and "value" in o
                ]
        else:
            server_names.add(name)
    return {
        "enums": enums,
        "types": types,
        "llm_names": llm_names,
        "server_names": server_names,
    }


def _selector_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Server-pinned values from the selector shape.

    ``settings`` entries are always pinned. ``parameters`` entries with
    ``auto == 0`` were switched from LLM-filled to a user-fixed value in the
    selector UI (``auto == 1`` means the agent fills it); their value is the
    inner expression object, resolved later by parameter_resolver.
    """

    config = dict(raw.get("settings") or {})
    parameters = raw.get("parameters") or {}
    if isinstance(parameters, dict):
        for key, entry in parameters.items():
            if (
                isinstance(entry, dict)
                and entry.get("auto") == 0
                and "value" in entry
            ):
                config[key] = entry.get("value")
    return config


def adapt_tool_entry(
    raw: dict[str, Any],
    metadata_resolver: Any | None = None,
) -> AdaptedTool | None:
    """Adapt one endpoint tool entry to an :class:`AdaptedTool`.

    Metadata priority:
    1. inline schema in the entry (``schemas`` / ``tool_parameters``)
    2. ``metadata_resolver`` (Dify Console API) for the real selector shape,
       which persists only values, never schemas
    If neither yields a schema the entry is NOT exposed — an empty schema for
    a tool whose parameters are unknown would silently invite wrong calls.

    Returns ``None`` for disabled, malformed, or schema-unresolvable entries.
    """

    if not raw.get("enabled", False):
        return None

    name = raw.get("tool_name") or raw.get("name")

    # Field-shape diagnostic for locating missing tool metadata. Emits only
    # key names and counts, never values, so credentials cannot leak.
    logger.warning(
        "TOOL_META_DIAG name=%s keys=%s schemas=%s tool_parameters=%s "
        "tool_configuration=%s settings=%s parameters=%s",
        name,
        sorted(raw.keys()),
        len(raw.get("schemas") or []),
        len(raw.get("tool_parameters") or {}),
        len(raw.get("tool_configuration") or {}),
        sorted((raw.get("settings") or {}).keys()),
        sorted((raw.get("parameters") or {}).keys())
        if isinstance(raw.get("parameters"), dict) else "non-dict",
    )

    if not name:
        logger.warning("Skipping endpoint tool entry without a tool_name")
        return None

    provider = raw.get("provider_name") or raw.get("provider_id") or raw.get("provider") or ""

    description = (
        _to_plain_text((raw.get("extra") or {}).get("description"))
        or _to_plain_text(raw.get("tool_description"))
        or _to_plain_text(raw.get("tool_label"))
        or name
    )

    selector_shape = "tool_parameters" in raw or "tool_configuration" in raw

    # Pick the metadata source by which one actually carries data, not by key
    # presence: a `schemas` key that is present but empty must not lock out the
    # other sources (that silent-empty-schema was the original bug class).
    schemas_value = raw.get("schemas") or []
    tool_parameters_value = raw.get("tool_parameters") or {}

    if schemas_value:
        param_dicts = list(schemas_value)
        config = dict(raw.get("settings") or {})
    elif tool_parameters_value:
        param_dicts = _llm_params_to_dicts(tool_parameters_value)
        config = dict(raw.get("tool_configuration") or raw.get("settings") or {})
    elif selector_shape:
        # selector shape present but with an empty llm map — could be a real
        # zero-parameter tool; config still comes from tool_configuration.
        param_dicts = []
        config = dict(raw.get("tool_configuration") or raw.get("settings") or {})
    else:
        # Real Dify selector shape: `parameters`/`settings` carry VALUES only
        # (no type/required/description). Type/required must come from the
        # metadata resolver (Console API); guessing them from key names is
        # forbidden. Even a tool with no configured values is verified through
        # the resolver — an empty config is indistinguishable from an
        # all-defaults config of a required-parameter tool. Without a schema
        # the tool is not exposed (taskbook C).
        config = _selector_config(raw)
        resolver = metadata_resolver
        if resolver is None or not getattr(resolver, "configured", False):
            logger.warning(
                "Tool '%s': no inline parameter schema and no console API "
                "metadata configured (set dify_console_api_base/"
                "dify_console_api_key/dify_workspace_id on this endpoint); "
                "not exposing the tool. value-only keys seen = %s",
                name,
                sorted(set((raw.get("parameters") or {}).keys())
                       | set(config.keys())),
            )
            return None
        params, reason = resolver.get_parameters(provider, name)
        if params is None:
            logger.warning(
                "Tool '%s': schema unavailable (%s); not exposing it to avoid "
                "advertising a wrong signature.", name, reason,
            )
            return None
        param_dicts = params

    input_schema, skipped = build_input_schema(param_dicts, pinned=config.keys())
    meta = _collect_schema_meta(param_dicts)

    # Everything the agent cannot freely choose is protected: server-configured
    # params (whatever their form) and non-llm params.
    protected = set(config.keys()) | meta["server_names"]
    # Params an agent may send: llm params not pinned by the server.
    sendable = meta["llm_names"] - set(config.keys())

    return AdaptedTool(
        name=name,
        provider=provider,
        provider_type=_provider_type_from_selector(raw.get("type")),
        description=description,
        input_schema=input_schema,
        llm_param_names=meta["llm_names"],
        sendable_param_names=sendable,
        required_llm=[r for r in input_schema["required"]],
        enum_params=meta["enums"],
        param_types=meta["types"],
        raw_config=config,
        protected_config_keys=protected,
        server_param_names=meta["server_names"],
        unsupported_params=skipped,
    )


def adapt_tools(settings_tools: Any, metadata_resolver: Any | None = None) -> list[AdaptedTool]:
    """Adapt the whole ``settings["tools"]`` list, skipping bad entries."""

    tools: list[AdaptedTool] = []
    if not isinstance(settings_tools, list):
        logger.warning("Endpoint 'tools' setting is missing or not a list")
        return tools
    for entry in settings_tools:
        if not isinstance(entry, dict):
            continue
        try:
            adapted = adapt_tool_entry(entry, metadata_resolver)
        except Exception:
            logger.exception("Failed to adapt Dify tool entry to MCP tool")
            continue
        if adapted:
            tools.append(adapted)
    return tools


def to_mcp_tool(tool: AdaptedTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }


def _coerce_value(name: str, tool: AdaptedTool, value: Any) -> Any:
    ptype = tool.param_types.get(name)
    enum = tool.enum_params.get(name)

    if ptype == "select" and enum:
        if value in enum:
            return value
        # {"value": "false"}-style options are strings; accept the str form
        # of an equivalent scalar but keep the enum string as the sent value.
        if str(value) in enum:
            return str(value)
        raise ToolValidationError(
            f"parameter '{name}' must be one of {enum}, got {value!r}"
        )

    if ptype == "number":
        if isinstance(value, bool):
            raise ToolValidationError(f"parameter '{name}' expects a number")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return int(value) if value.lstrip("-").isdigit() else float(value)
            except ValueError:
                raise ToolValidationError(
                    f"parameter '{name}' expects a number, got {value!r}"
                ) from None
        raise ToolValidationError(f"parameter '{name}' expects a number")

    if ptype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ToolValidationError(
            f"parameter '{name}' expects a boolean, got {value!r}"
        )

    return value


def _resolve_server_values(tool: AdaptedTool, keys: list[str]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for key in keys:
        if key not in tool.raw_config:
            continue
        try:
            resolved[key] = resolve_config_value(tool.raw_config[key])
        except UnresolvedParameterError as exc:
            raise ToolConfigError(
                f"tool '{tool.name}': server-side parameter '{key}' cannot be "
                f"resolved: {exc}"
            ) from exc
    return resolved


def prepare_invoke_parameters(
    tool: AdaptedTool, client_args: dict[str, Any]
) -> dict[str, Any]:
    """Validate client arguments and merge them with server-side config.

    - the client may only send ``sendable_param_names`` (llm params the server
      did not pin); anything else -> ToolValidationError
    - missing required params -> ToolValidationError
    - every server-configured value (pinned llm params + form/schema params)
      is resolved here and always sent; values keep their Dify types, so a
      select ``"false"`` stays the string ``"false"`` (root cause B)
    """

    if not isinstance(client_args, dict):
        raise ToolValidationError("tool call arguments must be an object")

    for key in client_args:
        if key not in tool.sendable_param_names:
            if key in tool.protected_config_keys or key in tool.server_param_names:
                raise ToolValidationError(
                    f"parameter '{key}' is fixed server-side configuration for "
                    f"tool '{tool.name}' and cannot be set by the caller"
                )
            allowed = sorted(tool.sendable_param_names) or ["(this tool takes no parameters)"]
            raise ToolValidationError(
                f"unknown parameter '{key}' for tool '{tool.name}'; "
                f"allowed: {', '.join(allowed)}"
            )

    missing = [r for r in tool.required_llm if r not in client_args]
    if missing:
        raise ToolValidationError(
            f"missing required parameter(s) for tool '{tool.name}': "
            f"{', '.join(missing)}"
        )

    validated = {
        name: _coerce_value(name, tool, value)
        for name, value in client_args.items()
    }

    # Every server-configured value is resolved and always wins.
    server_values = _resolve_server_values(tool, sorted(tool.raw_config.keys()))

    merged: dict[str, Any] = dict(validated)
    merged.update(server_values)
    return merged


def format_invoke_result(messages: Any) -> str:
    """Flatten ToolInvokeMessage stream into one MCP text result."""

    result = ""
    for response in messages:
        if response.type == ToolInvokeMessage.MessageType.TEXT:
            result += response.message.text
        elif response.type == ToolInvokeMessage.MessageType.LINK:
            result += (
                f"result link: {response.message.text}."
                + " please tell user to check it."
            )
        elif response.type in {
            ToolInvokeMessage.MessageType.IMAGE_LINK,
            ToolInvokeMessage.MessageType.IMAGE,
        }:
            result += "Not support message type: image. "
        elif response.type == ToolInvokeMessage.MessageType.JSON:
            text = json.dumps(
                response.message.json_object, ensure_ascii=False, default=str
            )
            result += f"tool response: {text}."
        else:
            result += f"tool response: {response.message!r}."
    return result
