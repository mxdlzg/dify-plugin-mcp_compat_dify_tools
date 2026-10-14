"""Shared core for turning Dify endpoint tool settings into MCP tools.

Both the Streamable HTTP endpoint (``endpoints/mcp_post.py``) and the legacy
SSE message endpoint (``endpoints/messages.py``) build their tool list,
input schemas, and tool-call parameters through this module, so the two
transports can never drift apart again.

The adapter is pure logic over plain dicts — the endpoint keeps ownership of
``self.session.tool.invoke``.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

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
    # optional display fields (MCP title / icons annotations)
    title: str = ""
    icons: list[dict[str, str]] = field(default_factory=list)
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
    # non-llm parameters the declaration marks required: a null/missing
    # configured value is a configuration error, never a silent omission
    required_server_params: set[str] = field(default_factory=set)
    # llm params declared Dify `file` / `files`: their MCP payload is a file
    # object that must be converted before invoke (core.file_adapter).
    file_param_names: set[str] = field(default_factory=set)
    multi_file_param_names: set[str] = field(default_factory=set)
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
    required_server: set[str] = set()
    file_names: set[str] = set()
    multi_file_names: set[str] = set()
    for p in params:
        name = p.get("name")
        if not name:
            continue
        if p.get("form", "llm") == "llm":
            llm_names.add(name)
            ptype = p.get("type") or "string"
            types[name] = ptype
            if ptype == "file":
                file_names.add(name)
            elif ptype == "files":
                file_names.add(name)
                multi_file_names.add(name)
            if p.get("options"):
                enums[name] = [
                    o.get("value") for o in p["options"] if isinstance(o, dict) and "value" in o
                ]
        else:
            server_names.add(name)
            if p.get("required"):
                required_server.add(name)
    return {
        "enums": enums,
        "types": types,
        "llm_names": llm_names,
        "server_names": server_names,
        "required_server": required_server,
        "file_names": file_names,
        "multi_file_names": multi_file_names,
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

    # Display metadata (provider title + full-URL icons); never required, an
    # unavailable entry must not hide a tool whose schema did resolve.
    meta_getter = getattr(metadata_resolver, "get_icon_meta", None)
    provider_meta = meta_getter(provider) if meta_getter else None

    input_schema, skipped = build_input_schema(param_dicts, pinned=config.keys())
    meta = _collect_schema_meta(param_dicts)

    # Everything the agent cannot freely choose is protected: server-configured
    # params (whatever their form) and non-llm params.
    protected = set(config.keys()) | meta["server_names"]
    # Params an agent may send: llm params not pinned by the server.
    sendable = meta["llm_names"] - set(config.keys())

    icons: list[dict[str, str]] = []
    if provider_meta is not None:
        if getattr(provider_meta, "icon", ""):
            icons.append({"src": provider_meta.icon, "theme": "light"})
        if getattr(provider_meta, "icon_dark", ""):
            icons.append({"src": provider_meta.icon_dark, "theme": "dark"})
    def _is_raw_id(value: str) -> bool:
        # getStructureValue() may store tool_label as the bare tool id; a
        # human-readable title must not be "tavily_search" or a plugin id.
        return value == name or value == provider

    # MCP title is the tool-level display name: the tool's own label wins,
    # then the provider title from the console list; a raw id is no title.
    raw_title = _to_plain_text(raw.get("tool_label"))
    provider_title = getattr(provider_meta, "title", "") or ""
    if raw_title and not _is_raw_id(raw_title):
        title = raw_title
    elif provider_title and not _is_raw_id(provider_title):
        title = provider_title
    else:
        title = ""

    return AdaptedTool(
        name=name,
        provider=provider,
        provider_type=_provider_type_from_selector(raw.get("type")),
        description=description,
        input_schema=input_schema,
        title=title,
        icons=icons,
        llm_param_names=meta["llm_names"],
        sendable_param_names=sendable,
        required_llm=[r for r in input_schema["required"]],
        enum_params=meta["enums"],
        param_types=meta["types"],
        raw_config=config,
        required_server_params=meta["required_server"],
        file_param_names=meta["file_names"],
        multi_file_param_names=meta["multi_file_names"],
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
    result: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }
    # Optional MCP display annotations; omitted when empty so older clients
    # never see blank fields.
    if tool.title:
        result["title"] = tool.title
    if tool.icons:
        result["icons"] = tool.icons
    return result


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
    tool: AdaptedTool,
    client_args: dict[str, Any],
    *,
    file_adapter: Any | None = None,
) -> dict[str, Any]:
    """Validate client arguments and merge them with server-side config.

    - the client may only send ``sendable_param_names`` (llm params the server
      did not pin); anything else -> ToolValidationError
    - missing required params -> ToolValidationError
    - Dify ``file``/``files`` params are converted to real Dify file
      parameters through ``file_adapter`` (upload + reference); sending the
      raw MCP wrapper is never allowed, and a file param without an available
      adapter is a config error rather than a silent passthrough
    - every server-configured value (pinned llm params + form/schema params)
      is resolved here and always wins; values keep their Dify types, so a
      select ``"false"`` stays the string ``"false"`` (root cause B)
    - a resolved server value of ``None`` is OMITTED (Dify "not configured"
      semantics, e.g. an empty optional text-input) — falsy-but-real values
      like ``False``/``0``/``""`` are still sent (never ``if not value``)
    - a parameter the declaration marks required whose configured value is
      None/missing -> ToolConfigError (never silently dropped)
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

    if tool.file_param_names & client_args.keys() and file_adapter is None:
        raise ToolConfigError(
            f"tool '{tool.name}' declares file parameters but no file "
            "upload path is available in this runtime; file parameters "
            "cannot be forwarded"
        )

    validated: dict[str, Any] = {}
    for name, value in client_args.items():
        if name in tool.file_param_names:
            if name in tool.multi_file_param_names:
                validated[name] = file_adapter.resolve_list(value, param_name=name)
            else:
                validated[name] = file_adapter.resolve(value, param_name=name)
        else:
            validated[name] = _coerce_value(name, tool, value)

    # Every server-configured value is resolved and always wins over anything
    # the client sent (defense in depth; client keys are already restricted).
    server_values = _resolve_server_values(tool, sorted(tool.raw_config.keys()))

    merged: dict[str, Any] = dict(validated)
    for key, resolved in server_values.items():
        if resolved is None:
            # Dify stores "not configured" as null (e.g. an empty optional
            # text-input / secret-input). Omit it so the tool applies its own
            # default instead of receiving a literal null.
            if key in tool.required_server_params:
                raise ToolConfigError(
                    f"tool '{tool.name}': required parameter '{key}' has no "
                    "configured value on this endpoint; configure it or make "
                    "it agent-provided"
                )
            logger.info(
                "Tool '%s': omitting server parameter '%s' (resolved to null; "
                "treated as not configured)",
                tool.name, key,
            )
            continue
        merged[key] = resolved

    # A required server parameter the endpoint never configured at all.
    unconfigured_required = [
        key for key in sorted(tool.required_server_params)
        if key not in merged
    ]
    if unconfigured_required:
        raise ToolConfigError(
            f"tool '{tool.name}': required parameter(s) not configured on this "
            f"endpoint: {', '.join(unconfigured_required)}"
        )
    return merged


#: Inline cap for image/blob content. A tools/call response is one JSON body,
#: so a large binary is better delivered as a link than base64-encoded here.
DEFAULT_INLINE_MAX_BYTES = 3 * 1024 * 1024


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _image_block(data: bytes, mime: str) -> dict[str, Any]:
    return {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mime,
    }


def _link_note(url: str, name: str, mime: str) -> dict[str, Any]:
    # `resource_link` exists only from protocol 2025-03-26 on; this server
    # speaks 2024-11-05, whose content union is text|image|resource (and a
    # resource requires inline text/blob). A URL is therefore emitted as text.
    return _text_block(f"{name} ({mime}): {url}")


def _blob_of(response: Any) -> bytes:
    message = getattr(response, "message", None)
    return bytes(getattr(message, "blob", b"") or b"")


def _mime_of(response: Any) -> str | None:
    meta = getattr(response, "meta", None)
    if isinstance(meta, dict):
        for key in ("mime_type", "mimeType"):
            value = meta.get(key)
            if isinstance(value, str) and "/" in value:
                return value
    return None


def guess_mime_from_url(url: str) -> str | None:
    guessed, _ = mimetypes.guess_type(urlsplit(url).path if "://" in url else url)
    return guessed


def _url_basename(url: str) -> str:
    tail = urlsplit(url).path.rsplit("/", 1)[-1]
    return tail or "file"


def _inline_binary_blocks(
    blob: bytes, mime: str, kind: str, inline_max_bytes: int
) -> list[dict[str, Any]]:
    """Turn inline bytes into content blocks, or into an honest text note.

    2024-11-05 has no block for arbitrary inline binary: `image` would be a lie
    for a PDF and `resource` requires a URI the message does not carry. Dify also
    gives no downloadable URL for inline blobs, so an oversized one is reported
    rather than truncated.
    """

    if not blob:
        return [_text_block(f"[{kind} message carried no data]")]
    if not mime.startswith("image/"):
        return [_text_block(
            f"[tool returned {len(blob)} bytes of {mime}; this protocol "
            "version has no content block for inline non-image binary]"
        )]
    if len(blob) > inline_max_bytes:
        return [_text_block(
            f"[{kind} of {len(blob)} bytes ({mime}) exceeds the "
            f"{inline_max_bytes} byte inline limit and has no downloadable URL]"
        )]
    return [_image_block(blob, mime)]


def format_invoke_content(
    messages: Any,
    *,
    inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES,
) -> list[dict[str, Any]]:
    """Convert a Dify ToolInvokeMessage stream into MCP content blocks.

    Order is preserved and one message may yield more than one block. Unknown
    types are logged and degraded to text — never dropped silently, never
    serialized as a raw Python repr (that produced the old meaningless output).
    """

    blocks: list[dict[str, Any]] = []
    # blob_chunk streams: id -> {"chunks": [(seq, bytes)], "mime": str|None}
    pending: dict[str, dict[str, Any]] = {}

    for response in messages:
        mtype = response.type
        if mtype == ToolInvokeMessage.MessageType.TEXT:
            blocks.append(_text_block(response.message.text))

        elif mtype == ToolInvokeMessage.MessageType.JSON:
            # Raw JSON, no "tool response:" prefix: clients can parse it.
            blocks.append(
                _text_block(
                    json.dumps(response.message.json_object, ensure_ascii=False, default=str)
                )
            )

        elif mtype == ToolInvokeMessage.MessageType.LINK:
            blocks.append(_text_block(f"result link: {response.message.text}"))

        elif mtype == ToolInvokeMessage.MessageType.IMAGE_LINK:
            url = response.message.text
            blocks.append(
                _link_note(url, _url_basename(url), guess_mime_from_url(url) or "image/*")
            )

        elif mtype in (
            ToolInvokeMessage.MessageType.IMAGE,
            ToolInvokeMessage.MessageType.BLOB,
        ):
            blob = _blob_of(response)
            default_mime = (
                "image/png"
                if mtype == ToolInvokeMessage.MessageType.IMAGE
                else "application/octet-stream"
            )
            mime = _mime_of(response) or default_mime
            blocks.extend(
                _inline_binary_blocks(blob, mime, mtype.value, inline_max_bytes)
            )

        elif mtype == ToolInvokeMessage.MessageType.BLOB_CHUNK:
            chunk = response.message
            entry = pending.setdefault(
                chunk.id, {"chunks": [], "mime": _mime_of(response)}
            )
            entry["chunks"].append((chunk.sequence, _blob_of(response)))
            if chunk.end:
                assembled = sorted(entry["chunks"], key=lambda item: item[0])
                blob = b"".join(data for _, data in assembled)
                mime = entry["mime"] or "application/octet-stream"
                blocks.extend(
                    _inline_binary_blocks(blob, mime, "BLOB_CHUNK", inline_max_bytes)
                )
                pending.pop(chunk.id, None)

        elif mtype == ToolInvokeMessage.MessageType.FILE:
            meta = response.meta or {}
            file_ref = meta.get("file") if isinstance(meta, dict) else None
            url = name = mime = None
            if isinstance(file_ref, dict):
                url = file_ref.get("url")
                name = file_ref.get("filename")
                mime = file_ref.get("mime_type")
            if url:
                blocks.append(_link_note(url, name or _url_basename(url), mime or "application/octet-stream"))
            else:
                logger.warning(
                    "Tool produced a FILE message with no resolvable URL (meta keys=%s)",
                    sorted(meta.keys()) if isinstance(meta, dict) else type(meta).__name__,
                )
                blocks.append(_text_block(
                    "[tool produced a file that carries no accessible URL]"
                ))

        elif mtype == ToolInvokeMessage.MessageType.VARIABLE:
            message = response.message
            blocks.append(_text_block(
                f"{message.variable_name}="
                f"{json.dumps(message.variable_value, ensure_ascii=False, default=str)}"
            ))

        elif mtype == ToolInvokeMessage.MessageType.RETRIEVER_RESOURCES:
            message = response.message
            blocks.append(_text_block(
                json.dumps(
                    {
                        "context": message.context,
                        "resources": [
                            r.model_dump(mode="json", exclude_none=True)
                            for r in message.retriever_resources
                        ],
                    },
                    ensure_ascii=False,
                )
            ))

        elif mtype == ToolInvokeMessage.MessageType.LOG:
            # Execution telemetry, not a result; surfacing it would also risk
            # leaking tool internals. Logged, deliberately not returned.
            logger.debug("Ignoring LOG message from tool")

        else:
            logger.warning("Unhandled tool message type '%s' degraded to text", mtype)
            blocks.append(_text_block(f"[unsupported tool message type: {mtype}]"))

    for orphan_id, entry in pending.items():
        logger.warning(
            "Tool blob stream '%s' ended without a final chunk (%d bytes received)",
            orphan_id, sum(len(d) for _, d in entry["chunks"]),
        )
    if not blocks:
        blocks.append(_text_block(""))
    return blocks


def format_invoke_result(messages: Any) -> str:
    """Flatten a message stream to text (kept for callers needing a string)."""

    return "\n".join(
        block["text"] for block in format_invoke_content(messages)
        if block["type"] == "text"
    )
