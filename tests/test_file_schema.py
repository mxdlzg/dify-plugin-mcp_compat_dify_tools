"""Dify file/files parameters must be advertised with a real contract."""

from core.schema_converter import FILE_INPUT_REF, build_input_schema
from core.tool_adapter import adapt_tool_entry, to_mcp_tool


def _file_tool( *, file_required=True, files_required=False, form="llm", pinned=None):
    params = [
        {"name": "query", "type": "string", "form": "llm", "required": True},
        {"name": "file", "type": "file", "form": form, "required": file_required,
         "llm_description": "the file to parse"},
    ]
    if files_required is not None:
        params.append(
            {"name": "docs", "type": "files", "form": form, "required": files_required}
        )
    return build_input_schema(params, pinned=pinned or set())


def test_required_file_is_advertised_and_required():
    schema, skipped = _file_tool()
    assert skipped == []
    assert schema["required"] == ["query", "file"]
    assert schema["properties"]["file"]["allOf"] == [{"$ref": FILE_INPUT_REF}]
    assert schema["properties"]["file"]["description"] == "the file to parse"
    # the $ref target must actually resolve inside the same document
    assert "fileInput" in schema["$defs"]
    assert schema["$defs"]["fileInput"]["oneOf"][0]["properties"]["source"]["const"] == "base64"


def test_optional_file_is_not_required():
    schema, _ = _file_tool(file_required=False)
    assert schema["required"] == ["query"]
    assert "file" in schema["properties"]


def test_required_files_gets_min_items_one():
    schema, _ = _file_tool(files_required=True)
    docs = schema["properties"]["docs"]
    assert docs["type"] == "array"
    assert docs["items"] == {"$ref": FILE_INPUT_REF}
    assert docs["minItems"] == 1
    assert "docs" in schema["required"]


def test_optional_files_has_no_min_items():
    schema, _ = _file_tool(files_required=False)
    assert "minItems" not in schema["properties"]["docs"]


def test_pinned_file_parameter_is_hidden():
    schema, _ = _file_tool(pinned={"file"})
    assert "file" not in schema["properties"]
    assert "file" not in schema["required"]


def test_form_file_parameter_is_hidden():
    # form=form is server configuration: never agent-visible, never overridable.
    schema, _ = _file_tool(form="form")
    assert "file" not in schema["properties"]


def test_no_defs_for_tools_without_files():
    # G5: non-file tools keep the exact schema shape they had before.
    schema, _ = build_input_schema(
        [{"name": "q", "type": "string", "form": "llm", "required": True}], pinned=set()
    )
    assert "$defs" not in schema
    assert schema == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }


def test_system_files_still_reported_as_unsupported():
    schema, skipped = build_input_schema(
        [{"name": "ctx", "type": "system-files", "form": "llm", "required": False}],
        pinned=set(),
    )
    assert skipped == ["ctx"]
    assert "ctx" not in schema["properties"]


def test_adapted_tool_tracks_file_params_for_the_adapter():
    entry = {
        "enabled": True,
        "type": "builtin",
        "provider_name": "langgenius/dify_extractor/dify_extractor",
        "tool_name": "dify_extractor",
        "tool_label": "Extractor",
        "schemas": [
            {"name": "file", "type": "file", "form": "llm", "required": True},
            {"name": "docs", "type": "files", "form": "llm", "required": False},
        ],
        "settings": {},
    }
    tool = adapt_tool_entry(entry)
    assert tool.file_param_names == {"file", "docs"}
    assert tool.multi_file_param_names == {"docs"}
    mcp = to_mcp_tool(tool)
    assert mcp["inputSchema"]["required"] == ["file"]
    assert "$defs" in mcp["inputSchema"]


# --- the Console API route, not just inline schemas ---

def test_file_param_survives_console_api_slimming():
    # Real regression risk: _slim_parameter drops unknown keys, so a `file`
    # declaration fetched from the console API must still reach the converter
    # with its type intact. Inline-schema tests alone would not catch that.
    from core.tool_metadata import ToolMetadataResolver, clear_cache

    clear_cache()
    raw = {
        "name": "file",
        "label": {"en_US": "File"},
        "human_description": {"en_US": "The file to be parsed."},
        "placeholder": None, "scope": None, "auto_generate": None,
        "template": None, "default": None, "min": None, "max": None,
        "precision": None, "options": None, "input_schema": None,
        "type": "file", "required": True, "form": "llm",
        "llm_description": "The file to be parsed.",
    }
    resolver = ToolMetadataResolver(
        {
            "dify_console_api_base": "http://dify.local",
            "dify_console_api_key": "k",
            "dify_workspace_id": "ws-1",
        },
        fetcher=lambda base, provider: [
            {"name": "dify_extractor", "label": {"en_US": "Extractor"},
             "parameters": [
                 raw,
                 {"name": "mode", "type": "select", "form": "llm",
                  "required": False,
                  "options": [{"value": "paper", "label": {"en_US": "Paper"}}]},
             ]},
        ],
    )
    entry = {
        "provider_name": "langgenius/dify_extractor/dify_extractor",
        "tool_name": "dify_extractor",
        "tool_label": "Extractor",
        "enabled": True,
        "type": "builtin",
        "settings": {},
        "parameters": {},
    }
    mcp = to_mcp_tool(adapt_tool_entry(entry, resolver))
    schema = mcp["inputSchema"]
    assert schema["properties"]["file"]["allOf"] == [{"$ref": FILE_INPUT_REF}]
    assert schema["properties"]["file"]["description"] == "The file to be parsed."
    assert schema["required"] == ["file"]
    assert schema["$defs"]["fileInput"]
