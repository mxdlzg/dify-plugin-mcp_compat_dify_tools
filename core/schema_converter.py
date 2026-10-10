"""Convert Dify tool parameter metadata into MCP JSON Schema.

Pure functions operating on plain data so they can be unit-tested without a
running Dify runtime. The only SDK dependency is the parameter *type* enum
values, imported as the source of truth for matching.
"""

from __future__ import annotations

from typing import Any

from dify_plugin.entities.tool import ToolParameter

_T = ToolParameter.ToolParameterType

# Types MCP cannot represent as a value an agent can fill in. Reported, never
# silently passed through as an illegal JSON-Schema ``type`` (root cause C).
UNSUPPORTED_TYPES: frozenset[str] = frozenset({
    _T.FILE.value,
    _T.FILES.value,
})

_S, _N, _B, _O, _A = "string", "number", "boolean", "object", "array"


def is_unsupported_type(param_type: str) -> bool:
    return param_type in UNSUPPORTED_TYPES


def _flatten_description(value: Any) -> str:
    """Descriptions must be one plain string, never an i18n object.

    Console-API params are already slimmed to a string; inline fixture schemas
    may still carry ``{"en_US": .., "zh_Hans": ..}``.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("en_US", "zh_Hans"):
            if value.get(key):
                return str(value[key])
    return ""


def build_property_schema(
    *,
    param_type: str,
    description: str,
    options: list[dict[str, Any]] | None = None,
    input_schema: dict[str, Any] | None = None,
    multiple: bool = False,
) -> dict[str, Any]:
    """Return a JSON-Schema property object for one Dify parameter.

    ``options`` are Dify select options (``[{"value": .., "label": ..}]``).
    ``input_schema`` is an optional nested schema for object/array params.
    Values keep their Dify source type; a select "false" stays the string
    "false" (taskbook principle 3).
    """

    if param_type == _T.SELECT.value:
        enum = [opt.get("value") for opt in (options or []) if "value" in opt]
        if multiple:
            item = {"enum": enum} if enum else {"type": _S}
            schema: dict[str, Any] = {"type": _A, "items": item}
        else:
            schema = {"type": _S}
            if enum:
                schema["enum"] = enum
    elif param_type == _T.DYNAMIC_SELECT.value:
        # Options resolve at runtime by Dify; expose a free-form string.
        schema = {"type": _S}
    elif param_type == _T.NUMBER.value:
        schema = {"type": _N}
    elif param_type in (_T.BOOLEAN.value, _T.CHECKBOX.value):
        schema = {"type": _B}
    elif param_type == _T.DATE.value:
        schema = {"type": _S, "format": "date"}
    elif param_type == _T.DATE_RANGE.value:
        schema = {
            "type": _O,
            "properties": {"start": {"type": _S}, "end": {"type": _S}},
        }
    elif param_type == _T.OBJECT.value:
        schema = {"type": _O}
        if input_schema:
            schema.update(input_schema)
    elif param_type == _T.ARRAY.value:
        schema = {"type": _A}
        if input_schema:
            schema.update(input_schema)
        else:
            # OpenAI-style clients require items on every array (issue #32894).
            schema["items"] = {}
    elif param_type in (_T.STRING.value, _T.SECRET_INPUT.value):
        schema = {"type": _S}
    else:
        # ANY / MODEL_SELECTOR / APP_SELECTOR / unknown: fall back to string
        # so the tool still advertises the parameter; caller logs a diagnostic.
        schema = {"type": _S}

    if description:
        schema["description"] = description
    return schema


def build_input_schema(
    parameters: list[dict[str, Any]],
    pinned: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build an MCP ``inputSchema`` from raw Dify parameter dicts.

    Returns ``(input_schema, skipped_unsupported_names)``.

    ``pinned`` are parameter names the server has fixed via endpoint config.
    They are deliberately omitted from the schema — an agent cannot fill or
    override what it never sees (taskbook: server config must not be
    overridable, and credentials/form params must stay hidden).
    """

    properties: dict[str, Any] = {}
    required: list[str] = []
    skipped: list[str] = []
    pinned = pinned or set()

    for param in parameters:
        form = param.get("form", "llm")
        if form != "llm":
            continue
        name = param.get("name")
        if not name:
            continue
        if name in pinned:
            continue
        ptype = param.get("type")
        if ptype in UNSUPPORTED_TYPES:
            skipped.append(name)
            continue
        properties[name] = build_property_schema(
            param_type=ptype,
            description=_flatten_description(
                param.get("llm_description") or param.get("human_description")
            ),
            options=param.get("options"),
            input_schema=param.get("input_schema"),
            multiple=bool(param.get("multiple")),
        )
        if param.get("required"):
            required.append(name)

    # required must only reference keys present in properties.
    required = [r for r in required if r in properties]
    return {"type": "object", "properties": properties, "required": required}, skipped
