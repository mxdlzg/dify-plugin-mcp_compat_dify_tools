# Dify Tool → MCP Gateway

将 Dify 平台的工具通过 MCP（Model Context Protocol）暴露给外部 Agent 使用。

Expose **any Dify tool** — marketplace plugin tools, custom API tools, workflow tools — to
external MCP clients such as **Claude Code, Codex, Cherry Studio, Cline** over
**Streamable HTTP** or legacy **HTTP with SSE**, without building any Dify workflow.

> **Note:** requires Dify **1.2.0+** and plugin daemon with Python 3.12 runner.
> **注：** 需要 Dify **1.2.0** 及以上版本。

**Author:** mxdlzg   
**Type:** extension   
**Github Repo:** https://github.com/mxdlzg/dify-plugin-mcp_compat_dify_tools   
**Github Issues:** https://github.com/mxdlzg/dify-plugin-mcp_compat_dify_tools/issues

Based on the fork of [Junjie.M/mcp_compat_dify_tools](https://github.com/junjiem/dify-plugin-mcp_compat_dify_tools)
(which references [hjlarry/dify-plugin-mcp_server](https://github.com/hjlarry/dify-plugin-mcp_server)),
rewritten in the 0.2.x line into a general-purpose gateway: correct tool schemas, protected
server configuration, real display metadata, and plugin-wide settings.
本仓库为上述项目的深度重构分支（0.2.x），已从"格式转换"演化为通用的 Dify 工具 → MCP 网关。

---

## Why this fork 为什么用这个分支

The upstream plugin advertised every tool with an **empty or wrong inputSchema**, because the
Dify Endpoint tool selector only persists *values* (`parameters`/`settings`), never the
parameter *declaration* (type / required / description). Clients therefore had no idea that
Tavily needs `query`, and calls failed inside the tool.
上游插件给所有工具生成空/错误的 inputSchema：Dify 端点的工具选择器只保存**值**，从不保存参数
**声明**（类型/必填/描述），导致 MCP 客户端不知道该传什么参数，调用在工具内部报错。

This fork resolves each tool's **real declaration** from the Dify Console API (read-only, never
invokes the tool), and builds honest MCP tools on top of it.
本分支通过 Dify Console API（只读、绝不触发工具执行）解析每个工具的**真实声明**，在其上构建
可信的 MCP 工具定义。

### Features 特性

- **Real schemas at `tools/list`** — `required`, types, `enum` options and LLM descriptions come
  from the original tool declaration fetched from the Console API (per-provider cache, 300s).
  A tool whose schema cannot be resolved is **hidden**, not advertised with a misleading empty
  schema. `tools/list` 输出**真实 Schema**（必填/类型/枚举/描述），来自 Console API；解析失败的
  工具不上架，而不是用空 Schema 误导客户端。
- **Protected server configuration 服务端配置保护** — parameters pinned in the endpoint (or the
  tool's credential/`form` fields) never appear in the schema and cannot be overridden by the
  MCP client; pinned values are merged into every call with their original Dify types (a select
  `"false"` stays the string `"false"`). 端点中固定的参数、凭据类 `form` 参数不出现在 Schema、
  不可被客户端覆盖，调用时按原始类型自动合并。
- **Display metadata 显示元数据** — MCP `title` plus `icons` (light/dark, official MCP ≥
  2025-09-24) taken from Dify provider metadata (full URLs).
- **Configure once 一次配置，处处继承** — the three console settings are auto-registered into
  plugin storage on first use; new endpoints leave them empty and inherit automatically
  (per-field override supported).
- **Single core, two transports 统一核心** — Streamable HTTP (`/mcp`) and legacy SSE (`/sse` +
  `/messages/`) share one dispatch/adapter/converter, behavior can never diverge.
- **No secret leakage 不泄露凭据** — failed calls return MCP `isError: true` with a masked
  message, full details stay in daemon logs; diagnostics only ever log field names and counts.

---

## Quick start 快速开始

### 1. Install the plugin 安装插件

Install from GitHub (Plugin management → Install via GitHub →
`https://github.com/mxdlzg/dify-plugin-mcp_compat_dify_tools`), or repackage as an offline
bundle with [dify-plugin-repackaging](https://github.com/junjiem/dify-plugin-repackaging).

If you see *plugin verification has been enabled … bad signature*, append
`FORCE_VERIFYING_SIGNATURE=false` to Dify's `.env` and restart.

### 2. Prepare the console access 准备 Console 访问凭据

Schema fetching reads (never invokes) two read-only Console API routes:

```
GET {base}/console/api/workspaces/current/tool-provider/builtin/{provider}/tools
GET {base}/console/api/workspaces/current/tools/builtin        # provider title + icons
```

They require `Authorization: Bearer <key>` and `X-WORKSPACE-ID`. Collect:

| Setting 配置项 | Where to get it 从哪里获取 | Example 示例 |
|---|---|---|
| `dify_console_api_base` | Dify API address **reachable from the plugin container** 插件容器可访问的 API 地址 | `http://api:5001` (same compose network) or `http://host.docker.internal:5001` |
| `dify_console_api_key` | Dify **Profile → API 设置** → create Secret key | `tvpm…` |
| `dify_workspace_id` | **团队设置** (team ID, also visible in browser URL) | `c4ae3307-d5b5-…` |

Only `scheme://host[:port]` goes into the base URL; a pasted full console URL is trimmed
automatically. 地址只填 `协议://主机:端口`，粘贴完整 URL 会自动截取。

### 3. Create an endpoint 创建 API 端点

Plugin → this plugin → **API Endpoint** → Add endpoint: pick the tools, and fill the three
fields above **on the first endpoint only**. Later endpoints leave them empty and inherit.
第一个端点填写三项配置；之后的端点留空即自动继承。

![add_endpoint](./_assets/add_endpoint.png)
![add_tool_list](./_assets/add_tool_list.png)
![save_endpoint](./_assets/save_endpoint.png)

> **Note:** changes to an existing endpoint's tool list only take effect after the endpoint is
> disabled and re-enabled. **And: reinstalling/upgrade of the plugin changes the endpoint ID —
> always re-copy the URL afterwards.**
> **注：** 修改已有端点的工具列表需停用再启用才生效。**重装/升级插件会更换端点 ID，务必重新复制
> URL**（旧 URL 会返回 `endpoint not found`）。

### 4. Connect your MCP client 接入 MCP 客户端

**Streamable HTTP (recommended 推荐)**

```bash
# Claude Code
claude mcp add --transport http dify-tools http://<your-dify>/e/<endpoint-id>/mcp
```

![mcp_streamable_http_url](./_assets/mcp_streamable_http_url.png)
![cherry_studio_mcp_streamable_http](./_assets/cherry_studio_mcp_streamable_http.png)

**Legacy HTTP with SSE (deprecated but supported)**: point the client at
`http://<your-dify>/e/<endpoint-id>/sse`.
![mcp_sse_url](./_assets/mcp_sse_url.png)

A `tools/list` entry now looks like:

```json
{
  "name": "tavily_search",
  "title": "Tavily Search",
  "description": "A search engine tailored to LLM agents",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "The search query."},
      "search_depth": {"type": "string", "enum": ["basic", "advanced", "fast", "ultra-fast"],
                       "description": "The depth of the search."}
    },
    "required": ["query"]
  },
  "icons": [
    {"src": "http://…/f101df9e…svg", "theme": "light"},
    {"src": "http://…/7be5cd10…svg", "theme": "dark"}
  ]
}
```

---

## Parameter type mapping 参数类型映射

| Dify type | MCP JSON Schema |
|---|---|
| string / secret-input | `string` |
| number | `number` |
| boolean / checkbox | `boolean` |
| select | `string` + `enum`（`multiple` → enum 数组 `array`） |
| dynamic-select | `string`（选项由 Dify 运行时解析，无法预先枚举） |
| date | `string` + `format: date` |
| date-range | `object {start, end}` |
| object / array | `object` / `array`（有嵌套元数据时保留） |

Advanced compatibility note: endpoint entries that inline a full `schemas` list or a
`tool_parameters` map (SDK `ToolSelector` shape) are still honored — useful for fixtures.
内联 `schemas` / `tool_parameters` 的旧数据结构仍然支持（高级用法）。

---

## Known limitations 已知限制

- Schema auto-resolution targets **builtin/marketplace** tool providers (Console API
  `builtin/` route). Custom **API / workflow / app** tools need an inline `schemas` fixture for
  now, otherwise they are not exposed. 自动解析目前覆盖内置/市场插件工具；自定义 API/工作流工具
  需要内联 `schemas`，否则不上架。
- `file`/`files` parameters are dropped from schemas (logged); image/file tool outputs return a
  text placeholder; `variable`/`mixed` dynamic config expressions are rejected at call time.
  文件类参数、图片/文件输出、动态变量表达式暂不支持。
- Endpoints have **no authentication / Origin validation** (inherited upstream): deploy on a
  trusted network or put an authenticating reverse proxy in front. 端点无鉴权（沿袭上游），请部署
  在可信网络或加反代鉴权。
- The auto-registered default config lives in plugin storage (plain KV, readable only by this
  plugin itself); the endpoint's own copy of the key stays daemon-encrypted.
- Provider metadata is cached 300s per base+workspace; icon/label changes need up to 5 minutes.

## FAQ

**Q: `endpoint not found` when calling the URL?**
Reinstalling or upgrading the plugin regenerates endpoint IDs — copy the URL again from the
endpoint list. 插件重装/升级后端点 ID 会变，去端点列表重新复制 URL。

**Q: Tools missing from `tools/list`?**
Check plugin daemon logs for `TOOL_META_DIAG` / "not exposing" lines: usually the console
access fields are unset (or the plugin container cannot reach `dify_console_api_base`), or the
tool is not builtin and has no inline schema. 看 daemon 日志里的 `TOOL_META_DIAG`：多为三个配置
项未填 / 容器访问不到 Console API 地址 / 非内置工具无内联 Schema。

**Q: Plugin fails to install dependencies (uv 404 from a mirror)?**
The mirror is serving a partial package index; restart the plugin daemon container (clears uv's
negative cache) or set `UV_DEFAULT_INDEX=https://pypi.org/simple` on the daemon. 镜像源同步延迟，
重启 plugin_daemon 容器，或给 daemon 配置 `UV_DEFAULT_INDEX`。

## Development 本地开发

```bash
# Python 3.12+ required (matches the plugin runner)
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt pytest
.venv/Scripts/python -m pytest tests -v
```

`core/` holds the transport-independent logic (adapter, schema converter, parameter resolver,
console metadata, global config); `endpoints/` are thin per-transport shells.

## Changelog 变更记录（0.2.x）

- **0.2.3** — MCP display metadata: `title` + light/dark `icons` from the Console provider
  list (full URLs). 显示元数据（title + 明暗图标）。
- **0.2.2** — Console auth corrected (`Authorization: Bearer` + `X-WORKSPACE-ID`, new
  `dify_workspace_id` setting); fetched schemas slimmed so `tools/list` stays compact;
  plugin-wide defaults with per-field inheritance; base-URL normalization.
  鉴权修正 + 响应瘦身 + 插件级默认值 + 地址规范化。
- **0.2.1** — Tool metadata resolution via read-only Console API; hide-on-unresolvable policy;
  selector `auto=0` pinning. 通过 Console API 只读解析真实工具声明。
- **0.2.0** — valid JSON Schema generation, Dify config-expression resolution, protected
  server configuration, unified core for both transports, MCP-compliant error handling.
  Schema 生成/配置解析/配置保护/统一核心/错误语义全面修复。
