# Design Notes — v0.2.0 MCP parameter adaptation fix

## Root cause analysis

### A. Empty MCP inputSchema
`_init_tools()` read `tool.get("schemas", [])`. The authoritative tool-selector
model in dify_plugin (`entities/tool.py::ToolSelector`) exposes llm parameters
as `tool_parameters` (a `dict[name, spec]`) and form configuration as
`tool_configuration`. When the endpoint settings used the selector shape, the
`schemas` lookup silently returned `[]` — no error, no log — producing
`{"properties": {}, "required": []}` for tools that do have required llm
parameters (e.g. Tavily `query`).

### B. Configuration expression objects passed to the tool
The old code did `runtime_parameters[name] = parameter_value.get("value")`,
assuming a single `{"type": "constant", "value": X}` wrapper. Nested wrappers
leaked the inner expression object into `tool.invoke()`, and dynamic
expressions (`variable`/`mixed`) were passed through untouched instead of
being rejected — this is how `include_answer` reached Tavily as
`{"type": "constant", "value": "false"}`.

### C. Illegal JSON Schema types
The Dify enum value was used directly as the JSON Schema `type`, so
`select`/`checkbox`/`any` etc. produced invalid schemas.

## Implementation model (v0.2.0)

- Parameters pinned by server config (any key present in `settings` /
  `tool_configuration`) are hidden from `tools/list` and protected at
  `tools/call`: a client that sends them gets a validation error.
- Non-`llm` form parameters (credentials etc.) are likewise hidden and
  protected.
- Clients may only send `llm` parameters that were not pinned. Required
  checking, and light coercion for select/number/boolean, run before invoke.
- Config values are resolved by `resolve_config_value`: `constant` (incl.
  nested) unwrapped with original types kept; `variable`/`mixed` raise a clear
  error; unknown dicts and plain business objects pass through untouched.
- Both endpoint settings shapes are accepted: legacy `schemas`/`settings` and
  selector `tool_parameters`/`tool_configuration`. If neither is present the
  adapter logs a warning instead of silently advertising a parameterless tool.
- Tool-call failures (unknown tool, invalid arguments, runtime exception)
  return an MCP tool result with `isError: true`; exception details stay in
  daemon logs only.
- Streamable HTTP (`endpoints/mcp_post.py`) and legacy SSE
  (`endpoints/messages.py`) share `core/mcp_handler.py` dispatch so the two
  transports cannot diverge.

## Known limitations

- `file`/`files` parameters are dropped from the schema (logged).
- Image/file output messages are replaced with a text placeholder.
- `variable`/`mixed` config expressions are rejected, not resolved.
- No authentication / Origin validation (pre-existing, out of scope).

## How to run tests

```
python -m venv .venv   # Python 3.12+ (plugin runner is 3.12)
.venv/Scripts/python -m pip install -r requirements.txt pytest
.venv/Scripts/python -m pytest tests -v
```
