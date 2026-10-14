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
- **File input & multimodal output 文件输入与多模态输出** — any tool declaring a Dify `file`/`files`
  parameter is discoverable and callable: send base64 (or an allow-listed URL), the gateway uploads
  it through Dify's own file pipeline and hands the tool a real Dify file object. Image/blob/JSON
  tool results come back as proper MCP content blocks instead of a text placeholder.
  任何声明 `file`/`files` 参数的 Dify 工具都能被发现和调用；图片/文件/JSON 结果按 MCP content
  block 返回，不再压平成文字。
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
| **file** | `$ref: #/$defs/fileInput`（`allOf` 包裹以保留描述） |
| **files** | `array` of `$ref: #/$defs/fileInput`；仅必填时 `minItems: 1` |
| system-files | 不支持，记录日志（语义依赖 workflow 上下文，端点拿不到） |

Advanced compatibility note: endpoint entries that inline a full `schemas` list or a
`tool_parameters` map (SDK `ToolSelector` shape) are still honored — useful for fixtures.
内联 `schemas` / `tool_parameters` 的旧数据结构仍然支持（高级用法）。

---

## File input 文件输入

工具声明的 `type: file` / `type: files`（`form: llm`）会真实出现在 `tools/list` 里，必填/可选语义
保留。`form: form` 的文件参数属于服务端配置，依旧不暴露、不可被客户端覆盖。

客户端必须按下面两种形态之一传值（schema 里的 `$defs.fileInput`，`oneOf` + `additionalProperties: false`）：

```jsonc
// 1) 内联 base64（推荐，默认唯一可用方式）
{
  "file": {
    "source": "base64",
    "filename": "report.pdf",
    "mimeType": "application/pdf",   // 可省略，网关按文件头嗅探
    "data": "JVBERi0xLjQK..."        // 纯 base64，也容忍 data:URL 前缀
  }
}

// 2) URL（默认关闭，需管理员开开关 + 配白名单）
{ "file": { "source": "url", "url": "https://files.example.com/report.pdf" } }

// 多文件参数（files）就是上面的数组，顺序与数量都会保留
{ "files": [ { "source": "base64", "filename": "a.txt", "data": "..." }, ... ] }
```

网关会把文件经 **Dify 官方上传链路**（`session.file.upload` → 签名 URL）转换成工具真正认识的
Dify 文件对象再下发，客户端的包装对象不会泄漏给下游；同一次调用内相同文件只上传一次。

**`source: "dify_file"` 明确不支持**：端点插件无法验证客户端给的 file id 是否属于本工作区，
透传会造成跨工作区读文件，所以直接拒绝而不是猜测。

### File settings 文件相关配置（端点设置，均可留空取默认）

| 设置项 | 默认 | 说明 |
|---|---|---|
| `file_max_bytes` | 5 MiB | 单文件上限（daemon 自身硬上限 15 MiB，超出会被收敛） |
| `file_max_count` | 5 | 单次调用最多文件数 |
| `file_max_total_bytes` | 10 MiB | 单次调用文件总量 |
| `file_url_enabled` | `false` | 是否允许 URL 形态输入 |
| `file_url_allow_hosts` | 空 | 可信域名白名单（含子域名）；未开启开关时不生效 |
| `file_url_allow_private` | `false` | 是否允许白名单域名解析到 RFC1918（内网文件服务器）；环回/链路本地/云元数据 IP 无论如何都拒绝 |
| `file_url_timeout_seconds` | 30 | URL 下载超时 |

URL 输入的安全边界：只允许 http(s)、只下载白名单域名、**发起请求前**先解析 DNS 并拒绝私网/环回/
链路本地/组播/保留地址（含 `169.254.169.254`）、每一跳重定向都重新校验、流式下载并限长、错误信息
不含 URL 与凭据。仍存在的已知残余风险：DNS 复核到实际连接之间有极窄的 TOCTOU 窗口——这正是 URL
默认关闭的原因。

---

## Tool output 工具输出

`tools/call` 返回结构化 MCP content block，顺序与 Dify 消息流一致：

| Dify 消息 | MCP 输出 |
|---|---|
| TEXT | `text` |
| JSON | `text`（裸 JSON 字符串，客户端可直接 parse；不再加 `tool response:` 前缀） |
| IMAGE / BLOB | 内联 base64 `image` 块，仅限 `image/*` 且 ≤3 MiB；非图片二进制或超限时给出明确文字说明，而不是伪造 `image` 块或静默截断 |
| BLOB_CHUNK | 按 `id`+`sequence` 聚合后同上 |
| IMAGE_LINK / FILE | 可访问 URL 的文本说明（`resource_link` 属于 2025-03-26 协议，本网关声明 2024-11-05，不产出客户端可能拒绝的块） |
| LINK | `text`（result link: …） |
| VARIABLE / RETRIEVER_RESOURCES | `text`（JSON 序列化保留） |
| LOG | 不返回（执行遥测，可能含内部信息），仅 debug 日志 |
| 未知类型 | 记 warning + 安全文字降级，绝不 `repr()` 内部对象 |

---

## Known limitations 已知限制

- Schema auto-resolution targets **builtin/marketplace** tool providers (Console API
  `builtin/` route). Custom **API / workflow / app** tools need an inline `schemas` fixture for
  now, otherwise they are not exposed. 自动解析目前覆盖内置/市场插件工具；自定义 API/工作流工具
  需要内联 `schemas`，否则不上架。
- `variable`-type config expressions (workflow variable references) are still rejected at call
  time; `mixed` fixed text is resolved, real `{{#…#}}` references are not guessed.
  `variable` 动态表达式仍拒绝；`mixed` 固定文本已支持。
- `source: "dify_file"`（引用已上传文件 id）不支持：端点无法验证该 id 属于本工作区，透传有跨工作区
  读取风险。文件请直接用 base64 传入。
- Inline binary output is capped (default 3 MiB): an oversized `IMAGE`/`BLOB` is reported as text
  rather than truncated, because Dify gives no downloadable URL for inline blobs. Non-image inline
  bytes (`application/pdf`…) are also reported as text — `2024-11-05` has no block for them, so
  emitting an `image` block would be a lie the client cannot render.
  内联二进制超限时会明确提示，不会静默截断；非图片的内联字节同样只给文字说明，不会伪装成 `image` 块。
- File input works at protocol `2024-11-05` (base64 lives in `arguments`). `resource_link` output
  blocks would need `2025-03-26`+; upgrading the negotiated version is a separate task.
  文件输入无需改协议；`resource_link` 等输出块需更高协议版本，留作后续。
- Endpoints have **no authentication / Origin validation** (inherited upstream). **File input makes
  this materially more dangerous**: an open endpoint lets anyone upload to your Dify file storage
  (and, if enabled, make the gateway fetch URLs). Do not expose the endpoint without an
  authenticating reverse proxy + HTTPS.
  端点无鉴权（沿袭上游）。加入文件输入后风险显著上升：公网裸奔等于任何人都能往你的 Dify 文件存储
  上传文件（开启 URL 后还能让网关去请求地址）。对外暴露必须加反代鉴权 + HTTPS。
- The auto-registered default config lives in plugin storage (plain KV, readable only by this
  plugin itself); the endpoint's own copy of the key stays daemon-encrypted.
- Provider metadata is cached 300s per base+workspace; icon/label changes need up to 5 minutes.
- Dify signs file URLs with a 300s expiry (`FILES_ACCESS_TIMEOUT`); a tool that lazily reads the
  file long after the call may find the URL expired. Raise it in Dify if needed.
  Dify 文件 URL 签名 300 秒过期，工具若延迟读取可能失效。

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

**Q: 怎么用一条 curl 验证文件输入链路？**
Base64 a file and post it as a `fileInput` object — extracted text in the response is the proof the
file reached the tool runtime (a raw wrapper would fail parameter validation instead).

```bash
B64=$(base64 -w0 report.pdf)
curl -sS -X POST http://<host>:18080/e/<endpoint-id>/mcp \
  -H 'Content-Type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"dify_extractor\",\"arguments\":{\"file\":{\"source\":\"base64\",\"filename\":\"report.pdf\",\"mimeType\":\"application/pdf\",\"data\":\"$B64\"}}}}"
```

**Q: 报 "uploading ... to Dify failed" / 文件上传失败？**
文件上传要经 Dify 的文件网关并回一个签名 URL（`FILES_URL` + `FILES_ACCESS_TIMEOUT`，默认 300s）。
如果 Dify 侧 `FILES_URL` 未配置或插件容器访问不到它，就没有可用 URL —— 网关会明确报
"no file URL"，而不是把无法访问的链接发给工具。

## Development 本地开发

```bash
# Python 3.12+ required (matches the plugin runner)
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt pytest
.venv/Scripts/python -m pytest tests -v
```

`core/` holds the transport-independent logic (adapter, schema converter, parameter resolver,
console metadata, global config); `endpoints/` are thin per-transport shells.

## Changelog 变更记录

- **0.3.0** — Generic file support: `file`/`files` parameters are advertised with a validated
  `fileInput` contract (`$defs`, `oneOf` base64/url, `minItems` only when required) and converted
  through Dify's upload pipeline into real Dify file parameters (`core/file_adapter.py`). URL input
  is opt-in with host allow-listing + pre-request private-IP/SSRF rejection. Tool results become
  structured MCP content blocks (text/image/reassembled blob chunks), mixed types keep order.
  通用文件输入（base64/白名单 URL，走 Dify 官方上传链路）与多模态输出（结构化 content block）。
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
