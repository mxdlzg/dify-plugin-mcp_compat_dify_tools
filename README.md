## Dify 1.0 Plugin Convert your Dify tools's API to MCP compatible API

**Author:** [Junjie.M](https://github.com/junjiem)   
**Type:** extension   
**Github Repo:** [https://github.com/junjiem/dify-plugin-mcp_compat_dify_tools](https://github.com/junjiem/dify-plugin-mcp_compat_dify_tools)   
**Github Issues:** [issues](https://github.com/junjiem/dify-plugin-mcp_compat_dify_tools/issues)  


Reference to https://github.com/hjlarry/dify-plugin-mcp_server


---


### Description

> **Note:** must be dify 1.2.0 and above.

> **注:** 必须是 dify 1.2.0及以上版本。

Convert your Dify tools's API to MCP compatible API (**Currently, two transports are supported: `HTTP with SSE` and `Streamable HTTP`**)

将您的 Dify 工具的 API 转换为 MCP 兼容 API (**目前已支持 `HTTP with SSE` 和 `Streamable HTTP` 两种传输方式**)

> **Note:** This fork requires dify_plugin >= 0.10.2 (Python 3.12 runner) and fixes tool
> parameter schema generation and configuration-parameter resolution for MCP clients.
> See "v0.2.0 changes" below.
>
> **注：** 本分支要求 dify_plugin >= 0.10.2（Python 3.12 runner），修复了 MCP 客户端看到的工具
> 参数 Schema 生成问题和 Dify 配置参数解析问题，详见下文“v0.2.0 变更”。

### v0.2.0 changes v0.2.0 变更

1. **MCP inputSchema is generated correctly.** Dify tool parameters are converted to valid
   JSON Schema: `select` → `string` + `enum`, `number`/`boolean` map 1:1, `object`/`array`
   keep nested metadata, `date` → `string(format: date)`. Illegal types such as `select` are
   never emitted. A required parameter such as Tavily's `query` now always appears in
   `properties` and `required` (previously an empty schema could be produced silently).

   **MCP inputSchema 正确生成。** Dify 参数被转换为合法 JSON Schema：`select` → `string`+`enum`，
   `number`/`boolean` 一一对应，`object`/`array` 保留嵌套结构。此前 Tavily 必填参数 `query`
   可能丢失、产生空 Schema 的问题已修复。

2. **Dify configuration expressions are resolved.** Server-side tool configuration stored as
   `{"type": "constant", "value": "false"}` (including nested wrapping) is unwrapped to the
   real value before invoking the tool, keeping its original type (the string `"false"` stays
   a string). `include_answer` no longer reaches tools as a raw expression object. Dynamic
   expressions (`variable`/`mixed`) are rejected with a clear error instead of passed through.

   **Dify 配置表达式被正确解析。** `{"type":"constant","value":...}`（含嵌套包装）在调用工具前
   被解包为实际值且保持原类型；`variable`/`mixed` 动态表达式明确报错而不是透传。

3. **Server-side configuration is protected.** Parameters pinned by the user in the Dify
   endpoint (and any credential/`form` parameter) are hidden from `tools/list` and cannot be
   overridden by the MCP client. The client may only send `llm` parameters that were not
   pinned.

   **服务端配置受保护。** 用户在 Dify Endpoint 中固定的参数以及凭据类 `form` 参数不会出现在
   `tools/list` 中，MCP Client 也无法越权覆盖；Client 只能提交未被固定的 `llm` 参数。

4. **Single shared core.** `endpoints/mcp_post.py` (Streamable HTTP) and `endpoints/messages.py`
   (legacy SSE) now share `core/tool_adapter.py`, `core/schema_converter.py`,
   `core/parameter_resolver.py` and `core/mcp_handler.py`, so both transports always behave
   identically.

   **统一核心逻辑。** Streamable HTTP 与旧版 SSE 两个 Endpoint 共用 `core/` 下的公共模块，
   行为完全一致。

5. **MCP-compliant error handling & no secret leakage.** Failed tool calls return MCP tool
   results with `isError: true`; internal exception details (which may contain provider
   credentials) stay in the plugin daemon logs only. The old debug print of the full tools
   configuration has been removed.

   **符合 MCP 语义的错误处理，不再泄露敏感数据。** 调用失败以 `isError: true` 的工具结果返回；
   异常细节只写入插件日志，不回传客户端；已移除打印完整 tools 配置的调试语句。

#### Supported parameter types 支持的参数类型

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

Not yet supported 暂不支持：`file`/`files` 参数（从 Schema 移除并在日志提示）、工具输出的
图片/文件类消息（返回文字占位说明）、`variable`/`mixed` 动态配置表达式（调用时明确报错）。

#### Testing 本地测试

```bash
# Python 3.12+ required (matches the plugin runner)
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt pytest
.venv/Scripts/python -m pytest tests -v
```


#### 1. Add a endpoint and add tool list. 添加API端点并添加工具列表。

![add_endpoint](./_assets/add_endpoint.png)

![add_tool_list](./_assets/add_tool_list.png)

![edit_tool](./_assets/edit_tool.png)

![save_endpoint](./_assets/save_endpoint.png)

> **Note:** The modification of the tool list for existing endpoint will take effect only after the endpoint are disabled and then enabled.

> **注：** 对已有 API 端点进行工具列表的修改需要停用再启用 API 端点后才会生效。


#### 2. Copy the endpoint url to your MCP client, like `Cherry Studio`  复制端点url到您的MCP客户端，如 `Cherry Studio`

![mcp_urls](./_assets/mcp_urls.png)


##### **Option 1:** Use the newest `Streamable HTTP` transport (**Recommended**)  使用最新的`Streamable HTTP`传输方式（**推荐**）

![mcp_streamable_http_url](./_assets/mcp_streamable_http_url.png)

![cherry_studio_mcp_streamable_http](./_assets/cherry_studio_mcp_streamable_http.png)

**OK!**


##### **Option 2:** Use the legacy `HTTP with SSE` transport  使用过时的`HTTP with SSE`传输方式

![mcp_sse_url](./_assets/mcp_sse_url.png)

![cherry_studio_mcp_sse](./_assets/cherry_studio_mcp_sse.png)

**OK!**



---



### Installing Plugins via GitHub  通过 GitHub 安装插件

Can install the plugin using the GitHub repository address. Visit the Dify platform's plugin management page, choose to install via GitHub, enter the repository address, select version number and package file to complete installation.

可以通过 GitHub 仓库地址安装该插件。访问 Dify 平台的插件管理页，选择通过 GitHub 安装插件，输入仓库地址后，选择版本号和包文件完成安装。

![install_plugin_via_github](_assets/install_plugin_via_github.png)



---



### FAQ

#### 1. How to Handle Errors When Installing Plugins? 安装插件时遇到异常应如何处理？

**Issue**: If you encounter the error message: plugin verification has been enabled, and the plugin you want to install has a bad signature, how to handle the issue?

**Solution**: Add the following line to the end of your .env configuration file: FORCE_VERIFYING_SIGNATURE=false
Once this field is added, the Dify platform will allow the installation of all plugins that are not listed (and thus not verified) in the Dify Marketplace.

**问题描述**：安装插件时遇到异常信息：plugin verification has been enabled, and the plugin you want to install has a bad signature，应该如何处理？

**解决办法**：在 .env 配置文件的末尾添加 FORCE_VERIFYING_SIGNATURE=false 字段即可解决该问题。
添加该字段后，Dify 平台将允许安装所有未在 Dify Marketplace 上架（审核）的插件，可能存在安全隐患。


#### 2. How to install the offline version 如何安装离线版本

Scripting tool for downloading Dify plugin package from Dify Marketplace and Github and repackaging [true] offline package (contains dependencies, no need to be connected to the Internet).

从Dify市场和Github下载Dify插件包并重新打【真】离线包（包含依赖，不需要再联网）的脚本工具。

Github Repo: https://github.com/junjiem/dify-plugin-repackaging

