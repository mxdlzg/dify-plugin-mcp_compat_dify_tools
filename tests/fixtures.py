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
