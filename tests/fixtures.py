from dify_plugin.entities.tool import ToolInvokeMessage


# A Tavily-like tool. Selector-shape = the shape dify_plugin's ToolSelector
# models (tool_parameters for llm params, tool_configuration for form params).
# Server-pinned include_answer is configured but must never be agent-visible.
def tavily_selector_shape():
    return {
        "enabled": True,
        "type": "builtin",
        "provider_name": "langgenius/tavily/tavily",
        "tool_name": "tavily_search",
        "tool_label": "Tavily Search",
        "tool_description": "A search engine tailored to LLM agents",
        "tool_parameters": {
            "query": {
                "name": "query",
                "type": "string",
                "required": True,
                "description": "Query string for search",
            },
            "include_answer": {
                "name": "include_answer",
                "type": "select",
                "required": True,
                "description": "Answer by the LLM",
                "options": [
                    {"value": "false"},
                    {"value": "basic"},
                    {"value": "advanced"},
                ],
            },
        },
        "tool_configuration": {
            "include_answer": {"type": "constant", "value": "false"},
        },
    }


# Legacy schemas-shape = full ToolParameter serialization with a form param.
def tavily_schemas_shape():
    return {
        "enabled": True,
        "type": "builtin",
        "provider_name": "langgenius/tavily/tavily",
        "tool_name": "tavily_search",
        "tool_label": "Tavily Search",
        "tool_description": "A search engine tailored to LLM agents",
        "schemas": [
            {
                "name": "query",
                "type": "string",
                "form": "llm",
                "required": True,
                "llm_description": "Query string for search",
                "label": {"en_US": "Query"},
                "human_description": {"en_US": "Query"},
            },
            {
                "name": "include_answer",
                "type": "select",
                "form": "llm",
                "required": True,
                "llm_description": "Answer by the LLM",
                "options": [
                    {"value": "false", "label": {"en_US": "Not Included"}},
                    {"value": "basic", "label": {"en_US": "Basic"}},
                    {"value": "advanced", "label": {"en_US": "Advanced"}},
                ],
                "label": {"en_US": "Include Answer"},
                "human_description": {"en_US": "Include Answer"},
            },
        ],
        "settings": {
            "include_answer": {"type": "constant", "value": "false"},
        },
    }


def string_only_tool():
    return {
        "enabled": True,
        "type": "builtin",
        "provider_name": "p",
        "tool_name": "echo",
        "tool_label": "Echo",
        "tool_parameters": {
            "text": {
                "name": "text",
                "type": "string",
                "required": True,
                "description": "text to echo",
            },
        },
        "tool_configuration": {},
    }


def number_bool_tool():
    return {
        "enabled": True,
        "type": "builtin",
        "provider_name": "p",
        "tool_name": "calc",
        "tool_label": "Calc",
        "tool_parameters": {
            "count": {"name": "count", "type": "number", "required": True, "description": "n"},
            "flag": {"name": "flag", "type": "boolean", "required": False, "description": "f"},
        },
        "tool_configuration": {},
    }


def select_tool():
    return {
        "enabled": True,
        "type": "builtin",
        "provider_name": "p",
        "tool_name": "chooser",
        "tool_label": "Chooser",
        "tool_parameters": {
            "mode": {
                "name": "mode",
                "type": "select",
                "required": True,
                "description": "mode",
                "options": [
                    {"value": "false"},
                    {"value": "true"},
                    {"value": "basic"},
                    {"value": "advanced"},
                ],
            },
        },
        "tool_configuration": {},
    }


def object_array_tool():
    return {
        "enabled": True,
        "type": "builtin",
        "provider_name": "p",
        "tool_name": "shaper",
        "tool_label": "Shaper",
        "tool_parameters": {
            "meta": {
                "name": "meta",
                "type": "object",
                "required": False,
                "description": "meta object",
                "input_schema": {
                    "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
                },
            },
            "tags": {
                "name": "tags",
                "type": "array",
                "required": False,
                "description": "tags",
                "input_schema": {"items": {"type": "string"}},
            },
        },
        "tool_configuration": {},
    }


def no_llm_tool():
    return {
        "enabled": True,
        "type": "builtin",
        "provider_name": "p",
        "tool_name": "ping",
        "tool_label": "Ping",
        "tool_configuration": {"region": {"type": "constant", "value": "us"}},
    }


# Dify 1.17.1 stores text-input/secret-input form values as type="mixed"
# (double-wrapped by getStructureValue()). project_id is an OPTIONAL form
# param exactly like Tavily's "Project ID" text-input that triggered the
# production "dynamic expression type 'mixed'" failure. Schemas-shape is used
# because the selector-shape tool_parameters map only ever carries llm params.
def form_mixed_tool(project_id_value, *, required=False, raw_settings=None):
    if raw_settings is None:
        raw_settings = {
            "project_id": {"value": {"type": "mixed", "value": project_id_value}}
        }
    return {
        "enabled": True,
        "type": "builtin",
        "provider_name": "p",
        "tool_name": "search_ctx",
        "tool_label": "Search Ctx",
        "schemas": [
            {
                "name": "query",
                "type": "string",
                "form": "llm",
                "required": True,
                "llm_description": "Query string",
            },
            {
                "name": "project_id",
                "type": "string",
                "form": "form",
                "required": required,
                "llm_description": "Tavily project id",
            },
            {
                "name": "max_results",
                "type": "number",
                "form": "form",
                "required": False,
                "llm_description": "Max results",
            },
        ],
        "settings": raw_settings,
    }


class FakeUploadedFile:
    """Stand-in for dify_plugin UploadFileResponse."""

    def __init__(self, filename: str, content: bytes, mimetype: str, seq: int):
        self.id = f"tool-file-{seq}"
        self.name = filename
        self.size = len(content)
        self.extension = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
        self.mime_type = mimetype
        self.type = "document"
        self.preview_url = (
            f"https://files.invalid/files/tools/{self.id}{self.extension}"
            "?timestamp=1&nonce=n&sign=s"
        )


class FakeFileUpload:
    """Stand-in for ``session.file.upload`` capturing uploads."""

    def __init__(self, error: Exception | None = None):
        self.calls = []
        self._error = error

    def __call__(self, filename, content, mimetype):
        self.calls.append({"filename": filename, "bytes": len(content), "mime": mimetype})
        if self._error is not None:
            raise self._error
        return FakeUploadedFile(filename, content, mimetype, len(self.calls))


class FakeToolInvocation:
    """Stand-in for ``self.session.tool.invoke`` capturing sent parameters."""

    def __init__(self):
        self.calls = []

    def __call__(self, tool, parameters):
        self.calls.append({"tool": tool.name, "parameters": parameters})
        return [
            ToolInvokeMessage(
                type=ToolInvokeMessage.MessageType.TEXT,
                message=ToolInvokeMessage.TextMessage(text=f"ran {tool.name}"),
            )
        ]
