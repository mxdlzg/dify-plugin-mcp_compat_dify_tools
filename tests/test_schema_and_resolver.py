"""Tests for the Dify parameter -> JSON Schema conversion and settings parsing."""

from core.parameter_resolver import (
    UnresolvedParameterError,
    resolve_config_value,
)
from core.schema_converter import build_input_schema, build_property_schema
from core.tool_adapter import adapt_tool_entry, to_mcp_tool

from fixtures import (
    tavily_selector_shape,
    tavily_schemas_shape,
    select_tool,
)


# --- Test 1: Tavily schema exposes required query, not an empty schema ---

def test_tavily_selector_schema_has_required_query():
    tool = adapt_tool_entry(tavily_selector_shape())
    schema = to_mcp_tool(tool)["inputSchema"]
    assert schema["properties"]["query"] == {
        "type": "string",
        "description": "Query string for search",
    }
    assert schema["required"] == ["query"]
    # server-pinned select must NOT be agent-visible
    assert "include_answer" not in schema["properties"]


def test_tavily_schemas_schema_has_required_query():
    tool = adapt_tool_entry(tavily_schemas_shape())
    schema = to_mcp_tool(tool)["inputSchema"]
    assert "query" in schema["properties"]
    assert schema["required"] == ["query"]
    assert "include_answer" not in schema["properties"]


# --- Test 2: select parameter -> string + enum, never type "select" ---

def test_select_parameter_becomes_string_enum():
    tool = adapt_tool_entry(select_tool())
    schema = to_mcp_tool(tool)["inputSchema"]
    mode = schema["properties"]["mode"]
    assert mode["type"] == "string"
    assert mode["enum"] == ["false", "true", "basic", "advanced"]
    assert schema["required"] == ["mode"]


def test_no_illegal_type_select_anywhere():
    for shape in (tavily_selector_shape(), tavily_schemas_shape(), select_tool()):
        tool = adapt_tool_entry(shape)
        for prop in to_mcp_tool(tool)["inputSchema"]["properties"].values():
            assert prop["type"] != "select"
            assert prop["type"] in {"string", "number", "boolean", "object", "array"}


def test_select_enum_preserves_string_value_semantics():
    # "false" must stay the string "false", not become a boolean.
    prop = build_property_schema(
        param_type="select",
        description="",
        options=[{"value": "false"}, {"value": "true"}],
    )
    assert prop["enum"] == ["false", "true"]
    assert all(isinstance(v, str) for v in prop["enum"])


# --- Test 3: constant / expression resolution ---

def test_constant_string_preserves_type():
    assert resolve_config_value({"type": "constant", "value": "false"}) == "false"
    assert resolve_config_value({"type": "constant", "value": "false"}) is not False


def test_constant_number():
    assert resolve_config_value({"type": "constant", "value": 42}) == 42
    assert resolve_config_value({"type": "constant", "value": 3.14}) == 3.14


def test_constant_boolean():
    assert resolve_config_value({"type": "constant", "value": True}) is True
    assert resolve_config_value({"type": "constant", "value": False}) is False


def test_constant_empty_string():
    assert resolve_config_value({"type": "constant", "value": ""}) == ""


def test_nested_constant_wrapper_is_unwrapped():
    # Verified double-wrapped form (root cause B).
    assert resolve_config_value(
        {"type": "constant", "value": {"type": "constant", "value": "false"}}
    ) == "false"


def test_plain_business_object_not_unwrapped():
    # A legit business dict that happens to carry a "value" key must survive.
    obj = {"value": "keep", "unit": "USD", "currency": "dollar"}
    assert resolve_config_value(obj) is obj


def test_non_dict_passes_through():
    assert resolve_config_value("raw") == "raw"
    assert resolve_config_value(7) == 7


def test_unknown_expression_type_returns_original():
    expr = {"type": "mystery", "value": "x"}
    assert resolve_config_value(expr) is expr


def test_dynamic_expression_raises():
    for t in ("variable", "mixed"):
        try:
            resolve_config_value({"type": t, "value": "{{#node.x#}}"})
            assert False, f"{t} should raise"
        except UnresolvedParameterError:
            pass


# --- Dify 1.17.1 `mixed` semantics ---
# The frontend stores EVERY text-input/secret-input as
# {"type": "mixed", "value": ...}, whether or not the text references a
# workflow variable. Only a real {{#...#}} reference needs workflow context.

def test_mixed_fixed_text_resolves_to_literal():
    assert resolve_config_value({"type": "mixed", "value": "proj-123"}) == "proj-123"


def test_mixed_empty_string_is_preserved_as_empty():
    # "" is a real configured value, distinct from null ("not configured").
    resolved = resolve_config_value({"type": "mixed", "value": ""})
    assert resolved == ""
    assert isinstance(resolved, str)


def test_mixed_null_resolves_to_none():
    assert resolve_config_value({"type": "mixed", "value": None}) is None


def test_mixed_sys_query_reference_raises():
    for ref in ("{{#sys.query#}}", "prefix {{#1711528712102.text#}} suffix"):
        try:
            resolve_config_value({"type": "mixed", "value": ref})
            assert False, f"variable reference should raise: {ref}"
        except UnresolvedParameterError:
            pass


def test_mixed_double_wrapper_resolves_through_wrapper():
    # Real getStructureValue() shape for a text-input left with fixed text.
    wrapped = {"value": {"type": "mixed", "value": "fixed text"}}
    assert resolve_config_value(wrapped) == "fixed text"
    # ...and the wrapped form with a variable reference still raises.
    try:
        resolve_config_value({"value": {"type": "mixed", "value": "{{#node.x#}}"}})
        assert False, "wrapped mixed reference should raise"
    except UnresolvedParameterError:
        pass


def test_constant_zero_and_false_keep_their_types():
    zero = resolve_config_value({"type": "constant", "value": 0})
    assert zero == 0
    assert isinstance(zero, int)
    assert zero is not False
    false = resolve_config_value({"type": "constant", "value": False})
    assert false is False


def test_references_variable_walks_containers():
    from core.parameter_resolver import references_variable

    assert references_variable({"a": ["{{#node.f#}}"]}) is True
    assert references_variable({"a": ["plain"]}) is False
    assert references_variable(5) is False


# --- Dify getStructureValue() double wrapper (real Endpoint settings shape) ---

def test_settings_double_wrapper_constant():
    # Real shape saved by the Dify tool selector for form/settings values.
    wrapped = {"value": {"type": "constant", "value": "false"}}
    resolved = resolve_config_value(wrapped)
    assert resolved == "false"
    assert isinstance(resolved, str)


def test_settings_double_wrapper_number_and_bool():
    assert resolve_config_value({"value": {"type": "constant", "value": 3}}) == 3
    assert resolve_config_value({"value": {"type": "constant", "value": False}}) is False


def test_settings_double_wrapper_dynamic_raises():
    wrapped = {"value": {"type": "variable", "value": ["node", "x"]}}
    try:
        resolve_config_value(wrapped)
        assert False, "variable inside wrapper should raise"
    except UnresolvedParameterError:
        pass


def test_business_dict_with_type_key_not_unwrapped():
    # A legitimate object carrying a non-expression "type" must survive.
    obj = {"value": "100", "type": "currency"}
    assert resolve_config_value(obj) is obj


def test_business_dict_single_value_key_not_unwrapped():
    # {"value": <non-expression>} is not a wrapper.
    obj = {"value": {"amount": 5, "unit": "USD"}}
    assert resolve_config_value(obj) is obj


# --- required only references visible properties ---

def test_required_subset_of_properties():
    # A tool where the only required param is server-pinned: it is hidden, so
    # it must not linger in `required`.
    params = [
        {"name": "query", "type": "string", "form": "llm", "required": True},
        {"name": "secret", "type": "string", "form": "form", "required": True},
    ]
    schema, _ = build_input_schema(params, pinned={"secret"})
    assert schema["required"] == ["query"]
    assert "secret" not in schema["properties"]
